"""Compilation, durability, and the driver that walks a ticket to the end.

One SQLite file holds all three durable things: LangGraph's checkpoints, the
effect claims, and the approval ledger. Losing one and keeping the others
would be worse than losing all three, so they live or die together.

Checkpoints are written with ``durability="sync"``. The default in LangGraph
is faster and perfectly reasonable for chat; it is the wrong default for a
graph that can move money, because the window where a node has run and its
checkpoint has not landed is exactly the window this repo exists to close.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from .approvals import ApprovalLedger, InMemoryApprovalLedger, SqliteApprovalLedger
from .effects import EffectStore, InMemoryEffectStore, SqliteEffectStore
from .faults import CrashHook, SimulatedCrash, no_crash
from .graph import GraphDeps, build_graph
from .llm.base import LLMCallable
from .models import GateRequest, HumanDecision, Ticket, utc_now
from .tools import ToolRegistry

RunStatus = Literal["completed", "awaiting_human"]


@dataclass(frozen=True)
class RunResult:
    """What came back from one invocation."""

    status: RunStatus
    state: dict[str, Any]
    gate: GateRequest | None = None

    @property
    def reply(self) -> str:
        return str(self.state.get("reply", ""))

    @property
    def outcome(self) -> str:
        return str(self.state.get("outcome", ""))

    @property
    def trail(self) -> list[str]:
        return list(self.state.get("trail", []))


class SupportRuntime:
    """Owns the compiled graph and the durable stores behind it."""

    def __init__(
        self,
        *,
        llm: LLMCallable,
        db_path: str | Path | None = None,
        registry: ToolRegistry | None = None,
        effects: EffectStore | None = None,
        approvals: ApprovalLedger | None = None,
        clock: Callable[[], datetime] = utc_now,
        min_confidence: float = 0.5,
        crash_hook: CrashHook = no_crash,
    ) -> None:
        self.db_path = str(db_path) if db_path is not None else None
        self._closers: list[Callable[[], None]] = []

        if self.db_path is not None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.db_path, check_same_thread=False)
            self._closers.append(connection.close)
            checkpointer: Any = SqliteSaver(connection)
            sqlite_effects = SqliteEffectStore(self.db_path)
            sqlite_approvals = SqliteApprovalLedger(self.db_path)
            self._closers.extend([sqlite_effects.close, sqlite_approvals.close])
            store: EffectStore = effects if effects is not None else sqlite_effects
            ledger: ApprovalLedger = approvals if approvals is not None else sqlite_approvals
        else:
            checkpointer = InMemorySaver()
            store = effects if effects is not None else InMemoryEffectStore()
            ledger = approvals if approvals is not None else InMemoryApprovalLedger()

        self.effects: EffectStore = store
        self.approvals: ApprovalLedger = ledger
        self.deps = GraphDeps(
            llm=llm,
            registry=registry or ToolRegistry(),
            effects=store,
            approvals=ledger,
            clock=clock,
            min_confidence=min_confidence,
            crash_hook=crash_hook,
        )
        self.graph: CompiledStateGraph[Any, Any, Any, Any] = build_graph(self.deps).compile(
            checkpointer=checkpointer
        )

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        for closer in self._closers:
            closer()
        self._closers.clear()

    def __enter__(self) -> "SupportRuntime":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- invocation ------------------------------------------------------

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _wrap(raw: dict[str, Any]) -> RunResult:
        interrupts = raw.get("__interrupt__") or []
        state = {key: value for key, value in raw.items() if not key.startswith("__")}
        if interrupts:
            gate = GateRequest.model_validate(interrupts[0].value)
            return RunResult(status="awaiting_human", state=state, gate=gate)
        return RunResult(status="completed", state=state)

    def start(self, ticket: Ticket, thread_id: str) -> RunResult:
        """Begin a ticket. Raises whatever the nodes raise, crashes included."""
        payload = {
            "ticket_id": ticket.id,
            "text": ticket.text,
            "account_tier": ticket.account_tier,
            "trail": [],
        }
        return self._wrap(self.graph.invoke(payload, self._config(thread_id), durability="sync"))

    def resume_with_decision(self, thread_id: str, decision: HumanDecision) -> RunResult:
        """Hand a human decision back to a waiting gate."""
        return self._wrap(
            self.graph.invoke(
                Command(resume=decision.model_dump(mode="json")),
                self._config(thread_id),
                durability="sync",
            )
        )

    def resume_with_payload(self, thread_id: str, payload: Any) -> RunResult:
        """Hand back whatever a caller actually sent, valid or not.

        The typed method above is what an application should call. This one
        exists because the gate's own validation has to be exercised with real
        rubbish, and a test that can only send well formed input is not
        testing the validation.
        """
        return self._wrap(
            self.graph.invoke(Command(resume=payload), self._config(thread_id), durability="sync")
        )

    def resume(self, thread_id: str) -> RunResult:
        """Pick a thread back up from its last checkpoint after a crash."""
        return self._wrap(self.graph.invoke(None, self._config(thread_id), durability="sync"))

    def snapshot(self, thread_id: str) -> dict[str, Any]:
        """Current committed state on disk, independent of any in-flight run."""
        return dict(self.graph.get_state(self._config(thread_id)).values)


Decider = Callable[[GateRequest], HumanDecision]
RuntimeFactory = Callable[[CrashHook], SupportRuntime]


@dataclass(frozen=True)
class DriveOutcome:
    """The end of a ticket plus what happened on the way."""

    result: RunResult
    crashes: int = 0
    gates: list[GateRequest] = field(default_factory=list)


def drive(
    *,
    runtime_factory: RuntimeFactory,
    ticket: Ticket,
    thread_id: str,
    decider: Decider,
    crash_hook: CrashHook = no_crash,
    max_steps: int = 16,
) -> DriveOutcome:
    """Run a ticket to completion, surviving gates and crashes.

    The factory is called again after every crash so the resumed leg runs on a
    fresh runtime with fresh connections, which is as close to a new process as
    a test can get without paying for one. State is never carried across in
    memory: the resumed runtime knows only what is on disk.

    Args:
        runtime_factory: builds a runtime given a crash hook.
        ticket: the ticket to run.
        thread_id: conversation id, and the namespace for idempotency keys.
        decider: supplies a human decision whenever the graph stops at a gate.
        crash_hook: injected into the first runtime only.
        max_steps: guard against a graph that will not settle.
    """
    runtime = runtime_factory(crash_hook)
    live: list[SupportRuntime] = [runtime]
    pending: Any = "start"
    crashes = 0
    gates: list[GateRequest] = []

    try:
        for _ in range(max_steps):
            try:
                if pending == "start":
                    result = runtime.start(ticket, thread_id)
                elif pending == "resume":
                    result = runtime.resume(thread_id)
                else:
                    result = runtime.resume_with_decision(thread_id, pending)
            except SimulatedCrash:
                crashes += 1
                runtime.close()
                runtime = runtime_factory(no_crash)
                live.append(runtime)
                pending = "resume"
                continue

            if result.status == "completed":
                return DriveOutcome(result=result, crashes=crashes, gates=gates)

            assert result.gate is not None
            gates.append(result.gate)
            pending = decider(result.gate)

        raise RuntimeError(f"ticket {ticket.id} did not settle within {max_steps} steps")
    finally:
        for item in live:
            item.close()
