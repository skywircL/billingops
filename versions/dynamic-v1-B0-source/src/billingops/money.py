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
