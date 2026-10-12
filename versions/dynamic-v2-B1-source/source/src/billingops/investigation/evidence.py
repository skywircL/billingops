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
    read_observations: list[dict[str, Any]] = field(default_factory=list)
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
    payment_refund_reads: dict[str, int] = {}
    customer_refund_read = 0
    for item in evidence:
        idx.read_count += 1
        # Keep each actual request as well as the convenient record indexes.
        # A failed read, an empty scoped listing and an unqueried record are
        # different evidence; skipping errors used to erase that distinction
        # when the conclusion model received only the success indexes.
        idx.read_observations.append({
            key: item[key]
            for key in ("seq", "tool", "args", "ok", "data", "error", "phase", "read_at")
            if key in item
        })
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
            payment_id = str(args.get("payment_id"))
            idx.refunds_by_payment[payment_id] = data
            payment_refund_reads[payment_id] = idx.read_count
        elif tool == "list_customer_refunds" and isinstance(data, list):
            idx.customer_refunds = data
            customer_refund_read = idx.read_count
        elif tool == "get_subscription_history" and isinstance(data, dict):
            idx.subscription_history = data
        elif tool == "get_policy" and isinstance(data, dict):
            idx.policies[str(data.get("name"))] = data
    if idx.customer_refunds is not None:
        # A complete account listing supplies the same refund information as
        # per-payment reads, including empty results for payments discovered
        # later. Use the most recent successful observation for each payment;
        # never overwrite a newer payment read with a stale account snapshot.
        grouped: dict[str, list[dict[str, Any]]] = {}
        for refund in idx.customer_refunds:
            grouped.setdefault(str(refund["payment_id"]), []).append(refund)
        payment_ids = set(idx.payments) | set(idx.refunds_by_payment) | set(grouped)
        for payment_id in payment_ids:
            if payment_refund_reads.get(payment_id, 0) < customer_refund_read:
                idx.refunds_by_payment[payment_id] = grouped.get(payment_id, [])
    return idx
