"""Did the graph stop when it had to, and did it stop when it must not?

Both directions are failures and they are reported separately, because they
cost different things:

- A **missed** interrupt executed something irreversible, or answered a ticket
  that needed a human, with nobody looking. That is the one that ends up in a
  post mortem.
- A **spurious** interrupt stopped a ticket that did not need stopping. Teams
  call that caution for about two weeks. Then the queue is full of approvals
  that never mattered, reviewers start clicking approve without reading, and
  the gate that was supposed to protect the irreversible action is protecting
  nothing. Counting it as a failure is the only way it stays visible.

Only the first leg of each run is needed: whether the graph came back asking
for a human. Nothing is approved, so nothing irreversible is executed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..llm.base import LLMCallable
from ..runtime import SupportRuntime
from ..tools import ToolRegistry
from .fixtures import TicketFixture


@dataclass(frozen=True)
class InterruptCase:
    ticket_id: str
    text: str
    must_interrupt: bool
    did_interrupt: bool
    gate_kind: str | None
    note: str


@dataclass(frozen=True)
class InterruptReport:
    cases: tuple[InterruptCase, ...]
    errors: tuple[tuple[str, str], ...] = ()

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def required(self) -> tuple[InterruptCase, ...]:
        return tuple(case for case in self.cases if case.must_interrupt)

    @property
    def forbidden(self) -> tuple[InterruptCase, ...]:
        return tuple(case for case in self.cases if not case.must_interrupt)

    @property
    def missed(self) -> tuple[InterruptCase, ...]:
        return tuple(case for case in self.required if not case.did_interrupt)

    @property
    def spurious(self) -> tuple[InterruptCase, ...]:
        return tuple(case for case in self.forbidden if case.did_interrupt)

    @property
    def correct(self) -> int:
        return self.total - len(self.missed) - len(self.spurious)

    @property
    def miss_rate(self) -> float:
        return len(self.missed) / len(self.required) if self.required else 0.0

    @property
    def fatigue_rate(self) -> float:
        return len(self.spurious) / len(self.forbidden) if self.forbidden else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "correct": self.correct,
            "must_interrupt": len(self.required),
            "must_not_interrupt": len(self.forbidden),
            "missed": [case.ticket_id for case in self.missed],
            "spurious": [case.ticket_id for case in self.spurious],
            "miss_rate": round(self.miss_rate, 4),
            "fatigue_rate": round(self.fatigue_rate, 4),
            "errors": [{"ticket_id": tid, "error": msg} for tid, msg in self.errors],
        }

    def render(self) -> str:
        lines = [
            "INTERRUPTS",
            f"  correct       {self.correct}/{self.total}",
            f"  must stop     {len(self.required)} tickets, missed {len(self.missed)}"
            f" (miss rate {self.miss_rate:.1%})",
            f"  must not stop {len(self.forbidden)} tickets, stopped {len(self.spurious)}"
            f" (fatigue rate {self.fatigue_rate:.1%})",
        ]
        if self.missed:
            lines.append("  missed a required stop")
            for case in self.missed:
                lines.append(f"    {case.ticket_id}  {case.text}")
        if self.spurious:
            lines.append("  stopped when it did not need to")
            for case in self.spurious:
                lines.append(f"    {case.ticket_id}  {case.text}")
                lines.append(f"      label rationale: {case.note}")
        if self.errors:
            lines.append("  errors")
            for ticket_id, message in self.errors:
                lines.append(f"    {ticket_id}  {message}")
        return "\n".join(lines)


def evaluate_interrupts(
    fixtures: Sequence[TicketFixture],
    *,
    llm: LLMCallable,
    registry: ToolRegistry | None = None,
    min_confidence: float = 0.5,
) -> InterruptReport:
    """Start every fixture and record whether the graph asked for a human."""
    cases: list[InterruptCase] = []
    errors: list[tuple[str, str]] = []

    runtime = SupportRuntime(llm=llm, registry=registry, min_confidence=min_confidence)
    try:
        for fixture in fixtures:
            try:
                result = runtime.start(fixture.to_ticket(), thread_id=f"eval-int-{fixture.id}")
            except Exception as exc:  # noqa: BLE001 - one bad ticket must not stop the sweep
                errors.append((fixture.id, f"{type(exc).__name__}: {exc}"))
                continue
            stopped = result.status == "awaiting_human"
            cases.append(
                InterruptCase(
                    ticket_id=fixture.id,
                    text=fixture.text,
                    must_interrupt=fixture.must_interrupt,
                    did_interrupt=stopped,
                    gate_kind=result.gate.kind if result.gate else None,
                    note=fixture.note,
                )
            )
    finally:
        runtime.close()

    return InterruptReport(cases=tuple(cases), errors=tuple(errors))
