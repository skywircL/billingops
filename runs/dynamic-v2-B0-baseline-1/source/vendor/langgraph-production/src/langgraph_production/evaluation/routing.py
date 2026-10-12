"""Routing accuracy, per branch, with the confusion matrix that explains it.

Accuracy alone hides the shape of the errors, and the shape is what matters
here: sending an escalation to the answer branch is a different kind of wrong
from sending an answer to the escalation branch. The first one ships a guess
to a customer, the second one costs a reviewer thirty seconds. A single
percentage treats them as equal; the matrix does not.

Tool choice is scored twice over, and the second one is the one that matters.
Naming ``issue_refund`` is the easy half; naming it with the right amount is
where the money is. A ticket asking for the difference back on a ninety dollar
charge, answered with a ninety dollar refund, picked the right tool and would
score a clean 1.0 on any metric that stops at the tool name.

Routing is measured on the classify node alone. No tool runs, no gate opens,
nothing is checkpointed. That makes the whole routing sweep cheap enough to
run on every commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from pydantic import ValidationError

from ..llm.base import LLMCallable, LLMRequest
from ..models import Route, summarise
from ..router import build_system_prompt, build_user_prompt, parse_router_output
from ..tools import ToolRegistry, ToolSpec
from .fixtures import TicketFixture

ROUTES: tuple[Route, ...] = ("answer", "tool", "escalate")


def canonical_args(spec: ToolSpec, args: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Arguments as the tool itself would read them, or None when unusable.

    Compared after parsing rather than as raw JSON, for the same reason the
    idempotency key is: 4500 and "4500" are one amount, and a comparison that
    calls them different would report mistakes that are not there.
    """
    if args is None:
        return None
    try:
        return spec.validate_args(args).model_dump(mode="json")
    except ValidationError:
        return None


@dataclass(frozen=True)
class BranchMetrics:
    """Precision, recall, and F1 for one branch."""

    branch: str
    support: int
    predicted: int
    true_positive: int

    @property
    def precision(self) -> float:
        return self.true_positive / self.predicted if self.predicted else 0.0

    @property
    def recall(self) -> float:
        return self.true_positive / self.support if self.support else 0.0

    @property
    def f1(self) -> float:
        precision, recall = self.precision, self.recall
        return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


@dataclass(frozen=True)
class RoutingMistake:
    """One wrong answer, with enough context to argue about the label."""

    ticket_id: str
    text: str
    expected_route: str
    actual_route: str
    expected_tool: str | None
    actual_tool: str | None
    fallback_reason: str | None
    note: str
    expected_args: dict[str, Any] | None = None
    actual_args: dict[str, Any] | None = None

    @property
    def kind(self) -> str:
        if self.expected_route != self.actual_route:
            return f"{self.expected_route} -> {self.actual_route}"
        if self.expected_tool != self.actual_tool:
            return f"{self.expected_route}: wrong tool"
        return f"{self.expected_route}: wrong arguments"


@dataclass(frozen=True)
class RoutingReport:
    total: int
    correct: int
    confusion: dict[str, dict[str, int]]
    per_branch: dict[str, BranchMetrics]
    tool_total: int
    tool_correct: int
    arg_correct: int = 0
    mistakes: tuple[RoutingMistake, ...] = ()
    fallbacks: dict[str, int] = field(default_factory=dict)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def macro_f1(self) -> float:
        if not self.per_branch:
            return 0.0
        return sum(metric.f1 for metric in self.per_branch.values()) / len(self.per_branch)

    @property
    def tool_accuracy(self) -> float:
        return self.tool_correct / self.tool_total if self.tool_total else 0.0

    @property
    def arg_accuracy(self) -> float:
        """Right tool *and* right arguments, over the tickets that need a tool.

        The strict one. A wrong tool is also wrong arguments, so this can only
        be lower than :attr:`tool_accuracy`, never higher.
        """
        return self.arg_correct / self.tool_total if self.tool_total else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "correct": self.correct,
            "accuracy": round(self.accuracy, 4),
            "macro_f1": round(self.macro_f1, 4),
            "tool_selection": {
                "total": self.tool_total,
                "correct": self.tool_correct,
                "accuracy": round(self.tool_accuracy, 4),
            },
            "tool_arguments": {
                "total": self.tool_total,
                "correct": self.arg_correct,
                "accuracy": round(self.arg_accuracy, 4),
            },
            "confusion": self.confusion,
            "per_branch": {
                name: {
                    "support": metric.support,
                    "predicted": metric.predicted,
                    "precision": round(metric.precision, 4),
                    "recall": round(metric.recall, 4),
                    "f1": round(metric.f1, 4),
                }
                for name, metric in self.per_branch.items()
            },
            "fallbacks": self.fallbacks,
            "mistakes": [
                {
                    "ticket_id": item.ticket_id,
                    "kind": item.kind,
                    "expected_tool": item.expected_tool,
                    "actual_tool": item.actual_tool,
                    "expected_args": item.expected_args,
                    "actual_args": item.actual_args,
                    "fallback_reason": item.fallback_reason,
                    "note": item.note,
                }
                for item in self.mistakes
            ],
        }

    def render(self) -> str:
        lines = [
            "ROUTING",
            f"  accuracy      {self.correct}/{self.total} = {self.accuracy:.1%}",
            f"  macro F1      {self.macro_f1:.3f}",
            f"  tool choice   {self.tool_correct}/{self.tool_total} correct"
            f" ({self.tool_accuracy:.1%}) on tickets that should call a tool",
            f"  tool call     {self.arg_correct}/{self.tool_total} correct"
            f" ({self.arg_accuracy:.1%}) counting the arguments, not just the name",
            "",
            "  confusion matrix (rows expected, columns predicted)",
            "               " + "".join(f"{route:>10}" for route in ROUTES),
        ]
        for expected in ROUTES:
            row = self.confusion[expected]
            lines.append(f"  {expected:<12}" + "".join(f"{row[actual]:>10}" for actual in ROUTES))
        lines.append("")
        lines.append("  per branch")
        for name in ROUTES:
            metric = self.per_branch[name]
            lines.append(
                f"    {name:<10} support {metric.support:>3}   precision {metric.precision:.3f}"
                f"   recall {metric.recall:.3f}   f1 {metric.f1:.3f}"
            )
        if self.mistakes:
            lines.append("")
            lines.append(f"  mistakes ({len(self.mistakes)})")
            for item in self.mistakes:
                lines.append(f"    {item.ticket_id}  {item.kind}")
                lines.append(f"      {item.text}")
                if item.kind.endswith("wrong arguments"):
                    lines.append(
                        f"      expected {summarise(item.expected_tool or '', item.expected_args or {})}"
                    )
                    lines.append(
                        f"      proposed {summarise(item.actual_tool or '', item.actual_args or {})}"
                    )
                lines.append(f"      label rationale: {item.note}")
        return "\n".join(lines)


def evaluate_routing(
    fixtures: Sequence[TicketFixture],
    *,
    llm: LLMCallable,
    registry: ToolRegistry | None = None,
    min_confidence: float = 0.5,
) -> RoutingReport:
    """Run the classify step over every fixture and score it."""
    tools = registry or ToolRegistry()
    system = build_system_prompt(tools)

    confusion: dict[str, dict[str, int]] = {
        expected: {actual: 0 for actual in ROUTES} for expected in ROUTES
    }
    fallbacks: dict[str, int] = {}
    mistakes: list[RoutingMistake] = []
    correct = 0
    tool_total = 0
    tool_correct = 0
    arg_correct = 0

    for fixture in fixtures:
        raw = llm(
            LLMRequest(
                tag="route",
                system=system,
                user=build_user_prompt(
                    ticket_id=fixture.id,
                    text=fixture.text,
                    account_tier=fixture.account_tier,
                ),
            )
        )
        decision = parse_router_output(raw, registry=tools, min_confidence=min_confidence)
        actual_tool = decision.tool_call.name if decision.tool_call else None
        confusion[fixture.expected_route][decision.route] += 1
        if decision.fallback_reason:
            fallbacks[decision.fallback_reason] = fallbacks.get(decision.fallback_reason, 0) + 1

        tool_ok = actual_tool == fixture.expected_tool
        expected_args: dict[str, Any] | None = None
        actual_args: dict[str, Any] | None = None
        if tool_ok and fixture.expected_tool is not None:
            spec = tools.require(fixture.expected_tool)
            expected_args = canonical_args(spec, fixture.expected_args)
            actual_args = canonical_args(
                spec, decision.tool_call.args if decision.tool_call else None
            )
        args_ok = tool_ok and expected_args == actual_args

        if fixture.expected_route == "tool":
            tool_total += 1
            if tool_ok:
                tool_correct += 1
            if args_ok:
                arg_correct += 1

        route_ok = decision.route == fixture.expected_route
        if route_ok and tool_ok and args_ok:
            correct += 1
        else:
            mistakes.append(
                RoutingMistake(
                    ticket_id=fixture.id,
                    text=fixture.text,
                    expected_route=fixture.expected_route,
                    actual_route=decision.route,
                    expected_tool=fixture.expected_tool,
                    actual_tool=actual_tool,
                    fallback_reason=decision.fallback_reason,
                    note=fixture.note,
                    expected_args=expected_args if expected_args is not None else fixture.expected_args,
                    actual_args=actual_args
                    if actual_args is not None
                    else (decision.tool_call.args if decision.tool_call else None),
                )
            )

    per_branch = {
        route: BranchMetrics(
            branch=route,
            support=sum(confusion[route].values()),
            predicted=sum(confusion[expected][route] for expected in ROUTES),
            true_positive=confusion[route][route],
        )
        for route in ROUTES
    }

    return RoutingReport(
        total=len(fixtures),
        correct=correct,
        confusion=confusion,
        per_branch=per_branch,
        tool_total=tool_total,
        tool_correct=tool_correct,
        arg_correct=arg_correct,
        mistakes=tuple(mistakes),
        fallbacks=fallbacks,
    )
