"""Turn the flat evidence log into the index code reasons over."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from langgraph_production.models import digest


@dataclass
class EvidenceIndex:
    invoices: dict[str, dict[str, Any]] = field(default_factory=dict)
    payments: dict[str, dict[str, Any]] = field(default_factory=dict)
    payments_by_invoice: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    refunds_by_payment: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    siblings_by_invoice: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    invoice_list: list[dict[str, Any]] | None = None
    customer_refunds: list[dict[str, Any]] | None = None
    subscription_history: dict[str, Any] | None = None
    policies: dict[str, dict[str, Any]] = field(default_factory=dict)
    read_count: int = 0

    def critical_versions(self, invoice_id: str | None, payment_id: str | None) -> dict[str, str]:
        """Digests of the records a refund proposal depends on.

        Baked into the proposal fingerprint, so an approval given over one
        state of the invoice, its payments and its refunds does not carry
        over to another.
        """
        out: dict[str, str] = {}
        if invoice_id and invoice_id in self.invoices:
            out["invoice"] = digest(self.invoices[invoice_id], length=16)
        if invoice_id and invoice_id in self.payments_by_invoice:
            out["payments"] = digest(self.payments_by_invoice[invoice_id], length=16)
        if payment_id and payment_id in self.refunds_by_payment:
            out["refunds"] = digest(self.refunds_by_payment[payment_id], length=16)
        return out


def build_index(evidence: Iterable[Mapping[str, Any]]) -> EvidenceIndex:
    idx = EvidenceIndex()
    for item in evidence:
        idx.read_count += 1
        if not item.get("ok"):
            continue
        tool, args, data = item["tool"], item.get("args", {}), item.get("data")
        if tool == "get_invoice" and isinstance(data, dict):
            idx.invoices[data["id"]] = data
        elif tool == "list_invoices" and isinstance(data, list):
            idx.invoice_list = data
            for inv in data:
                idx.invoices.setdefault(inv["id"], inv)
        elif tool == "list_period_invoices" and isinstance(data, list):
            idx.siblings_by_invoice[str(args.get("invoice_id"))] = data
        elif tool == "list_payments" and isinstance(data, list):
            invoice_id = str(args.get("invoice_id"))
            idx.payments_by_invoice[invoice_id] = data
            for p in data:
                idx.payments[p["id"]] = p
            # The listing is customer-scoped and every row names its customer, so the
            # invoice's ownership is established even when the invoice itself was not read.
            owners = {p.get("customer_id") for p in data}
            if len(owners) == 1 and invoice_id not in idx.invoices:
                idx.invoices[invoice_id] = {"id": invoice_id, "customer_id": owners.pop(), "derived_from": "list_payments"}
        elif tool == "get_payment" and isinstance(data, dict):
            idx.payments[data["id"]] = data
        elif tool == "list_refunds" and isinstance(data, list):
            idx.refunds_by_payment[str(args.get("payment_id"))] = data
        elif tool == "list_customer_refunds" and isinstance(data, list):
            idx.customer_refunds = data
        elif tool == "get_subscription_history" and isinstance(data, dict):
            idx.subscription_history = data
        elif tool == "get_policy" and isinstance(data, dict):
            idx.policies[str(data.get("name"))] = data
    return idx
