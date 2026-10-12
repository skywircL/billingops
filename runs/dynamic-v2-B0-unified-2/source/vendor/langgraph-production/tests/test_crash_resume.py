"""Kill it everywhere, bring it back, prove nothing ran twice.

Each test builds its own SQLite file and its own runtimes. When a run crashes,
the next leg is served by a *new* runtime with new connections, so anything the
resumed graph knows it read back off disk.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable

import pytest

from langgraph_production.effects import EffectInDoubt
from langgraph_production.faults import CRASH_POINTS, CrashInjector, RecordingHook, SimulatedCrash
from langgraph_production.llm.offline import OfflineSupportLLM
from langgraph_production.models import GateRequest, HumanDecision, Ticket
from langgraph_production.runtime import SupportRuntime, drive

from samples import ANSWER_TICKET, FIXED_TIME, LOOKUP_TICKET, REFUND_TICKET


def make_factory(db_path: Path) -> Callable[..., SupportRuntime]:
    def factory(hook: Any = None) -> SupportRuntime:
        kwargs: dict[str, Any] = {
            "llm": OfflineSupportLLM(),
            "db_path": db_path,
            "clock": lambda: FIXED_TIME,
        }
        if hook is not None:
            kwargs["crash_hook"] = hook
        return SupportRuntime(**kwargs)

    return factory


def approver(gate: GateRequest) -> HumanDecision:
    return HumanDecision(
        token=gate.token,
        approver="ops.dana",
        decision="approve",
        note="checked",
        decided_at=FIXED_TIME,
    )


def comparable(state: dict[str, Any]) -> dict[str, Any]:
    """Everything except the one flag whose job is to say a replay happened."""
    trimmed = copy.deepcopy(state)
    receipt = trimmed.get("receipt")
    if isinstance(receipt, dict):
        receipt.pop("replayed", None)
    return trimmed


def executions(db_path: Path) -> int:
    runtime = make_factory(db_path)()
    try:
        return runtime.effects.total_executions()
    finally:
        runtime.close()


def run(db_path: Path, ticket: Ticket, thread_id: str, crash_point: str | None = None):
    injector = CrashInjector(point=crash_point) if crash_point else None
    outcome = drive(
        runtime_factory=make_factory(db_path),
        ticket=ticket,
        thread_id=thread_id,
        decider=approver,
        crash_hook=injector or (lambda _point: None),
    )
    return outcome, injector


THREAD = "support-thread-1"
"""Baseline and crashed runs share a thread id on purpose.

Tokens and idempotency keys are namespaced by the thread, so comparing a run
on thread A with a run on thread B would compare two different namespaces and
prove nothing. Different database files keep the runs isolated instead.
"""


@pytest.fixture
def baseline(tmp_path: Path):
    db_path = tmp_path / "baseline.sqlite3"
    outcome, _ = run(db_path, REFUND_TICKET, THREAD)
    return outcome, executions(db_path)


IN_DOUBT_POINTS = ("act:claimed", "act:executed", "act:recorded")
"""The three windows inside the effect that a checkpoint cannot close."""

NON_DOUBT_POINTS = tuple(point for point in CRASH_POINTS if point not in IN_DOUBT_POINTS)


@pytest.mark.parametrize("crash_point", NON_DOUBT_POINTS)
def test_a_refund_survives_a_crash_at_every_boundary(
    tmp_path: Path, baseline: tuple[Any, int], crash_point: str
) -> None:
    base_outcome, base_executions = baseline
    db_path = tmp_path / f"crash-{crash_point.replace(':', '_')}.sqlite3"
    outcome, injector = run(db_path, REFUND_TICKET, THREAD, crash_point)

    assert injector is not None and injector.did_fire, f"{crash_point} was never reached"
    assert outcome.crashes == 1
    assert comparable(outcome.result.state) == comparable(base_outcome.result.state)
    assert executions(db_path) == base_executions == 1


@pytest.mark.parametrize("crash_point", NON_DOUBT_POINTS)
def test_the_refund_amount_is_paid_once_whatever_the_boundary(
    tmp_path: Path, crash_point: str
) -> None:
    db_path = tmp_path / f"amount-{crash_point.replace(':', '_')}.sqlite3"
    outcome, _ = run(db_path, REFUND_TICKET, THREAD, crash_point)
    receipt = outcome.result.state["receipt"]
    assert receipt["result"]["refunded_cents"] == 4500
    assert executions(db_path) == 1


def test_the_trail_never_shows_a_node_twice(tmp_path: Path) -> None:
    for crash_point in NON_DOUBT_POINTS:
        db_path = tmp_path / f"trail-{crash_point.replace(':', '_')}.sqlite3"
        outcome, _ = run(db_path, REFUND_TICKET, THREAD, crash_point)
        trail = outcome.result.trail
        assert trail == ["classify", "plan", "human_gate", "act", "compose"], crash_point
        assert len(trail) == len(set(trail))


def test_a_crash_after_the_effect_replays_the_receipt(tmp_path: Path) -> None:
    db_path = tmp_path / "after.sqlite3"
    outcome, _ = run(db_path, REFUND_TICKET, THREAD, "act:after_effect")
    assert outcome.result.state["receipt"]["replayed"] is True
    assert executions(db_path) == 1


def test_a_crash_before_the_effect_runs_it_normally(tmp_path: Path) -> None:
    db_path = tmp_path / "before.sqlite3"
    outcome, _ = run(db_path, REFUND_TICKET, THREAD, "act:enter")
    assert outcome.result.state["receipt"]["replayed"] is False
    assert executions(db_path) == 1


@pytest.mark.parametrize(
    ("crash_point", "expected_executions"),
    [("act:claimed", 0), ("act:executed", 0), ("act:recorded", 1)],
)
def test_a_crash_inside_the_effect_stops_and_says_so(
    tmp_path: Path, crash_point: str, expected_executions: int
) -> None:
    """The windows a key cannot close. They fail loud instead of guessing."""
    db_path = tmp_path / f"doubt-{crash_point.replace(':', '_')}.sqlite3"
    with pytest.raises(EffectInDoubt) as caught:
        run(db_path, REFUND_TICKET, THREAD, crash_point)
    assert caught.value.executions == expected_executions
    assert executions(db_path) == expected_executions


def test_a_crash_after_the_execution_was_recorded_does_not_claim_it_was_lost(
    tmp_path: Path,
) -> None:
    """The message has to match what the ledger can prove, not what is convenient."""
    db_path = tmp_path / "recorded.sqlite3"
    with pytest.raises(EffectInDoubt) as caught:
        run(db_path, REFUND_TICKET, THREAD, "act:recorded")
    assert "the work happened" in str(caught.value)

    with pytest.raises(EffectInDoubt) as unproven:
        run(tmp_path / "claimed.sqlite3", REFUND_TICKET, THREAD, "act:claimed")
    assert "may still have run" in str(unproven.value)


def test_the_in_doubt_thread_stays_stuck_until_a_human_looks(tmp_path: Path) -> None:
    db_path = tmp_path / "doubt2.sqlite3"
    with pytest.raises(EffectInDoubt):
        run(db_path, REFUND_TICKET, THREAD, "act:claimed")
    runtime = make_factory(db_path)()
    try:
        with pytest.raises(EffectInDoubt):
            runtime.resume(THREAD)
        assert runtime.effects.total_executions() == 0
    finally:
        runtime.close()


@pytest.mark.parametrize("crash_point", ["classify:enter", "classify:exit", "compose:enter"])
def test_a_ticket_with_no_effect_also_resumes_cleanly(
    tmp_path: Path, crash_point: str
) -> None:
    clean_db = tmp_path / f"clean-{crash_point.replace(':', '_')}.sqlite3"
    crash_db = tmp_path / f"dirty-{crash_point.replace(':', '_')}.sqlite3"
    expected, _ = run(clean_db, ANSWER_TICKET, THREAD)
    actual, injector = run(crash_db, ANSWER_TICKET, THREAD, crash_point)
    assert injector is not None and injector.did_fire
    assert comparable(actual.result.state) == comparable(expected.result.state)


@pytest.mark.parametrize("crash_point", ["plan:enter", "act:enter", "act:after_effect"])
def test_a_read_only_tool_also_runs_once(tmp_path: Path, crash_point: str) -> None:
    db_path = tmp_path / f"read-{crash_point.replace(':', '_')}.sqlite3"
    outcome, injector = run(db_path, LOOKUP_TICKET, THREAD, crash_point)
    assert injector is not None and injector.did_fire
    assert outcome.result.outcome == "answered"
    assert executions(db_path) == 1


def test_a_crash_before_the_gate_reopens_it(tmp_path: Path) -> None:
    """Crashing on the way into the gate must not silently skip the approval."""
    db_path = tmp_path / "gate.sqlite3"
    outcome, injector = run(db_path, REFUND_TICKET, THREAD, "gate:enter")
    assert injector is not None and injector.did_fire
    assert len(outcome.gates) >= 1
    assert outcome.result.state["approval"]["approver"] == "ops.dana"


def test_the_token_survives_a_crash_at_the_gate(tmp_path: Path) -> None:
    db_path = tmp_path / "token.sqlite3"
    outcome, _ = run(db_path, REFUND_TICKET, THREAD, "gate:enter")
    tokens = {gate.token for gate in outcome.gates}
    assert len(tokens) == 1, "a resumed gate must mint the same token, not a new one"


def test_an_uninjected_run_touches_every_expected_boundary(tmp_path: Path) -> None:
    hook = RecordingHook()
    factory = make_factory(tmp_path / "hooks.sqlite3")
    outcome = drive(
        runtime_factory=lambda h: factory(h),
        ticket=REFUND_TICKET,
        thread_id=THREAD,
        decider=approver,
        crash_hook=hook,
    )
    assert outcome.result.outcome == "acted"
    assert set(hook.seen) <= set(CRASH_POINTS)
    for expected in ("classify:enter", "plan:exit", "gate:resumed", "act:claimed", "compose:exit"):
        assert expected in hook.seen


def test_the_injector_only_fires_the_number_of_times_it_was_told() -> None:
    injector = CrashInjector(point="act:enter", times=2)
    for _ in range(2):
        with pytest.raises(SimulatedCrash):
            injector("act:enter")
    injector("act:enter")
    assert injector.fired == 2


def test_the_injector_ignores_other_points() -> None:
    injector = CrashInjector(point="act:enter")
    injector("classify:enter")
    assert injector.did_fire is False
