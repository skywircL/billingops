"""Failure injection for the Billing service.

Three scenarios the plan keeps, plus the one knob the controlled concurrency
test needs. Nothing here is a framework: a fault is a row in a dict, armed
over HTTP by a test or a demo script, and consumed when it fires.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


class LostResponse(RuntimeError):
    """Raised after COMMIT so the HTTP layer answers with nothing useful.

    The refund is on disk. The caller sees a 500 with no body, which is
    exactly as informative as a dropped connection: it does not know.
    """


@dataclass
class FaultBox:
    """Armed faults, each consumed a fixed number of times."""

    lost_response_after_commit: int = 0
    verification_unavailable: int = 0
    delay_before_commit_ms: int = 0
    delay_before_commit_uses: int = 0
    reads_unavailable: int = 0
    write_unavailable: int = 0
    """The next n refund writes fail as temporarily unavailable before anything is written (a 503 the caller cannot
    tell apart from a lost commit without verifying)."""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def arm(self, spec: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for key, value in spec.items():
                if not hasattr(self, key) or key.startswith("_"):
                    raise KeyError(key)
                setattr(self, key, int(value))
            return self.snapshot()

    def clear(self) -> None:
        with self._lock:
            self.lost_response_after_commit = 0
            self.verification_unavailable = 0
            self.delay_before_commit_ms = 0
            self.delay_before_commit_uses = 0
            self.reads_unavailable = 0
            self.write_unavailable = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "lost_response_after_commit": self.lost_response_after_commit,
            "verification_unavailable": self.verification_unavailable,
            "delay_before_commit_ms": self.delay_before_commit_ms,
            "delay_before_commit_uses": self.delay_before_commit_uses,
            "reads_unavailable": self.reads_unavailable,
            "write_unavailable": self.write_unavailable,
        }

    # -- hooks the service calls ---------------------------------------

    def before_commit(self) -> None:
        with self._lock:
            if self.delay_before_commit_uses > 0 and self.delay_before_commit_ms > 0:
                self.delay_before_commit_uses -= 1
                delay = self.delay_before_commit_ms / 1000
            else:
                return
        time.sleep(delay)

    def after_commit(self) -> None:
        with self._lock:
            if self.lost_response_after_commit > 0:
                self.lost_response_after_commit -= 1
                fire = True
            else:
                fire = False
        if fire:
            raise LostResponse("response lost after commit")

    def take_verification_unavailable(self) -> bool:
        with self._lock:
            if self.verification_unavailable > 0:
                self.verification_unavailable -= 1
                return True
            return False

    def take_write_unavailable(self) -> bool:
        with self._lock:
            if self.write_unavailable > 0:
                self.write_unavailable -= 1
                return True
            return False

    def take_reads_unavailable(self) -> bool:
        with self._lock:
            if self.reads_unavailable > 0:
                self.reads_unavailable -= 1
                return True
            return False
