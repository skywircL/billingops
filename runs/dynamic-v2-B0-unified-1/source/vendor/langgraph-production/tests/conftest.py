from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

import pytest

from langgraph_production.faults import CrashHook, no_crash
from langgraph_production.llm.base import LLMCallable
from langgraph_production.llm.offline import OfflineSupportLLM
from langgraph_production.models import GateRequest, HumanDecision
from langgraph_production.runtime import SupportRuntime
from langgraph_production.tools import ToolRegistry

from samples import FIXED_TIME


@pytest.fixture
def clock() -> Callable[[], datetime]:
    return lambda: FIXED_TIME


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


@pytest.fixture
def offline_llm() -> LLMCallable:
    return OfflineSupportLLM()


@pytest.fixture
def runtime(offline_llm: LLMCallable, clock: Callable[[], datetime]):
    with SupportRuntime(llm=offline_llm, clock=clock) as instance:
        yield instance


@pytest.fixture
def durable_factory(
    tmp_path: Path,
    offline_llm: LLMCallable,
    clock: Callable[[], datetime],
) -> Callable[..., SupportRuntime]:
    """Builds runtimes that all share one SQLite file, like restarts of one service."""
    db_path = tmp_path / "state.sqlite3"

    def factory(hook: CrashHook = no_crash) -> SupportRuntime:
        return SupportRuntime(llm=offline_llm, db_path=db_path, clock=clock, crash_hook=hook)

    return factory


@pytest.fixture
def approve() -> Callable[[GateRequest], HumanDecision]:
    def decider(gate: GateRequest) -> HumanDecision:
        return HumanDecision(
            token=gate.token,
            approver="ops.dana",
            decision="approve",
            note="checked the invoice",
            decided_at=FIXED_TIME,
        )

    return decider


@pytest.fixture
def deny() -> Callable[[GateRequest], HumanDecision]:
    def decider(gate: GateRequest) -> HumanDecision:
        return HumanDecision(
            token=gate.token,
            approver="ops.dana",
            decision="deny",
            note="the customer is not entitled to this",
            decided_at=FIXED_TIME,
        )

    return decider
