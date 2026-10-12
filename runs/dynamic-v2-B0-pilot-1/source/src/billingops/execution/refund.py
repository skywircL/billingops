"""Send the approved refund once, then find out what happened.

The effect ledger (upstream ``SqliteEffectStore``) is the arbiter. The claim is
taken and the send is recorded in ``effect_executions`` *before* the POST
leaves the process, so a crash anywhere in the send window leaves a row that
says "a send may have happened". Recovery reads that row and verifies; it
never sends again.

Ledger statuses used here:

    sending                   claimed, POST in flight or interrupted
    in_doubt                  POST answered or failed, not yet independently verified
    done                      verified: matching successful receipt
    confirmed_not_executed    verified: persisted terminal rejection, nothing moved

Only ``done`` and ``confirmed_not_executed`` are final. Everything else keeps
the claim, and neither a human nor a resume can turn it into a second POST.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from langgraph_production.effects import EffectStore, idempotency_key
from langgraph_production.faults import CrashHook, no_crash
from langgraph_production.models import utc_now

from ..billing_client import BillingClient, BillingUnavailable
from ..cases.store import CaseStore
from ..config import MAX_AUTO_VERIFICATIONS

FINAL_STATUSES = frozenset({"done", "confirmed_not_executed"})


@dataclass
class ExecutionDeps:
    store: CaseStore
    effects: EffectStore
    billing: BillingClient
    clock: Callable[[], datetime] = utc_now
    crash_hook: CrashHook = no_crash


def action_key(case_id: str, proposal: Mapping[str, Any]) -> str:
    return idempotency_key(case_id, str(proposal["action_id"]))


def _matches(record: Mapping[str, Any], proposal: Mapping[str, Any]) -> bool:
    return (
        record.get("customer_id") == proposal["customer_id"]
        and record.get("payment_id") == proposal["payment_id"]
        and int(record.get("amount_cents", -1)) == int(proposal["amount_cents"])
        and record.get("currency") == proposal["currency"]
    )


def execute_approved(deps: ExecutionDeps, case: Mapping[str, Any], proposal: Mapping[str, Any], *, actor: str = "system") -> dict[str, Any]:
    """Run the write-then-verify pipeline for an approved proposal. Idempotent by construction."""
    case_id = str(case["id"])
    key = action_key(case_id, proposal)
    deps.store.update_case(case_id, action_key=key)
    claim = {
        "status": "sending",
        "action_id": proposal["action_id"],
        "tool": "refund",
        "claimed_at": deps.clock().isoformat(),
        "receipt": None,
        "case_id": case_id,
        "proposal_version": proposal["version"],
        "customer_id": proposal["customer_id"],
        "payment_id": proposal["payment_id"],
        "amount_cents": proposal["amount_cents"],
        "currency": proposal["currency"],
        "dispute_key": proposal["dispute_key"],
        "verify_attempts": 0,
        "verifications": [],
        "send_result": None,
    }
    stored, was_taken = deps.effects.claim(key, claim)

    if was_taken:
        if stored.get("status") in FINAL_STATUSES:
            _reconcile_case_with_ledger(deps, case_id, key, stored, actor=actor)
            return stored
        # A previous attempt claimed this key. Whether its POST left the
        # process is unknown, so verification is the only move.
        deps.store.append_event(case_id, "recovery_verify_first", actor, {"key": key, "ledger_status": stored.get("status")})
        return verify(deps, case_id, key, proposal, attempts=MAX_AUTO_VERIFICATIONS, actor=actor)

    if deps.store.get_case(case_id)["state"] != "executing":
        deps.store.set_state(case_id, "executing", actor=actor)
    # One send per action. This row is the proof that a send was attempted,
    # written before the request leaves.
    deps.effects.record_execution(key)
    deps.store.append_event(case_id, "refund_sending", actor, {"key": key, "payment_id": proposal["payment_id"], "amount_cents": proposal["amount_cents"]})
    deps.crash_hook("refund:before_send")
    send_result: dict[str, Any]
    try:
        response = deps.billing.post_refund(
            {
                "idempotency_key": key,
                "dispute_key": proposal["dispute_key"],
                "customer_id": proposal["customer_id"],
                "payment_id": proposal["payment_id"],
                "amount_cents": int(proposal["amount_cents"]),
                "currency": proposal["currency"],
                "reason": f"duplicate charge, case {case_id}, proposal v{proposal['version']}",
            }
        )
        send_result = {"status": response.status, "body": response.body, "at": deps.clock().isoformat()}
    except Exception as exc:  # timeout, connection reset, anything: the outcome is unknown
        send_result = {"status": None, "error": f"{type(exc).__name__}: {exc}", "at": deps.clock().isoformat()}
    deps.crash_hook("refund:after_send")
    deps.effects.finalize(key, {**claim, "status": "in_doubt", "send_result": send_result})
    deps.store.append_event(case_id, "refund_sent", actor, {"key": key, "http_status": send_result.get("status"), "error": send_result.get("error")})
    deps.store.set_state(case_id, "verifying", actor=actor)
    return verify(deps, case_id, key, proposal, attempts=MAX_AUTO_VERIFICATIONS, actor=actor)


def verify(deps: ExecutionDeps, case_id: str, key: str, proposal: Mapping[str, Any], *, attempts: int, actor: str = "system") -> dict[str, Any]:
    """Query Billing by key up to ``attempts`` times and settle the ledger if the answer is final.

    Returns the ledger row as it stands afterwards. A 404, a 503, a timeout or a
    mismatching record all leave the row in doubt: none of them proves the
    original request will not commit later.
    """
    row = deps.effects.get(key)
    assert row is not None
    if row.get("status") in FINAL_STATUSES:
        return row
    for _ in range(max(0, attempts)):
        row = deps.effects.get(key) or row
        attempt_no = int(row.get("verify_attempts", 0)) + 1
        outcome: dict[str, Any] = {"attempt": attempt_no, "at": deps.clock().isoformat()}
        try:
            record = deps.billing.refund_by_key(key)
        except BillingUnavailable as exc:
            outcome["result"] = "unavailable"
            outcome["detail"] = str(exc)
            record = None
        else:
            if record is None:
                outcome["result"] = "not_found"
            elif record.get("status") == "succeeded" and _matches(record, proposal):
                outcome["result"] = "matched_success"
            elif record.get("status") == "rejected":
                outcome["result"] = "terminal_rejection"
                outcome["rejection_code"] = record.get("rejection_code")
            else:
                outcome["result"] = "mismatch"
                outcome["detail"] = {k: record.get(k) for k in ("status", "customer_id", "payment_id", "amount_cents", "currency")}
        verifications = list(row.get("verifications", [])) + [outcome]
        deps.store.append_event(case_id, "verification", actor, {"key": key, **outcome})

        if outcome["result"] == "matched_success":
            final = {**row, "status": "done", "verify_attempts": attempt_no, "verifications": verifications, "receipt": record, "verified_at": deps.clock().isoformat()}
            deps.effects.finalize(key, final)
            deps.store.append_event(case_id, "refund_verified", actor, {"key": key, "refund_id": (record or {}).get("refund", {}).get("id")})
            _close_after_success(deps, case_id, record or {}, actor=actor)
            return final
        if outcome["result"] == "terminal_rejection":
            final = {**row, "status": "confirmed_not_executed", "verify_attempts": attempt_no, "verifications": verifications, "receipt": record, "verified_at": deps.clock().isoformat()}
            deps.effects.finalize(key, final)
            deps.store.append_event(case_id, "refund_confirmed_not_executed", actor, {"key": key, "rejection_code": outcome.get("rejection_code")})
            _handoff_after_rejection(deps, case_id, record or {}, actor=actor)
            return final
        row = {**row, "status": "in_doubt", "verify_attempts": attempt_no, "verifications": verifications}
        deps.effects.finalize(key, row)

    deps.store.append_event(case_id, "refund_in_doubt", actor, {"key": key, "verify_attempts": row.get("verify_attempts")})
    case = deps.store.get_case(case_id)
    if case and case["state"] in ("executing", "verifying"):
        deps.store.set_state(case_id, "uncertain", actor=actor)
    return row


def _close_after_success(deps: ExecutionDeps, case_id: str, record: Mapping[str, Any], *, actor: str) -> None:
    refund = record.get("refund") or {}
    message = (
        f"Your refund of {int(record.get('amount_cents', 0)) / 100:.2f} yuan on payment {record.get('payment_id')} "
        f"has been issued (refund {refund.get('id', '?')})."
    )
    deps.store.set_state(case_id, "closed", actor=actor, final_message=message)


def _handoff_after_rejection(deps: ExecutionDeps, case_id: str, record: Mapping[str, Any], *, actor: str) -> None:
    escalation = {
        "reason": "terminal_rejection",
        "rejection_code": record.get("rejection_code"),
        "detail": record.get("detail"),
        "by": actor,
        "at": deps.clock().isoformat(),
        "original_action": "refund",
    }
    deps.store.set_state(case_id, "escalated", actor=actor, escalation=escalation)
    deps.store.append_event(case_id, "escalated", actor, escalation)


def _reconcile_case_with_ledger(deps: ExecutionDeps, case_id: str, key: str, row: Mapping[str, Any], *, actor: str) -> None:
    case = deps.store.get_case(case_id)
    if case is None:
        return
    if row.get("status") == "done" and case["state"] != "closed":
        _close_after_success(deps, case_id, row.get("receipt") or {}, actor=actor)
    elif row.get("status") == "confirmed_not_executed" and case["state"] != "escalated":
        _handoff_after_rejection(deps, case_id, row.get("receipt") or {}, actor=actor)


def ledger_view(effects: EffectStore, key: str | None) -> dict[str, Any] | None:
    """What the UI shows next to the business state: the execution outcome, unhidden."""
    if not key:
        return None
    row = effects.get(key)
    if row is None:
        return None
    return {
        "key": key,
        "status": row.get("status"),
        "sends_recorded": effects.execution_count(key),
        "verify_attempts": row.get("verify_attempts", 0),
        "last_verification": (row.get("verifications") or [None])[-1],
        "send_result": row.get("send_result"),
        "receipt": row.get("receipt"),
        "amount_cents": row.get("amount_cents"),
        "payment_id": row.get("payment_id"),
        "proposal_version": row.get("proposal_version"),
    }
