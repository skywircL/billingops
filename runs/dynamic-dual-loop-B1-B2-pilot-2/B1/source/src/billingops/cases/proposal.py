"""Dispute key, proposal fingerprint, and the action identity execution hangs off.

The dispute key is ``customer + duplicate_charge + extra payment id``, built
here from validated Billing records. The model never supplies it. The
proposal fingerprint covers customer, payment target, amount, currency and
the versions of the critical evidence, so approval binds to all of them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from langgraph_production.models import digest, utc_now

from ..money import RefundableResult


def dispute_key(customer_id: str, payment_id: str) -> str:
    return f"{customer_id}:duplicate_charge:{payment_id}"


def build_proposal(
    *,
    case_id: str,
    customer_id: str,
    verdict: RefundableResult,
    evidence_versions: Mapping[str, str],
    version: int,
    target_resolution: Mapping[str, Any] | None = None,
    clock: Any = utc_now,
) -> dict[str, Any]:
    if not verdict.eligible or not verdict.payment_id:
        raise ValueError("a proposal needs an eligible verdict with a payment target")
    key = dispute_key(customer_id, verdict.payment_id)
    fingerprint = digest(
        {
            "proposal_version": version,
            "customer_id": customer_id,
            "payment_id": verdict.payment_id,
            "invoice_id": verdict.invoice_id,
            "amount_cents": verdict.amount_cents,
            "currency": verdict.currency,
            "dispute_key": key,
            "evidence_versions": dict(evidence_versions),
            "target_resolution": dict(target_resolution or {}),
        },
        length=32,
    )
    action_id = digest({"case_id": case_id, "fingerprint": fingerprint}, length=16)
    when: datetime = clock()
    return {
        "version": version,
        "case_id": case_id,
        "customer_id": customer_id,
        "payment_id": verdict.payment_id,
        "invoice_id": verdict.invoice_id,
        "amount_cents": verdict.amount_cents,
        "currency": verdict.currency,
        "already_refunded_cents": verdict.already_refunded_cents,
        "dispute_key": key,
        "evidence_versions": dict(evidence_versions),
        "target_resolution": dict(target_resolution or {}),
        "fingerprint": fingerprint,
        "action_id": action_id,
        "created_at": when.isoformat(),
    }


def same_conditions(proposal: Mapping[str, Any], verdict: RefundableResult) -> bool:
    """True when a fresh verdict still supports the approved proposal exactly."""
    return bool(
        verdict.eligible
        and verdict.payment_id == proposal["payment_id"]
        and verdict.amount_cents == proposal["amount_cents"]
        and verdict.currency == proposal["currency"]
    )


def build_batch_proposal(
    *,
    case_id: str,
    customer_id: str,
    verdicts: Sequence[RefundableResult],
    evidence_versions: Mapping[str, str],
    version: int,
    target_resolution: Mapping[str, Any] | None = None,
    clock: Any = utc_now,
) -> dict[str, Any]:
    """Bind approval and every action key to the complete invoice batch.

    Each child is the existing proposal contract with a ``batch_context``
    evidence digest. Its stable action_id is used unchanged on restart and
    verification. Top-level payment_id is for legacy display only: execution
    MUST iterate items and may never send the top-level total to that payment.
    """
    items = list(verdicts)
    if not items or any(not item.eligible or not item.payment_id or not item.invoice_id
                        or type(item.amount_cents) is not int or item.amount_cents <= 0 for item in items):
        raise ValueError("a batch needs eligible positive payment verdicts")
    if len({item.payment_id for item in items}) != len(items):
        raise ValueError("batch contains duplicate payment targets")
    if len({item.invoice_id for item in items}) != 1 or len({item.currency for item in items}) != 1:
        raise ValueError("a batch must belong to one invoice and currency")
    invoice_id, currency = items[0].invoice_id, items[0].currency
    total = sum(item.amount_cents for item in items)
    resolution = dict(target_resolution or {})
    frozen_when = clock()
    context = {
        "kind": "batch_refund", "case_id": case_id, "customer_id": customer_id,
        "proposal_version": version, "invoice_id": invoice_id,
        "currency": currency, "amount_cents": total,
        "items": [{"payment_id": item.payment_id, "amount_cents": item.amount_cents,
                   "currency": item.currency, "dispute_key": dispute_key(customer_id, str(item.payment_id))}
                  for item in items],
        "target_resolution": resolution, "evidence_versions": dict(evidence_versions),
    }
    versions = {**evidence_versions, "batch_context": digest(context, length=32)}
    children = [build_proposal(case_id=case_id, customer_id=customer_id,
                              verdict=item, evidence_versions=versions, version=version,
                              target_resolution=resolution, clock=lambda: frozen_when)
                for item in items]
    fingerprint = digest({**context, "children": [{"fingerprint": child["fingerprint"],
                                                   "action_id": child["action_id"]} for child in children]}, length=32)
    return {
        "kind": "batch_refund", "version": version, "case_id": case_id,
        "customer_id": customer_id, "invoice_id": invoice_id,
        "payment_id": items[0].payment_id, "amount_cents": total,
        "currency": currency, "already_refunded_cents": sum(item.already_refunded_cents for item in items),
        "dispute_key": f"{customer_id}:duplicate_invoice:{invoice_id}",
        "items": children, "evidence_versions": dict(evidence_versions),
        "target_resolution": resolution, "fingerprint": fingerprint,
        "action_id": digest({"case_id": case_id, "fingerprint": fingerprint}, length=16),
        "created_at": frozen_when.isoformat(),
    }


def same_batch_conditions(proposal: Mapping[str, Any], verdicts: Sequence[RefundableResult]) -> bool:
    """Before execution, the complete fresh allocation must equal approved items.

    Partial execution recovery compares against each already recorded child
    action separately; it must not re-propose only the remaining children.
    """
    if proposal.get("kind") != "batch_refund" or not verdicts or any(not v.eligible for v in verdicts):
        return False
    def fresh_key(item: RefundableResult) -> tuple[Any, ...]:
        return (item.invoice_id, item.payment_id, item.amount_cents, item.currency)
    def proposed_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
        return (item.get("invoice_id"), item.get("payment_id"), item.get("amount_cents"), item.get("currency"))
    old = list(proposal.get("items") or [])
    if len({v.payment_id for v in verdicts}) != len(verdicts) or len({v.get("payment_id") for v in old}) != len(old):
        return False
    return (sorted(map(fresh_key, verdicts)) == sorted(map(proposed_key, old))
            and sum(v.amount_cents for v in verdicts) == proposal.get("amount_cents")
            and all(v.invoice_id == proposal.get("invoice_id") and v.currency == proposal.get("currency") for v in verdicts))
