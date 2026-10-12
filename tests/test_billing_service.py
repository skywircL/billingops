"""The Billing transaction, tested over HTTP against the real service."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest

from billingops.billing_client import BillingClient, BillingUnavailable
from billingops.testing import World

REQ = {
    "idempotency_key": "case-a:0000000000000001",
    "dispute_key": "C-1001:duplicate_charge:PAY-88213",
    "customer_id": "C-1001",
    "payment_id": "PAY-88213",
    "amount_cents": 4900,
    "currency": "CNY",
}


def post(billing: BillingClient, **over):
    return billing.post_refund({**REQ, **over})


def test_success_then_same_key_replays_without_second_refund(billing: BillingClient) -> None:
    first = post(billing)
    assert first.status == 201 and first.body["status"] == "succeeded"
    again = post(billing)
    assert again.status == 200 and again.body["refund"]["id"] == first.body["refund"]["id"]
    assert len(billing.payment_refunds("C-1001", "PAY-88213")) == 1


def test_same_key_different_parameters_is_refused(billing: BillingClient) -> None:
    post(billing)
    conflict = post(billing, amount_cents=100)
    assert conflict.status == 409
    assert len(billing.payment_refunds("C-1001", "PAY-88213")) == 1


def test_same_dispute_under_new_key_is_a_persisted_terminal_rejection(billing: BillingClient) -> None:
    post(billing)
    second = post(billing, idempotency_key="case-b:0000000000000002")
    assert second.status == 422 and second.body["rejection_code"] == "dispute_already_refunded"
    # The rejection is on record under its own key and replays as itself.
    stored = billing.refund_by_key("case-b:0000000000000002")
    assert stored["status"] == "rejected" and stored["rejection_code"] == "dispute_already_refunded"
    assert len(billing.payment_refunds("C-1001", "PAY-88213")) == 1


@pytest.mark.parametrize("amount", [4901, 1_000_000, 0, -5])
def test_amount_boundaries(billing: BillingClient, amount: int) -> None:
    response = post(billing, amount_cents=amount)
    assert response.status == 422
    assert response.body["rejection_code"] in ("exceeds_refundable_balance", "invalid_amount")
    assert billing.payment_refunds("C-1001", "PAY-88213") == []


def test_cumulative_refunds_respect_existing_partial_refund(billing: BillingClient) -> None:
    from billingops.testing import duplicate_world

    billing.load_world(duplicate_world(prior_refund_cents=1500))
    too_much = post(billing, amount_cents=3401)
    assert too_much.status == 422 and too_much.body["rejection_code"] == "exceeds_refundable_balance"
    exact = post(billing, idempotency_key="case-c:0000000000000003", amount_cents=3400)
    assert exact.status == 201
    total = sum(r["amount_cents"] for r in billing.payment_refunds("C-1001", "PAY-88213"))
    assert total == 4900


def test_cross_customer_payment_is_rejected(billing: BillingClient) -> None:
    response = post(billing, customer_id="C-2002", dispute_key="C-2002:duplicate_charge:PAY-88213")
    assert response.status == 422 and response.body["rejection_code"] == "customer_mismatch"


def test_unsettled_payment_is_rejected(billing: BillingClient) -> None:
    from billingops.testing import duplicate_world

    billing.load_world(duplicate_world(settled=False))
    response = post(billing)
    assert response.status == 422 and response.body["rejection_code"] == "payment_not_settled"


def test_concurrent_refunds_never_exceed_the_balance_and_the_loser_gets_an_answer(billing: BillingClient) -> None:
    results: list = []

    def worker(n: int) -> None:
        results.append(post(billing, idempotency_key=f"case-race:{n:016d}", dispute_key=f"C-1001:duplicate_charge:PAY-88213:{n}", amount_cents=3000))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    statuses = sorted(r.status for r in results)
    assert statuses.count(201) == 1, statuses
    assert all(s in (201, 422) for s in statuses), statuses
    assert sum(r["amount_cents"] for r in billing.payment_refunds("C-1001", "PAY-88213")) == 3000


def test_lost_response_after_commit_is_still_on_disk_and_verifiable_by_key(billing: BillingClient) -> None:
    billing.arm_faults(lost_response_after_commit=1)
    response = post(billing)
    assert response.status == 500 and response.body is None
    record = billing.refund_by_key(REQ["idempotency_key"])
    assert record is not None and record["status"] == "succeeded" and record["amount_cents"] == 4900
    assert len(billing.payment_refunds("C-1001", "PAY-88213")) == 1


def test_verification_endpoint_unavailable_is_an_error_not_a_404(billing: BillingClient) -> None:
    post(billing)
    billing.arm_faults(verification_unavailable=1)
    with pytest.raises(BillingUnavailable):
        billing.refund_by_key(REQ["idempotency_key"])
    assert billing.refund_by_key(REQ["idempotency_key"])["status"] == "succeeded"


def test_locked_database_is_reported_as_unavailable_not_as_a_rejection(billing_server) -> None:
    """Hold a write lock from outside; the service must answer 503 and persist nothing."""
    from billing_service.db import connect

    outside = connect(billing_server.db_path, busy_timeout_ms=100)
    outside.execute("BEGIN IMMEDIATE")
    try:
        response = httpx.post(f"{billing_server.url}/refunds", json={**REQ, "idempotency_key": "case-lock:000000000000"}, timeout=10)
        assert response.status_code == 503
    finally:
        outside.execute("ROLLBACK")
        outside.close()
    assert httpx.get(f"{billing_server.url}/refunds/by-key/case-lock:000000000000").status_code == 404


def test_reads_are_customer_scoped(billing: BillingClient) -> None:
    other = World().customer("C-2002").invoice("INV-X-1", "C-2002", 100, "2026-01-01", "2026-01-31").payment("PAY-X-1", "C-2002", "INV-X-1", 100, "2026-01-02").build()
    world = billing.dump_world()
    for table in ("customers", "invoices", "payments"):
        world[table].extend(other[table])
    billing.load_world(world)
    from billingops.billing_client import NotFound

    with pytest.raises(NotFound):
        billing.get_invoice("C-1001", "INV-X-1")
    with pytest.raises(NotFound):
        billing.get_payment("C-1001", "PAY-X-1")
    assert billing.get_invoice("C-2002", "INV-X-1")["id"] == "INV-X-1"
