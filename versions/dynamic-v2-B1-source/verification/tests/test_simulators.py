"""Simulator decisions follow disclosed scripts, never the evaluation oracle."""

from __future__ import annotations

import pytest

from billingops.evaluation.simulators import ApprovalSimulator


def test_approval_script_does_not_correct_an_incorrect_proposal() -> None:
    simulator = ApprovalSimulator(script={"decision": "approve"})
    wrong_proposal = {
        "invoice_id": "INV-WRONG",
        "payment_id": "PAY-WRONG",
        "amount_cents": 999999,
    }

    assert simulator.decide(wrong_proposal) == "approve"
    assert simulator.approved == [wrong_proposal]
    assert simulator.decisions == ["approve"]


@pytest.mark.parametrize("note", ["", None])
def test_rejection_without_reason_does_not_invent_approval_note(note: str | None) -> None:
    simulator = ApprovalSimulator(script={"decision": "reject", "note": note})

    assert simulator.decide({"payment_id": "PAY-2", "amount_cents": 4900}) == "reject"
    assert simulator.note == ""
    assert simulator.approved == []


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_missing_note_uses_a_neutral_default(decision: str) -> None:
    simulator = ApprovalSimulator(script={"decision": decision})

    assert simulator.decide({"payment_id": "PAY-2", "amount_cents": 4900}) == decision
    assert simulator.note == "simulated decision"


def test_reject_first_uses_the_script_reason_without_changing_later_decisions() -> None:
    simulator = ApprovalSimulator(
        script={"decision": "approve", "reject_first": True, "note": "operator supplied reason"}
    )
    first = {"payment_id": "PAY-2", "amount_cents": 4900}
    revised = {"payment_id": "PAY-3", "amount_cents": 4900}

    assert simulator.decide(first) == "reject"
    assert simulator.note == "operator supplied reason"
    assert simulator.decide(revised) == "approve"
    assert simulator.note == "operator supplied reason"
    assert simulator.approved == [revised]
    assert simulator.decisions == ["reject", "approve"]
