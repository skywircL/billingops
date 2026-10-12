"""The graph state and typed accessors for it.

The state is a ``TypedDict`` of plain JSON values. Nothing in it is a pydantic
instance, which keeps a checkpoint readable by any version of this package and
by anything else that can open a SQLite file. The accessors below are the only
sanctioned way to read a structured field back out.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from .models import (
    ApprovalRecord,
    EffectReceipt,
    GateRequest,
    PlannedAction,
    RouterDecision,
    digest,
)


class SupportState(TypedDict, total=False):
    """State carried between nodes.

    ``trail`` uses an append reducer so the execution path is recorded without
    a node needing to read the previous value. It doubles as the assertion
    surface for resume determinism: two runs of the same ticket must produce
    the same trail, and a crashed-then-resumed run must not show a node twice.
    """

    ticket_id: str
    text: str
    account_tier: str
    trail: Annotated[list[str], operator.add]
    decision: dict[str, Any]
    action: dict[str, Any] | None
    gate: dict[str, Any] | None
    approval: dict[str, Any] | None
    gate_rejection: str | None
    receipt: dict[str, Any] | None
    reply: str
    outcome: str


def read_decision(state: SupportState) -> RouterDecision | None:
    raw = state.get("decision")
    return RouterDecision.model_validate(raw) if raw else None


def read_action(state: SupportState) -> PlannedAction | None:
    raw = state.get("action")
    return PlannedAction.model_validate(raw) if raw else None


def read_gate(state: SupportState) -> GateRequest | None:
    raw = state.get("gate")
    return GateRequest.model_validate(raw) if raw else None


def read_approval(state: SupportState) -> ApprovalRecord | None:
    raw = state.get("approval")
    return ApprovalRecord.model_validate(raw) if raw else None


def read_receipt(state: SupportState) -> EffectReceipt | None:
    raw = state.get("receipt")
    return EffectReceipt.model_validate(raw) if raw else None


def state_digest(state: SupportState) -> str:
    """Digest of the decision-relevant slice of the state.

    Deliberately narrow: it covers what a human is approving, not the whole
    state. A node re-executing after a resume recomputes the same digest, so
    the approval token stays valid across a crash. A different ticket, a
    different route, or different action arguments produce a different digest,
    so an approval cannot travel.
    """
    return digest(
        {
            "ticket_id": state.get("ticket_id"),
            "text": state.get("text"),
            "decision": state.get("decision"),
            "action": state.get("action"),
        }
    )
