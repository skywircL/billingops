from __future__ import annotations

from billingops.observability.tracing import Tracer

from conftest import approve, run_case


class ExplodingClient:
    def start_observation(self, **kwargs):
        raise RuntimeError("langfuse is down")

    def flush(self):
        raise RuntimeError("still down")


def test_tracing_failures_never_change_the_outcome(runtime_factory, billing) -> None:
    tracer = Tracer(enabled=False)
    tracer._client = ExplodingClient()
    tracer.enabled = True
    rt = runtime_factory()
    rt.tracer = tracer
    view = approve(rt, run_case(rt)["case"]["id"])
    assert view["case"]["state"] == "closed" and view["execution"]["status"] == "done"
    tracer.flush()
    # The database audit log is the record, and it is complete.
    kinds = [e["kind"] for e in rt.store.events(view["case"]["id"])]
    assert "refund_verified" in kinds and "approval_recorded" in kinds
    assert tracer.dropped > 0
    assert rt.store.metrics_summary()["telemetry_dropped_operations"] > 0


class RecordingSpan:
    def __init__(self, client, fields):
        self.client, self.fields, self.ended = client, fields, False
        self.updates = []

    def start_observation(self, **fields):
        child = RecordingSpan(self.client, {**fields, "parent": self.fields["name"]})
        self.client.spans.append(child)
        return child

    def update(self, **fields):
        self.updates.append(fields)

    def end(self):
        self.ended = True


class RecordingClient:
    def __init__(self):
        self.spans = []

    def start_observation(self, **fields):
        span = RecordingSpan(self, fields)
        self.spans.append(span)
        return span

    def get_trace_url(self, *, trace_id):
        return f"https://example.test/traces/{trace_id}"

    def flush(self):
        pass


def test_positive_traces_link_task_command_and_parent_across_restart(runtime_factory, billing):
    import pytest
    pytest.importorskip("langfuse")
    client = RecordingClient()
    rt = runtime_factory()
    rt.tracer = Tracer(enabled=True, client=client)
    view = run_case(rt)
    cid = view["case"]["id"]
    rt.close()
    restarted = runtime_factory()
    restarted.tracer = Tracer(enabled=True, client=client)
    final = approve(restarted, cid)
    assert final["case"]["state"] == "closed"
    events = restarted.store.events(cid)
    runs = [e["payload"] for e in events if e["kind"] == "observation_started"]
    assert len(runs) == 2 and runs[1]["parent_run_id"] == runs[0]["run_id"]
    assert runs[1]["command_id"] and runs[0]["task_id"] != runs[1]["task_id"]
    names = {s.fields["name"] for s in client.spans}
    assert {"model.extract", "model.investigate", "model.conclude", "tool.list_payments", "approval_recorded", "refund_verified"}.issubset(names)
    assert all(s.ended for s in client.spans)
    approval = next(s for s in client.spans if s.fields["name"] == "approval_recorded")
    assert approval.fields["metadata"].get("token") in (None, "[REDACTED]")
    assert restarted.tracer.dropped == 0
