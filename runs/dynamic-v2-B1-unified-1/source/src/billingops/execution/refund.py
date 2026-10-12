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

Only ``done`` and ``confirmed_not_executed`` are final for a single item.
Batch roots can also finish with ``partial_failure`` or retain a monotonic
customer stop while claimed items remain verifiable. No resume can turn an
existing item claim into a second POST or clear a batch customer stop.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import json
import re
import sqlite3
import threading
from typing import Any, Callable, Mapping

from langgraph_production.effects import EffectStore, idempotency_key
from langgraph_production.faults import CrashHook, no_crash
from langgraph_production.models import utc_now

from ..billing_client import BillingClient, BillingUnavailable
from ..cases.store import CaseStore
from ..config import MAX_AUTO_VERIFICATIONS

FINAL_STATUSES = frozenset({"done", "confirmed_not_executed", "partial_failure"})


@dataclass
class ExecutionDeps:
    store: CaseStore
    effects: EffectStore
    billing: BillingClient
    clock: Callable[[], datetime] = utc_now
    crash_hook: CrashHook = no_crash
    # Linearize customer command persistence with a new item claim, never with
    # network I/O. A command accepted after a claim cannot undo that send.
    command_lock: Any = field(default_factory=threading.RLock)


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
    if proposal.get("kind") == "batch_refund":
        return execute_batch(deps, case, proposal, actor=actor)
    return _execute_one(deps, case, proposal, actor=actor)


def _execute_one(deps: ExecutionDeps, case: Mapping[str, Any], proposal: Mapping[str, Any], *, actor: str,
                 manage_case: bool = True, verification_attempts: int = MAX_AUTO_VERIFICATIONS,
                 before_new_claim: Callable[[], bool] | None = None) -> dict[str, Any]:
    case_id = str(case["id"])
    key = action_key(case_id, proposal)
    if manage_case:
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
    with deps.command_lock:
        if deps.effects.get(key) is None and before_new_claim is not None and not before_new_claim():
            return {"status": "not_started", "send_blocked": True}
        stored, was_taken = deps.effects.claim(key, claim)

    if was_taken:
        if stored.get("status") in FINAL_STATUSES:
            if manage_case:
                _reconcile_case_with_ledger(deps, case_id, key, stored, actor=actor)
            return stored
        # A previous attempt claimed this key. Whether its POST left the
        # process is unknown, so verification is the only move.
        deps.store.append_event(case_id, "recovery_verify_first", actor, {"key": key, "ledger_status": stored.get("status")})
        return verify(deps, case_id, key, proposal, attempts=verification_attempts, actor=actor, manage_case=manage_case)

    if deps.store.get_case(case_id)["state"] != "executing":
        deps.store.set_state(case_id, "executing", actor=actor)
    # One send per action. This row is the proof that a send was attempted,
    # written before the request leaves.
    deps.effects.record_execution(key)
    deps.store.append_event(case_id, "refund_sending", actor, {"key": key, "payment_id": proposal["payment_id"], "amount_cents": proposal["amount_cents"], "proposal_version": proposal["version"]})
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
    return verify(deps, case_id, key, proposal, attempts=verification_attempts, actor=actor, manage_case=manage_case)


def verify(deps: ExecutionDeps, case_id: str, key: str, proposal: Mapping[str, Any], *, attempts: int, actor: str = "system", manage_case: bool = True) -> dict[str, Any]:
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
            if manage_case:
                _close_after_success(deps, case_id, record or {}, actor=actor)
            return final
        if outcome["result"] == "terminal_rejection":
            final = {**row, "status": "confirmed_not_executed", "verify_attempts": attempt_no, "verifications": verifications, "receipt": record, "verified_at": deps.clock().isoformat()}
            deps.effects.finalize(key, final)
            deps.store.append_event(case_id, "refund_confirmed_not_executed", actor, {"key": key, "rejection_code": outcome.get("rejection_code")})
            if manage_case:
                _handoff_after_rejection(deps, case_id, record or {}, actor=actor)
            return final
        row = {**row, "status": "in_doubt", "verify_attempts": attempt_no, "verifications": verifications}
        deps.effects.finalize(key, row)

    deps.store.append_event(case_id, "refund_in_doubt", actor, {"key": key, "verify_attempts": row.get("verify_attempts")})
    case = deps.store.get_case(case_id)
    if manage_case and case and case["state"] in ("executing", "verifying"):
        deps.store.set_state(case_id, "uncertain", actor=actor)
    return row


def _batch_items(case: Mapping[str, Any], proposal: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate the approved envelope; no item or amount is invented here."""
    raw = proposal.get("items")
    if not isinstance(raw, list) or not raw or proposal.get("kind") != "batch_refund":
        raise ValueError("a batch refund needs approved item proposals")
    if proposal.get("case_id") != case["id"] or proposal.get("customer_id") != case["customer_id"]:
        raise ValueError("batch identity differs from the authenticated case")
    items = [dict(item) for item in raw]
    for item in items:
        if any(item.get(k) != proposal.get(k) for k in ("version", "case_id", "customer_id", "invoice_id", "currency")):
            raise ValueError("batch items must share case, customer, invoice, currency and approval version")
        if type(item.get("amount_cents")) is not int or item["amount_cents"] <= 0:
            raise ValueError("batch item amounts must be positive integer cents")
        if any(not item.get(k) for k in ("payment_id", "dispute_key", "action_id", "fingerprint")):
            raise ValueError("batch item action identity is incomplete")
    for field in ("payment_id", "dispute_key", "action_id"):
        if len({item[field] for item in items}) != len(items):
            raise ValueError(f"batch repeats {field}")
    if type(proposal.get("amount_cents")) is not int or proposal["amount_cents"] != sum(item["amount_cents"] for item in items):
        raise ValueError("batch total differs from its approved items")
    if not proposal.get("action_id") or not proposal.get("fingerprint") or proposal["action_id"] in {item["action_id"] for item in items}:
        raise ValueError("batch needs a separate complete action identity")
    return items


def _batch_precheck(deps: ExecutionDeps, proposal: Mapping[str, Any], unsent: list[dict[str, Any]]) -> dict[str, Any]:
    """Re-read the invoice envelope before a new send, including prior own writes.

    Billing must also enforce this envelope atomically. These reads make stale
    approvals stop early; they do not close the check/write concurrency window.
    """
    customer, invoice_id = proposal["customer_id"], proposal["invoice_id"]
    try:
        invoice = deps.billing.get_invoice(customer, invoice_id)
        payments = deps.billing.invoice_payments(customer, invoice_id)
        refunds = {p["id"]: deps.billing.payment_refunds(customer, p["id"]) for p in payments}
    except BillingUnavailable as exc:
        return {"ok": False, "reason": "billing_unavailable", "detail": str(exc)}
    if invoice.get("id") != invoice_id or invoice.get("customer_id") != customer or invoice.get("currency") != proposal["currency"]:
        return {"ok": False, "reason": "invoice_conditions_changed"}
    if any(p.get("customer_id") != customer or p.get("invoice_id") != invoice_id or p.get("currency") != proposal["currency"] for p in payments):
        return {"ok": False, "reason": "payment_scope_changed"}
    by_id = {p["id"]: p for p in payments}
    counted = {pid: sum(int(r["amount_cents"]) for r in rs if r.get("status") in {"pending", "completed"}) for pid, rs in refunds.items()}
    cap = max(0, sum(int(p["amount_cents"]) for p in payments if p.get("status") == "succeeded") - int(invoice["amount_cents"]) - sum(counted.values()))
    requested = sum(item["amount_cents"] for item in unsent)
    result = {"ok": True, "reason": "same_approved_remaining_scope", "invoice_remaining_cents": cap,
              "approved_remaining_cents": requested, "payment_remaining_cents": {}}
    for item in unsent:
        p = by_id.get(item["payment_id"])
        if not p or p.get("status") != "succeeded" or not p.get("settled"):
            return {**result, "ok": False, "reason": "payment_no_longer_eligible", "payment_id": item["payment_id"]}
        remaining = int(p["amount_cents"]) - counted[item["payment_id"]]
        result["payment_remaining_cents"][item["payment_id"]] = remaining
        if item["amount_cents"] > remaining:
            return {**result, "ok": False, "reason": "payment_balance_changed", "payment_id": item["payment_id"]}
    if requested > cap:
        return {**result, "ok": False, "reason": "invoice_balance_changed"}
    return result


def batch_followup_kind(text: str) -> str:
    """Only a narrowly recognized status question is safe to ignore during a write.

    There is no model call in the write window. Other messages conservatively
    pause the remaining scope for review, rather than supplying new consent.
    """
    value = re.sub(r"[?？!！。，,.；;]+", "", text.strip().lower())
    value = re.sub(r"\s+", " ", value).strip()
    if re.search(r"撤回|取消|不要退|别退|先不退|先别退|暂不退|停止退款|暂停退款|\b(?:cancel|withdraw|stop|hold|wait)\b|\b(?:don't|do not)\s+(?:refund|proceed)", value):
        return "withdraw"
    if value in {
        "进度", "退款进度", "现在什么进度", "现在处理到哪一步了", "处理到哪一步了", "现在处理到哪了", "处理到哪了",
        "退款处理到哪了", "退款到哪了", "退款什么时候到账", "什么时候到账", "到账了吗", "退了吗", "审批了吗",
        "status", "status update", "refund status", "what is the status", "what's the status", "what is my refund status",
        "any update", "any updates", "how is it going", "how is this going", "how long will it take", "when will it arrive",
        "when will my refund arrive", "has the refund been issued", "has it been approved", "please give me a status update",
    }:
        return "progress"
    if re.search(r"INV-|PAY-|\d|报错|搞错|更正|更改|改成|换成|另一|账单|付款|\b(?:instead|wrong|change|invoice|payment)\b", text, re.I):
        return "new_info"
    return "unclear"


def _batch_commands(deps: ExecutionDeps, case_id: str) -> list[dict[str, Any]]:
    # The commands table, not the task queue or an event appended afterwards,
    # is the durable input. A crash after record_command must not lose a stop.
    with sqlite3.connect(deps.store.path, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT rowid AS command_seq, * FROM commands WHERE case_id = ? ORDER BY rowid", (case_id,)).fetchall()
    return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]


def _approval_command_boundary(deps: ExecutionDeps, case_id: str, version: int) -> int:
    commands = _batch_commands(deps, case_id)
    events = [e for e in deps.store.events(case_id) if e["kind"] == "approval_recorded" and e["payload"].get("decision") == "approve"
              and e["payload"].get("proposal_version") == version]
    if events and events[-1]["payload"].get("command_id"):
        command_id = events[-1]["payload"]["command_id"]
        authorized = [c["command_seq"] for c in commands if c["id"] == command_id and c["kind"] == "decision"
                      and c["payload"].get("decision") == "approve" and c["payload"].get("proposal_version") == version]
        # A later duplicate/invalid approve command cannot consume a withdrawal.
        return authorized[0] if len(authorized) == 1 else 0
    # Direct approved-execution callers may only have an approval event.
    # Without either proof we ignore no earlier customer messages.
    cutoff = events[-1]["at"] if events else ""
    return max((c["command_seq"] for c in commands if cutoff and c["created_at"] <= cutoff), default=0)


def observe_batch_commands(deps: ExecutionDeps, case_id: str, key: str, *, actor: str = "system") -> dict[str, Any]:
    """Persist a monotonic stop; even a later 'yes' or resume cannot clear it."""
    with deps.command_lock:
        root = deps.effects.get(key)
        assert root is not None and root.get("tool") == "refund_batch"
        observed = set(root.get("observed_customer_commands", []))
        for command in _batch_commands(deps, case_id):
            if command["kind"] != "message" or command["command_seq"] <= root.get("command_after_rowid", 0) or command["id"] in observed:
                continue
            kind = batch_followup_kind(str(command["payload"].get("text", "")))
            observed.add(command["id"])
            observation = {"command_id": command["id"], "kind": kind, "proposal_version": root["proposal_version"], "batch_key": key}
            if kind != "progress" and not root.get("stop_request"):
                root["stop_request"] = {**observation, "at": command["created_at"], "by": command["actor"],
                                        "reason": "customer_withdrawal" if kind == "withdraw" else "customer_scope_requires_review"}
            root["observed_customer_commands"] = sorted(observed)
            deps.effects.finalize(key, root)
            deps.store.append_event(case_id, "batch_followup_classified", actor, observation)
        return root


def reconcile_batch_commands(deps: ExecutionDeps, case_id: str, key: str, *, actor: str = "system") -> dict[str, Any]:
    """Immediate acknowledgement only; claimed items are verified by the executor."""
    root = observe_batch_commands(deps, case_id, key, actor=actor)
    return _finish_batch(deps, case_id, key, actor=actor) if root.get("stop_request") else root


def _batch_message(proposal: Mapping[str, Any], rows: list[Mapping[str, Any] | None], stop: Mapping[str, Any] | None = None) -> str:
    parts = []
    if stop:
        parts.append("Your withdrawal has been recorded." if stop["kind"] == "withdraw" else "Your new message requires a scope review.")
        parts.append("No remaining unclaimed items in this proposal will be sent. Already claimed items can only be verified; their execution cannot be cancelled here.")
    if all(row and row.get("status") == "done" for row in rows):
        parts.append(f"Refunds totaling {proposal['amount_cents'] / 100:.2f} yuan have been issued for invoice {proposal['invoice_id']}.")
    for item, row in zip(proposal["items"], rows):
        prefix = f"Payment {item['payment_id']}, {item['amount_cents'] / 100:.2f} yuan: "
        if row is None:
            parts.append(prefix + "not sent.")
        elif row.get("status") == "done":
            receipt = row.get("receipt") or {}
            parts.append(prefix + f"refund issued (refund {(receipt.get('refund') or {}).get('id', '?')}).")
        elif row.get("status") == "confirmed_not_executed":
            parts.append(prefix + f"not issued; confirmed rejection {(row.get('receipt') or {}).get('rejection_code', 'unknown')}.")
        else:
            parts.append(prefix + "the execution outcome is still being verified; whether it was refunded is not yet confirmed.")
    return " ".join(parts)


def _finish_batch(deps: ExecutionDeps, case_id: str, key: str, *, actor: str,
                  blocked: Mapping[str, Any] | None = None) -> dict[str, Any]:
    with deps.command_lock:
        return _finish_batch_locked(deps, case_id, key, actor=actor, blocked=blocked)


def _finish_batch_locked(deps: ExecutionDeps, case_id: str, key: str, *, actor: str,
                         blocked: Mapping[str, Any] | None = None) -> dict[str, Any]:
    root = deps.effects.get(key)
    assert root is not None
    proposal = root["proposal"]
    rows = [deps.effects.get(item_key) for item_key in root["item_keys"]]
    done = sum(bool(row and row.get("status") == "done") for row in rows)
    rejected = any(row and row.get("status") == "confirmed_not_executed" for row in rows)
    stop = root.get("stop_request")
    unresolved = any(row and row.get("status") not in FINAL_STATUSES for row in rows)
    if stop and done != len(rows):
        status = "stopping" if unresolved else "stopped"
    elif blocked and blocked.get("reason") == "billing_unavailable":
        status = "blocked"
    elif rejected or blocked:
        status = "partial_failure" if done else "confirmed_not_executed"
    elif done == len(rows):
        status = "done"
    elif any(row and row.get("status") not in FINAL_STATUSES for row in rows):
        status = "in_doubt"
    else:
        status = "executing"
    result = {**root, "status": status, "completed_items": done,
              "item_statuses": [row.get("status") if row else "not_started" for row in rows]}
    if blocked:
        result["blocked_reason"] = dict(blocked)
    else:
        result.pop("blocked_reason", None)
    deps.effects.finalize(key, result)
    case = deps.store.get_case(case_id)
    message = _batch_message(proposal, rows, stop)
    if status == "done":
        if case and case["state"] != "closed":
            deps.store.set_state(case_id, "closed", actor=actor, final_message=message)
    elif status in {"partial_failure", "confirmed_not_executed", "stopped"}:
        escalation = {"reason": (stop or {}).get("reason") if status == "stopped" else "batch_conditions_changed" if blocked else "batch_terminal_rejection",
                      "completed_items": done, "total_items": len(rows), "by": actor,
                      "at": deps.clock().isoformat(), "original_action": "refund", "batch_key": key}
        if case and case["state"] != "escalated":
            deps.store.set_state(case_id, "escalated", actor=actor, escalation=escalation, final_message=message)
            deps.store.append_event(case_id, "escalated", actor, escalation)
        elif case:
            deps.store.update_case(case_id, escalation=escalation, final_message=message)
    elif status == "blocked":
        if case and case["state"] != "escalated":
            deps.store.set_state(case_id, "error", actor=actor, error="batch_precheck_unavailable", final_message=message)
        elif case:
            deps.store.update_case(case_id, final_message=message)
    elif status in {"in_doubt", "stopping"}:
        if case and case["state"] != "escalated":
            deps.store.set_state(case_id, "uncertain", actor=actor, final_message=message)
        elif case:
            deps.store.update_case(case_id, final_message=message)
    return result


def execute_batch(deps: ExecutionDeps, case: Mapping[str, Any], proposal: Mapping[str, Any], *, actor: str = "system",
                  verification_attempts: int = MAX_AUTO_VERIFICATIONS) -> dict[str, Any]:
    """Resume the approved batch; known items never send twice, unknown items stop it."""
    items = _batch_items(case, proposal)
    case_id, key = str(case["id"]), action_key(str(case["id"]), proposal)
    previous_key = case.get("action_key")
    if previous_key and previous_key != key and deps.effects.get(previous_key):
        raise ValueError("a case with an execution claim cannot replace its approved batch")
    claim = {"tool": "refund_batch", "status": "executing", "case_id": case_id,
             "proposal_version": proposal["version"], "action_id": proposal["action_id"],
             "proposal": deepcopy(dict(proposal)), "amount_cents": proposal["amount_cents"],
             "item_keys": [action_key(case_id, item) for item in items],
             "claimed_at": deps.clock().isoformat(), "completed_items": 0,
             "command_after_rowid": _approval_command_boundary(deps, case_id, proposal["version"]),
             "observed_customer_commands": []}
    with deps.command_lock:
        stored, existed = deps.effects.claim(key, claim)
    if stored.get("tool") != "refund_batch" or stored.get("proposal") != proposal:
        raise ValueError("batch proposal differs from its durable approved execution claim")
    deps.store.update_case(case_id, action_key=key)
    stored = observe_batch_commands(deps, case_id, key, actor=actor)
    if stored.get("status") in FINAL_STATUSES:
        return _finish_batch(deps, case_id, key, actor=actor, blocked=stored.get("blocked_reason"))
    if existed:
        deps.store.append_event(case_id, "batch_recovery_started", actor, {"key": key, "proposal_version": proposal["version"]})
    for item, item_key in zip(items, stored["item_keys"]):
        current = observe_batch_commands(deps, case_id, key, actor=actor)
        row = deps.effects.get(item_key)
        if row and row.get("status") == "done":
            continue
        if row and row.get("status") == "confirmed_not_executed":
            return _finish_batch(deps, case_id, key, actor=actor)
        if row is None:
            if current.get("stop_request"):
                continue
            unsent = [candidate for candidate, candidate_key in zip(items, stored["item_keys"]) if deps.effects.get(candidate_key) is None]
            check = _batch_precheck(deps, proposal, unsent)
            deps.store.append_event(case_id, "batch_item_precheck", actor, {"batch_key": key, "item_key": item_key, "proposal_version": proposal["version"], **check})
            if not check["ok"]:
                return _finish_batch(deps, case_id, key, actor=actor, blocked=check)
        row = _execute_one(deps, case, item, actor=actor, manage_case=False, verification_attempts=verification_attempts,
                           before_new_claim=lambda: not observe_batch_commands(deps, case_id, key, actor=actor).get("stop_request"))
        result = _finish_batch(deps, case_id, key, actor=actor)
        deps.crash_hook("batch:after_item")
        if row.get("status") != "done":
            return result
    return _finish_batch(deps, case_id, key, actor=actor)


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
    if row.get("tool") == "refund_batch":
        items = []
        for item, item_key in zip(row["proposal"]["items"], row["item_keys"]):
            items.append(ledger_view(effects, item_key) or {"key": item_key, "status": "not_started", "sends_recorded": 0,
                         "verify_attempts": 0, "payment_id": item["payment_id"], "amount_cents": item["amount_cents"],
                         "proposal_version": item["version"], "receipt": None})
        return {"key": key, "kind": "batch_refund", "status": row.get("status"), "items": items,
                "amount_cents": row["amount_cents"], "proposal_version": row["proposal_version"],
                "completed_items": sum(item["status"] == "done" for item in items),
                "sends_recorded": sum(item["sends_recorded"] for item in items),
                "verify_attempts": sum(item["verify_attempts"] for item in items),
                "blocked_reason": row.get("blocked_reason"), "stop_request": row.get("stop_request")}
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
