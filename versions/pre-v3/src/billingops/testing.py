"""Helpers shared by the test suite, the demo scripts and the evaluation harness."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import httpx
import uvicorn

from billing_service.app import create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class BillingServer:
    """A real Billing process boundary, minus the process: uvicorn in a thread on a free port."""

    def __init__(self, db_path: str | Path, *, admin_token: str = "demo-admin") -> None:
        self.db_path = str(db_path)
        self.admin_token = admin_token
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(create_app(self.db_path, admin_token=admin_token), host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> "BillingServer":
        self._thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.url}/health", timeout=0.5).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("billing server did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


@contextmanager
def billing_server(db_path: str | Path) -> Iterator[BillingServer]:
    server = BillingServer(db_path).start()
    try:
        yield server
    finally:
        server.stop()


# -- a compact world builder -------------------------------------------------------------


class World:
    """Builds the JSON the Billing service loads. Every id is explicit so tests can assert on it."""

    def __init__(self) -> None:
        self.data: dict[str, list[dict[str, Any]]] = {
            "customers": [],
            "subscriptions": [],
            "subscription_history": [],
            "invoices": [],
            "payments": [],
            "refunds": [],
            "policies": [],
        }

    def customer(self, cid: str, name: str = "Customer") -> "World":
        self.data["customers"].append({"id": cid, "name": name, "email": f"{cid.lower()}@example.test"})
        return self

    def subscription(self, sid: str, cid: str, plan: str, price_cents: int, started_at: str, status: str = "active") -> "World":
        self.data["subscriptions"].append({"id": sid, "customer_id": cid, "plan": plan, "price_cents": price_cents, "currency": "CNY", "started_at": started_at, "status": status})
        return self

    def history(self, sid: str, cid: str, effective_at: str, event: str, old_plan: str | None, new_plan: str | None, old_price: int | None, new_price: int | None, note: str = "") -> "World":
        self.data["subscription_history"].append({"subscription_id": sid, "customer_id": cid, "effective_at": effective_at, "event": event, "old_plan": old_plan, "new_plan": new_plan, "old_price_cents": old_price, "new_price_cents": new_price, "note": note})
        return self

    def invoice(self, iid: str, cid: str, amount_cents: int, period_start: str, period_end: str, *, issued_at: str | None = None, status: str = "paid", subscription_id: str | None = None, description: str = "") -> "World":
        self.data["invoices"].append({"id": iid, "customer_id": cid, "subscription_id": subscription_id, "period_start": period_start, "period_end": period_end, "amount_cents": amount_cents, "currency": "CNY", "status": status, "issued_at": issued_at or period_start, "description": description})
        return self

    def payment(self, pid: str, cid: str, iid: str, amount_cents: int, paid_at: str, *, status: str = "succeeded", settled: bool = True, method: str = "card") -> "World":
        self.data["payments"].append({"id": pid, "customer_id": cid, "invoice_id": iid, "amount_cents": amount_cents, "currency": "CNY", "status": status, "method": method, "paid_at": paid_at, "settled": 1 if settled else 0})
        return self

    def refund(self, rid: str, pid: str, cid: str, amount_cents: int, created_at: str, *, status: str = "completed", completed_at: str | None = None, reason: str = "") -> "World":
        self.data["refunds"].append({"id": rid, "payment_id": pid, "customer_id": cid, "amount_cents": amount_cents, "currency": "CNY", "status": status, "reason": reason, "created_at": created_at, "completed_at": completed_at, "request_key": None})
        return self

    def policy(self, name: str, version: int, effective_from: str, body: dict[str, Any]) -> "World":
        self.data["policies"].append({"name": name, "version": version, "effective_from": effective_from, "body": body})
        return self

    def build(self) -> dict[str, Any]:
        return {k: list(v) for k, v in self.data.items()}


def duplicate_world(cid: str = "C-1001", *, prior_refund_cents: int = 0, settled: bool = True) -> dict[str, Any]:
    """The canonical duplicate-charge case: INV-2026-0142 paid twice by PAY-88212 and PAY-88213."""
    w = (
        World()
        .customer(cid, "Lin Wei")
        .subscription("SUB-1", cid, "pro", 4900, "2025-11-01")
        .invoice("INV-2026-0142", cid, 4900, "2026-02-01", "2026-02-28", issued_at="2026-02-01", subscription_id="SUB-1", description="Pro plan, February 2026")
        .invoice("INV-2026-0099", cid, 4900, "2026-01-01", "2026-01-31", issued_at="2026-01-01", subscription_id="SUB-1", description="Pro plan, January 2026")
        .payment("PAY-88100", cid, "INV-2026-0099", 4900, "2026-01-02T09:00:00")
        .payment("PAY-88212", cid, "INV-2026-0142", 4900, "2026-02-02T09:00:00")
        .payment("PAY-88213", cid, "INV-2026-0142", 4900, "2026-02-02T09:00:41", settled=settled)
        .policy("pricing", 3, "2026-01-01", {"pro_monthly_cents": 4900, "proration": "daily", "note": "Plan changes are prorated by day."})
        .policy("refunds", 2, "2026-01-01", {"duplicate_charge": "full refund of the extra payment", "price_dispute": "operator review"})
    )
    if prior_refund_cents:
        w.refund("RF-0001", "PAY-88213", cid, prior_refund_cents, "2026-02-05T10:00:00", completed_at="2026-02-06T10:00:00", reason="partial goodwill")
    return w.build()


def ambiguous_world(cid: str = "C-1001") -> dict[str, Any]:
    """Two invoices, each paid twice. "Charged twice" cannot be resolved without asking which one."""
    w = (
        World()
        .customer(cid, "Lin Wei")
        .subscription("SUB-1", cid, "pro", 4900, "2025-11-01")
        .invoice("INV-2026-0099", cid, 4900, "2026-01-01", "2026-01-31", issued_at="2026-01-01", subscription_id="SUB-1", description="Pro plan, January 2026")
        .invoice("INV-2026-0142", cid, 4900, "2026-02-01", "2026-02-28", issued_at="2026-02-01", subscription_id="SUB-1", description="Pro plan, February 2026")
        .payment("PAY-88100", cid, "INV-2026-0099", 4900, "2026-01-02T09:00:00")
        .payment("PAY-88101", cid, "INV-2026-0099", 4900, "2026-01-02T09:00:35")
        .payment("PAY-88212", cid, "INV-2026-0142", 4900, "2026-02-02T09:00:00")
        .payment("PAY-88213", cid, "INV-2026-0142", 4900, "2026-02-02T09:00:41")
        .policy("pricing", 3, "2026-01-01", {"pro_monthly_cents": 4900, "proration": "daily"})
        .policy("refunds", 2, "2026-01-01", {"duplicate_charge": "full refund of the extra payment", "price_dispute": "operator review"})
    )
    return w.build()
