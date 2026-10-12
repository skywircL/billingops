"""Dispute key, proposal fingerprint, and the action identity execution hangs off.

The dispute key is ``customer + duplicate_charge + extra payment id``, built
here from validated Billing records. The model never supplies it. The
proposal fingerprint covers customer, payment target, amount, currency and
the versions of the critical evidence, so approval binds to all of them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

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
