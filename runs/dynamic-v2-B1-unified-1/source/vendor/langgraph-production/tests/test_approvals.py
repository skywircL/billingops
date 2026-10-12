from __future__ import annotations

from pathlib import Path

import pytest

from langgraph_production.approvals import (
    AuthorizationError,
    InMemoryApprovalLedger,
    SqliteApprovalLedger,
    authorize,
    mint_token,
    parse_decision,
)
from langgraph_production.models import ApprovalRecord

from samples import FIXED_TIME

BASE = {
    "thread_id": "th-1",
    "ticket_id": "T-1",
    "kind": "approval",
    "action_id": "act-1",
    "state_digest": "sd-1",
}


@pytest.fixture(params=["memory", "sqlite"])
def ledger(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "memory":
        yield InMemoryApprovalLedger()
    else:
        instance = SqliteApprovalLedger(tmp_path / "approvals.sqlite3")
        yield instance
        instance.close()


def record(token: str, **overrides: object) -> ApprovalRecord:
    payload: dict[str, object] = {
        "token": token,
        "ticket_id": "T-1",
        "action_id": "act-1",
        "kind": "approval",
        "approver": "ops.dana",
        "decision": "approve",
        "note": "",
        "decided_at": FIXED_TIME,
        "state_digest": "sd-1",
    }
    payload.update(overrides)
    return ApprovalRecord.model_validate(payload)


def test_the_token_is_deterministic() -> None:
    assert mint_token(**BASE) == mint_token(**BASE)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["thread_id", "ticket_id", "action_id", "state_digest"])
def test_the_token_changes_with_every_binding(field: str) -> None:
    changed = dict(BASE)
    changed[field] = "something else"
    assert mint_token(**changed) != mint_token(**BASE)  # type: ignore[arg-type]


def test_a_handoff_token_differs_from_an_approval_token() -> None:
    handoff = dict(BASE, kind="handoff", action_id=None)
    assert mint_token(**handoff) != mint_token(**BASE)  # type: ignore[arg-type]


def test_a_matching_approval_is_honoured_once(ledger) -> None:
    token = mint_token(**BASE)  # type: ignore[arg-type]
    assert authorize(record=record(token), expected_token=token, ledger=ledger) is True
    assert ledger.count() == 1


def test_the_identical_approval_replayed_is_still_honoured(ledger) -> None:
    """A resumed node presents the same decision again. That is not reuse."""
    token = mint_token(**BASE)  # type: ignore[arg-type]
    authorize(record=record(token), expected_token=token, ledger=ledger)
    assert authorize(record=record(token), expected_token=token, ledger=ledger) is False


def test_a_token_from_another_gate_is_refused(ledger) -> None:
    mine = mint_token(**BASE)  # type: ignore[arg-type]
    theirs = mint_token(**dict(BASE, action_id="act-2"))  # type: ignore[arg-type]
    with pytest.raises(AuthorizationError) as caught:
        authorize(record=record(theirs), expected_token=mine, ledger=ledger)
    assert caught.value.reason == "token_mismatch"


def test_a_consumed_token_cannot_authorise_a_different_decision(ledger) -> None:
    token = mint_token(**BASE)  # type: ignore[arg-type]
    authorize(record=record(token), expected_token=token, ledger=ledger)
    with pytest.raises(AuthorizationError) as caught:
        authorize(
            record=record(token, approver="ops.mallory", note="approving again"),
            expected_token=token,
            ledger=ledger,
        )
    assert caught.value.reason == "token_replayed"


def test_a_denial_also_consumes_the_token(ledger) -> None:
    token = mint_token(**BASE)  # type: ignore[arg-type]
    authorize(record=record(token, decision="deny"), expected_token=token, ledger=ledger)
    with pytest.raises(AuthorizationError):
        authorize(record=record(token, decision="approve"), expected_token=token, ledger=ledger)


def test_a_note_is_part_of_the_decision(ledger) -> None:
    """Same verdict, different words. The ledger would have kept the first note."""
    token = mint_token(**BASE)  # type: ignore[arg-type]
    authorize(record=record(token, note="checked the invoice"), expected_token=token, ledger=ledger)
    with pytest.raises(AuthorizationError) as caught:
        authorize(
            record=record(token, note="approved without checking"),
            expected_token=token,
            ledger=ledger,
        )
    assert caught.value.reason == "token_replayed"
    stored = ledger.lookup(token)
    assert stored is not None and stored.note == "checked the invoice"


def test_the_ledger_records_first_use_and_replay(ledger) -> None:
    """The audit trail the authorize docstring promises."""
    token = mint_token(**BASE)  # type: ignore[arg-type]
    assert ledger.presentations(token) == ()
    authorize(record=record(token), expected_token=token, ledger=ledger)
    authorize(record=record(token), expected_token=token, ledger=ledger)
    assert ledger.presentations(token) == (True, False)


def test_a_refused_approval_is_not_recorded_as_honoured(ledger) -> None:
    token = mint_token(**BASE)  # type: ignore[arg-type]
    authorize(record=record(token), expected_token=token, ledger=ledger)
    with pytest.raises(AuthorizationError):
        authorize(record=record(token, approver="ops.mallory"), expected_token=token, ledger=ledger)
    assert ledger.presentations(token) == (True,)


def test_the_ledger_keeps_the_record_for_audit(ledger) -> None:
    token = mint_token(**BASE)  # type: ignore[arg-type]
    authorize(record=record(token, note="spoke to finance"), expected_token=token, ledger=ledger)
    stored = ledger.lookup(token)
    assert stored is not None
    assert stored.note == "spoke to finance"
    assert stored.approver == "ops.dana"


def test_an_unknown_token_looks_up_as_missing(ledger) -> None:
    assert ledger.lookup("nope") is None


def test_a_sqlite_ledger_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "approvals.sqlite3"
    token = mint_token(**BASE)  # type: ignore[arg-type]
    first = SqliteApprovalLedger(path)
    authorize(record=record(token), expected_token=token, ledger=first)
    first.close()

    second = SqliteApprovalLedger(path)
    try:
        assert authorize(record=record(token), expected_token=token, ledger=second) is False
        assert second.count() == 1
    finally:
        second.close()


def test_parse_decision_reads_a_mapping() -> None:
    assert parse_decision({"decision": "approve"})["decision"] == "approve"


def test_parse_decision_reads_json_text() -> None:
    assert parse_decision('{"decision": "deny"}')["decision"] == "deny"


def test_parse_decision_reads_a_pydantic_model() -> None:
    assert parse_decision(record("tok"))["approver"] == "ops.dana"


def test_parse_decision_refuses_anything_else() -> None:
    with pytest.raises(TypeError):
        parse_decision(42)
