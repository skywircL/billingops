from __future__ import annotations

from billingops.money import evaluate_duplicate_refund, extra_payments, remaining_refundable

INVOICE = {"id": "INV-1", "customer_id": "C-1", "amount_cents": 4900, "currency": "CNY"}
P1 = {"id": "PAY-1", "customer_id": "C-1", "invoice_id": "INV-1", "amount_cents": 4900, "currency": "CNY", "status": "succeeded", "paid_at": "2026-02-02T09:00:00", "settled": 1}
P2 = {**P1, "id": "PAY-2", "paid_at": "2026-02-02T09:00:41"}
P3 = {**P1, "id": "PAY-3", "paid_at": "2026-02-02T09:01:00"}


def verdict(**over):
    args = dict(customer_id="C-1", invoice=INVOICE, payments=[P1, P2], target_payment_id="PAY-2", refunds_by_payment={"PAY-2": []})
    args.update(over)
    return evaluate_duplicate_refund(**args)


def test_extra_payments_are_everything_after_the_first_succeeded_one() -> None:
    assert [p["id"] for p in extra_payments(INVOICE, [P2, P1, P3])] == ["PAY-2", "PAY-3"]
    pending = {**P2, "status": "pending"}
    assert extra_payments(INVOICE, [P1, pending]) == []


def test_full_refund_when_nothing_was_refunded() -> None:
    v = verdict()
    assert v.eligible and v.amount_cents == 4900 and v.payment_id == "PAY-2"


def test_partial_refund_leaves_the_remainder() -> None:
    refunds = [{"amount_cents": 1500, "status": "completed"}, {"amount_cents": 100, "status": "failed"}]
    v = verdict(refunds_by_payment={"PAY-2": refunds})
    assert v.eligible and v.amount_cents == 3400 and v.already_refunded_cents == 1500
    assert remaining_refundable(P2, refunds) == 3400


def test_pending_refunds_count_against_the_balance() -> None:
    v = verdict(refunds_by_payment={"PAY-2": [{"amount_cents": 4900, "status": "pending"}]})
    assert not v.eligible and v.reason == "already_fully_refunded"


def test_no_duplicate_when_one_payment() -> None:
    v = verdict(payments=[P1], target_payment_id=None)
    assert not v.eligible and v.reason == "no_duplicate_payment"


def test_target_must_be_an_extra_payment_not_the_original() -> None:
    v = verdict(target_payment_id="PAY-1")
    assert not v.eligible and v.reason == "target_is_not_an_extra_payment"


def test_ambiguous_without_target_when_two_extras() -> None:
    v = verdict(payments=[P1, P2, P3], target_payment_id=None)
    assert not v.eligible and v.reason == "ambiguous_duplicate_target" and v.extra_payment_ids == ("PAY-2", "PAY-3")


def test_unsettled_extra_payment_is_not_executable() -> None:
    v = verdict(payments=[P1, {**P2, "settled": 0}])
    assert not v.eligible and v.reason == "payment_not_settled"


def test_ownership_is_checked_on_invoice_and_payment() -> None:
    assert verdict(customer_id="C-9").reason == "invoice_not_owned_by_customer"
    assert verdict(payments=[P1, {**P2, "customer_id": "C-9"}]).reason == "payment_not_owned_by_customer"


def test_refunds_must_have_been_read_before_an_amount_is_computed() -> None:
    v = verdict(refunds_by_payment={})
    assert not v.eligible and v.reason == "refunds_not_read"


def test_invoice_ownership_is_derived_from_a_customer_scoped_payment_listing() -> None:
    from billingops.investigation.evidence import build_index

    idx = build_index([
        {"tool": "list_payments", "args": {"invoice_id": "INV-1"}, "ok": True, "data": [P1, P2]},
        {"tool": "list_refunds", "args": {"payment_id": "PAY-2"}, "ok": True, "data": []},
    ])
    assert idx.invoices["INV-1"]["customer_id"] == "C-1" and idx.invoices["INV-1"]["derived_from"] == "list_payments"
    v = evaluate_duplicate_refund(customer_id="C-1", invoice=idx.invoices["INV-1"], payments=idx.payments_by_invoice["INV-1"], target_payment_id="PAY-2", refunds_by_payment=idx.refunds_by_payment)
    assert v.eligible and v.amount_cents == 4900
    # A listing that came back empty establishes nothing.
    assert "INV-9" not in build_index([{"tool": "list_payments", "args": {"invoice_id": "INV-9"}, "ok": True, "data": []}]).invoices
