from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from langgraph_production.faults import CrashInjector
from langgraph_production.llm.offline import OfflineSupportLLM
from langgraph_production.models import GateRequest, HumanDecision
from langgraph_production.runtime import SupportRuntime, drive

from samples import ANSWER_TICKET, FIXED_TIME, REFUND_TICKET


def test_an_in_memory_runtime_needs_no_file(offline_llm) -> None:
    with SupportRuntime(llm=offline_llm) as runtime:
        assert runtime.db_path is None
        assert runtime.start(ANSWER_TICKET, thread_id="mem").status == "completed"


def test_a_durable_runtime_creates_its_file(tmp_path: Path, offline_llm) -> None:
    db_path = tmp_path / "nested" / "state.sqlite3"
    with SupportRuntime(llm=offline_llm, db_path=db_path) as runtime:
        runtime.start(ANSWER_TICKET, thread_id="disk")
    assert db_path.exists()


def test_the_snapshot_matches_the_returned_state(runtime: SupportRuntime) -> None:
    result = runtime.start(ANSWER_TICKET, thread_id="snap")
    assert runtime.snapshot("snap")["reply"] == result.reply


def test_a_snapshot_of_a_waiting_thread_shows_the_progress(runtime: SupportRuntime) -> None:
    runtime.start(REFUND_TICKET, thread_id="snap-2")
    snapshot = runtime.snapshot("snap-2")
    assert snapshot["trail"] == ["classify", "plan"]
    assert snapshot.get("receipt") is None


def test_two_threads_do_not_share_state(runtime: SupportRuntime) -> None:
    runtime.start(ANSWER_TICKET, thread_id="a")
    runtime.start(REFUND_TICKET, thread_id="b")
    assert runtime.snapshot("a")["ticket_id"] == ANSWER_TICKET.id
    assert runtime.snapshot("b")["ticket_id"] == REFUND_TICKET.id


def test_the_same_ticket_on_two_threads_is_two_effects(
    durable_factory: Callable[..., SupportRuntime],
    approve: Callable[[GateRequest], HumanDecision],
) -> None:
    """Two conversations about the same refund really are two refunds."""
    for thread in ("dup-1", "dup-2"):
        drive(
            runtime_factory=durable_factory,
            ticket=REFUND_TICKET,
            thread_id=thread,
            decider=approve,
        )
    runtime = durable_factory()
    try:
        assert runtime.effects.total_executions() == 2
    finally:
        runtime.close()


def test_replaying_the_same_thread_does_not_pay_twice(
    durable_factory: Callable[..., SupportRuntime],
    approve: Callable[[GateRequest], HumanDecision],
) -> None:
    drive(
        runtime_factory=durable_factory,
        ticket=REFUND_TICKET,
        thread_id="same",
        decider=approve,
    )
    runtime = durable_factory()
    try:
        runtime.resume("same")
        assert runtime.effects.total_executions() == 1
    finally:
        runtime.close()


def test_a_run_result_exposes_the_useful_fields(runtime: SupportRuntime) -> None:
    result = runtime.start(ANSWER_TICKET, thread_id="fields")
    assert isinstance(result.reply, str) and result.reply
    assert result.outcome == "answered"
    assert result.trail


def test_drive_reports_the_gates_it_passed(
    durable_factory: Callable[..., SupportRuntime],
    approve: Callable[[GateRequest], HumanDecision],
) -> None:
    outcome = drive(
        runtime_factory=durable_factory,
        ticket=REFUND_TICKET,
        thread_id="gates",
        decider=approve,
    )
    assert len(outcome.gates) == 1
    assert outcome.crashes == 0


def test_drive_counts_the_crashes_it_survived(
    durable_factory: Callable[..., SupportRuntime],
    approve: Callable[[GateRequest], HumanDecision],
) -> None:
    outcome = drive(
        runtime_factory=durable_factory,
        ticket=REFUND_TICKET,
        thread_id="crashy",
        decider=approve,
        crash_hook=CrashInjector(point="act:enter"),
    )
    assert outcome.crashes == 1
    assert outcome.result.outcome == "acted"


def test_drive_gives_up_rather_than_looping_forever(
    durable_factory: Callable[..., SupportRuntime],
) -> None:
    def never_settles(gate: GateRequest) -> HumanDecision:
        return HumanDecision(
            token="wrong-token",
            approver="ops.dana",
            decision="approve",
            note="",
            decided_at=FIXED_TIME,
        )

    outcome = drive(
        runtime_factory=durable_factory,
        ticket=REFUND_TICKET,
        thread_id="stubborn",
        decider=never_settles,
        max_steps=4,
    )
    assert outcome.result.outcome == "gate_rejected"


def test_a_graph_without_a_thread_id_refuses_to_run(offline_llm) -> None:
    with SupportRuntime(llm=offline_llm) as runtime:
        with pytest.raises(ValueError, match="thread_id"):
            runtime.graph.invoke(
                {"ticket_id": "T", "text": "refund $45.00 on INV-10032", "trail": []},
                {"configurable": {}},
            )


def test_close_is_safe_to_call_twice(tmp_path: Path) -> None:
    runtime = SupportRuntime(llm=OfflineSupportLLM(), db_path=tmp_path / "twice.sqlite3")
    runtime.close()
    runtime.close()
