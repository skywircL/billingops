"""Kill it at every boundary, bring it back, and check nothing moved twice.

For each scenario the harness runs the ticket once with no faults to get a
baseline, then runs it again once per crash point. Every crashed run continues
on a **new runtime with new connections**, so the resumed leg knows only what
is on disk.

Two things are asserted for every boundary the crash actually reached:

1. The final state matches the baseline. One field is allowed to differ,
   ``receipt.replayed``, because its entire job is to record that a resume
   happened. The comparison reports it rather than hiding it.
2. The effect handler ran exactly as many times as in the baseline, counted in
   a table the crash cannot roll back.

Three boundaries are expected not to be deterministic, and all three sit
inside the effect: ``act:claimed`` (the key is taken, the handler has not been
called), ``act:executed`` (the handler returned, the execution row has not
landed) and ``act:recorded`` (the execution row landed, the receipt did not).
A resume in any of them stops with
:class:`~langgraph_production.effects.EffectInDoubt` instead of guessing.
They are reported in their own bucket, with the execution count, rather than
being counted as passes or hidden as failures.

The execution count in that bucket is what the ledger can prove, not what
happened. A crash at ``act:executed`` ran the handler and left no row, so it
reports zero. Reporting the convenient number instead would make this sweep a
worse witness than no sweep at all.
"""

from __future__ import annotations

import copy
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..effects import EffectInDoubt
from ..faults import CRASH_POINTS, CrashInjector, no_crash
from ..llm.base import LLMCallable
from ..models import GateRequest, HumanDecision
from ..runtime import SupportRuntime, drive
from ..tools import ToolRegistry
from .fixtures import TicketFixture

FROZEN_CLOCK = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
"""Time is injected and frozen so two runs of the same ticket are comparable.

Production passes the real clock. A determinism check that has to make
allowances for timestamps is not checking determinism.
"""

EVAL_APPROVER = "eval.harness"


def approve_everything(gate: GateRequest) -> HumanDecision:
    """The decider used by this harness. It approves, so ``act`` is exercised."""
    return HumanDecision(
        token=gate.token,
        approver=EVAL_APPROVER,
        decision="approve",
        note="approved by the evaluation harness",
        decided_at=FROZEN_CLOCK,
    )


def _comparable(state: dict[str, Any]) -> dict[str, Any]:
    """Copy of the state with the one field a resume is allowed to change."""
    trimmed = copy.deepcopy(state)
    receipt = trimmed.get("receipt")
    if isinstance(receipt, dict):
        receipt.pop("replayed", None)
    return trimmed


def _diff_keys(left: dict[str, Any], right: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key)))


@dataclass(frozen=True)
class BoundaryResult:
    ticket_id: str
    crash_point: str
    status: str
    """One of: deterministic, mismatch, duplicate_effect, in_doubt, not_reached, error."""
    detail: str = ""
    executions: int = 0


@dataclass(frozen=True)
class ScenarioResult:
    ticket_id: str
    baseline_trail: tuple[str, ...]
    baseline_executions: int
    boundaries: tuple[BoundaryResult, ...]


@dataclass(frozen=True)
class ResumeReport:
    scenarios: tuple[ScenarioResult, ...]

    @property
    def boundaries(self) -> tuple[BoundaryResult, ...]:
        return tuple(item for scenario in self.scenarios for item in scenario.boundaries)

    def _with_status(self, status: str) -> tuple[BoundaryResult, ...]:
        return tuple(item for item in self.boundaries if item.status == status)

    @property
    def reached(self) -> tuple[BoundaryResult, ...]:
        return tuple(item for item in self.boundaries if item.status != "not_reached")

    @property
    def deterministic(self) -> tuple[BoundaryResult, ...]:
        return self._with_status("deterministic")

    @property
    def mismatches(self) -> tuple[BoundaryResult, ...]:
        return self._with_status("mismatch")

    @property
    def duplicates(self) -> tuple[BoundaryResult, ...]:
        return self._with_status("duplicate_effect")

    @property
    def in_doubt(self) -> tuple[BoundaryResult, ...]:
        return self._with_status("in_doubt")

    @property
    def errors(self) -> tuple[BoundaryResult, ...]:
        return self._with_status("error")

    @property
    def clean(self) -> bool:
        return not (self.mismatches or self.duplicates or self.errors)

    @property
    def reached_rate(self) -> float:
        """Share of swept boundaries the crash actually landed on.

        The number that catches a sweep which stopped sweeping. A harness
        reporting nothing but ``not_reached`` has stopped being evidence, and
        it looks exactly like a clean run to any check that only counts
        failures.
        """
        return len(self.reached) / len(self.boundaries) if self.boundaries else 0.0

    @property
    def deterministic_rate(self) -> float:
        return len(self.deterministic) / len(self.reached) if self.reached else 0.0

    @property
    def in_doubt_rate(self) -> float:
        return len(self.in_doubt) / len(self.reached) if self.reached else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "scenarios": len(self.scenarios),
            "boundaries_swept": len(self.boundaries),
            "boundaries_reached": len(self.reached),
            "reached_rate": round(self.reached_rate, 4),
            "deterministic": len(self.deterministic),
            "deterministic_rate": round(self.deterministic_rate, 4),
            "in_doubt_rate": round(self.in_doubt_rate, 4),
            "in_doubt": [item.crash_point for item in self.in_doubt],
            "mismatches": [
                {"ticket_id": item.ticket_id, "crash_point": item.crash_point, "detail": item.detail}
                for item in self.mismatches
            ],
            "duplicate_effects": [
                {"ticket_id": item.ticket_id, "crash_point": item.crash_point, "detail": item.detail}
                for item in self.duplicates
            ],
            "errors": [
                {"ticket_id": item.ticket_id, "crash_point": item.crash_point, "detail": item.detail}
                for item in self.errors
            ],
        }

    def render(self) -> str:
        lines = [
            "RESUME",
            f"  scenarios     {len(self.scenarios)}",
            f"  boundaries    {len(self.reached)} reached of {len(self.boundaries)} swept"
            f" ({self.reached_rate:.1%})",
            f"  deterministic {len(self.deterministic)} identical final state,"
            f" effect count unchanged ({self.deterministic_rate:.1%} of reached)",
            f"  in doubt      {len(self.in_doubt)} stopped and refused to guess"
            f" ({self.in_doubt_rate:.1%} of reached)",
            f"  mismatches    {len(self.mismatches)}",
            f"  duplicates    {len(self.duplicates)}",
        ]
        for item in self.mismatches + self.duplicates + self.errors:
            lines.append(f"    {item.ticket_id} @ {item.crash_point}: {item.detail}")
        if self.in_doubt:
            points = sorted({item.crash_point for item in self.in_doubt})
            executed = sum(item.executions for item in self.in_doubt)
            lines.append(f"  in doubt windows {', '.join(points)}")
            lines.append(
                f"  handler runs the ledger can prove across those: {executed}"
                " (work done whose receipt was lost, never work repeated)"
            )
        return "\n".join(lines)


def select_scenarios(
    fixtures: Sequence[TicketFixture],
    *,
    limit: int,
    registry: ToolRegistry | None = None,
) -> tuple[TicketFixture, ...]:
    """Pick a spread of tickets that between them walk every path.

    Deterministic: same fixtures in, same scenarios out. Round robin across
    the four shapes (answer, read only tool, irreversible tool, escalation) so
    a small sample still covers the gate and the effect.
    """
    tools = registry or ToolRegistry()
    buckets: dict[str, list[TicketFixture]] = {
        "answer": [],
        "read_tool": [],
        "write_tool": [],
        "escalate": [],
    }
    for fixture in fixtures:
        if fixture.expected_route == "answer":
            buckets["answer"].append(fixture)
        elif fixture.expected_route == "escalate":
            buckets["escalate"].append(fixture)
        elif fixture.expected_tool and tools.is_irreversible(fixture.expected_tool):
            buckets["write_tool"].append(fixture)
        else:
            buckets["read_tool"].append(fixture)

    picked: list[TicketFixture] = []
    index = 0
    while len(picked) < limit and any(index < len(items) for items in buckets.values()):
        for name in ("write_tool", "read_tool", "escalate", "answer"):
            items = buckets[name]
            if index < len(items) and len(picked) < limit:
                picked.append(items[index])
        index += 1
    return tuple(picked)


def _run_once(
    *,
    fixture: TicketFixture,
    llm: LLMCallable,
    registry: ToolRegistry | None,
    min_confidence: float,
    crash_point: str | None,
    workdir: Path,
) -> tuple[dict[str, Any], int, CrashInjector | None, Exception | None]:
    """One full drive in its own database. Returns state, executions, injector, error."""
    db_path = workdir / "run.sqlite3"
    injector = CrashInjector(point=crash_point) if crash_point else None

    def factory(hook: Any) -> SupportRuntime:
        return SupportRuntime(
            llm=llm,
            db_path=db_path,
            registry=registry,
            clock=lambda: FROZEN_CLOCK,
            min_confidence=min_confidence,
            crash_hook=hook,
        )

    error: Exception | None = None
    state: dict[str, Any] = {}
    try:
        outcome = drive(
            runtime_factory=factory,
            ticket=fixture.to_ticket(),
            thread_id=f"resume-{fixture.id}",
            decider=approve_everything,
            crash_hook=injector or no_crash,
        )
        state = outcome.result.state
    except Exception as exc:  # noqa: BLE001 - classified by the caller
        error = exc

    inspector = SupportRuntime(llm=llm, db_path=db_path, registry=registry)
    try:
        executions = inspector.effects.total_executions()
    finally:
        inspector.close()
    return state, executions, injector, error


def evaluate_resume(
    fixtures: Sequence[TicketFixture],
    *,
    llm: LLMCallable,
    registry: ToolRegistry | None = None,
    min_confidence: float = 0.5,
    limit: int = 8,
    crash_points: Sequence[str] = CRASH_POINTS,
) -> ResumeReport:
    """Sweep every crash point over a representative set of tickets."""
    scenarios: list[ScenarioResult] = []

    for fixture in select_scenarios(fixtures, limit=limit, registry=registry):
        root = Path(tempfile.mkdtemp(prefix=f"resume-{fixture.id}-"))
        try:
            baseline_dir = root / "baseline"
            baseline_dir.mkdir(parents=True)
            baseline_state, baseline_executions, _, baseline_error = _run_once(
                fixture=fixture,
                llm=llm,
                registry=registry,
                min_confidence=min_confidence,
                crash_point=None,
                workdir=baseline_dir,
            )
            if baseline_error is not None:
                scenarios.append(
                    ScenarioResult(
                        ticket_id=fixture.id,
                        baseline_trail=(),
                        baseline_executions=0,
                        boundaries=(
                            BoundaryResult(
                                ticket_id=fixture.id,
                                crash_point="baseline",
                                status="error",
                                detail=f"{type(baseline_error).__name__}: {baseline_error}",
                            ),
                        ),
                    )
                )
                continue

            expected = _comparable(baseline_state)
            boundaries: list[BoundaryResult] = []

            for point in crash_points:
                run_dir = root / point.replace(":", "_")
                run_dir.mkdir(parents=True)
                state, executions, injector, error = _run_once(
                    fixture=fixture,
                    llm=llm,
                    registry=registry,
                    min_confidence=min_confidence,
                    crash_point=point,
                    workdir=run_dir,
                )

                if injector is None or not injector.did_fire:
                    boundaries.append(
                        BoundaryResult(
                            ticket_id=fixture.id,
                            crash_point=point,
                            status="not_reached",
                            detail="this ticket never passes through that point",
                            executions=executions,
                        )
                    )
                    continue

                if isinstance(error, EffectInDoubt):
                    boundaries.append(
                        BoundaryResult(
                            ticket_id=fixture.id,
                            crash_point=point,
                            status="in_doubt",
                            detail=str(error),
                            executions=executions,
                        )
                    )
                    continue

                if error is not None:
                    boundaries.append(
                        BoundaryResult(
                            ticket_id=fixture.id,
                            crash_point=point,
                            status="error",
                            detail=f"{type(error).__name__}: {error}",
                            executions=executions,
                        )
                    )
                    continue

                if executions != baseline_executions:
                    boundaries.append(
                        BoundaryResult(
                            ticket_id=fixture.id,
                            crash_point=point,
                            status="duplicate_effect",
                            detail=(
                                f"handler ran {executions} times, baseline ran "
                                f"{baseline_executions}"
                            ),
                            executions=executions,
                        )
                    )
                    continue

                differing = _diff_keys(expected, _comparable(state))
                if differing:
                    boundaries.append(
                        BoundaryResult(
                            ticket_id=fixture.id,
                            crash_point=point,
                            status="mismatch",
                            detail=f"state fields differ from baseline: {', '.join(differing)}",
                            executions=executions,
                        )
                    )
                    continue

                boundaries.append(
                    BoundaryResult(
                        ticket_id=fixture.id,
                        crash_point=point,
                        status="deterministic",
                        executions=executions,
                    )
                )

            scenarios.append(
                ScenarioResult(
                    ticket_id=fixture.id,
                    baseline_trail=tuple(baseline_state.get("trail", [])),
                    baseline_executions=baseline_executions,
                    boundaries=tuple(boundaries),
                )
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    return ResumeReport(scenarios=tuple(scenarios))
