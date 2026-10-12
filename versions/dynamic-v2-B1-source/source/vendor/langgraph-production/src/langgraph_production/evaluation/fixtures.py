"""Labelled tickets and the policy they were labelled under.

The labelling policy, written down so a reviewer can disagree with it:

1. A ticket that asks for a specific irreversible change **and** carries every
   identifier that change needs is ``tool``, with that tool, and must stop for
   a human.
2. A ticket that asks a factual question about the customer's own records and
   carries the identifier is ``tool``, with the read only tool, and must not
   stop.
3. A ticket that needs judgement (legal, security, commercial), asks for more
   than one irreversible change at once, or is missing an identifier the
   action requires, is ``escalate`` and must stop.
4. Anything answerable from product documentation is ``answer`` and must not
   stop.

A ``tool`` label is not complete without the arguments. "Call issue_refund"
is satisfied by refunding the whole invoice when the customer asked for the
difference, so ``expected_args`` carries what the tool should be called with
and the harness scores it.

``must_interrupt`` follows from the route and the tool rather than being an
independent opinion, and :func:`check_consistency` enforces that, so the
fixture file cannot drift into contradicting itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from ..models import Route, Ticket
from ..tools import ToolRegistry

_RELATIVE_FIXTURES = Path("evals") / "fixtures" / "tickets.jsonl"
DEFAULT_FIXTURES = Path(__file__).resolve().parents[3] / _RELATIVE_FIXTURES


def default_fixture_path() -> Path:
    """Where the fixtures live for a source checkout, or under the working directory.

    An installed wheel has no ``evals`` directory next to the package, so the
    working directory is checked as well before giving up. The CLI takes an
    explicit path either way.
    """
    for candidate in (DEFAULT_FIXTURES, Path.cwd() / _RELATIVE_FIXTURES):
        if candidate.exists():
            return candidate
    return DEFAULT_FIXTURES


class TicketFixture(BaseModel):
    """One labelled ticket.

    ``expected_args`` is not decoration. Naming the right tool with the wrong
    arguments is the mistake that costs money: a refund of the whole invoice
    when the customer asked for the difference reaches a handler, passes a
    human who is reading a summary, and pays out. A label that stops at the
    tool name cannot see it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    text: str
    account_tier: Literal["free", "pro", "enterprise"]
    expected_route: Route
    expected_tool: str | None
    expected_args: dict[str, Any] | None = None
    must_interrupt: bool
    note: str

    def to_ticket(self) -> Ticket:
        return Ticket(id=self.id, text=self.text, account_tier=self.account_tier)


def load_fixtures(path: str | Path | None = None) -> tuple[TicketFixture, ...]:
    """Read a JSONL fixture file. Blank lines are skipped, bad rows raise."""
    source = Path(path) if path is not None else default_fixture_path()
    rows: list[TicketFixture] = []
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            rows.append(TicketFixture.model_validate(json.loads(stripped)))
        except Exception as exc:  # noqa: BLE001 - the line number is the useful part
            raise ValueError(f"{source}:{number}: {exc}") from exc
    if not rows:
        raise ValueError(f"{source} contains no fixtures")
    return tuple(rows)


def check_consistency(
    fixtures: Iterable[TicketFixture],
    registry: ToolRegistry | None = None,
) -> tuple[str, ...]:
    """Return the ways the fixture file contradicts its own policy."""
    tools = registry or ToolRegistry()
    problems: list[str] = []
    seen: set[str] = set()

    for fixture in fixtures:
        if fixture.id in seen:
            problems.append(f"{fixture.id}: duplicate id")
        seen.add(fixture.id)

        if fixture.expected_route == "tool":
            if fixture.expected_tool is None:
                problems.append(f"{fixture.id}: route 'tool' with no expected_tool")
                continue
            if fixture.expected_tool not in tools:
                problems.append(f"{fixture.id}: unknown tool {fixture.expected_tool!r}")
                continue
            if fixture.expected_args is None:
                problems.append(f"{fixture.id}: route 'tool' with no expected_args")
            else:
                try:
                    tools.require(fixture.expected_tool).validate_args(fixture.expected_args)
                except ValidationError as exc:
                    problems.append(
                        f"{fixture.id}: expected_args do not fit {fixture.expected_tool}: "
                        f"{exc.error_count()} error(s)"
                    )
            implied = tools.is_irreversible(fixture.expected_tool)
        elif fixture.expected_route == "escalate":
            if fixture.expected_tool is not None:
                problems.append(f"{fixture.id}: route 'escalate' cannot name a tool")
            if fixture.expected_args is not None:
                problems.append(f"{fixture.id}: route 'escalate' cannot name arguments")
            implied = True
        else:
            if fixture.expected_tool is not None:
                problems.append(f"{fixture.id}: route 'answer' cannot name a tool")
            if fixture.expected_args is not None:
                problems.append(f"{fixture.id}: route 'answer' cannot name arguments")
            implied = False

        if fixture.must_interrupt != implied:
            problems.append(
                f"{fixture.id}: must_interrupt={fixture.must_interrupt} contradicts "
                f"route={fixture.expected_route} tool={fixture.expected_tool}"
            )

    return tuple(problems)


def route_counts(fixtures: Sequence[TicketFixture]) -> dict[str, int]:
    counts: dict[str, int] = {"answer": 0, "tool": 0, "escalate": 0}
    for fixture in fixtures:
        counts[fixture.expected_route] += 1
    return counts
