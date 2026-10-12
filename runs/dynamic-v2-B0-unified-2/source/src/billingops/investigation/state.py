"""Graph state: plain JSON values only, so a checkpoint outlives the code that wrote it."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from ..config import MAX_CLARIFICATIONS, MAX_INVESTIGATION_CALLS, MAX_READS, MAX_REPAIRS


class CaseState(TypedDict, total=False):
    case_id: str
    customer_id: str
    text: str
    mode: str
    trail: Annotated[list[str], operator.add]

    extracted: dict[str, Any] | None
    extraction_fallback: bool
    messages: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    budgets: dict[str, int]
    usage: list[dict[str, Any]]
    protocol_errors: list[dict[str, Any]]
    clarifications: list[dict[str, Any]]
    pending_question: str | None
    pending_tool: dict[str, Any] | None
    consumed_commands: list[str]
    extra_info: list[str]

    draft: dict[str, Any] | None
    verdict: dict[str, Any] | None
    conclusion: dict[str, Any] | None
    proposal: dict[str, Any] | None
    proposal_version: int
    invalidated_proposals: list[dict[str, Any]]
    gate: dict[str, Any] | None
    approval: dict[str, Any] | None
    gate_rejection: str | None
    gate_rejections: list[str]
    followup: dict[str, Any] | None
    precheck: dict[str, Any] | None
    blocked_proposals: list[dict[str, Any]]

    outcome: str | None
    escalation_reason: str | None


def fresh_budgets() -> dict[str, int]:
    return {"reads": 0, "clarifications": 0, "model_calls": 0, "repairs": 0}


BUDGET_LIMITS = {
    "reads": MAX_READS,
    "clarifications": MAX_CLARIFICATIONS,
    "model_calls": MAX_INVESTIGATION_CALLS,
    "repairs": MAX_REPAIRS,
}


def budget_left(budgets: dict[str, int], name: str) -> int:
    return BUDGET_LIMITS[name] - int(budgets.get(name, 0))
