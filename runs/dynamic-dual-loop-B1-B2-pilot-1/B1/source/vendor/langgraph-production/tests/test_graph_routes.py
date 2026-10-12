"""End to end behaviour of each branch, and the property that matters most:
nothing irreversible executes without the gate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from langgraph_production.effects import EffectFailed, open_effects, resolve_effect
from langgraph_production.faults import CrashHook, no_crash
from langgraph_production.llm.offline import ScriptedLLM
from langgraph_production.models import GateRequest, HumanDecision, Ticket
from langgraph_production.runtime import SupportRuntime, drive
from langgraph_production.tools import ToolRegistry

from samples import ANSWER_TICKET, ESCALATE_TICKET, FIXED_TIME, LOOKUP_TICKET, REFUND_TICKET

IRREVERSIBLE_ARGS = {
    "issue_refund": {"invoice_ref": "INV-10032", "amount_cents": 4500},
    "cancel_subscription": {"account_ref": "ACC-2041"},
    "reset_credentials": {"account_ref": "ACC-2041"},
}
SAFE_ARGS = {
    "lookup_order_status": {"order_ref": "ORD-40021"},
    "lookup_invoice": {"invoice_ref": "INV-10032"},
    "check_service_status": {"region": "eu-west"},
}


def scripted(tool: str, args: dict[str, object]) -> ScriptedLLM:
    return ScriptedLLM(
        {
            "route": json.dumps({"route": "tool", "tool": tool, "args": args, "confidence": 0.95}),
            "compose": "handled",
        }
    )


def test_an_answer_ticket_never_reaches_a_tool(runtime: SupportRuntime) -> None:
    result = runtime.start(ANSWER_TICKET, thread_id="t-answer")
    assert result.status == "completed"
    assert result.trail == ["classify", "compose"]
    assert result.outcome == "answered"


def test_a_read_only_tool_runs_without_a_gate(runtime: SupportRuntime) -> None:
    result = runtime.start(LOOKUP_TICKET, thread_id="t-lookup")
    assert result.status == "completed"
    assert result.trail == ["classify", "plan", "act", "compose"]
    assert result.state["receipt"]["tool"] == "lookup_order_status"


def test_a_read_only_tool_answers_with_the_record(runtime: SupportRuntime) -> None:
    result = runtime.start(LOOKUP_TICKET, thread_id="t-lookup-2")
    assert "in_transit" in result.reply


def test_an_irreversible_tool_stops_before_acting(runtime: SupportRuntime) -> None:
    result = runtime.start(REFUND_TICKET, thread_id="t-refund")
    assert result.status == "awaiting_human"
    assert result.gate is not None
    assert result.gate.kind == "approval"
    assert result.trail == ["classify", "plan"]


def test_nothing_executes_while_a_gate_is_open(runtime: SupportRuntime) -> None:
    runtime.start(REFUND_TICKET, thread_id="t-refund-3")
    assert runtime.effects.total_executions() == 0


def test_an_escalation_stops_for_a_handoff(runtime: SupportRuntime) -> None:
    result = runtime.start(ESCALATE_TICKET, thread_id="t-escalate")
    assert result.status == "awaiting_human"
    assert result.gate is not None
    assert result.gate.kind == "handoff"
    assert result.gate.action is None


@pytest.mark.parametrize("tool", sorted(IRREVERSIBLE_ARGS))
def test_every_irreversible_tool_is_gated(tool: str, clock: Callable[[], object]) -> None:
    """The guarantee stated in the graph docstring, checked per tool."""
    with SupportRuntime(llm=scripted(tool, IRREVERSIBLE_ARGS[tool]), clock=clock) as runtime:  # type: ignore[arg-type]
        result = runtime.start(Ticket(id=f"T-{tool}", text="do it"), thread_id=f"th-{tool}")
        assert result.status == "awaiting_human"
        assert result.gate is not None
        assert result.gate.action is not None
        assert result.gate.action.irreversible is True
        assert runtime.effects.total_executions() == 0


@pytest.mark.parametrize("tool", sorted(SAFE_ARGS))
def test_every_read_only_tool_runs_straight_through(tool: str, clock: Callable[[], object]) -> None:
    with SupportRuntime(llm=scripted(tool, SAFE_ARGS[tool]), clock=clock) as runtime:  # type: ignore[arg-type]
        result = runtime.start(Ticket(id=f"T-{tool}", text="check it"), thread_id=f"th-{tool}")
        assert result.status == "completed"
        assert runtime.effects.total_executions() == 1


def test_the_registry_and_the_gate_agree_on_what_is_irreversible(registry: ToolRegistry) -> None:
    assert set(IRREVERSIBLE_ARGS) == set(registry.irreversible_names)


def test_approving_executes_the_effect_once(
    durable_factory: Callable[..., SupportRuntime],
    approve: Callable[[GateRequest], HumanDecision],
) -> None:
    outcome = drive(
        runtime_factory=durable_factory,
        ticket=REFUND_TICKET,
        thread_id="t-approve",
        decider=approve,
    )
    assert outcome.result.outcome == "acted"
    assert outcome.result.trail == ["classify", "plan", "human_gate", "act", "compose"]
    assert outcome.result.state["receipt"]["result"]["refunded_cents"] == 4500


@pytest.mark.parametrize("second_spelling", ["4500", 4500.0])
def test_the_same_refund_spelled_differently_is_paid_once(
    tmp_path: Path,
    clock: Callable[[], object],
    approve: Callable[[GateRequest], HumanDecision],
    second_spelling: object,
) -> None:
    """Two deliveries of one refund, with the amount typed differently.

    Nothing forces a model to spell an amount the same way twice. If the
    second spelling produced a second idempotency key, the customer would be
    refunded twice, and the guarantee this repo is about would be worth
    nothing.
    """
    db_path = tmp_path / "state.sqlite3"
    ticket = Ticket(id="T-REFUND", text="Refund the double charge on INV-10032.")

    def run(amount: object) -> Any:
        args = {"invoice_ref": "INV-10032", "amount_cents": amount}

        def factory(hook: CrashHook = no_crash) -> SupportRuntime:
            return SupportRuntime(
                llm=scripted("issue_refund", args),
                db_path=db_path,
                clock=clock,  # type: ignore[arg-type]
                crash_hook=hook,
            )

        return drive(
            runtime_factory=factory,
            ticket=ticket,
            thread_id="th-refund-spelling",
            decider=approve,
        )

    first = run(4500)
    second = run(second_spelling)

    assert first.result.state["receipt"]["replayed"] is False
    assert second.result.state["receipt"]["replayed"] is True
    assert (
        second.result.state["receipt"]["idempotency_key"]
        == first.result.state["receipt"]["idempotency_key"]
    )

    with SupportRuntime(llm=scripted("issue_refund", {}), db_path=db_path) as inspector:
        assert inspector.effects.total_executions() == 1


def test_a_handler_refusal_does_not_strand_the_ticket(
    tmp_path: Path,
    clock: Callable[[], object],
    approve: Callable[[GateRequest], HumanDecision],
) -> None:
    """INV-99999 is a well formed reference to an invoice that does not exist.

    The schema lets it through, the human approves it, and the handler is the
    one that says no. That used to leave the claim pending, so every later
    attempt came back in doubt and the ticket was finished. Now the failure is
    definite and the claim is closed rather than open.
    """
    db_path = tmp_path / "state.sqlite3"
    args = {"invoice_ref": "INV-99999", "amount_cents": 4500}

    def factory(hook: CrashHook = no_crash) -> SupportRuntime:
        return SupportRuntime(
            llm=scripted("issue_refund", args),
            db_path=db_path,
            clock=clock,  # type: ignore[arg-type]
            crash_hook=hook,
        )

    with pytest.raises(EffectFailed) as caught:
        drive(
            runtime_factory=factory,
            ticket=Ticket(id="T-GHOST", text="Refund INV-99999."),
            thread_id="th-ghost",
            decider=approve,
        )

    with SupportRuntime(llm=scripted("issue_refund", args), db_path=db_path) as inspector:
        assert inspector.effects.total_executions() == 0
        assert list(open_effects(inspector.effects)) == [caught.value.key]
        resolve_effect(
            store=inspector.effects,
            key=caught.value.key,
            outcome="not_performed",
            resolver="ops.dana",
        )
        assert open_effects(inspector.effects) == {}


def test_denying_leaves_the_effect_unrun(
    durable_factory: Callable[..., SupportRuntime],
    deny: Callable[[GateRequest], HumanDecision],
) -> None:
    outcome = drive(
        runtime_factory=durable_factory,
        ticket=REFUND_TICKET,
        thread_id="t-deny",
        decider=deny,
    )
    assert outcome.result.outcome == "denied"
    assert outcome.result.state.get("receipt") is None
    assert "act" not in outcome.result.trail


def test_a_denial_reaches_the_customer_reply(
    durable_factory: Callable[..., SupportRuntime],
    deny: Callable[[GateRequest], HumanDecision],
) -> None:
    outcome = drive(
        runtime_factory=durable_factory,
        ticket=REFUND_TICKET,
        thread_id="t-deny-2",
        decider=deny,
    )
    assert "not entitled" in outcome.result.reply


def test_an_approved_handoff_ends_as_handed_off(
    durable_factory: Callable[..., SupportRuntime],
    approve: Callable[[GateRequest], HumanDecision],
) -> None:
    outcome = drive(
        runtime_factory=durable_factory,
        ticket=ESCALATE_TICKET,
        thread_id="t-handoff",
        decider=approve,
    )
    assert outcome.result.outcome == "handed_off"
    assert outcome.result.state.get("receipt") is None


def test_the_gate_shows_the_router_rationale(runtime: SupportRuntime) -> None:
    result = runtime.start(ESCALATE_TICKET, thread_id="t-context")
    assert result.gate is not None
    assert result.gate.context["router_rationale"]


def test_an_untrusted_router_decision_escalates_to_a_human(clock: Callable[[], object]) -> None:
    llm = ScriptedLLM({"route": "I will just refund it", "compose": "handled"})
    with SupportRuntime(llm=llm, clock=clock) as runtime:  # type: ignore[arg-type]
        result = runtime.start(Ticket(id="T-BAD", text="refund me"), thread_id="t-bad")
        assert result.status == "awaiting_human"
        assert result.gate is not None
        assert result.gate.context["router_trusted"] is False
        assert result.gate.context["router_fallback"] == "invalid_json"


def test_the_decision_is_recorded_in_the_state(runtime: SupportRuntime) -> None:
    result = runtime.start(LOOKUP_TICKET, thread_id="t-decision")
    assert result.state["decision"]["route"] == "tool"
    assert result.state["decision"]["confidence"] > 0


def test_approval_details_survive_into_the_final_state(
    durable_factory: Callable[..., SupportRuntime],
    approve: Callable[[GateRequest], HumanDecision],
) -> None:
    outcome = drive(
        runtime_factory=durable_factory,
        ticket=REFUND_TICKET,
        thread_id="t-audit",
        decider=approve,
    )
    approval = outcome.result.state["approval"]
    assert approval["approver"] == "ops.dana"
    assert approval["decision"] == "approve"
    assert approval["decided_at"].startswith(FIXED_TIME.strftime("%Y-%m-%d"))
    assert approval["state_digest"]
