"""The refund transaction.

    BEGIN IMMEDIATE -> read idempotency record and balance -> validate
                    -> write refund and result record -> COMMIT

The transaction opens *before* the balance is read, so two concurrent
requests for the same payment cannot both read the same balance. The lock
wait is bounded by the connection's ``busy_timeout``; when it expires the
request fails with a 503 and nothing is written, which is not the same thing
as a persisted rejection and is never reported as one.

Inside one transaction the service guarantees:

* the idempotency key is unique, and the same key with different parameters
  is refused (409) without touching the stored result;
* the same dispute cannot be refunded twice through a new key;
* cumulative refunds never exceed the payment's remaining balance;
* the refund row and its successful receipt are committed together.

A terminal rejection produced by business validation is also persisted under
the key, so a replay of that key returns the same rejection instead of
re-running validation against a world that may have changed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .faults import FaultBox

TERMINAL_REJECTIONS: frozenset[str] = frozenset(
    {
        "payment_not_found",
        "customer_mismatch",
        "payment_not_settled",
        "currency_mismatch",
        "dispute_already_refunded",
        "exceeds_refundable_balance",
        "exceeds_invoice_overcharge",
        "invalid_amount",
    }
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_digest(req: Mapping[str, Any]) -> str:
    """Digest of the fields that make two requests 'the same request'."""
    material = {
        "customer_id": req["customer_id"],
        "payment_id": req["payment_id"],
        "amount_cents": int(req["amount_cents"]),
        "currency": req["currency"],
        "dispute_key": req["dispute_key"],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class RefundOutcome:
    """What the HTTP layer turns into a response."""

    http_status: int
    body: dict[str, Any]
    replayed: bool = False


class TemporarilyUnavailable(RuntimeError):
    """The database could not be locked in time. Nothing was written."""


def _reject(
    conn: sqlite3.Connection,
    req: Mapping[str, Any],
    digest: str,
    code: str,
    detail: str,
    now: str,
) -> RefundOutcome:
    """Persist a terminal rejection under the key and commit it.

    Rejections are part of the record: the same key asked again must get the
    same answer, and an operator reading the ledger later must be able to see
    that the request was refused rather than never received.
    """
    result = {
        "idempotency_key": req["idempotency_key"],
        "status": "rejected",
        "rejection_code": code,
        "detail": detail,
        "customer_id": req["customer_id"],
        "payment_id": req["payment_id"],
        "amount_cents": int(req["amount_cents"]),
        "currency": req["currency"],
        "dispute_key": req["dispute_key"],
        "refund": None,
        "recorded_at": now,
    }
    conn.execute(
        """
        INSERT INTO refund_requests
            (idempotency_key, request_digest, dispute_key, customer_id, payment_id,
             amount_cents, currency, status, rejection_code, refund_id, result_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'rejected', ?, NULL, ?, ?)
        """,
        (
            req["idempotency_key"],
            digest,
            req["dispute_key"],
            req["customer_id"],
            req["payment_id"],
            int(req["amount_cents"]),
            req["currency"],
            code,
            json.dumps(result, sort_keys=True),
            now,
        ),
    )
    conn.execute("COMMIT")
    return RefundOutcome(http_status=422, body=result)


def create_refund(conn: sqlite3.Connection, req: Mapping[str, Any], faults: FaultBox) -> RefundOutcome:
    """Run the refund transaction described in the module docstring."""
    digest = request_digest(req)
    now = utc_now_iso()
    if faults.take_write_unavailable():
        # Injected: the service refuses before opening the transaction, so nothing is written.
        raise TemporarilyUnavailable("write unavailable (injected fault)")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise TemporarilyUnavailable(str(exc)) from exc

    try:
        existing = conn.execute(
            "SELECT * FROM refund_requests WHERE idempotency_key = ?", (req["idempotency_key"],)
        ).fetchone()
        if existing is not None:
            conn.execute("ROLLBACK")
            if existing["request_digest"] != digest:
                return RefundOutcome(
                    http_status=409,
                    body={
                        "error": "idempotency_key_reused_with_different_request",
                        "idempotency_key": req["idempotency_key"],
                    },
                )
            body = json.loads(existing["result_json"])
            status = 200 if existing["status"] == "succeeded" else 422
            return RefundOutcome(http_status=status, body=body, replayed=True)

        amount = int(req["amount_cents"])
        if amount <= 0:
            return _reject(conn, req, digest, "invalid_amount", "amount must be positive", now)

        payment = conn.execute("SELECT * FROM payments WHERE id = ?", (req["payment_id"],)).fetchone()
        if payment is None:
            return _reject(conn, req, digest, "payment_not_found", "no such payment", now)
        if payment["customer_id"] != req["customer_id"]:
            return _reject(conn, req, digest, "customer_mismatch", "payment belongs to another customer", now)
        if payment["status"] != "succeeded" or not payment["settled"]:
            return _reject(conn, req, digest, "payment_not_settled", "payment is not settled", now)
        if payment["currency"] != req["currency"]:
            return _reject(conn, req, digest, "currency_mismatch", "currency does not match the payment", now)

        dispute_done = conn.execute(
            "SELECT idempotency_key FROM refund_requests WHERE dispute_key = ? AND status = 'succeeded'",
            (req["dispute_key"],),
        ).fetchone()
        if dispute_done is not None:
            return _reject(
                conn,
                req,
                digest,
                "dispute_already_refunded",
                f"dispute already refunded under key {dispute_done['idempotency_key']}",
                now,
            )

        refunded = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM refunds "
            "WHERE payment_id = ? AND status IN ('pending', 'completed')",
            (req["payment_id"],),
        ).fetchone()["total"]
        remaining = int(payment["amount_cents"]) - int(refunded)
        if amount > remaining:
            return _reject(
                conn,
                req,
                digest,
                "exceeds_refundable_balance",
                f"requested {amount}, remaining refundable {remaining}",
                now,
            )

        # A different transaction id must not bypass a refund already paid or
        # reserved on this invoice. Keep this read and the write under the same
        # BEGIN IMMEDIATE lock, including concurrent requests on different payments.
        invoice = conn.execute("SELECT * FROM invoices WHERE id = ?", (payment["invoice_id"],)).fetchone()
        paid = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM payments WHERE invoice_id = ? AND status = 'succeeded'",
            (payment["invoice_id"],),
        ).fetchone()["total"]
        invoice_refunded = conn.execute(
            "SELECT COALESCE(SUM(r.amount_cents), 0) AS total FROM refunds r JOIN payments p ON p.id = r.payment_id "
            "WHERE p.invoice_id = ? AND r.status IN ('pending', 'completed')", (payment["invoice_id"],),
        ).fetchone()["total"]
        invoice_remaining = max(0, int(paid) - int(invoice["amount_cents"]) - int(invoice_refunded)) if invoice is not None else 0
        if amount > invoice_remaining:
            return _reject(conn, req, digest, "exceeds_invoice_overcharge",
                           f"requested {amount}, invoice overcharge remaining {invoice_remaining}", now)

        seq = conn.execute("SELECT COUNT(*) AS n FROM refunds").fetchone()["n"] + 1
        refund_id = f"RF-{seq:05d}-{digest[:6]}"
        conn.execute(
            """
            INSERT INTO refunds (id, payment_id, customer_id, amount_cents, currency, status,
                                 reason, created_at, completed_at, request_key)
            VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?)
            """,
            (
                refund_id,
                req["payment_id"],
                req["customer_id"],
                amount,
                req["currency"],
                str(req.get("reason", "")),
                now,
                now,
                req["idempotency_key"],
            ),
        )
        refund = {
            "id": refund_id,
            "payment_id": req["payment_id"],
            "customer_id": req["customer_id"],
            "amount_cents": amount,
            "currency": req["currency"],
            "status": "completed",
            "created_at": now,
            "completed_at": now,
            "request_key": req["idempotency_key"],
        }
        result = {
            "idempotency_key": req["idempotency_key"],
            "status": "succeeded",
            "rejection_code": None,
            "detail": None,
            "customer_id": req["customer_id"],
            "payment_id": req["payment_id"],
            "amount_cents": amount,
            "currency": req["currency"],
            "dispute_key": req["dispute_key"],
            "refund": refund,
            "recorded_at": now,
        }
        conn.execute(
            """
            INSERT INTO refund_requests
                (idempotency_key, request_digest, dispute_key, customer_id, payment_id,
                 amount_cents, currency, status, rejection_code, refund_id, result_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'succeeded', NULL, ?, ?, ?)
            """,
            (
                req["idempotency_key"],
                digest,
                req["dispute_key"],
                req["customer_id"],
                req["payment_id"],
                amount,
                req["currency"],
                refund_id,
                json.dumps(result, sort_keys=True),
                now,
            ),
        )
        faults.before_commit()
        conn.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        # Lock wait expired or the file went away mid-transaction. Roll back
        # everything and say "unavailable": no rejection is persisted, no
        # partial write survives, and there is no retry loop hidden in here.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise TemporarilyUnavailable(str(exc)) from exc
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise

    # The write is durable at this point. Whatever happens below, the refund
    # exists and a verification by key will find it.
    faults.after_commit()
    return RefundOutcome(http_status=201, body=result)


def lookup_by_key(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT result_json FROM refund_requests WHERE idempotency_key = ?", (key,)).fetchone()
    return json.loads(row["result_json"]) if row else None
