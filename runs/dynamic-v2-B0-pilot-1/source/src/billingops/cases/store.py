"""Application database: cases, tasks, commands, idempotency keys, audit events.

Short transactions only. Nothing here is held open across a model call or an
HTTP call to Billing. The audit ``events`` table is the business source of
truth; observability exports are derived from it and may fail without
consequence.

The upstream effect ledger and approval ledger live in the same file (their
own tables, their own connections), as does LangGraph's checkpointer.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

BUSINESS_STATES: tuple[str, ...] = (
    "investigating",
    "awaiting_user",
    "awaiting_approval",
    "executing",
    "verifying",
    "uncertain",
    "error",
    "escalated",
    "closed",
)

# Which states accept which incoming commands. Anything else is a 409.
ALLOWED_COMMANDS: dict[str, frozenset[str]] = {
    # Spec 5.4 / 5.5 (v1.2): outside awaiting_user and awaiting_approval a customer message is
    # only a progress question. It is answered from the case record and changes nothing.
    "investigating": frozenset({"message", "escalate"}),
    "awaiting_user": frozenset({"message", "escalate"}),
    "awaiting_approval": frozenset({"message", "decision", "escalate"}),
    "executing": frozenset({"message"}),
    "verifying": frozenset({"message"}),
    "uncertain": frozenset({"message", "resume", "escalate"}),
    "error": frozenset({"message", "resume", "escalate"}),
    "escalated": frozenset({"message", "resume"}),
    "closed": frozenset(),
}
GRAPH_MESSAGE_STATES = frozenset({"awaiting_user", "awaiting_approval"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    mode TEXT NOT NULL,
    text TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    pending_question TEXT,
    proposal_json TEXT,
    proposal_version INTEGER NOT NULL DEFAULT 0,
    gate_token TEXT,
    conclusion_json TEXT,
    final_message TEXT,
    escalation_json TEXT,
    action_key TEXT,
    error TEXT,
    waiting_since TEXT,
    human_wait_seconds REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, id);
CREATE TABLE IF NOT EXISTS commands (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    scope_key TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    command_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_case ON events(case_id, seq);
CREATE TABLE IF NOT EXISTS runtime_lock (
    name TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class CaseStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # -- cases -------------------------------------------------------------------

    def create_case(self, *, case_id: str, customer_id: str, created_by: str, mode: str, text: str) -> dict[str, Any]:
        now = now_iso()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO cases (id, customer_id, created_by, mode, text, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'investigating', ?, ?)",
                (case_id, customer_id, created_by, mode, text, now, now),
            )
            c.execute("COMMIT")
        case = self.get_case(case_id)
        assert case is not None
        return case

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        for key in ("proposal_json", "conclusion_json", "escalation_json"):
            out[key[:-5]] = json.loads(out.pop(key)) if out.get(key) else None
        return out

    def list_cases(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT id FROM cases ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [case for case in (self.get_case(r["id"]) for r in rows) if case]

    def update_case(self, case_id: str, **fields: Any) -> None:
        if not fields:
            return
        encoded: dict[str, Any] = {}
        for key, value in fields.items():
            if key in ("proposal", "conclusion", "escalation"):
                encoded[key + "_json"] = json.dumps(value, default=str) if value is not None else None
            else:
                encoded[key] = value
        encoded["updated_at"] = now_iso()
        assignments = ", ".join(f"{k} = ?" for k in encoded)
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(f"UPDATE cases SET {assignments} WHERE id = ?", [*encoded.values(), case_id])
            c.execute("COMMIT")

    def set_state(self, case_id: str, state: str, *, actor: str = "system", **fields: Any) -> None:
        if state not in BUSINESS_STATES:
            raise ValueError(state)
        case = self.get_case(case_id)
        previous = case["state"] if case else None
        wait_fields: dict[str, Any] = {}
        now = datetime.now(timezone.utc)
        if case:
            waiting_states = ("awaiting_user", "awaiting_approval")
            if previous in waiting_states and case.get("waiting_since"):
                since = datetime.fromisoformat(case["waiting_since"])
                wait_fields["human_wait_seconds"] = float(case.get("human_wait_seconds", 0)) + (now - since).total_seconds()
                wait_fields["waiting_since"] = None
            if state in waiting_states:
                wait_fields["waiting_since"] = now.isoformat()
        self.update_case(case_id, state=state, **wait_fields, **fields)
        self.append_event(case_id, "state_changed", actor, {"from": previous, "to": state})

    # -- tasks ----------------------------------------------------------------------

    def enqueue_task(self, case_id: str, kind: str, payload: Mapping[str, Any] | None = None) -> int:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(
                "INSERT INTO tasks (case_id, kind, payload_json, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
                (case_id, kind, json.dumps(dict(payload or {}), default=str), now_iso()),
            )
            c.execute("COMMIT")
            return int(cur.lastrowid)

    def claim_next_task(self) -> dict[str, Any] | None:
        """Serial claim: the oldest pending task flips to running in one statement."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM tasks WHERE status = 'pending' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                c.execute("COMMIT")
                return None
            c.execute("UPDATE tasks SET status = 'running', started_at = ? WHERE id = ? AND status = 'pending'", (now_iso(), row["id"]))
            c.execute("COMMIT")
        task = dict(row)
        task["payload"] = json.loads(task.pop("payload_json"))
        return task

    def finish_task(self, task_id: int, *, status: str = "done", error: str | None = None) -> None:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE tasks SET status = ?, finished_at = ?, error = ? WHERE id = ?", (status, now_iso(), error, task_id))
            c.execute("COMMIT")

    def requeue_running_tasks(self) -> int:
        """Startup recovery: whatever was running when the previous process died goes back to pending."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute("UPDATE tasks SET status = 'pending', started_at = NULL WHERE status = 'running'")
            c.execute("COMMIT")
            return int(cur.rowcount)

    def pending_tasks(self, case_id: str | None = None) -> list[dict[str, Any]]:
        with self._conn() as c:
            if case_id is None:
                rows = c.execute("SELECT * FROM tasks WHERE status IN ('pending','running') ORDER BY id").fetchall()
            else:
                rows = c.execute("SELECT * FROM tasks WHERE status IN ('pending','running') AND case_id = ? ORDER BY id", (case_id,)).fetchall()
        return [dict(r) for r in rows]

    def tasks(self, case_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM tasks WHERE case_id = ? ORDER BY id", (case_id,)).fetchall()
        return [dict(r) for r in rows]

    # -- commands and idempotency ---------------------------------------------------------

    def record_command(self, *, case_id: str, kind: str, payload: Mapping[str, Any], actor: str) -> str:
        command_id = new_id("cmd")
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO commands (id, case_id, kind, payload_json, actor, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (command_id, case_id, kind, json.dumps(dict(payload), default=str), actor, now_iso()),
            )
            c.execute("COMMIT")
        return command_id

    def lookup_idempotency(self, scope_key: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM idempotency WHERE scope_key = ?", (scope_key,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["response"] = json.loads(out.pop("response_json"))
        return out

    def store_idempotency(self, *, scope_key: str, digest: str, status_code: int, response: Mapping[str, Any], command_id: str | None) -> bool:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(
                "INSERT OR IGNORE INTO idempotency (scope_key, digest, status_code, response_json, command_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (scope_key, digest, status_code, json.dumps(dict(response), default=str), command_id, now_iso()),
            )
            c.execute("COMMIT")
            return cur.rowcount == 1

    # -- audit events -------------------------------------------------------------------------

    def append_event(self, case_id: str, kind: str, actor: str, payload: Mapping[str, Any]) -> int:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(
                "INSERT INTO events (case_id, at, kind, actor, payload_json) VALUES (?, ?, ?, ?, ?)",
                (case_id, now_iso(), kind, actor, json.dumps(dict(payload), default=str)),
            )
            c.execute("COMMIT")
            return int(cur.lastrowid)

    def events(self, case_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM events WHERE case_id = ? ORDER BY seq", (case_id,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["payload"] = json.loads(item.pop("payload_json"))
            out.append(item)
        return out

    def count_events(self, kind: str) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM events WHERE kind = ?", (kind,)).fetchone()[0])

    # -- runtime lock (single executor per database) ---------------------------------------------

    def acquire_runtime_lock(self, pid: int, *, stale_after_s: float = 30.0, force: bool = False) -> bool:
        """Claim the executor role for this database.

        A previous holder that stopped heartbeating longer than ``stale_after_s``
        ago is taken to have exited; its running tasks are requeued by the
        caller. A live holder keeps the lock and the caller must not run.
        """
        now = datetime.now(timezone.utc)
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM runtime_lock WHERE name = 'executor'").fetchone()
            if row is not None and not force:
                last = datetime.fromisoformat(row["heartbeat_at"])
                if (now - last).total_seconds() < stale_after_s and int(row["pid"]) != pid and _pid_alive(int(row["pid"])):
                    c.execute("COMMIT")
                    return False
            c.execute("DELETE FROM runtime_lock WHERE name = 'executor'")
            c.execute("INSERT INTO runtime_lock (name, pid, started_at, heartbeat_at) VALUES ('executor', ?, ?, ?)", (pid, now.isoformat(), now.isoformat()))
            c.execute("COMMIT")
            return True

    def heartbeat(self, pid: int) -> None:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE runtime_lock SET heartbeat_at = ? WHERE name = 'executor' AND pid = ?", (now_iso(), pid))
            c.execute("COMMIT")

    def release_runtime_lock(self, pid: int) -> None:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM runtime_lock WHERE name = 'executor' AND pid = ?", (pid,))
            c.execute("COMMIT")

    # -- metrics ----------------------------------------------------------------------------------

    def metrics_summary(self) -> dict[str, Any]:
        with self._conn() as c:
            by_state = {r["state"]: r["n"] for r in c.execute("SELECT state, COUNT(*) AS n FROM cases GROUP BY state").fetchall()}
            total = int(c.execute("SELECT COUNT(*) FROM cases").fetchone()[0])
            wait = c.execute("SELECT COALESCE(SUM(human_wait_seconds), 0) FROM cases").fetchone()[0]
            kinds = {r["kind"]: r["n"] for r in c.execute("SELECT kind, COUNT(*) AS n FROM events GROUP BY kind").fetchall()}
            pending = int(c.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('pending','running')").fetchone()[0])
        now = datetime.now(timezone.utc)
        cases = self.list_cases()
        uncertain = []
        dropped = 0
        tool_ok, tool_total = 0, 0
        for case in cases:
            events = self.events(case["id"])
            entries = [e for e in events if e["kind"] == "state_changed" and e["payload"].get("to") == "uncertain"]
            if case["state"] == "uncertain" and entries:
                uncertain.append({"case_id": case["id"], "since": entries[-1]["at"], "age_seconds": max(0, (now - datetime.fromisoformat(entries[-1]["at"])).total_seconds())})
            for e in events:
                if e["kind"] == "observation_finished":
                    dropped += int(e["payload"].get("dropped_operations", 0))
                if e["kind"] == "tool_read":
                    tool_total += 1
                    tool_ok += int(bool(e["payload"].get("ok")))
        return {
            "schema_version": "audit-v1",
            "window": {"kind": "all_time", "start": min((c["created_at"] for c in cases), default=None), "end": now.isoformat()},
            "denominators": {"cases": total, "tool_reads": tool_total},
            "tool_read_success_rate": tool_ok / tool_total if tool_total else None,
            "uncertain_backlog": uncertain,
            "telemetry_dropped_operations": dropped,
            "quality_accuracy": None,
            "quality_note": "runtime health is not task accuracy; no online ground truth",
            "cases_total": total,
            "cases_by_state": by_state,
            "tasks_open": pending,
            "human_wait_seconds_total": float(wait),
            "events": {
                "tool_reads": kinds.get("tool_read", 0),
                "clarifications": kinds.get("clarification_answered", 0),
                "protocol_errors": kinds.get("protocol_error", 0),
                "budget_exhausted": kinds.get("budget_exhausted", 0),
                "proposals_blocked": kinds.get("proposal_blocked", 0),
                "approvals": kinds.get("approval_recorded", 0),
                "gate_rejected": kinds.get("gate_rejected", 0),
                "refund_sent": kinds.get("refund_sent", 0),
                "refund_verified": kinds.get("refund_verified", 0),
                "refund_in_doubt": kinds.get("refund_in_doubt", 0),
                "escalations": kinds.get("escalated", 0),
                "duplicate_commands_ignored": kinds.get("duplicate_command_ignored", 0),
            },
        }


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
