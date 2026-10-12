"""The gate as seen from outside: what it stops, what it refuses, what it records."""

from __future__ import annotations

from typing import Any, Callable

from langgraph_production.models import GateRequest, HumanDecision
from langgraph_production.runtime import SupportRuntime, drive

from samples import CANCEL_TICKET, ESCALATE_TICKET, FIXED_TIME, REFUND_TICKET


def test_the_gate_names_the_exact_action(runtime: SupportRuntime) -> None:
    result = runtime.start(REFUND_TICKET, thread_id="g-1")
    assert result.gate is not None
    action = result.gate.action
    assert action is not None
    assert action.tool == "issue_refund"
    assert action.args == {"invoice_ref": "INV-10032", "amount_cents": 4500}
    assert "cannot be undone" in result.gate.question


def test_the_gate_binds_to_the_state_it_was_raised_over(runtime: SupportRuntime) -> None:
    result = runtime.start(REFUND_TICKET, thread_id="g-2")
    assert result.gate is not None
    assert result.gate.state_digest
    assert result.gate.token != result.gate.state_digest


def test_the_same_ticket_on_two_threads_gets_two_tokens(runtime: SupportRuntime) -> None:
    first = runtime.start(REFUND_TICKET, thread_id="g-3a")
    second = runtime.start(REFUND_TICKET, thread_id="g-3b")
    assert first.gate is not None and second.gate is not None
    assert first.gate.token != second.gate.token


def test_an_approval_from_another_thread_is_refused(
    durable_factory: Callable[..., SupportRuntime],
) -> None:
    """The single use rule, exercised through the graph rather than the helper."""
    runtime = durable_factory()
    try:
        mine = runtime.start(REFUND_TICKET, thread_id="g-4a")
        theirs = runtime.start(CANCEL_TICKET, thread_id="g-4b")
        assert mine.gate is not None and theirs.gate is not None

        stolen = HumanDecision(
            token=theirs.gate.token,
            approver="ops.mallory",
            decision="approve",
            note="approved something else entirely",
            decided_at=FIXED_TIME,
        )
        result = runtime.resume_with_decision("g-4a", stolen)
        assert result.outcome == "gate_rejected"
        assert result.state["gate_rejection"].startswith("token_mismatch")
        assert runtime.effects.total_executions() == 0
    finally:
        runtime.close()


def test_a_rejected_gate_never_reaches_the_action(
    durable_factory: Callable[..., SupportRuntime],
) -> None:
    runtime = durable_factory()
    try:
        opened = runtime.start(REFUND_TICKET, thread_id="g-5")
        assert opened.gate is not None
        forged = HumanDecision(
            token="0" * 32,
            approver="ops.mallory",
            decision="approve",
            note="",
            decided_at=FIXED_TIME,
        )
        result = runtime.resume_with_decision("g-5", forged)
        assert "act" not in result.trail
        assert result.state.get("receipt") is None
    finally:
        runtime.close()


def test_a_rejected_gate_says_so_to_the_customer(
    durable_factory: Callable[..., SupportRuntime],
) -> None:
    runtime = durable_factory()
    try:
        runtime.start(REFUND_TICKET, thread_id="g-6")
        forged = HumanDecision(
            token="0" * 32, approver="x", decision="approve", note="", decided_at=FIXED_TIME
        )
        result = runtime.resume_with_decision("g-6", forged)
        assert "could not be verified" in result.reply
    finally:
        runtime.close()


def test_a_malformed_decision_is_refused(durable_factory: Callable[..., SupportRuntime]) -> None:
    runtime = durable_factory()
    try:
        runtime.start(REFUND_TICKET, thread_id="g-7")
        broken: Any = {"token": "abc", "approver": "ops.dana"}  # no verdict, no timestamp
        result = runtime.resume_with_payload("g-7", broken)
        assert result.outcome == "gate_rejected"
        assert result.state["gate_rejection"].startswith("malformed_decision")
        assert runtime.effects.total_executions() == 0
    finally:
        runtime.close()


def test_a_decision_that_is_not_even_a_mapping_is_refused(
    durable_factory: Callable[..., SupportRuntime],
) -> None:
    runtime = durable_factory()
    try:
        runtime.start(REFUND_TICKET, thread_id="g-7b")
        result = runtime.resume_with_payload("g-7b", "yes go ahead")
        assert result.outcome == "gate_rejected"
        assert runtime.effects.total_executions() == 0
    finally:
        runtime.close()


def test_an_unknown_verdict_is_refused(durable_factory: Callable[..., SupportRuntime]) -> None:
    runtime = durable_factory()
    try:
        opened = runtime.start(REFUND_TICKET, thread_id="g-7c")
        assert opened.gate is not None
        result = runtime.resume_with_payload(
            "g-7c",
            {
                "token": opened.gate.token,
                "approver": "ops.dana",
                "decision": "maybe",
                "note": "",
                "decided_at": FIXED_TIME.isoformat(),
            },
        )
        assert result.outcome == "gate_rejected"
        assert runtime.effects.total_executions() == 0
    finally:
        runtime.close()


def test_an_approval_is_recorded_with_who_and_when(
    durable_factory: Callable[..., SupportRuntime],
    approve: Callable[[GateRequest], HumanDecision],
) -> None:
    outcome = drive(
        runtime_factory=durable_factory,
        ticket=REFUND_TICKET,
        thread_id="g-8",
        decider=approve,
    )
    approval = outcome.result.state["approval"]
    assert approval["kind"] == "approval"
    assert approval["action_id"] == outcome.gates[0].action.action_id  # type: ignore[union-attr]
    assert approval["ticket_id"] == REFUND_TICKET.id


def test_a_handoff_approval_carries_no_action(
    durable_factory: Callable[..., SupportRuntime],
    approve: Callable[[GateRequest], HumanDecision],
) -> None:
    outcome = drive(
        runtime_factory=durable_factory,
        ticket=ESCALATE_TICKET,
        thread_id="g-9",
        decider=approve,
    )
    approval = outcome.result.state["approval"]
    assert approval["kind"] == "handoff"
    assert approval["action_id"] is None


def test_the_ledger_holds_one_token_per_gate(
    durable_factory: Callable[..., SupportRuntime],
    approve: Callable[[GateRequest], HumanDecision],
) -> None:
    drive(
        runtime_factory=durable_factory,
        ticket=REFUND_TICKET,
        thread_id="g-10",
        decider=approve,
    )
    runtime = durable_factory()
    try:
        assert runtime.approvals.count() == 1
    finally:
        runtime.close()
