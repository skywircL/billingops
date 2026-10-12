from __future__ import annotations

from pathlib import Path

import pytest

from billingops.llm.base import Message, ModelConfig, ToolCall
from billingops.llm.fake import ScriptedModel
from billingops.llm.replay import RecordingModel, ReplayMiss, ReplayModel, Trace, normalise, request_key

from conftest import DUP_TEXT, run_case


def _messages(call_id: str, key_order: bool) -> list[Message]:
    args = {"invoice_id": "INV-1", "note": "x"} if key_order else {"note": "x", "invoice_id": "INV-1"}
    return [
        Message(role="system", content="s"),
        Message(role="user", content="u"),
        Message(role="assistant", tool_calls=[ToolCall(id=call_id, name="get_invoice", arguments=args)], provider_content=[{"type": "thinking", "signature": call_id}]),
        Message(role="tool", tool_call_id=call_id, content='{"ok": true}'),
    ]


CONFIG = ModelConfig(tag="investigate", model="m")


def test_tool_call_ids_and_key_order_are_normalised_but_order_is_kept() -> None:
    a = request_key(_messages("toolu_abc", True), [], CONFIG)
    b = request_key(_messages("toolu_xyz", False), [], CONFIG)
    assert a == b
    request, id_map = normalise(_messages("toolu_abc", True), [], CONFIG)
    assert id_map == {"toolu_abc": "tc1"}
    assert request["messages"][2]["tool_calls"][0]["id"] == "tc1" and request["messages"][3]["tool_call_id"] == "tc1"
    assert "provider_content" not in request["messages"][2]
    # Message order matters: swapping two messages changes the key.
    swapped = _messages("toolu_abc", True)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    assert request_key(swapped, [], CONFIG) != a


def test_record_then_replay_reproduces_the_case_and_a_miss_fails(tmp_path: Path, runtime_factory, billing) -> None:
    trace = Trace(tmp_path / "trace.json")
    rt = runtime_factory(model=RecordingModel(ScriptedModel(), trace))
    recorded = run_case(rt)
    assert recorded["case"]["state"] == "awaiting_approval"
    assert len(trace) == recorded["budgets"]["model_calls"] + 2  # + extract + conclude
    rt.close()

    replay = ReplayModel(Trace.load(tmp_path / "trace.json"))
    rt2 = runtime_factory(model=replay)
    replayed = run_case(rt2)
    assert replayed["case"]["proposal"]["payment_id"] == recorded["case"]["proposal"]["payment_id"]
    assert replayed["case"]["conclusion"]["text"] == recorded["case"]["conclusion"]["text"]
    assert replay.served == len(trace)

    rt3 = runtime_factory(model=ReplayModel(Trace.load(tmp_path / "trace.json")))
    view = run_case(rt3, "A different message the trace has never seen, INV-2026-0142 twice.")
    assert view["case"]["state"] == "error"
    assert "ReplayMiss" in (view["case"]["error"] or "") or any("ReplayMiss" in (t["error"] or "") for t in rt3.store.tasks(view["case"]["id"]))
