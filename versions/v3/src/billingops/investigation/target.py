"""Establish which charge the customer means before money eligibility is checked.

A model's proposed id is never itself evidence of customer intent. With no
user identifier, every listed invoice must have been inspected and exactly
one extra payment must exist. Refundability is a separate decision.
"""
from __future__ import annotations

import re
from typing import Any

from .evidence import EvidenceIndex

INVOICE_RE = re.compile(r"\bINV-[A-Za-z0-9-]+\b")
PAYMENT_RE = re.compile(r"\bPAY-[A-Za-z0-9-]+\b")


def user_ids(text: str, answers: list[str]) -> tuple[list[str], list[str]]:
    # A later answer containing ids is a correction, not an additional target.
    for utterance in [*reversed(answers), text]:
        invoices = sorted(set(INVOICE_RE.findall(utterance)))
        payments = sorted(set(PAYMENT_RE.findall(utterance)))
        if invoices or payments:
            return invoices, payments
    return [], []


def extra_payments(payments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((p for p in payments if p["status"] == "succeeded"), key=lambda p: (p["paid_at"], p["id"]))[1:]


def resolve_target(state: dict[str, Any], idx: EvidenceIndex, invoice_id: str | None,
                   payment_id: str | None) -> dict[str, Any]:
    answers = [c["answer"] for c in state.get("clarifications", [])] + list(state.get("extra_info", []))
    invoices, payments = user_ids(state["text"], answers)
    def result(ok: bool, reason: str, scope: str = "explicit", ids: list[str] | None = None) -> dict[str, Any]:
        return {"ok": ok, "reason": reason, "scope": scope, "invoice_ids": ids or [], "payment_id": payment_id}

    if len(payments) > 1 or len(invoices) > 1:
        return result(False, "ambiguous_customer_target")
    if payments:
        if payment_id != payments[0] or payment_id not in idx.payments:
            return result(False, "customer_payment_mismatch")
        if invoices and invoices[0] != invoice_id:
            # A typo in an invoice can be corrected by a valid named payment,
            # but only after the typo has actually been checked and not found.
            not_found = any(e.get("tool") == "get_invoice" and e.get("args", {}).get("invoice_id") == invoices[0]
                            and not e.get("ok") and str(e.get("error", "")).startswith("not_found:")
                            for e in state.get("evidence", []))
            if not not_found:
                return result(False, "conflicting_customer_identifiers")
        return result(True, "customer_named_payment", ids=[str(invoice_id)])
    if invoices:
        if invoice_id != invoices[0]:
            return result(False, "customer_invoice_mismatch")
        candidate_ids = invoices
        scope = "invoice"
    else:
        if idx.invoice_list is None:
            return result(False, "target_scope_not_read", "account")
        candidate_ids = sorted(i["id"] for i in idx.invoice_list)
        scope = "account"
    if any(i not in idx.payments_by_invoice for i in candidate_ids):
        return result(False, "target_scope_incomplete", scope, candidate_ids)
    extras = [p for i in candidate_ids for p in extra_payments(idx.payments_by_invoice[i])]
    if len(extras) != 1:
        return result(False, "ambiguous_customer_target" if extras else "no_unique_duplicate_target", scope, candidate_ids)
    if extras[0]["id"] != payment_id or extras[0]["invoice_id"] != invoice_id:
        return result(False, "customer_target_mismatch", scope, candidate_ids)
    return result(True, "unique_duplicate_target", scope, candidate_ids)
