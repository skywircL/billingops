"""Rule-based investigation steps.

Used twice: as the whole of the fixed workflow (group A), and as the policy of
the scripted stand-in model. One implementation, so a test of the fixed
workflow and a fake-model test of the dynamic protocol exercise the same
decisions and differ only in who is taking them.

A rule produces one :class:`Step` at a time from an :class:`EvidenceView`.
Reads go through the same budget as the model's reads.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..tools import CLARIFY_TOOL

INVOICE_RE = re.compile(r"\bINV-[A-Za-z0-9-]+\b")
PAYMENT_RE = re.compile(r"\bPAY-[A-Za-z0-9-]+\b")

CLARIFY_QUESTION = (
    "Could you share the invoice number (INV-...) or payment id (PAY-...) of the charge you believe was taken twice?"
)
UNRESOLVED_QUESTION = (
    "We could not find the invoice or payment you mentioned on your account. Could you double-check the invoice number (INV-...) or payment id (PAY-...)?"
)
DEMAND_WORDS = ("refund the difference", "want my money", "pay me back", "compensat", "退差价", "赔", "charge back", "chargeback")


class EvidenceView(Protocol):
    text: str
    extracted: dict[str, Any]

    def result_for(self, tool: str, **args: Any) -> dict[str, Any] | None: ...
    def results(self, tool: str) -> list[tuple[dict[str, Any], dict[str, Any]]]: ...
    def answers(self) -> list[str]: ...
    def budget_exhausted(self) -> bool: ...


@dataclass(frozen=True)
class Read:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Ask:
    question: str


@dataclass(frozen=True)
class End:
    conclusion_type: str
    summary: str
    target_invoice_id: str | None = None
    target_payment_id: str | None = None


Step = Read | Ask | End


def _ok(r: dict[str, Any] | None) -> Any:
    return r.get("data") if r and r.get("ok") else None


def known_ids(view: EvidenceView) -> tuple[list[str], list[str]]:
    ex = view.extracted
    ids = set(ex.get("invoice_ids", [])) | set(ex.get("payment_ids", []))
    for answer in view.answers():
        ids |= set(INVOICE_RE.findall(answer)) | set(PAYMENT_RE.findall(answer))
    return sorted(i for i in ids if i.startswith("INV-")), sorted(i for i in ids if i.startswith("PAY-"))


def category_of(extracted: dict[str, Any]) -> str:
    if extracted.get("mentions_subscription_price") and not extracted.get("mentions_duplicate"):
        return "subscription"
    if extracted.get("mentions_refund_status") and not extracted.get("mentions_duplicate"):
        return "refund_status"
    return "duplicate"


def next_step(view: EvidenceView, *, ask_when_missing: bool = True, batch: bool = False) -> Step:
    """Decide the next investigation step from what has been gathered so far.

    ``batch`` lets the fixed workflow expand relationships more eagerly (it
    checks payments on several candidate invoices when the customer named
    none); the budget is still charged one read per call.
    """
    if view.budget_exhausted():
        return End("insufficient_information", "The read budget was exhausted before the facts could be established.")
    category = category_of(view.extracted)
    if category == "subscription":
        return _subscription(view)
    if category == "refund_status":
        return _refund_status(view)
    return _duplicate(view, ask_when_missing=ask_when_missing, batch=batch)


def _duplicate(view: EvidenceView, *, ask_when_missing: bool, batch: bool) -> Step:
    invoice_ids, payment_ids = known_ids(view)
    invoice: dict[str, Any] | None = None
    for iid in invoice_ids:
        r = view.result_for("get_invoice", invoice_id=iid)
        if r is None:
            return Read("get_invoice", {"invoice_id": iid})
        if _ok(r):
            invoice = _ok(r)
            break
    if invoice is None:
        for pid in payment_ids:
            r = view.result_for("get_payment", payment_id=pid)
            if r is None:
                return Read("get_payment", {"payment_id": pid})
            payment = _ok(r)
            if payment:
                r2 = view.result_for("get_invoice", invoice_id=payment["invoice_id"])
                if r2 is None:
                    return Read("get_invoice", {"invoice_id": payment["invoice_id"]})
                if _ok(r2):
                    invoice = _ok(r2)
                    break
    if invoice is None:
        asked = len(view.results(CLARIFY_TOOL))
        listed = view.result_for("list_invoices")
        if invoice_ids or payment_ids:
            # The customer named records that are not theirs or do not exist.
            # Ask once for a correction; never go looking for a different charge to refund.
            if ask_when_missing and asked == 0:
                return Ask(UNRESOLVED_QUESTION)
            return End("insufficient_information", "The identifiers given do not match any record of this customer.")
        if ask_when_missing and asked == 0 and listed is None:
            return Ask(CLARIFY_QUESTION)
        if listed is None:
            return Read("list_invoices", {})
        candidates = list(_ok(listed) or [])
        amounts = set(view.extracted.get("amounts_cents", []))
        matched = sorted((i for i in candidates if i["amount_cents"] in amounts), key=lambda i: i["issued_at"], reverse=True)
        others = sorted((i for i in candidates if i["amount_cents"] not in amounts), key=lambda i: i["issued_at"], reverse=True)
        ranked = matched + others
        limit = 4 if batch else 3
        for inv in ranked[:limit]:
            r = view.result_for("list_payments", invoice_id=inv["id"])
            if r is None:
                return Read("list_payments", {"invoice_id": inv["id"]})
            pays = _ok(r) or []
            if sum(1 for p in pays if p["status"] == "succeeded") >= 2:
                invoice = inv
                break
        if invoice is None:
            return End("insufficient_information", "The customer could not identify the charge and no recent invoice shows more than one payment.")

    r = view.result_for("list_payments", invoice_id=invoice["id"])
    if r is None:
        return Read("list_payments", {"invoice_id": invoice["id"]})
    payments = _ok(r) or []
    succeeded = sorted((p for p in payments if p["status"] == "succeeded"), key=lambda p: (p["paid_at"], p["id"]))
    extras = succeeded[1:]
    if not extras:
        rp = view.result_for("list_period_invoices", invoice_id=invoice["id"])
        if rp is None:
            return Read("list_period_invoices", {"invoice_id": invoice["id"]})
        siblings = _ok(rp) or []
        summary = f"Invoice {invoice['id']} has one succeeded payment. " + (
            f"{len(siblings)} other invoice(s) share the billing period; they are separate charges."
            if siblings
            else "No other invoice shares the period."
        )
        return End("no_duplicate_found", summary, target_invoice_id=invoice["id"])
    target = next((p for p in extras if p["id"] in payment_ids), extras[-1])
    if view.result_for("list_refunds", payment_id=target["id"]) is None:
        return Read("list_refunds", {"payment_id": target["id"]})
    return End(
        "duplicate_charge_refund",
        f"Invoice {invoice['id']} was paid {len(succeeded)} times; {target['id']} is the extra payment.",
        target_invoice_id=invoice["id"],
        target_payment_id=target["id"],
    )


def _refund_status(view: EvidenceView) -> Step:
    _, payment_ids = known_ids(view)
    if payment_ids:
        for pid in payment_ids:
            if view.result_for("list_refunds", payment_id=pid) is None:
                return Read("list_refunds", {"payment_id": pid})
        return End("explain_refund_status", "Refund records for the named payment were read.", target_payment_id=payment_ids[0])
    if view.result_for("list_customer_refunds") is None:
        return Read("list_customer_refunds", {})
    return End("explain_refund_status", "All refunds for this customer were read.")


def _subscription(view: EvidenceView) -> Step:
    if view.result_for("get_subscription_history") is None:
        return Read("get_subscription_history", {})
    if view.result_for("get_policy", name="pricing") is None:
        return Read("get_policy", {"name": "pricing", "version": None})
    lowered = view.text.lower()
    if any(w in lowered for w in DEMAND_WORDS):
        return End("escalate", "The customer demands a price-difference refund, which this desk cannot grant.")
    return End("explain_subscription", "Subscription history and pricing policy were read.")


def facts_from(view: EvidenceView) -> list[str]:
    out: list[str] = []
    for name in ("get_invoice", "get_payment", "list_payments", "list_refunds", "list_period_invoices", "list_invoices", "list_customer_refunds", "get_subscription_history", "get_policy"):
        for args, r in view.results(name):
            if r.get("ok"):
                out.append(f"{name}({json.dumps(args, sort_keys=True)})")
    return out[:20]
