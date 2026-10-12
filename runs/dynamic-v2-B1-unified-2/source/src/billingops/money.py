"""Amount rules. Code, not the model, decides how much is refundable.

Duplicate charge, fixed on D1:

* An invoice is *duplicately charged* when more than one **succeeded** payment
  is attached to it. Pending and failed payments do not count.
* Payments are ordered by ``paid_at`` then ``id``. The first succeeded payment
  is the one that paid the invoice; every later succeeded payment is an
  *extra* payment. When a customer names a specific payment it must be one of
  the extra ones; when they do not, the extra payments are the candidates.
* The refundable amount of an extra payment is its amount minus every refund
  already recorded against it with status ``pending`` or ``completed``.
* An extra payment that is not settled is a duplicate but is **not
  executable** yet.
* Ownership: the invoice and the payment must both belong to the case's
  customer, which the server supplies. Nothing the model or the user says can
  change that.

Every number here is integer RMB cents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

REFUND_COUNTING_STATUSES = frozenset({"pending", "completed"})


def _sort_key(payment: Mapping[str, Any]) -> tuple[str, str]:
    return (str(payment.get("paid_at", "")), str(payment.get("id", "")))


def succeeded_payments(payments: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted((dict(p) for p in payments if p.get("status") == "succeeded"), key=_sort_key)


def extra_payments(invoice: Mapping[str, Any], payments: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Succeeded payments beyond the first one on this invoice, oldest first."""
    ordered = [p for p in succeeded_payments(payments) if p.get("invoice_id") == invoice.get("id")]
    return ordered[1:]


def refunded_total(refunds: Iterable[Mapping[str, Any]]) -> int:
    return sum(int(r.get("amount_cents", 0)) for r in refunds if r.get("status") in REFUND_COUNTING_STATUSES)


def remaining_refundable(payment: Mapping[str, Any], refunds: Iterable[Mapping[str, Any]]) -> int:
    return max(0, int(payment["amount_cents"]) - refunded_total(refunds))


@dataclass(frozen=True)
class RefundableResult:
    """The verdict of the amount rules on one candidate payment."""

    eligible: bool
    reason: str
    payment_id: str | None = None
    invoice_id: str | None = None
    amount_cents: int = 0
    currency: str = ""
    already_refunded_cents: int = 0
    extra_payment_ids: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "payment_id": self.payment_id,
            "invoice_id": self.invoice_id,
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "already_refunded_cents": self.already_refunded_cents,
            "extra_payment_ids": list(self.extra_payment_ids),
        }


def evaluate_duplicate_refund(
    *,
    customer_id: str,
    invoice: Mapping[str, Any] | None,
    payments: Sequence[Mapping[str, Any]],
    target_payment_id: str | None,
    refunds_by_payment: Mapping[str, Sequence[Mapping[str, Any]]],
) -> RefundableResult:
    """Decide whether a duplicate-charge refund is due, for which payment, and how much.

    Args:
        customer_id: the case's customer, from server context.
        invoice: the invoice record the investigation settled on, or None.
        payments: every payment read for that invoice.
        target_payment_id: the payment the investigation proposes to refund. If
            None and exactly one extra payment exists, that one is used.
        refunds_by_payment: refunds already read, keyed by payment id. A
            candidate whose refunds were never read is **not** eligible:
            the amount cannot be computed from evidence that was not gathered.
    """
    if invoice is None:
        return RefundableResult(False, "no_invoice_identified")
    if invoice.get("customer_id") != customer_id:
        return RefundableResult(False, "invoice_not_owned_by_customer", invoice_id=invoice.get("id"))

    extras = extra_payments(invoice, payments)
    extra_ids = tuple(str(p["id"]) for p in extras)
    if not extras:
        return RefundableResult(False, "no_duplicate_payment", invoice_id=invoice.get("id"))

    if target_payment_id is None:
        if len(extras) != 1:
            return RefundableResult(
                False, "ambiguous_duplicate_target", invoice_id=invoice.get("id"), extra_payment_ids=extra_ids
            )
        target = extras[0]
    else:
        matches = [p for p in extras if p["id"] == target_payment_id]
        if not matches:
            return RefundableResult(
                False,
                "target_is_not_an_extra_payment",
                payment_id=target_payment_id,
                invoice_id=invoice.get("id"),
                extra_payment_ids=extra_ids,
            )
        target = matches[0]

    if target.get("customer_id") != customer_id:
        return RefundableResult(False, "payment_not_owned_by_customer", payment_id=target["id"], invoice_id=invoice.get("id"))
    if not target.get("settled"):
        return RefundableResult(
            False, "payment_not_settled", payment_id=target["id"], invoice_id=invoice.get("id"), extra_payment_ids=extra_ids
        )
    if target["id"] not in refunds_by_payment:
        return RefundableResult(
            False, "refunds_not_read", payment_id=target["id"], invoice_id=invoice.get("id"), extra_payment_ids=extra_ids
        )

    already = refunded_total(refunds_by_payment[target["id"]])
    remaining = max(0, int(target["amount_cents"]) - already)
    if remaining <= 0:
        return RefundableResult(
            False,
            "already_fully_refunded",
            payment_id=target["id"],
            invoice_id=invoice.get("id"),
            currency=str(target.get("currency", "")),
            already_refunded_cents=already,
            extra_payment_ids=extra_ids,
        )
    return RefundableResult(
        True,
        "refundable",
        payment_id=target["id"],
        invoice_id=invoice.get("id"),
        amount_cents=remaining,
        currency=str(target.get("currency", "")),
        already_refunded_cents=already,
        extra_payment_ids=extra_ids,
    )


@dataclass(frozen=True)
class InvoiceRefundResult:
    """V2 allocation from a complete invoice envelope, never a model amount.

    ``items`` are individually executable full-payment refunds. The caller must
    separately establish customer authorization, batch confirmation and operator
    approval. Legacy ``evaluate_duplicate_refund`` deliberately keeps its old
    behavior for persisted cases and explicitly selected legacy execution.
    """

    eligible: bool
    reason: str
    invoice_id: str | None = None
    currency: str = ""
    amount_cents: int = 0
    already_refunded_cents: int = 0
    overcharge_cents: int = 0
    available_cents: int = 0
    items: tuple[RefundableResult, ...] = field(default_factory=tuple)
    retained_payment_id: str | None = None
    successful_payment_ids: tuple[str, ...] = field(default_factory=tuple)
    missing_refund_payment_ids: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible, "reason": self.reason,
            "invoice_id": self.invoice_id, "currency": self.currency,
            "amount_cents": self.amount_cents,
            "already_refunded_cents": self.already_refunded_cents,
            "overcharge_cents": self.overcharge_cents,
            "available_cents": self.available_cents,
            "items": [item.as_dict() for item in self.items],
            "retained_payment_id": self.retained_payment_id,
            "successful_payment_ids": list(self.successful_payment_ids),
            "missing_refund_payment_ids": list(self.missing_refund_payment_ids),
        }


def evaluate_invoice_refund(
    *,
    customer_id: str,
    invoice: Mapping[str, Any] | None,
    payments: Sequence[Mapping[str, Any]],
    refunds_by_payment: Mapping[str, Sequence[Mapping[str, Any]]],
    requested_payment_ids: Sequence[str] | None = None,
    reserved_by_payment: Mapping[str, int] | None = None,
) -> InvoiceRefundResult:
    """Compute the V2 whole-invoice budget and a deterministic full-item list.

    Every successful payment needs a complete refund listing, including a
    successful empty listing for the payment retained to cover the invoice.
    A complete customer-scoped refund query may be expanded into this mapping
    by EvidenceIndex; an absent key never means an empty result. Reservations
    contain only unresolved amounts NOT already represented by refund rows.
    """
    if invoice is None:
        return InvoiceRefundResult(False, "no_invoice_identified")
    iid, currency = invoice.get("id"), str(invoice.get("currency", ""))
    base: dict[str, Any] = {"invoice_id": iid, "currency": currency}

    def reject(reason: str, **extra: Any) -> InvoiceRefundResult:
        return InvoiceRefundResult(False, reason, **{**base, **extra})

    if invoice.get("customer_id") != customer_id:
        return reject("invoice_not_owned_by_customer")
    due = invoice.get("amount_cents")
    if due is None:
        return reject("invoice_amount_not_read")
    if type(due) is not int or due <= 0:
        return reject("invalid_invoice_amount")
    if currency != "CNY":
        return reject("unsupported_currency")
    seen: set[str] = set()
    for row in payments:
        pid = str(row.get("id", ""))
        if not pid or pid in seen:
            return reject("invalid_or_duplicate_payment_record")
        seen.add(pid)
        if row.get("invoice_id") != iid:
            return reject("payment_invoice_mismatch")
        if row.get("customer_id") != customer_id:
            return reject("payment_not_owned_by_customer")
        if row.get("currency") != currency:
            return reject("payment_currency_mismatch")
        if type(row.get("amount_cents")) is not int or row["amount_cents"] <= 0:
            return reject("invalid_payment_amount")
    successful = succeeded_payments(payments)
    pids = tuple(str(row["id"]) for row in successful)
    base["successful_payment_ids"] = pids
    if any(row["amount_cents"] != due for row in successful):
        return reject("unsupported_non_full_payment")
    if len(successful) < 2:
        return reject("no_duplicate_payment")
    if any(not row.get("settled") for row in successful):
        return reject("payment_not_settled")
    overcharge = sum(row["amount_cents"] for row in successful) - due
    base["overcharge_cents"] = overcharge
    missing = tuple(pid for pid in pids if pid not in refunds_by_payment)
    if missing:
        return reject("invoice_refunds_not_read", missing_refund_payment_ids=missing)
    counted: dict[str, int] = {}
    seen_refunds: set[str] = set()
    for row in successful:
        pid, subtotal = str(row["id"]), 0
        for refund in refunds_by_payment[pid]:
            if refund.get("payment_id", pid) != pid:
                return reject("refund_payment_mismatch")
            if refund.get("customer_id", customer_id) != customer_id:
                return reject("refund_not_owned_by_customer")
            if refund.get("currency", currency) != currency:
                return reject("refund_currency_mismatch")
            rid = refund.get("id")
            if rid and rid in seen_refunds:
                return reject("duplicate_refund_record")
            if rid:
                seen_refunds.add(str(rid))
            amount = refund.get("amount_cents")
            if type(amount) is not int or amount <= 0:
                return reject("invalid_refund_amount")
            if refund.get("status") not in {"pending", "completed", "failed"}:
                return reject("unknown_refund_status")
            if refund["status"] in REFUND_COUNTING_STATUSES:
                subtotal += amount
        if subtotal > row["amount_cents"]:
            return reject("historical_refunds_exceed_payment")
        if subtotal not in (0, row["amount_cents"]):
            return reject("unsupported_historical_partial_refund")
        counted[pid] = subtotal
    refunded = sum(counted.values())
    base["already_refunded_cents"] = refunded
    if refunded > overcharge:
        return reject("historical_refunds_exceed_invoice_overcharge")
    reservations = dict(reserved_by_payment or {})
    if any(pid not in pids or type(amount) is not int or amount < 0 for pid, amount in reservations.items()):
        return reject("invalid_unresolved_reservation")
    balances = {row["id"]: row["amount_cents"] - counted[row["id"]] - reservations.get(row["id"], 0) for row in successful}
    if any(balance < 0 for balance in balances.values()):
        return reject("refund_and_reservation_exceed_payment")
    available = overcharge - refunded - sum(reservations.values())
    if available < 0:
        return reject("refund_and_reservation_exceed_invoice_overcharge")
    base["available_cents"] = available
    if available == 0:
        return reject("refund_in_doubt" if sum(reservations.values()) else "already_fully_refunded")
    if any(balance not in (0, due) for balance in balances.values()):
        return reject("unsupported_partial_refund_balance")
    by_id = {row["id"]: row for row in successful}
    if requested_payment_ids is not None:
        requested = list(requested_payment_ids)
        if not requested or len(set(requested)) != len(requested):
            return reject("invalid_explicit_payment_scope")
        if any(pid not in by_id for pid in requested):
            return reject("requested_payment_not_succeeded_on_invoice")
        if any(balances[pid] != due for pid in requested):
            return reject("requested_payment_balance_unavailable")
        targets = [row for row in successful if row["id"] in requested]
        if sum(row["amount_cents"] for row in targets) > available:
            return reject("requested_scope_exceeds_invoice_budget")
        retained = next((row for row in successful if row["id"] not in requested and balances[row["id"]] == due), None)
    else:
        retained = next((row for row in successful if balances[row["id"]] == due), None)
        if retained is None:
            return reject("no_payment_covers_invoice")
        targets = [row for row in successful if row["id"] != retained["id"] and balances[row["id"]] == due]
        if sum(row["amount_cents"] for row in targets) != available:
            return reject("invoice_allocation_does_not_cover_available_amount")
    if any(not row.get("settled") for row in targets):
        return reject("payment_not_settled")
    if retained is None:
        return reject("no_payment_covers_invoice")
    ids = tuple(str(row["id"]) for row in targets)
    items = tuple(RefundableResult(True, "refundable", payment_id=row["id"], invoice_id=str(iid),
                                  amount_cents=row["amount_cents"], currency=currency,
                                  already_refunded_cents=counted[row["id"]], extra_payment_ids=ids)
                  for row in targets)
    return InvoiceRefundResult(True, "refundable", **base,
                               amount_cents=sum(item.amount_cents for item in items),
                               items=items, retained_payment_id=str(retained["id"]))
