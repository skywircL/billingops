"""Record real model calls once, replay them in an isolated run.

Replay is specific to this project's adapter and message shape. Normalisation
before hashing:

* tool-call ids are renamed ``tc1, tc2, ...`` in order of first appearance,
  and a tool result is renamed to match the call it answers, so the
  correspondence survives while the provider's random ids do not;
* JSON object keys are sorted; message order and array order are kept;
* opaque ``provider_content`` (signed thinking blocks) is dropped.

A replay miss raises. Record/replay is off during the formal experiment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from langgraph_production.models import digest

from .base import Message, ModelConfig, ModelResponse, ToolCall


class ReplayMiss(KeyError):
    pass


def normalise(messages: Sequence[Message], tool_schemas: Sequence[dict[str, Any]], config: ModelConfig) -> tuple[dict[str, Any], dict[str, str]]:
    """Return the canonical request and the id map used to build it."""
    id_map: dict[str, str] = {}

    def mapped(raw_id: str | None) -> str | None:
        if raw_id is None:
            return None
        if raw_id not in id_map:
            id_map[raw_id] = f"tc{len(id_map) + 1}"
        return id_map[raw_id]

    out_messages: list[dict[str, Any]] = []
    for msg in messages:
        item: dict[str, Any] = {"role": msg.role, "content": msg.content}
        if msg.tool_calls:
            item["tool_calls"] = [
                {"id": mapped(tc.id), "name": tc.name, "arguments": tc.arguments} for tc in msg.tool_calls
            ]
        if msg.tool_call_id is not None:
            item["tool_call_id"] = mapped(msg.tool_call_id)
        if msg.is_error:
            item["is_error"] = True
        out_messages.append(item)
    request = {
        "config": {"tag": config.tag, "model": config.model, "effort": config.effort, "max_tokens": config.max_tokens},
        "tools": [json.loads(json.dumps(s, sort_keys=True)) for s in tool_schemas],
        "messages": out_messages,
    }
    # Preserve archived auto-choice keys; explicit B2 recovery is a different request.
    if config.tool_choice != 'auto':
        request['config']['tool_choice'] = config.tool_choice
    return request, id_map


def request_key(messages: Sequence[Message], tool_schemas: Sequence[dict[str, Any]], config: ModelConfig) -> str:
    request, _ = normalise(messages, tool_schemas, config)
    return digest(request, length=32)


def _normalise_response(response: ModelResponse, id_map: dict[str, str]) -> dict[str, Any]:
    next_n = len(id_map) + 1
    calls = []
    for tc in response.message.tool_calls:
        if tc.id not in id_map:
            id_map[tc.id] = f"tc{next_n}"
            next_n += 1
        calls.append({"id": id_map[tc.id], "name": tc.name, "arguments": tc.arguments})
    return {
        "message": {"role": "assistant", "content": response.message.content, "tool_calls": calls},
        "usage": response.usage.as_dict(),
        "stop_reason": response.stop_reason,
        "model": response.model,
    }


@dataclass
class Trace:
    path: Path
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Trace":
        p = Path(path)
        entries = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        return cls(path=p, entries=entries)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")

    def __len__(self) -> int:
        return len(self.entries)


class RecordingModel:
    """Calls ``inner`` and appends every exchange to the trace. No reuse: each call is paid for."""

    def __init__(self, inner: Any, trace: Trace) -> None:
        self.inner = inner
        self.trace = trace

    def generate(self, messages: Sequence[Message], tool_schemas: Sequence[dict[str, Any]], config: ModelConfig) -> ModelResponse:
        request, id_map = normalise(messages, tool_schemas, config)
        key = digest(request, length=32)
        response = self.inner.generate(messages, tool_schemas, config)
        self.trace.entries[key] = {"request": request, "response": _normalise_response(response, id_map)}
        self.trace.save()
        return response


class ReplayModel:
    """Serves recorded responses. A request the trace has never seen fails immediately."""

    def __init__(self, trace: Trace) -> None:
        self.trace = trace
        self.served = 0

    def generate(self, messages: Sequence[Message], tool_schemas: Sequence[dict[str, Any]], config: ModelConfig) -> ModelResponse:
        request, _ = normalise(messages, tool_schemas, config)
        key = digest(request, length=32)
        entry = self.trace.entries.get(key)
        if entry is None:
            raise ReplayMiss(f"no recorded response for tag={config.tag} key={key}")
        self.served += 1
        raw = entry["response"]
        message = Message(
            role="assistant",
            content=raw["message"].get("content", ""),
            tool_calls=[ToolCall(**tc) for tc in raw["message"].get("tool_calls", [])],
        )
        return ModelResponse.from_dict({**raw, "message": message.to_dict()})
