"""HTTP surface of the Billing service.

Every read is scoped by customer in the path. An invoice that exists but
belongs to someone else is a 404 for this customer, not a 403: the service
does not confirm the existence of other customers' records.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import db as dbmod
from .faults import FaultBox, LostResponse
from .refunds import TemporarilyUnavailable, create_refund, lookup_by_key


class RefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=200)
    dispute_key: str = Field(min_length=3, max_length=200)
    customer_id: str
    payment_id: str
    amount_cents: int
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    reason: str = ""


def create_app(db_path: str | Path, *, admin_token: str | None = None) -> FastAPI:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with dbmod.connect(db_path) as boot:
        dbmod.init_schema(boot)
    faults = FaultBox()
    admin_token = admin_token if admin_token is not None else os.environ.get("BILLING_ADMIN_TOKEN", "demo-admin")

    app = FastAPI(title="Simulated Billing", version="0.1.0")
    app.state.db_path = db_path
    app.state.faults = faults

    def conn() -> sqlite3.Connection:
        c = dbmod.connect(db_path)
        try:
            yield c
        finally:
            c.close()

    def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
        if x_admin_token != admin_token:
            raise HTTPException(status_code=403, detail="admin token required")

    def customer_or_404(c: sqlite3.Connection, customer_id: str) -> None:
        if c.execute("SELECT 1 FROM customers WHERE id = ?", (customer_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="customer not found")

    def guard_reads() -> None:
        if faults.take_reads_unavailable():
            raise HTTPException(status_code=503, detail="billing reads unavailable")

    # -- health and admin ---------------------------------------------

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "faults": faults.snapshot()}

    @app.put("/_admin/world", dependencies=[Depends(require_admin)])
    def put_world(world: dict[str, Any], c: sqlite3.Connection = Depends(conn)) -> dict[str, Any]:
        dbmod.load_world(c, world)
        faults.clear()
        return {"loaded": {t: len(world.get(t, [])) for t in dbmod.WORLD_TABLES}}

    @app.get("/_admin/world", dependencies=[Depends(require_admin)])
    def get_world(c: sqlite3.Connection = Depends(conn)) -> dict[str, Any]:
        return dbmod.dump_world(c)

    @app.post("/_faults", dependencies=[Depends(require_admin)])
    def arm_faults(spec: dict[str, Any]) -> dict[str, Any]:
        try:
            return faults.arm(spec)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=f"unknown fault {exc}") from exc

    @app.delete("/_faults", dependencies=[Depends(require_admin)])
    def clear_faults() -> dict[str, Any]:
        faults.clear()
        return faults.snapshot()

    # -- customer scoped reads ------------------------------------------

    @app.get("/customers/{customer_id}/invoices")
    def list_invoices(customer_id: str, c: sqlite3.Connection = Depends(conn)) -> list[dict[str, Any]]:
        guard_reads()
        customer_or_404(c, customer_id)
        rows = c.execute(
            "SELECT * FROM invoices WHERE customer_id = ? ORDER BY issued_at DESC, id", (customer_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/customers/{customer_id}/invoices/{invoice_id}")
    def get_invoice(customer_id: str, invoice_id: str, c: sqlite3.Connection = Depends(conn)) -> dict[str, Any]:
        guard_reads()
        row = c.execute(
            "SELECT * FROM invoices WHERE id = ? AND customer_id = ?", (invoice_id, customer_id)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="invoice not found for this customer")
        return dict(row)

    @app.get("/customers/{customer_id}/invoices/{invoice_id}/period")
    def period_invoices(
        customer_id: str, invoice_id: str, c: sqlite3.Connection = Depends(conn)
    ) -> list[dict[str, Any]]:
        """Other invoices of the same customer whose period overlaps this one."""
        guard_reads()
        anchor = c.execute(
            "SELECT * FROM invoices WHERE id = ? AND customer_id = ?", (invoice_id, customer_id)
        ).fetchone()
        if anchor is None:
            raise HTTPException(status_code=404, detail="invoice not found for this customer")
        rows = c.execute(
            """
            SELECT * FROM invoices
            WHERE customer_id = ? AND id != ?
              AND period_start <= ? AND period_end >= ?
            ORDER BY issued_at, id
            """,
            (customer_id, invoice_id, anchor["period_end"], anchor["period_start"]),
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/customers/{customer_id}/invoices/{invoice_id}/payments")
    def invoice_payments(
        customer_id: str, invoice_id: str, c: sqlite3.Connection = Depends(conn)
    ) -> list[dict[str, Any]]:
        guard_reads()
        anchor = c.execute(
            "SELECT 1 FROM invoices WHERE id = ? AND customer_id = ?", (invoice_id, customer_id)
        ).fetchone()
        if anchor is None:
            raise HTTPException(status_code=404, detail="invoice not found for this customer")
        rows = c.execute(
            "SELECT * FROM payments WHERE invoice_id = ? AND customer_id = ? ORDER BY paid_at, id",
            (invoice_id, customer_id),
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/customers/{customer_id}/payments/{payment_id}")
    def get_payment(customer_id: str, payment_id: str, c: sqlite3.Connection = Depends(conn)) -> dict[str, Any]:
        guard_reads()
        row = c.execute(
            "SELECT * FROM payments WHERE id = ? AND customer_id = ?", (payment_id, customer_id)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="payment not found for this customer")
        return dict(row)

    @app.get("/customers/{customer_id}/payments/{payment_id}/refunds")
    def payment_refunds(
        customer_id: str, payment_id: str, c: sqlite3.Connection = Depends(conn)
    ) -> list[dict[str, Any]]:
        guard_reads()
        anchor = c.execute(
            "SELECT 1 FROM payments WHERE id = ? AND customer_id = ?", (payment_id, customer_id)
        ).fetchone()
        if anchor is None:
            raise HTTPException(status_code=404, detail="payment not found for this customer")
        rows = c.execute(
            "SELECT * FROM refunds WHERE payment_id = ? ORDER BY created_at, id", (payment_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/customers/{customer_id}/refunds")
    def customer_refunds(customer_id: str, c: sqlite3.Connection = Depends(conn)) -> list[dict[str, Any]]:
        guard_reads()
        customer_or_404(c, customer_id)
        rows = c.execute(
            "SELECT * FROM refunds WHERE customer_id = ? ORDER BY created_at, id", (customer_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/customers/{customer_id}/subscription-history")
    def subscription_history(customer_id: str, c: sqlite3.Connection = Depends(conn)) -> dict[str, Any]:
        guard_reads()
        customer_or_404(c, customer_id)
        subs = c.execute("SELECT * FROM subscriptions WHERE customer_id = ?", (customer_id,)).fetchall()
        hist = c.execute(
            "SELECT * FROM subscription_history WHERE customer_id = ? ORDER BY effective_at, seq",
            (customer_id,),
        ).fetchall()
        return {"subscriptions": [dict(r) for r in subs], "history": [dict(r) for r in hist]}

    @app.get("/policies/{name}")
    def get_policy(name: str, version: int | None = None, c: sqlite3.Connection = Depends(conn)) -> dict[str, Any]:
        guard_reads()
        if version is None:
            row = c.execute(
                "SELECT * FROM policies WHERE name = ? ORDER BY version DESC LIMIT 1", (name,)
            ).fetchone()
        else:
            row = c.execute("SELECT * FROM policies WHERE name = ? AND version = ?", (name, version)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="policy not found")
        out = dict(row)
        import json

        out["body"] = json.loads(out.pop("body_json"))
        return out

    # -- the write, and the read that verifies it ------------------------

    @app.post("/refunds")
    def post_refund(req: RefundRequest, c: sqlite3.Connection = Depends(conn)) -> Response:
        try:
            outcome = create_refund(c, req.model_dump(), faults)
        except TemporarilyUnavailable as exc:
            raise HTTPException(status_code=503, detail=f"billing temporarily unavailable: {exc}") from exc
        except LostResponse:
            # Committed. Answer with nothing: the caller must verify by key.
            return Response(status_code=500)
        headers = {"X-Replayed": "1"} if outcome.replayed else {}
        return JSONResponse(status_code=outcome.http_status, content=outcome.body, headers=headers)

    @app.get("/refunds/by-key/{key}")
    def refund_by_key(key: str, c: sqlite3.Connection = Depends(conn)) -> dict[str, Any]:
        if faults.take_verification_unavailable():
            raise HTTPException(status_code=503, detail="verification endpoint unavailable")
        found = lookup_by_key(c, key)
        if found is None:
            raise HTTPException(status_code=404, detail="no refund request under this key")
        return found

    return app
