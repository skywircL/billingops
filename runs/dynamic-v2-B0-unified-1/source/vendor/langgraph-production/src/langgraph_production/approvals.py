"""Single-use human approvals.

An approval in this graph authorises one action, on one ticket, over one
state, once. It is not a flag on a config file, it is not an id someone pastes
into a second request, and it does not carry over to whatever the agent
decides to do next.

Two mechanisms enforce that:

1. **The token.** Minted from the thread, the ticket, the action id, and a
   digest of the decision-relevant state. Change any of those and the token
   changes, so an approval issued for a 45.00 refund does not authorise a
   4500.00 one.
2. **The ledger.** A durable record of which token authorised which decision.
   Presenting a token that is already bound to a different decision is refused.

The second one has to tolerate one thing: LangGraph re-executes an interrupted
node from the top when it resumes, so the same approval is legitimately
presented more than once. The ledger tells that apart from reuse by comparing
the fingerprint of the decision, not just the token.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Protocol

from .models import ApprovalRecord, GateKind, GateRejection, digest


def mint_token(
    *,
    thread_id: str,
    ticket_id: str,
    kind: GateKind,
    action_id: str | None,
    state_digest: str,
) -> str:
    """Deterministic token for one gate stop.

    Deterministic on purpose: a node re-executed after a crash mints the same
    token, so a crash does not invalidate an approval a human already gave.
    """
    return digest(
        {
            "thread_id": thread_id,
            "ticket_id": ticket_id,
            "kind": kind,
            "action_id": action_id,
            "state": state_digest,
        }
    )


class ApprovalLedger(Protocol):
    """Durable record of consumed approval tokens."""

    def bind(self, token: str, fingerprint: str, record: ApprovalRecord) -> tuple[str, bool]:
        """Bind ``token`` to ``fingerprint`` if it is free.

        Returns:
            The fingerprint now bound to the token, and a flag that is True
            when the token was already bound.
        """

    def note_presentation(self, token: str, first: bool) -> None:
        """Append one presentation of this approval, first use or replay."""

    def presentations(self, token: str) -> tuple[bool, ...]:
        """Every presentation of this token, in order, as first-use flags."""

    def lookup(self, token: str) -> ApprovalRecord | None: ...

    def count(self) -> int: ...


class InMemoryApprovalLedger:
    def __init__(self) -> None:
        self._rows: dict[str, tuple[str, str]] = {}
        self._presentations: list[tuple[str, bool]] = []

    def bind(self, token: str, fingerprint: str, record: ApprovalRecord) -> tuple[str, bool]:
        existing = self._rows.get(token)
        if existing is not None:
            return existing[0], True
        self._rows[token] = (fingerprint, record.model_dump_json())
        return fingerprint, False

    def note_presentation(self, token: str, first: bool) -> None:
        self._presentations.append((token, first))

    def presentations(self, token: str) -> tuple[bool, ...]:
        return tuple(first for seen, first in self._presentations if seen == token)

    def lookup(self, token: str) -> ApprovalRecord | None:
        existing = self._rows.get(token)
        return ApprovalRecord.model_validate_json(existing[1]) if existing else None

    def count(self) -> int:
        return len(self._rows)


class SqliteApprovalLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                token TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                record TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approval_presentations (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL,
                first_use INTEGER NOT NULL
            );
            """
        )
        self._conn.commit()

    def bind(self, token: str, fingerprint: str, record: ApprovalRecord) -> tuple[str, bool]:
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO approvals (token, fingerprint, record) VALUES (?, ?, ?)",
            (token, fingerprint, record.model_dump_json()),
        )
        self._conn.commit()
        inserted = cursor.rowcount == 1
        row = self._conn.execute(
            "SELECT fingerprint FROM approvals WHERE token = ?", (token,)
        ).fetchone()
        return str(row[0]), not inserted

    def note_presentation(self, token: str, first: bool) -> None:
        self._conn.execute(
            "INSERT INTO approval_presentations (token, first_use) VALUES (?, ?)",
            (token, int(first)),
        )
        self._conn.commit()

    def presentations(self, token: str) -> tuple[bool, ...]:
        cursor = self._conn.execute(
            "SELECT first_use FROM approval_presentations WHERE token = ? ORDER BY seq", (token,)
        )
        return tuple(bool(row[0]) for row in cursor.fetchall())

    def lookup(self, token: str) -> ApprovalRecord | None:
        row = self._conn.execute("SELECT record FROM approvals WHERE token = ?", (token,)).fetchone()
        return ApprovalRecord.model_validate_json(row[0]) if row else None

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM approvals").fetchone()
        return int(row[0])

    def close(self) -> None:
        self._conn.close()


class AuthorizationError(Exception):
    """An approval was presented that this gate will not honour."""

    def __init__(self, reason: GateRejection, detail: str) -> None:
        super().__init__(detail)
        self.reason: GateRejection = reason
        self.detail = detail


def authorize(
    *,
    record: ApprovalRecord,
    expected_token: str,
    ledger: ApprovalLedger,
) -> bool:
    """Check an approval and consume it.

    Every presentation is appended to the ledger, first use or replay, so the
    audit trail shows which one happened. The caller does not have to remember
    to record it, and the graph deliberately does not carry it in the state:
    a resumed node presents the approval again, and a state field that flipped
    on the second presentation would make the resume look non-deterministic
    when it is exactly right.

    Returns:
        True when this is the first time the approval is being honoured,
        False when it is a legitimate replay of the identical decision after a
        resume. Both mean the action may proceed.

    Raises:
        AuthorizationError: the token does not belong to this gate, or it is
            already bound to a different decision.
    """
    if record.token != expected_token:
        raise AuthorizationError(
            "token_mismatch",
            f"approval token does not match this gate (got {record.token[:12]}..., "
            f"expected {expected_token[:12]}...)",
        )

    fingerprint = record.fingerprint()
    bound, was_taken = ledger.bind(record.token, fingerprint, record)
    if was_taken and bound != fingerprint:
        raise AuthorizationError(
            "token_replayed",
            "this approval token is already bound to a different decision",
        )
    ledger.note_presentation(record.token, not was_taken)
    return not was_taken


def parse_decision(raw: object) -> dict[str, object]:
    """Normalise whatever came back through the interrupt into a mapping."""
    if isinstance(raw, str):
        return dict(json.loads(raw))
    if isinstance(raw, dict):
        return dict(raw)
    if hasattr(raw, "model_dump"):
        return dict(raw.model_dump())  # type: ignore[union-attr]
    raise TypeError(f"cannot read a human decision out of {type(raw).__name__}")
