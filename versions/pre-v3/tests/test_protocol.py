"""The single-tool protocol, repairs, and budgets, driven by a misbehaving model."""

from __future__ import annotations

from typing import Any, Sequence

from billingops.config import MAX_CLARIFICATIONS, MAX_INVESTIGATION_CALLS, MAX_READS, MAX_REPAIRS
from billingops.llm.base import Message, ModelConfig, ModelResponse, ToolCall, Usage
from billingops.llm.fake import ChaosModel, ScriptedModel

from conftest import DUP_TEXT, run_case


def test_two_tool_calls_in_one_turn_execute_nothing_and_cost_a_repair(runtime_factory, billing) -> None:
    rt = runtime_factory(model=ChaosModel(ScriptedModel(), double_call_on={1}))
    view = run_case(rt)
    assert view["case"]["state"] == "awaiting_approval"  # recovered and finished
    assert view["budgets"]["repairs"] == 1
    assert [e["reason"] for e in view["protocol_errors"]] == ["multiple_tool_calls"]
    # Neither of the two reads happened: the evidence log has no list_invoices / list_customer_refunds.
    assert not any(e["tool"] in ("list_invoices", "list_customer_refunds") for e in view["evidence"])
    events = [e for e in rt.store.events(view["case"]["id"]) if e["kind"] == "protocol_error"]
    assert len(events) == 1


def test_prose_instead_of_a_tool_call_is_repaired_once(runtime_factory, billing) -> None:
    rt = runtime_factory(model=ChaosModel(ScriptedModel(), prose_on={2}))
    view = run_case(rt)
    assert view["case"]["state"] == "awaiting_approval"
    assert view["budgets"]["repairs"] == 1 and view["budgets"]["model_calls"] == 5


def test_unknown_tool_such_as_a_refund_is_refused_and_repaired(runtime_factory, billing) -> None:
    rt = runtime_factory(model=ChaosModel(ScriptedModel(), unknown_tool_on={1}))
    view = run_case(rt)
    assert view["budgets"]["repairs"] == 1
    assert [e["reason"] for e in view["protocol_errors"]] == ["unknown_tool"]
    assert billing.payment_refunds("C-1001", "PAY-88213") == []


def test_repair_budget_exhaustion_escalates_and_keeps_the_evidence(runtime_factory, billing) -> None:
    rt = runtime_factory(model=ChaosModel(ScriptedModel(), double_call_on={2, 3, 4}))
    view = run_case(rt)
    assert view["case"]["state"] == "escalated"
    assert view["case"]["escalation"]["reason"] == "repair_budget_exhausted"
    assert view["budgets"]["repairs"] == MAX_REPAIRS + 1
    assert len(view["evidence"]) == 1  # the read from call 1 is preserved
    assert view["budgets"]["model_calls"] == 4  # repairs count toward the investigation-call budget


class EndlessReader:
    """Always asks for another read, never ends: only budgets can stop it."""

    def generate(self, messages: Sequence[Message], tool_schemas: Sequence[dict[str, Any]], config: ModelConfig) -> ModelResponse:
        n = sum(1 for m in messages if m.role == "assistant") + 1
        if config.tag == "extract":
            return ScriptedModel().generate(messages, tool_schemas, config)
        if config.tag == "conclude":
            return ModelResponse(Message(role="assistant", content="done"), Usage())
        last = messages[-1]
        if last.role == "tool" and "read_budget_exhausted" in last.content:
            call = ToolCall(id=f"e-{n}", name="end_investigation", arguments={"conclusion_type": "insufficient_information", "target_invoice_id": None, "target_payment_id": None, "summary": "out of reads", "facts": []})
        else:
            call = ToolCall(id=f"e-{n}", name="get_invoice", arguments={"invoice_id": "INV-2026-0142"})
        return ModelResponse(Message(role="assistant", tool_calls=[call]), Usage(10, 10), "tool_use", "endless")


def test_read_budget_stops_reads_and_the_model_must_conclude(runtime_factory, billing) -> None:
    rt = runtime_factory(model=EndlessReader())
    view = run_case(rt)
    assert view["budgets"]["reads"] == MAX_READS
    assert len(view["evidence"]) == MAX_READS
    assert view["case"]["state"] == "closed" and view["case"]["conclusion"]["type"] == "insufficient_information"


class NeverEnds:
    def generate(self, messages, tool_schemas, config) -> ModelResponse:
        n = sum(1 for m in messages if m.role == "assistant") + 1
        if config.tag != "investigate":
            return ScriptedModel().generate(messages, tool_schemas, config)
        return ModelResponse(Message(role="assistant", tool_calls=[ToolCall(id=f"n-{n}", name="get_invoice", arguments={"invoice_id": "INV-2026-0142"})]), Usage(10, 10), "tool_use", "never")


def test_model_call_budget_escalates_with_evidence(runtime_factory, billing) -> None:
    rt = runtime_factory(model=NeverEnds())
    view = run_case(rt)
    assert view["case"]["state"] == "escalated"
    assert view["case"]["escalation"]["reason"] == "model_call_budget_exhausted"
    assert view["budgets"]["model_calls"] == MAX_INVESTIGATION_CALLS
    assert view["budgets"]["reads"] == MAX_READS


class Asker:
    """Asks the customer every turn; the clarification budget must cut it off."""

    def generate(self, messages, tool_schemas, config) -> ModelResponse:
        n = sum(1 for m in messages if m.role == "assistant") + 1
        if config.tag != "investigate":
            return ScriptedModel().generate(messages, tool_schemas, config)
        last = messages[-1]
        if last.role == "tool" and "clarification_budget_exhausted" in last.content:
            return ModelResponse(Message(role="assistant", tool_calls=[ToolCall(id=f"a-{n}", name="end_investigation", arguments={"conclusion_type": "insufficient_information", "target_invoice_id": None, "target_payment_id": None, "summary": "gave up", "facts": []})]), Usage(), "tool_use", "asker")
        return ModelResponse(Message(role="assistant", tool_calls=[ToolCall(id=f"a-{n}", name="ask_customer", arguments={"question": f"Question number {n}?"})]), Usage(), "tool_use", "asker")


def test_clarification_budget_accumulates_across_pauses(runtime_factory, billing) -> None:
    rt = runtime_factory(model=Asker())
    case = rt.submit_case(customer_id="C-1001", created_by="cust-c1001", text="charged twice")
    rt.drain()
    asked = 0
    while rt.store.get_case(case["id"])["state"] == "awaiting_user":
        asked += 1
        rt.submit_message(case["id"], text="I don't have that information", actor="cust-c1001")
        rt.drain()
    assert asked == MAX_CLARIFICATIONS
    view = rt.case_view(case["id"])
    assert view["budgets"]["clarifications"] == MAX_CLARIFICATIONS
    assert view["case"]["state"] == "closed" and view["case"]["conclusion"]["type"] == "insufficient_information"
