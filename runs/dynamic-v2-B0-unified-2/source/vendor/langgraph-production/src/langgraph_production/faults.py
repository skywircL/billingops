"""Deterministic crash injection.

Killing a process for real inside a test suite is slow and flaky. What matters
for durability is not the signal, it is that the node stopped without
committing its writes and that the next attempt starts from the last
checkpoint on disk. Raising at a named point does exactly that, and it is
repeatable.

The graph calls :data:`CrashHook` at every point listed in
:data:`CRASH_POINTS`. In production the hook is :func:`no_crash` and costs a
function call per node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

CrashHook = Callable[[str], None]

CRASH_POINTS: tuple[str, ...] = (
    "classify:enter",
    "classify:exit",
    "plan:enter",
    "plan:exit",
    "gate:enter",
    "gate:resumed",
    "act:enter",
    "act:claimed",
    "act:executed",
    "act:recorded",
    "act:after_effect",
    "compose:enter",
    "compose:exit",
)
"""Every point this package can interrupt a run at, node boundaries included.

Three of them sit inside the effect helper rather than between nodes, because
that is where the windows a checkpoint cannot close actually are:

- ``act:claimed``: the key is claimed, the handler has not been called.
- ``act:executed``: the handler returned, the execution is not on disk yet.
  Indistinguishable from the one above when the process comes back, which is
  the honest reason the in-doubt message for a zero execution count says the
  handler *may* have run.
- ``act:recorded``: the execution is on disk, the receipt is not. A resume
  here can prove the work happened, and says so.

Between nodes a resume is deterministic. Inside these three it is not, and
listing them here is what gets them swept rather than hand waved.
"""


class SimulatedCrash(RuntimeError):
    """Stands in for the process going away."""


def no_crash(point: str) -> None:
    """The production hook."""
    return None


@dataclass
class CrashInjector:
    """Raises at one named point, a fixed number of times."""

    point: str
    times: int = 1
    fired: int = 0
    seen: list[str] = field(default_factory=list)

    def __call__(self, point: str) -> None:
        self.seen.append(point)
        if point == self.point and self.fired < self.times:
            self.fired += 1
            raise SimulatedCrash(point)

    @property
    def did_fire(self) -> bool:
        return self.fired > 0


@dataclass
class RecordingHook:
    """Records the points a run passed through without interfering."""

    seen: list[str] = field(default_factory=list)

    def __call__(self, point: str) -> None:
        self.seen.append(point)
