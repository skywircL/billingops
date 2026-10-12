"""Exactly-once side effects.

A checkpointed graph resumes by re-running the node that died. If that node
moved money, the money moves twice. The fix is not to hope the crash lands in
a convenient place: it is to give every effect an identity and to make a
durable store the arbiter.

``INSERT OR IGNORE`` followed by a read is the whole trick. The insert either
claims the key or loses to a row already there, and the read tells the caller
which happened. Claim and check are one statement, so two attempts cannot both
believe they are first.

The claim is taken *before* the handler runs, which makes the guarantee
at-most-once rather than at-least-once. For a refund that is the right way
round. It leaves the stretch between the claim and the receipt open, and this
module refuses to guess about it: it raises :class:`EffectInDoubt` so a human
reconciles instead of the machine deciding. Every handler run is appended to
an ``executions`` table, so "exactly once" is something the test suite
measures rather than something this docstring claims, and so an interrupted
attempt that already ran can be told apart from one that never started.

An in-doubt key is not a dead ticket. :func:`resolve_effect` is the way a
human closes one, either by recording the outcome the handler had or by
releasing the claim so the work can be attempted again;
``python -m langgraph_production.reconcile`` is the same thing from a
terminal.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol

from pydantic import ValidationError

from .models import EffectReceipt, PlannedAction, utc_now
from .tools import ToolRegistry

HANDLER_REFUSALS: tuple[type[BaseException], ...] = (LookupError, ValidationError)
"""Exceptions that mean the handler decided not to act.

A well formed identifier that does not exist, or arguments the schema
rejects, are refusals: the handler looked, said no, and touched nothing. Those
close the key as ``failed`` and the ticket gets a definite answer.

Anything else is not classifiable from here. A timeout, a dropped connection,
a bug halfway through: the handler may have acted before it raised, so the
claim stays open and the key goes in doubt. Treating every exception as a
clean refusal would be the cheap version of this, and it would quietly release
keys whose money already moved.
"""


class EffectFailed(RuntimeError):
    """The handler refused this action, and nothing was done.

    Distinct from :class:`EffectInDoubt` on purpose. In doubt means nobody
    knows; this means the handler looked at the request and declined it. The
    key is closed rather than left open, so the ticket fails the same way on
    every attempt instead of turning into a mystery.
    """

    def __init__(self, message: str, *, key: str) -> None:
        super().__init__(message)
        self.key = key


class ReconciliationError(RuntimeError):
    """A reconciliation was asked for that this store will not perform."""


class EffectInDoubt(RuntimeError):
    """A previous attempt claimed this key and never recorded a result.

    Raised instead of retrying. Retrying would risk a second charge; assuming
    success would risk a customer who was never refunded. Neither is ours to
    decide, so the run stops and says which key needs reconciling.

    ``executions`` is what the ledger knows: 0 means no handler run was ever
    recorded for this key, which still does not prove the handler never ran,
    because the crash could have landed between the handler returning and the
    row being written. Anything above 0 is proof that it did run and that only
    the result was lost. The two need different reconciliations, so they are
    reported differently rather than blurred into one message.
    """

    def __init__(self, message: str, *, key: str, executions: int) -> None:
        super().__init__(message)
        self.key = key
        self.executions = executions


def idempotency_key(thread_id: str, action_id: str) -> str:
    """One key per (conversation, action). Stable across restarts."""
    return f"{thread_id}:{action_id}"


class EffectStore(Protocol):
    """Where effect claims, executions, and receipts live."""

    def claim(self, key: str, payload: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """Store ``payload`` under ``key`` if the key is free.

        Returns:
            The stored payload and a flag that is True when the key was
            already taken, meaning this call is not the first attempt.
        """

    def finalize(self, key: str, payload: Mapping[str, Any]) -> None:
        """Overwrite a claimed key with its completed record."""

    def release(self, key: str) -> bool:
        """Drop a claim so the action can be attempted again.

        Only a human decides this, through :func:`resolve_effect`. Returns
        True when a row was actually removed.
        """

    def record_execution(self, key: str) -> None:
        """Append one handler execution. Never overwritten, never deduplicated."""

    def execution_count(self, key: str) -> int: ...

    def total_executions(self) -> int: ...

    def get(self, key: str) -> dict[str, Any] | None: ...

    def rows(self) -> dict[str, dict[str, Any]]:
        """Every claim in the store, for the reconciliation queue."""

    def count(self) -> int: ...


def _json_round_trip(payload: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(payload), sort_keys=True, default=str))


class InMemoryEffectStore:
    """Dict backed store. Fine for one process and for unit tests."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._executions: list[str] = []

    def claim(self, key: str, payload: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        existing = self._rows.get(key)
        if existing is not None:
            return existing, True
        stored = _json_round_trip(payload)
        self._rows[key] = stored
        return stored, False

    def finalize(self, key: str, payload: Mapping[str, Any]) -> None:
        self._rows[key] = _json_round_trip(payload)

    def release(self, key: str) -> bool:
        return self._rows.pop(key, None) is not None

    def record_execution(self, key: str) -> None:
        self._executions.append(key)

    def execution_count(self, key: str) -> int:
        return self._executions.count(key)

    def total_executions(self) -> int:
        return len(self._executions)

    def get(self, key: str) -> dict[str, Any] | None:
        return self._rows.get(key)

    def rows(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self._rows.items()}

    def count(self) -> int:
        return len(self._rows)


class SqliteEffectStore:
    """Durable store. It outlives the process that wrote it, which is the point."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS effects (
                key TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS effect_executions (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def claim(self, key: str, payload: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        blob = json.dumps(dict(payload), sort_keys=True, default=str)
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO effects (key, payload) VALUES (?, ?)",
            (key, blob),
        )
        self._conn.commit()
        inserted = cursor.rowcount == 1
        stored = self.get(key)
        assert stored is not None  # the row was either found or just written
        return stored, not inserted

    def finalize(self, key: str, payload: Mapping[str, Any]) -> None:
        blob = json.dumps(dict(payload), sort_keys=True, default=str)
        self._conn.execute("UPDATE effects SET payload = ? WHERE key = ?", (blob, key))
        self._conn.commit()

    def release(self, key: str) -> bool:
        cursor = self._conn.execute("DELETE FROM effects WHERE key = ?", (key,))
        self._conn.commit()
        return cursor.rowcount == 1

    def record_execution(self, key: str) -> None:
        self._conn.execute("INSERT INTO effect_executions (key) VALUES (?)", (key,))
        self._conn.commit()

    def execution_count(self, key: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM effect_executions WHERE key = ?", (key,)
        ).fetchone()
        return int(row[0])

    def total_executions(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM effect_executions").fetchone()
        return int(row[0])

    def get(self, key: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT payload FROM effects WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def rows(self) -> dict[str, dict[str, Any]]:
        cursor = self._conn.execute("SELECT key, payload FROM effects ORDER BY key")
        return {str(key): json.loads(payload) for key, payload in cursor.fetchall()}

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM effects").fetchone()
        return int(row[0])

    def close(self) -> None:
        self._conn.close()


def _noop_hook(point: str) -> None:
    return None


def perform_effect(
    *,
    action: PlannedAction,
    thread_id: str,
    registry: ToolRegistry,
    store: EffectStore,
    clock: Callable[[], datetime] = utc_now,
    hook: Callable[[str], None] = _noop_hook,
    refusals: tuple[type[BaseException], ...] = HANDLER_REFUSALS,
) -> EffectReceipt:
    """Run a tool at most once for a given (thread, action).

    Args:
        action: what to run, carrying the id the key is built from.
        thread_id: the conversation, so the same action in two conversations
            is two effects.
        registry: where the handler comes from.
        store: durable arbiter of who ran first.
        clock: injected so a test can freeze time and compare two runs.
        hook: fires at each of the three windows inside this function, named
            ``act:claimed``, ``act:executed`` and ``act:recorded``. Production
            passes nothing; the test suite passes a crash, so every window is
            exercised rather than assumed.
        refusals: exception types that mean the handler declined without
            acting. See :data:`HANDLER_REFUSALS`.

    Raises:
        EffectInDoubt: a previous attempt claimed the key and never finished.
        EffectFailed: the handler refused this action, on this attempt or an
            earlier one.
    """
    spec = registry.require(action.tool)
    key = idempotency_key(thread_id, action.action_id)
    claim_payload = {
        "status": "pending",
        "action_id": action.action_id,
        "tool": action.tool,
        "claimed_at": clock().isoformat(),
        "receipt": None,
    }
    stored, was_taken = store.claim(key, claim_payload)

    if was_taken:
        if stored.get("status") == "done" and stored.get("receipt"):
            return EffectReceipt.model_validate({**stored["receipt"], "replayed": True})
        if stored.get("status") == "failed":
            raise EffectFailed(
                f"effect {key} was refused by the handler on an earlier attempt "
                f"({stored.get('error')}); nothing was done and nothing will be until "
                "the request changes or the claim is released",
                key=key,
            )
        executions = store.execution_count(key)
        if executions:
            raise EffectInDoubt(
                f"effect {key} ran {executions} time(s) on an earlier attempt and the result "
                "was never persisted; the work happened, the receipt did not, so reconcile "
                "the outcome before this ticket can move",
                key=key,
                executions=executions,
            )
        raise EffectInDoubt(
            f"effect {key} was claimed by an earlier attempt that recorded no execution; "
            "the handler may still have run without the run being recorded, so reconcile "
            "it before this ticket can move",
            key=key,
            executions=0,
        )

    hook("act:claimed")

    try:
        result = spec.run(action.args)
    except refusals as exc:
        # The handler looked and declined. Leaving the claim pending would
        # turn a definite no into a permanent in-doubt key, and the ticket
        # would never move again.
        store.finalize(
            key,
            {
                "status": "failed",
                "action_id": action.action_id,
                "tool": action.tool,
                "claimed_at": claim_payload["claimed_at"],
                "receipt": None,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise EffectFailed(
            f"effect {key} was refused by the handler: {type(exc).__name__}: {exc}",
            key=key,
        ) from exc
    hook("act:executed")

    store.record_execution(key)
    hook("act:recorded")

    receipt = EffectReceipt(
        idempotency_key=key,
        action_id=action.action_id,
        tool=action.tool,
        result=result,
        performed_at=clock(),
        replayed=False,
    )
    store.finalize(
        key,
        {
            "status": "done",
            "action_id": action.action_id,
            "tool": action.tool,
            "claimed_at": claim_payload["claimed_at"],
            "receipt": receipt.model_dump(mode="json"),
        },
    )
    return receipt


Resolution = Literal["performed", "not_performed"]
"""What a human found when they went and looked at the downstream system."""


def open_effects(store: EffectStore) -> dict[str, dict[str, Any]]:
    """Every key that is not closed, which is the reconciliation queue.

    A pending row is a claim whose attempt never came back. A failed row is a
    handler refusal that a human may still want to release. Both need a person;
    ``done`` rows need nobody.
    """
    return {key: row for key, row in store.rows().items() if row.get("status") != "done"}


def resolve_effect(
    *,
    store: EffectStore,
    key: str,
    outcome: Resolution,
    resolver: str,
    result: Mapping[str, Any] | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> dict[str, Any]:
    """Close an open key with what a human found, and return the new row.

    This is the other half of :class:`EffectInDoubt`. Raising it and offering
    no way to answer it would leave the ticket stuck for good, which is a
    worse failure than the one the exception was protecting against.

    Args:
        store: the store holding the claim.
        key: the idempotency key named in the error.
        outcome: ``performed`` when the work is confirmed done downstream, so
            the claim is closed with a receipt and the graph replays it
            instead of running the handler again. ``not_performed`` when the
            work is confirmed not done, so the claim is released and the next
            attempt runs normally.
        resolver: who looked. Recorded on the row.
        result: what the downstream system reports, for a ``performed``
            resolution. Stored verbatim as the receipt result.
        clock: injected.

    Raises:
        ReconciliationError: the key is unknown, or it is already closed and
            there is nothing for a human to decide.
    """
    row = store.get(key)
    if row is None:
        raise ReconciliationError(f"no effect claimed under {key}")
    if row.get("status") == "done":
        raise ReconciliationError(f"effect {key} is already closed and needs no reconciliation")

    when = clock()
    if outcome == "not_performed":
        store.release(key)
        # The claim is gone so the action can be attempted again. The
        # executions table is untouched: it stays the record of what really
        # ran, and a release cannot rewrite that.
        return {
            "key": key,
            "status": "released",
            "reconciled_by": resolver,
            "reconciled_at": when.isoformat(),
            "executions": store.execution_count(key),
        }

    receipt = EffectReceipt(
        idempotency_key=key,
        action_id=str(row.get("action_id", "")),
        tool=str(row.get("tool", "")),
        result=dict(result or {}),
        performed_at=when,
        replayed=False,
    )
    payload: dict[str, Any] = {
        "status": "done",
        "action_id": row.get("action_id"),
        "tool": row.get("tool"),
        "claimed_at": row.get("claimed_at"),
        "receipt": receipt.model_dump(mode="json"),
        "reconciled_by": resolver,
        "reconciled_at": when.isoformat(),
    }
    store.finalize(key, payload)
    return payload
