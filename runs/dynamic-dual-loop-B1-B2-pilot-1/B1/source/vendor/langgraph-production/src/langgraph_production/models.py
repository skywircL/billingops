"""Typed payloads that cross node boundaries.

Every object here is JSON serialisable on purpose. The graph state itself
holds plain dicts (see :mod:`langgraph_production.state`), and these models are
the validation layer at the edges: parse on the way in, dump on the way out.
A checkpoint written today has to be readable by a build shipped six months
from now, and plain JSON is the only shape that survives that.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field

Route = Literal["answer", "tool", "escalate"]
"""What the router decided to do with a ticket."""

GateKind = Literal["approval", "handoff"]
"""Why a human was pulled in: an irreversible action, or a full handoff."""

Outcome = Literal["answered", "acted", "denied", "handed_off", "gate_rejected"]
"""How a run ended."""

RouterFallback = Literal[
    "empty_output",
    "invalid_json",
    "not_an_object",
    "unknown_route",
    "missing_tool",
    "unknown_tool",
    "invalid_tool_args",
    "invalid_confidence",
    "low_confidence",
]
"""Named reasons the router refused to trust the model and escalated instead."""

GateRejection = Literal[
    "malformed_decision",
    "token_mismatch",
    "token_replayed",
]
"""Named reasons an approval was not honoured."""


def utc_now() -> datetime:
    """Default clock. Injected everywhere so tests can freeze it."""
    return datetime.now(timezone.utc)


def canonical_json(payload: Any) -> str:
    """Stable JSON for hashing: sorted keys, no incidental whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def digest(payload: Any, *, length: int = 32) -> str:
    """Short, stable hex digest of any JSON serialisable payload."""
    raw = canonical_json(payload).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:length]


class Ticket(BaseModel):
    """A support request as it enters the graph."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    text: str
    account_tier: Literal["free", "pro", "enterprise"] = "pro"


class ToolCall(BaseModel):
    """A tool the router wants to run, with arguments already validated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class RouterDecision(BaseModel):
    """The router's verdict, including how much of it came from the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: Route
    tool_call: ToolCall | None = None
    confidence: float = 0.0
    rationale: str = ""
    trusted: bool = True
    """False when the model output was rejected and this decision is the fallback."""
    fallback_reason: RouterFallback | None = None


class ArgumentSpec(Protocol):
    """The slice of a tool definition an action needs to build its identity.

    Declared here rather than imported so this module keeps no dependency on
    the tool registry. :class:`langgraph_production.tools.ToolSpec` satisfies
    it structurally.
    """

    irreversible: bool

    def validate_args(self, args: Mapping[str, Any]) -> BaseModel: ...


def summarise(tool: str, args: Mapping[str, Any]) -> str:
    """One line naming the tool and every argument, for a human to read."""
    rendered = ", ".join(f"{key}={value}" for key, value in sorted(args.items()))
    return f"{tool}({rendered})"


class PlannedAction(BaseModel):
    """A tool call promoted to an action, with an identity of its own.

    ``action_id`` is derived from the ticket and the **validated** arguments,
    so the same action planned twice produces the same id, and a different
    amount produces a different one. Everything downstream (idempotency keys,
    approval tokens) hangs off this id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: str
    tool: str
    args: dict[str, Any]
    irreversible: bool
    summary: str

    @classmethod
    def build(cls, *, ticket_id: str, call: ToolCall, spec: ArgumentSpec) -> "PlannedAction":
        """Promote a tool call to an action, keyed by its parsed arguments.

        The arguments are put through the tool's own schema first, and the id
        is the digest of *that* result rather than of the raw JSON a model
        emitted. ``4500``, ``"4500"`` and ``4500.0`` are one refund of forty
        five dollars, and they have to produce one id: the id is the namespace
        of the idempotency key, so any spelling difference that survives to
        here is a second payment.

        Raises:
            pydantic.ValidationError: the arguments do not fit the tool's
                schema. The router refuses those before planning; reaching
                here with invalid arguments is a bug, not a model mistake.
        """
        args = spec.validate_args(call.args).model_dump(mode="json")
        action_id = digest(
            {"ticket": ticket_id, "tool": call.name, "args": args},
            length=16,
        )
        return cls(
            action_id=action_id,
            tool=call.name,
            args=args,
            irreversible=spec.irreversible,
            summary=summarise(call.name, args),
        )


class GateRequest(BaseModel):
    """What a human is shown when the graph stops and asks.

    The ``token`` binds this request to one ticket, one state, and one action.
    An approval carrying a different token authorises nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: GateKind
    token: str
    ticket_id: str
    state_digest: str
    action: PlannedAction | None = None
    question: str
    context: dict[str, Any] = Field(default_factory=dict)


class HumanDecision(BaseModel):
    """What a human sends back. Anything else is refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: str
    approver: str
    decision: Literal["approve", "deny"]
    note: str = ""
    decided_at: datetime


class ApprovalRecord(BaseModel):
    """The audit row: who approved what, when, over which state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: str
    ticket_id: str
    action_id: str | None
    kind: GateKind
    approver: str
    decision: Literal["approve", "deny"]
    note: str
    decided_at: datetime
    state_digest: str

    def fingerprint(self) -> str:
        """Identity of the decision, used to tell a legitimate replay from a reuse.

        The note is part of it. A resumed node presents the decision it was
        given, note included, so an identical presentation still matches. Two
        presentations that differ only in what the reviewer wrote are two
        decisions, and the ledger would otherwise keep the first note and
        report the second one as a legitimate replay of it.
        """
        return digest(
            {
                "token": self.token,
                "ticket_id": self.ticket_id,
                "action_id": self.action_id,
                "approver": self.approver,
                "decision": self.decision,
                "note": self.note,
                "decided_at": self.decided_at.isoformat(),
            },
            length=16,
        )


class EffectReceipt(BaseModel):
    """Proof that an effect happened exactly once.

    ``replayed`` is True when this run asked for an effect that a previous run
    had already performed. The work is not repeated; the original receipt comes
    back instead.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: str
    action_id: str
    tool: str
    result: dict[str, Any]
    performed_at: datetime
    replayed: bool = False
