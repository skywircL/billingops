from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from langgraph_production.models import (
    ApprovalRecord,
    PlannedAction,
    Ticket,
    ToolCall,
    canonical_json,
    digest,
)
from langgraph_production.tools import ToolRegistry

WHEN = datetime(2026, 1, 1, tzinfo=timezone.utc)
REGISTRY = ToolRegistry()


def plan(ticket_id: str, tool: str, **args: object) -> PlannedAction:
    return PlannedAction.build(
        ticket_id=ticket_id,
        call=ToolCall(name=tool, args=args),
        spec=REGISTRY.require(tool),
    )


def test_canonical_json_is_key_order_independent() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_digest_is_stable_across_calls() -> None:
    payload = {"tool": "issue_refund", "args": {"amount_cents": 4500}}
    assert digest(payload) == digest(dict(payload))


def test_digest_changes_when_any_value_changes() -> None:
    base = digest({"amount_cents": 4500})
    assert base != digest({"amount_cents": 4501})


def test_action_id_is_derived_from_ticket_and_arguments() -> None:
    first = plan("T-1", "issue_refund", invoice_ref="INV-10032", amount_cents=4500)
    second = plan("T-1", "issue_refund", invoice_ref="INV-10032", amount_cents=4500)
    assert first.action_id == second.action_id


def test_action_id_differs_when_the_amount_differs() -> None:
    small = plan("T-1", "issue_refund", invoice_ref="INV-10032", amount_cents=4500)
    large = plan("T-1", "issue_refund", invoice_ref="INV-10032", amount_cents=450000)
    assert small.action_id != large.action_id


def test_action_id_differs_across_tickets() -> None:
    first = plan("T-1", "cancel_subscription", account_ref="ACC-2041")
    second = plan("T-2", "cancel_subscription", account_ref="ACC-2041")
    assert first.action_id != second.action_id


@pytest.mark.parametrize("spelling", [4500, "4500", 4500.0])
def test_the_same_amount_spelled_three_ways_is_one_action(spelling: object) -> None:
    """A model that types the amount as a string is asking for the same refund.

    The id is the namespace of the idempotency key, so a spelling difference
    that survives into it is a second payment.
    """
    canonical = plan("T-1", "issue_refund", invoice_ref="INV-10032", amount_cents=4500)
    other = plan("T-1", "issue_refund", invoice_ref="INV-10032", amount_cents=spelling)
    assert other.action_id == canonical.action_id
    assert other.args == {"invoice_ref": "INV-10032", "amount_cents": 4500}


def test_the_action_carries_the_parsed_arguments_not_the_raw_ones() -> None:
    action = plan("T-1", "issue_refund", invoice_ref="INV-10032", amount_cents="4500")
    assert action.args["amount_cents"] == 4500
    assert action.summary == "issue_refund(amount_cents=4500, invoice_ref=INV-10032)"


def test_the_irreversible_flag_comes_from_the_tool_not_the_caller() -> None:
    assert plan("T-1", "issue_refund", invoice_ref="INV-10032", amount_cents=1).irreversible is True
    assert plan("T-1", "lookup_invoice", invoice_ref="INV-10032").irreversible is False


def test_planning_an_action_the_schema_rejects_raises() -> None:
    with pytest.raises(ValidationError):
        plan("T-1", "issue_refund", invoice_ref="the may invoice", amount_cents=4500)


def _record(**overrides: object) -> ApprovalRecord:
    base = {
        "token": "tok",
        "ticket_id": "T-1",
        "action_id": "abc",
        "kind": "approval",
        "approver": "ops.dana",
        "decision": "approve",
        "note": "",
        "decided_at": WHEN,
        "state_digest": "sd",
    }
    base.update(overrides)
    return ApprovalRecord.model_validate(base)


def test_approval_fingerprint_is_stable() -> None:
    assert _record().fingerprint() == _record().fingerprint()


def test_approval_fingerprint_tracks_the_verdict() -> None:
    assert _record().fingerprint() != _record(decision="deny").fingerprint()


def test_approval_fingerprint_tracks_the_approver() -> None:
    assert _record().fingerprint() != _record(approver="ops.sam").fingerprint()


def test_ticket_rejects_an_unknown_tier() -> None:
    with pytest.raises(ValidationError):
        Ticket(id="T-1", text="hello", account_tier="platinum")  # type: ignore[arg-type]


def test_models_reject_unexpected_fields() -> None:
    with pytest.raises(ValidationError):
        Ticket.model_validate({"id": "T-1", "text": "hello", "sneaky": True})
