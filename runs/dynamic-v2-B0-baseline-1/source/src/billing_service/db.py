"""Schema, connections, and world loading for the Billing service."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    plan TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    started_at TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscription_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    event TEXT NOT NULL,
    old_plan TEXT,
    new_plan TEXT,
    old_price_cents INTEGER,
    new_price_cents INTEGER,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    subscription_id TEXT,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    invoice_id TEXT NOT NULL REFERENCES invoices(id),
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    method TEXT NOT NULL,
    paid_at TEXT NOT NULL,
    settled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS refunds (
    id TEXT PRIMARY KEY,
    payment_id TEXT NOT NULL REFERENCES payments(id),
    customer_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT,
    request_key TEXT
);
CREATE TABLE IF NOT EXISTS refund_requests (
    idempotency_key TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    dispute_key TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    rejection_code TEXT,
    refund_id TEXT,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS refund_requests_dispute ON refund_requests(dispute_key, status);
CREATE TABLE IF NOT EXISTS policies (
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    effective_from TEXT NOT NULL,
    body_json TEXT NOT NULL,
    PRIMARY KEY (name, version)
);
"""

WORLD_TABLES: tuple[str, ...] = (
    "customers",
    "subscriptions",
    "subscription_history",
    "invoices",
    "payments",
    "refunds",
    "policies",
)


def connect(path: str | Path, *, busy_timeout_ms: int = 2000) -> sqlite3.Connection:
    """Open a connection with a bounded lock wait.

    The bounded wait is what keeps a locked database from turning into a hang,
    and it is also what lets the refund transaction fail *loudly* with
    ``database is locked`` instead of the handler silently queueing behind a
    writer that may never finish.
    """
    conn = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Autocommit mode: every transaction in this service is opened explicitly
    # with BEGIN IMMEDIATE so the boundaries are visible in the code.
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def load_world(conn: sqlite3.Connection, world: Mapping[str, Any]) -> None:
    """Replace every business table with the rows in ``world``.

    The world format is the same JSON the evaluation data files carry, so a
    case can be loaded into a fresh service with one call. ``refund_requests``
    is deliberately wiped rather than loaded: it is the service's own memory
    of what it was asked to do, and a snapshot never carries one.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Children before parents: foreign keys are enforced on this connection.
        for table in ("refund_requests",) + tuple(reversed(WORLD_TABLES)):
            conn.execute(f"DELETE FROM {table}")
        for table in WORLD_TABLES:
            rows = world.get(table, [])
            for row in rows:
                row = dict(row)
                if table == "policies" and not isinstance(row.get("body_json"), str):
                    row["body_json"] = json.dumps(row.pop("body", row.get("body_json", {})), sort_keys=True)
                if table == "subscription_history":
                    row.pop("seq", None)
                columns = sorted(row.keys())
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    [row[c] for c in columns],
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def dump_world(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for table in WORLD_TABLES + ("refund_requests",):
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        out[table] = [dict(r) for r in rows]
    return out


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
