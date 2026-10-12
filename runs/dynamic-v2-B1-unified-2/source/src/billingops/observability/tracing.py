"""Best-effort Langfuse v4 instrumentation; telemetry cannot control execution."""
from __future__ import annotations

import logging
import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

log = logging.getLogger("billingops.tracing")
_active: ContextVar[Any] = ContextVar("billingops_trace", default=None)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "[REDACTED]" if k.lower() in {"token", "gate_token", "authorization", "api_key", "secret_key", "password"} else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class Tracer:
    def __init__(self, *, enabled: bool | None = None, client: Any = None) -> None:
        self._client = client
        self.enabled = False
        self.dropped = 0
        self.last_error_type: str | None = None
        want = enabled if enabled is not None else bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))
        if not want:
            return
        try:
            if self._client is None:
                from langfuse import Langfuse
                self._client = Langfuse(base_url=os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST") or None, timeout=10, mask=lambda *, data, **_: redact(data))
            self.enabled = True
        except Exception as exc:
            self._drop(exc)

    def _drop(self, exc: Exception) -> None:
        self.dropped += 1
        self.last_error_type = type(exc).__name__
        log.warning("telemetry operation dropped (%s); business execution continues", self.last_error_type)

    @contextmanager
    def task(self, store: Any, task: dict):
        if not self.enabled:
            yield None
            return
        case_id = task["case_id"]
        state = {"case_id": case_id, "run_id": uuid.uuid4().hex, "trace_id": uuid.uuid4().hex,
                 "task_id": task["id"], "command_id": task["payload"].get("command_id"), "parent_run_id": None}
        span, propagation, after_seq = None, None, 0
        before_drops = self.dropped
        try:
            from langfuse import propagate_attributes
            events = store.events(case_id)
            previous = [e for e in events if e["kind"] == "observation_started"]
            finished = [e for e in events if e["kind"] == "observation_finished"]
            if previous:
                state["parent_run_id"] = previous[-1]["payload"]["run_id"]
            if finished:
                after_seq = finished[-1]["seq"]
            propagation = propagate_attributes(session_id=case_id, metadata={k: str(v) for k, v in state.items() if v is not None})
            propagation.__enter__()
            span = self._client.start_observation(name="billingops.task", as_type="agent", trace_context={"trace_id": state["trace_id"]}, metadata=state,
                                                   input={"task_kind": task["kind"], "case_text": (store.get_case(case_id) or {}).get("text")})
            state["trace_url"] = self._client.get_trace_url(trace_id=state["trace_id"])
        except Exception as exc:
            self._drop(exc)
        try:
            store.append_event(case_id, "observation_started", "telemetry", state)
        except Exception as exc:
            self._drop(exc)
        token = _active.set({"span": span, "state": state})
        try:
            yield state
        finally:
            try:
                for event in store.events(case_id):
                    if event["seq"] > after_seq and not event["kind"].startswith("observation_"):
                        self.event(case_id=case_id, run_id=state["run_id"], kind=event["kind"], payload={"event_seq": event["seq"], "recorded_at": event["at"], "event_mirror": True, **redact(event["payload"])})
                if span is not None:
                    span.update(output={"case_state": (store.get_case(case_id) or {}).get("state")}, metadata={**state, "dropped_operations": self.dropped - before_drops})
                    span.end()
            except Exception as exc:
                self._drop(exc)
            finally:
                _active.reset(token)
                if propagation is not None:
                    try:
                        propagation.__exit__(None, None, None)
                    except Exception as exc:
                        self._drop(exc)
            try:
                store.append_event(case_id, "observation_finished", "telemetry", {**state, "dropped_operations": self.dropped - before_drops, "delivery": "queued_unverified" if self.dropped == before_drops else "degraded"})
            except Exception as exc:
                self._drop(exc)

    @contextmanager
    def observation(self, name: str, *, as_type: str = "span", **fields: Any):
        span = None
        parent = _active.get()
        if self.enabled and parent and parent["span"] is not None:
            try:
                span = parent["span"].start_observation(name=name, as_type=as_type, **redact(fields))
            except Exception as exc:
                self._drop(exc)
        try:
            yield span
        finally:
            if span is not None:
                try:
                    span.end()
                except Exception as exc:
                    self._drop(exc)

    def update(self, span: Any, **fields: Any) -> None:
        if span is not None:
            try:
                span.update(**redact(fields))
            except Exception as exc:
                self._drop(exc)

    def event(self, *, case_id: str, run_id: str | None, kind: str, payload: dict, action_key: str | None = None) -> None:
        with self.observation(kind, metadata={"case_id": case_id, "run_id": run_id, "action_key": action_key, **redact(payload)}):
            pass

    def flush(self) -> None:
        if self.enabled:
            try:
                self._client.flush()
            except Exception as exc:
                self._drop(exc)


class ObservedModel:
    def __init__(self, model: Any, tracer: Any) -> None:
        self.model, self.tracer = model, tracer

    def generate(self, messages: Any, tool_schemas: Any, config: Any) -> Any:
        tracer = self.tracer()
        with tracer.observation(f"model.{config.tag}", as_type="generation", model=config.model, input={"messages": [m.to_dict() for m in messages], "tools": tool_schemas}, model_parameters={"max_tokens": config.max_tokens}) as span:
            try:
                result = self.model.generate(messages, tool_schemas, config)
            except Exception as exc:
                tracer.update(span, level="ERROR", status_message=type(exc).__name__)
                raise
            tracer.update(span, output=result.message.to_dict(), usage_details={"input": result.usage.input_tokens, "output": result.usage.output_tokens, "cache_read_input_tokens": result.usage.cache_read_input_tokens})
            return result


class ObservedTools:
    def __init__(self, runner: Any, tracer: Any) -> None:
        self.runner, self.tracer = runner, tracer

    def run(self, name: str, args: dict) -> Any:
        tracer = self.tracer()
        with tracer.observation(f"tool.{name}", as_type="tool", input=args) as span:
            result = self.runner.run(name, args)
            tracer.update(span, output=result.as_dict(), level="DEFAULT" if result.ok else "WARNING")
            return result
