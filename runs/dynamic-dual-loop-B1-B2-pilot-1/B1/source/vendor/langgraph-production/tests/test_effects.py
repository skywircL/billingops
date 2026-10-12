from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

import pytest

from langgraph_production.effects import (
    EffectInDoubt,
    EffectStore,
    InMemoryEffectStore,
    SqliteEffectStore,
    idempotency_key,
    perform_effect,
)
from langgraph_production.models import PlannedAction, ToolCall
from langgraph_production.tools import ToolRegistry

REGISTRY = ToolRegistry()


def plan(tool: str, **args: object) -> PlannedAction:
    return PlannedAction.build(
        ticket_id="T-1",
        call=ToolCall(name=tool, args=args),
        spec=REGISTRY.require(tool),
    )


REFUND = plan("issue_refund", invoice_ref="INV-10032", amount_cents=4500)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path):
    """Both implementations have to behave the same, so both are tested the same."""
    if request.param == "memory":
        yield InMemoryEffectStore()
    else:
        instance = SqliteEffectStore(tmp_path / "effects.sqlite3")
        yield instance
        instance.close()


def run(store: EffectStore, registry: ToolRegistry, clock: Callable[[], datetime], **kwargs: object):
    return perform_effect(
        action=REFUND,
        thread_id="th-1",
        registry=registry,
        store=store,
        clock=clock,
        **kwargs,  # type: ignore[arg-type]
    )


def test_the_key_names_the_thread_and_the_action() -> None:
    assert idempotency_key("th-1", "abc") == "th-1:abc"


def test_the_first_call_performs_the_effect(store, registry, clock) -> None:
    receipt = run(store, registry, clock)
    assert receipt.replayed is False
    assert receipt.result["refunded_cents"] == 4500
    assert store.execution_count(receipt.idempotency_key) == 1


def test_a_second_call_replays_instead_of_repeating(store, registry, clock) -> None:
    first = run(store, registry, clock)
    second = run(store, registry, clock)
    assert second.replayed is True
    assert second.result == first.result
    assert store.total_executions() == 1


def test_ten_calls_still_execute_once(store, registry, clock) -> None:
    for _ in range(10):
        run(store, registry, clock)
    assert store.total_executions() == 1


def test_a_different_thread_is_a_different_effect(store, registry, clock) -> None:
    perform_effect(action=REFUND, thread_id="th-1", registry=registry, store=store, clock=clock)
    perform_effect(action=REFUND, thread_id="th-2", registry=registry, store=store, clock=clock)
    assert store.count() == 2
    assert store.total_executions() == 2


def test_a_different_amount_is_a_different_effect(store, registry, clock) -> None:
    bigger = plan("issue_refund", invoice_ref="INV-10032", amount_cents=9000)
    perform_effect(action=REFUND, thread_id="th-1", registry=registry, store=store, clock=clock)
    perform_effect(action=bigger, thread_id="th-1", registry=registry, store=store, clock=clock)
    assert store.total_executions() == 2


def test_the_same_amount_spelled_differently_is_one_effect(store, registry, clock) -> None:
    """The reproduction of the bug this key exists to prevent.

    A model that emits 4500 on one attempt and "4500" on the next is asking
    for one refund of forty five dollars. Three claims for one refund would
    mean three payments.
    """
    for spelling in (4500, "4500", 4500.0):
        action = plan("issue_refund", invoice_ref="INV-10032", amount_cents=spelling)
        perform_effect(
            action=action, thread_id="th-1", registry=registry, store=store, clock=clock
        )
    assert store.count() == 1
    assert store.total_executions() == 1


def crash_at(point: str) -> Callable[[str], None]:
    """A hook that stands in for the process going away at one window."""

    def hook(seen: str) -> None:
        if seen == point:
            raise RuntimeError(f"process gone at {seen}")

    return hook


@pytest.mark.parametrize("point", ["act:claimed", "act:executed", "act:recorded"])
def test_a_crash_inside_the_effect_leaves_it_in_doubt(store, registry, clock, point) -> None:
    with pytest.raises(RuntimeError):
        run(store, registry, clock, hook=crash_at(point))
    with pytest.raises(EffectInDoubt):
        run(store, registry, clock)


def test_a_crash_before_the_handler_never_runs_it(store, registry, clock) -> None:
    with pytest.raises(RuntimeError):
        run(store, registry, clock, hook=crash_at("act:claimed"))
    for _ in range(3):
        with pytest.raises(EffectInDoubt):
            run(store, registry, clock)
    assert store.total_executions() == 0


def test_an_unrecorded_execution_does_not_claim_the_handler_ran(store, registry, clock) -> None:
    """The honest half: the ledger cannot see a run it never got to record."""
    with pytest.raises(RuntimeError):
        run(store, registry, clock, hook=crash_at("act:executed"))
    assert store.total_executions() == 0
    with pytest.raises(EffectInDoubt) as caught:
        run(store, registry, clock)
    assert caught.value.executions == 0
    assert "may still have run" in str(caught.value)


def test_a_recorded_execution_is_reported_as_work_that_happened(store, registry, clock) -> None:
    """The other half: the handler ran, the receipt did not land, and it says so."""
    with pytest.raises(RuntimeError):
        run(store, registry, clock, hook=crash_at("act:recorded"))
    assert store.total_executions() == 1
    with pytest.raises(EffectInDoubt) as caught:
        run(store, registry, clock)
    assert caught.value.executions == 1
    assert "the work happened" in str(caught.value)
    assert store.total_executions() == 1


def test_the_in_doubt_error_names_the_key(store, registry, clock) -> None:
    with pytest.raises(RuntimeError):
        run(store, registry, clock, hook=crash_at("act:claimed"))
    with pytest.raises(EffectInDoubt, match="th-1:") as caught:
        run(store, registry, clock)
    assert caught.value.key.startswith("th-1:")


def test_a_receipt_is_readable_back_from_the_store(store, registry, clock) -> None:
    receipt = run(store, registry, clock)
    stored = store.get(receipt.idempotency_key)
    assert stored is not None
    assert stored["status"] == "done"
    assert stored["receipt"]["result"] == receipt.result


def test_an_unclaimed_key_reads_back_as_missing(store) -> None:
    assert store.get("th-1:nothing") is None


def test_a_sqlite_store_outlives_the_object_that_wrote_it(
    tmp_path: Path, registry: ToolRegistry, clock: Callable[[], datetime]
) -> None:
    path = tmp_path / "effects.sqlite3"
    first = SqliteEffectStore(path)
    receipt = perform_effect(
        action=REFUND, thread_id="th-1", registry=registry, store=first, clock=clock
    )
    first.close()

    second = SqliteEffectStore(path)
    try:
        replay = perform_effect(
            action=REFUND, thread_id="th-1", registry=registry, store=second, clock=clock
        )
        assert replay.replayed is True
        assert replay.result == receipt.result
        assert second.total_executions() == 1
    finally:
        second.close()


def test_claim_reports_who_was_first(store) -> None:
    _, taken = store.claim("k", {"status": "pending"})
    assert taken is False
    stored, taken_again = store.claim("k", {"status": "pending", "other": True})
    assert taken_again is True
    assert "other" not in stored
