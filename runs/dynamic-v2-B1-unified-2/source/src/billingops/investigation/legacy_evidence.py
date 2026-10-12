"""Frozen B0 conclusion contract retained for archived replay compatibility."""
from __future__ import annotations
from typing import Any
from .state import CaseState
from .evidence import EvidenceIndex
from ..money import RefundableResult

def legacy_evidence_bundle(state: CaseState, idx: EvidenceIndex, final_type: str, verdict: RefundableResult | None, draft: dict[str, Any], proposal: dict[str, Any] | None) -> dict[str, Any]:
    """The unified evidence format both groups feed to the conclusion model.

    No transcript, no model summary: only business records and the code verdict.
    """
    invoice_id = verdict.invoice_id if verdict else draft.get("target_invoice_id")
    payment_id = verdict.payment_id if verdict else draft.get("target_payment_id")
    refunds: list[dict[str, Any]] = []
    if payment_id and payment_id in idx.refunds_by_payment:
        refunds = idx.refunds_by_payment[payment_id]
    elif idx.customer_refunds is not None:
        refunds = idx.customer_refunds
    return {
        "customer_id": state["customer_id"],
        "extracted": state.get("extracted"),
        "clarifications": [{"question": c["question"], "answer": c["answer"]} for c in state.get("clarifications", [])],
        "conclusion_type": final_type,
        "verdict": verdict.as_dict() if verdict else None,
        "proposal": {k: proposal[k] for k in ("version", "payment_id", "invoice_id", "amount_cents", "currency")} if proposal else None,
        # Account-wide conclusions need the actual records too; a null target
        # must not erase successful reads or be interpreted as a failed lookup.
        "invoice_list": idx.invoice_list,
        "payments_by_invoice": idx.payments_by_invoice,
        "siblings_by_invoice": idx.siblings_by_invoice,
        "record_availability": "only records explicitly present below were read; absent fields do not mean not_found",
        "invoice": idx.invoices.get(str(invoice_id)) if invoice_id else None,
        "payments": idx.payments_by_invoice.get(str(invoice_id)) if invoice_id else None,
        "period_invoices": idx.siblings_by_invoice.get(str(invoice_id)) if invoice_id else None,
        "refunds": refunds,
        "subscription_history": idx.subscription_history,
        "policies": idx.policies or None,
    }
