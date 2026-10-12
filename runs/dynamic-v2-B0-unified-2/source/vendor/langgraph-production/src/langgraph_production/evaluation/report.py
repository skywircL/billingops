"""Aggregation and the gate that fails the build.

Thresholds live in a JSON file, not in the code, so tightening one is a
reviewable change with a diff rather than an edit somebody makes on a branch
and forgets. The gate is deliberately asymmetric:

- ``max_interrupt_miss_rate`` defaults to zero. Executing something
  irreversible without the stop it was supposed to get is not a metric to
  trade off.
- ``max_interrupt_fatigue_rate`` has a budget. Over stopping is a real cost
  but a recoverable one, and pretending a stand in classifier will never over
  stop would just mean a threshold nobody believes.

The resume thresholds are rates rather than counts, on purpose. A count would
be right for one ``--resume-limit`` and wrong for every other one, and a gate
that only holds at the default is a gate that stops holding the first time
somebody runs the harness differently. They exist because "no mismatches"
passes trivially on a sweep that reached no boundaries at all, and a harness
that quietly stopped testing is the failure this file is supposed to catch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..llm.base import LLMCallable
from ..llm.offline import OfflineSupportLLM
from ..tools import ToolRegistry
from .fixtures import TicketFixture, check_consistency, load_fixtures, route_counts
from .interrupts import InterruptReport, evaluate_interrupts
from .resume import ResumeReport, evaluate_resume
from .routing import RoutingReport, evaluate_routing

DEFAULT_THRESHOLDS = Path(__file__).resolve().parents[3] / "evals" / "thresholds.json"


@dataclass(frozen=True)
class Thresholds:
    min_routing_accuracy: float = 0.85
    min_macro_f1: float = 0.80
    min_tool_accuracy: float = 0.85
    min_arg_accuracy: float = 0.85
    max_interrupt_miss_rate: float = 0.0
    max_interrupt_fatigue_rate: float = 0.15
    require_resume_determinism: bool = True
    min_resume_reached_rate: float = 0.60
    min_resume_deterministic_rate: float = 0.75
    max_resume_in_doubt_rate: float = 0.25

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Thresholds":
        known = {field: payload[field] for field in cls.__dataclass_fields__ if field in payload}
        unknown = set(payload) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown threshold(s): {', '.join(sorted(unknown))}")
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


def load_thresholds(path: str | Path | None = None) -> Thresholds:
    """Read thresholds from JSON, falling back to the defaults above.

    An explicitly named file that does not exist is an error. Silently running
    the defaults because a path was mistyped is how a gate ends up looser than
    the person who set it believes.
    """
    if path is not None:
        explicit = Path(path)
        if not explicit.exists():
            raise FileNotFoundError(explicit)
        return Thresholds.from_dict(json.loads(explicit.read_text(encoding="utf-8")))

    for candidate in (DEFAULT_THRESHOLDS, Path.cwd() / "evals" / "thresholds.json"):
        if candidate.exists():
            return Thresholds.from_dict(json.loads(candidate.read_text(encoding="utf-8")))
    return Thresholds()


@dataclass(frozen=True)
class EvalReport:
    routing: RoutingReport
    interrupts: InterruptReport
    resume: ResumeReport | None
    thresholds: Thresholds
    fixture_problems: tuple[str, ...] = ()
    fixture_counts: dict[str, int] | None = None

    @property
    def failures(self) -> tuple[str, ...]:
        checks: list[str] = []
        limits = self.thresholds

        if self.fixture_problems:
            checks.append(f"fixture file is inconsistent: {len(self.fixture_problems)} problem(s)")

        if self.routing.accuracy < limits.min_routing_accuracy:
            checks.append(
                f"routing accuracy {self.routing.accuracy:.3f} below "
                f"{limits.min_routing_accuracy:.3f}"
            )
        if self.routing.macro_f1 < limits.min_macro_f1:
            checks.append(
                f"routing macro F1 {self.routing.macro_f1:.3f} below {limits.min_macro_f1:.3f}"
            )
        if self.routing.tool_accuracy < limits.min_tool_accuracy:
            checks.append(
                f"tool selection accuracy {self.routing.tool_accuracy:.3f} below "
                f"{limits.min_tool_accuracy:.3f}"
            )
        if self.routing.arg_accuracy < limits.min_arg_accuracy:
            checks.append(
                f"tool call accuracy {self.routing.arg_accuracy:.3f} below "
                f"{limits.min_arg_accuracy:.3f} counting arguments: "
                f"{', '.join(item.ticket_id for item in self.routing.mistakes if item.kind.endswith('wrong arguments'))}"
            )
        if self.interrupts.miss_rate > limits.max_interrupt_miss_rate:
            checks.append(
                f"missed interrupt rate {self.interrupts.miss_rate:.3f} above "
                f"{limits.max_interrupt_miss_rate:.3f}: "
                f"{', '.join(case.ticket_id for case in self.interrupts.missed)}"
            )
        if self.interrupts.fatigue_rate > limits.max_interrupt_fatigue_rate:
            checks.append(
                f"approval fatigue rate {self.interrupts.fatigue_rate:.3f} above "
                f"{limits.max_interrupt_fatigue_rate:.3f}: "
                f"{', '.join(case.ticket_id for case in self.interrupts.spurious)}"
            )
        if self.interrupts.errors:
            checks.append(f"{len(self.interrupts.errors)} ticket(s) raised during the interrupt sweep")
        if limits.require_resume_determinism and self.resume is not None:
            checks.extend(self._resume_failures(self.resume, limits))
        return tuple(checks)

    @staticmethod
    def _resume_failures(resume: ResumeReport, limits: Thresholds) -> list[str]:
        """Everything the crash sweep can fail on.

        The first check is the obvious one. The other three are there because
        the first one passes on a report that measured nothing: a sweep whose
        boundaries all read ``not_reached``, or one where every resume ended in
        doubt, has no mismatches to find and would sail through.
        """
        checks: list[str] = []
        if not resume.clean:
            checks.append(
                f"resume sweep found {len(resume.mismatches)} mismatch(es), "
                f"{len(resume.duplicates)} duplicate effect(s), "
                f"{len(resume.errors)} error(s)"
            )
        if resume.reached_rate < limits.min_resume_reached_rate:
            checks.append(
                f"resume sweep reached {len(resume.reached)} of {len(resume.boundaries)} "
                f"boundaries ({resume.reached_rate:.3f}), below "
                f"{limits.min_resume_reached_rate:.3f}: the sweep is not exercising the graph"
            )
        if resume.deterministic_rate < limits.min_resume_deterministic_rate:
            checks.append(
                f"resume determinism {resume.deterministic_rate:.3f} below "
                f"{limits.min_resume_deterministic_rate:.3f}: "
                f"{len(resume.deterministic)} of {len(resume.reached)} reached boundaries "
                "came back identical"
            )
        if resume.in_doubt_rate > limits.max_resume_in_doubt_rate:
            checks.append(
                f"in doubt rate {resume.in_doubt_rate:.3f} above "
                f"{limits.max_resume_in_doubt_rate:.3f}: {len(resume.in_doubt)} of "
                f"{len(resume.reached)} reached boundaries could not be resumed"
            )
        return checks

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failures": list(self.failures),
            "thresholds": self.thresholds.to_dict(),
            "fixtures": self.fixture_counts or {},
            "fixture_problems": list(self.fixture_problems),
            "routing": self.routing.to_dict(),
            "interrupts": self.interrupts.to_dict(),
            "resume": self.resume.to_dict() if self.resume else None,
        }

    def render(self) -> str:
        counts = self.fixture_counts or {}
        header = [
            "=" * 72,
            "SUPPORT GRAPH EVALUATION",
            "=" * 72,
            "  fixtures      "
            + ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))
            + f"  (total {sum(counts.values())})",
            "",
        ]
        blocks = [self.routing.render(), "", self.interrupts.render()]
        if self.resume is not None:
            blocks.extend(["", self.resume.render()])
        else:
            blocks.extend(["", "RESUME", "  skipped"])

        footer = ["", "-" * 72]
        if self.passed:
            footer.append("GATE: pass")
        else:
            footer.append("GATE: fail")
            for reason in self.failures:
                footer.append(f"  {reason}")
        footer.append("-" * 72)
        return "\n".join(header + blocks + footer)


def run_evaluation(
    *,
    fixtures: Sequence[TicketFixture] | None = None,
    fixtures_path: str | Path | None = None,
    thresholds: Thresholds | None = None,
    llm: LLMCallable | None = None,
    registry: ToolRegistry | None = None,
    min_confidence: float = 0.5,
    resume_limit: int = 16,
    skip_resume: bool = False,
) -> EvalReport:
    """Run every measurement and apply the gate."""
    tools = registry or ToolRegistry()
    cases = tuple(fixtures) if fixtures is not None else load_fixtures(fixtures_path)
    model: LLMCallable = llm if llm is not None else OfflineSupportLLM()
    limits = thresholds if thresholds is not None else load_thresholds()

    routing = evaluate_routing(
        cases, llm=model, registry=tools, min_confidence=min_confidence
    )
    interrupts = evaluate_interrupts(
        cases, llm=model, registry=tools, min_confidence=min_confidence
    )
    resume = (
        None
        if skip_resume
        else evaluate_resume(
            cases,
            llm=model,
            registry=tools,
            min_confidence=min_confidence,
            limit=resume_limit,
        )
    )

    return EvalReport(
        routing=routing,
        interrupts=interrupts,
        resume=resume,
        thresholds=limits,
        fixture_problems=check_consistency(cases, tools),
        fixture_counts=route_counts(cases),
    )
