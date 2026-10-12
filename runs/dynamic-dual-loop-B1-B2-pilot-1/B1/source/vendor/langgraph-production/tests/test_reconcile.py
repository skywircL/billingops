"""What happens after the graph stops and refuses to guess.

An in-doubt key that nobody can close is not caution, it is a ticket that
never moves again. These tests cover both directions of the resolution and the
handler refusal that used to leave a claim pending for good.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

import pytest

from langgraph_production.effects import (
    EffectFailed,
    EffectInDoubt,
    EffectStore,
    InMemoryEffectStore,
    ReconciliationError,
    SqliteEffectStore,
    open_effects,
    perform_effect,
    resolve_effect,
)
from langgraph_production.models import PlannedAction, ToolCall
from langgraph_production.reconcile import main as reconcile_main
from langgraph_production.tools import ToolRegistry, UnknownRecord

REGISTRY = ToolRegistry()


def plan(tool: str, **args: object) -> PlannedAction:
    return PlannedAction.build(
        ticket_id="T-1",
        call=ToolCall(name=tool, args=args),
        spec=REGISTRY.require(tool),
    )


REFUND = plan("issue_refund", invoice_ref="INV-10032", amount_cents=4500)
GHOST = plan("issue_refund", invoice_ref="INV-99999", amount_cents=4500)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "memory":
        yield InMemoryEffectStore()
    else:
        instance = SqliteEffectStore(tmp_path / "effects.sqlite3")
        yield instance
        instance.close()


def run(
    store: EffectStore,
    registry: ToolRegistry,
    clock: Callable[[], datetime],
    action: PlannedAction = REFUND,
    **kwargs: object,
):
    return perform_effect(
        action=action,
        thread_id="th-1",
        registry=registry,
        store=store,
        clock=clock,
        **kwargs,  # type: ignore[arg-type]
    )


def crash_at(point: str) -> Callable[[str], None]:
    def hook(seen: str) -> None:
        if seen == point:
            raise RuntimeError(f"process gone at {seen}")

    return hook


# -- a handler that refuses ---------------------------------------------


def test_a_refused_action_fails_instead_of_hanging(store, registry, clock) -> None:
    """A well formed identifier that does not exist is a definite no.

    It used to leave the claim pending, so every later attempt raised
    EffectInDoubt and the ticket was stuck for good over a typo.
    """
    with pytest.raises(EffectFailed):
        run(store, registry, clock, GHOST)
    row = store.get(f"th-1:{GHOST.action_id}")
    assert row is not None
    assert row["status"] == "failed"
    assert "UnknownRecord" in row["error"]


def test_a_refused_action_fails_the_same_way_on_every_attempt(store, registry, clock) -> None:
    for _ in range(3):
        with pytest.raises(EffectFailed):
            run(store, registry, clock, GHOST)
    assert store.total_executions() == 0


def test_the_underlying_refusal_is_kept_as_the_cause(store, registry, clock) -> None:
    with pytest.raises(EffectFailed) as caught:
        run(store, registry, clock, GHOST)
    assert isinstance(caught.value.__cause__, UnknownRecord)


def test_an_unclassifiable_failure_stays_in_doubt(tmp_path: Path, registry, clock) -> None:
    """A handler that dies for an unknown reason may have acted before dying.

    Closing that key as failed would be the convenient answer and the wrong
    one, so it goes in doubt like any other interrupted attempt.
    """

    class Exploding:
        irreversible = True

        def run(self, args: object) -> dict[str, object]:
            raise RuntimeError("connection dropped mid call")

        def validate_args(self, args: object) -> object:
            return args

    class OneToolRegistry(ToolRegistry):
        def require(self, name: str):  # type: ignore[override]
            return Exploding()  # type: ignore[return-value]

    store = InMemoryEffectStore()
    with pytest.raises(RuntimeError):
        perform_effect(
            action=REFUND, thread_id="th-1", registry=OneToolRegistry(), store=store, clock=clock
        )
    with pytest.raises(EffectInDoubt):
        perform_effect(
            action=REFUND, thread_id="th-1", registry=OneToolRegistry(), store=store, clock=clock
        )


# -- resolving in doubt keys --------------------------------------------


def test_the_open_queue_lists_only_what_needs_a_human(store, registry, clock) -> None:
    run(store, registry, clock)
    with pytest.raises(RuntimeError):
        run(store, registry, clock, GHOST, hook=crash_at("act:claimed"))
    assert list(open_effects(store)) == [f"th-1:{GHOST.action_id}"]


def test_resolving_as_not_performed_lets_the_action_run(store, registry, clock) -> None:
    with pytest.raises(RuntimeError):
        run(store, registry, clock, hook=crash_at("act:claimed"))
    key = f"th-1:{REFUND.action_id}"
    with pytest.raises(EffectInDoubt):
        run(store, registry, clock)

    resolve_effect(store=store, key=key, outcome="not_performed", resolver="ops.dana")

    receipt = run(store, registry, clock)
    assert receipt.replayed is False
    assert receipt.result["refunded_cents"] == 4500
    assert store.total_executions() == 1


def test_resolving_as_performed_replays_instead_of_running(store, registry, clock) -> None:
    with pytest.raises(RuntimeError):
        run(store, registry, clock, hook=crash_at("act:recorded"))
    key = f"th-1:{REFUND.action_id}"

    resolve_effect(
        store=store,
        key=key,
        outcome="performed",
        resolver="ops.dana",
        result={"invoice_ref": "INV-10032", "refunded_cents": 4500, "remaining_cents": 4500},
    )

    receipt = run(store, registry, clock)
    assert receipt.replayed is True
    assert receipt.result["refunded_cents"] == 4500
    assert store.total_executions() == 1, "the handler must not run a second time"


def test_a_resolution_is_signed(store, registry, clock) -> None:
    with pytest.raises(RuntimeError):
        run(store, registry, clock, hook=crash_at("act:claimed"))
    key = f"th-1:{REFUND.action_id}"
    row = resolve_effect(
        store=store, key=key, outcome="performed", resolver="ops.dana", result={}
    )
    assert row["reconciled_by"] == "ops.dana"
    assert row["reconciled_at"]
    assert store.get(key)["reconciled_by"] == "ops.dana"  # type: ignore[index]


def test_releasing_a_claim_does_not_rewrite_the_execution_ledger(store, registry, clock) -> None:
    with pytest.raises(RuntimeError):
        run(store, registry, clock, hook=crash_at("act:recorded"))
    key = f"th-1:{REFUND.action_id}"
    assert store.execution_count(key) == 1

    resolve_effect(store=store, key=key, outcome="not_performed", resolver="ops.dana")
    assert store.execution_count(key) == 1

    run(store, registry, clock)
    assert store.execution_count(key) == 2, "a second real run is a second recorded execution"


def test_a_refused_action_can_be_released_too(store, registry, clock) -> None:
    with pytest.raises(EffectFailed):
        run(store, registry, clock, GHOST)
    resolve_effect(
        store=store, key=f"th-1:{GHOST.action_id}", outcome="not_performed", resolver="ops.dana"
    )
    assert open_effects(store) == {}


def test_an_unknown_key_cannot_be_resolved(store) -> None:
    with pytest.raises(ReconciliationError, match="no effect claimed"):
        resolve_effect(store=store, key="th-1:nothing", outcome="performed", resolver="ops.dana")


def test_a_closed_key_cannot_be_resolved(store, registry, clock) -> None:
    receipt = run(store, registry, clock)
    with pytest.raises(ReconciliationError, match="already closed"):
        resolve_effect(
            store=store,
            key=receipt.idempotency_key,
            outcome="performed",
            resolver="ops.dana",
        )


# -- the command line ----------------------------------------------------


def open_claim(db_path: Path, clock: Callable[[], datetime]) -> str:
    store = SqliteEffectStore(db_path)
    try:
        with pytest.raises(RuntimeError):
            perform_effect(
                action=REFUND,
                thread_id="th-1",
                registry=ToolRegistry(),
                store=store,
                clock=clock,
                hook=crash_at("act:claimed"),
            )
    finally:
        store.close()
    return f"th-1:{REFUND.action_id}"


def test_the_cli_lists_open_claims(
    tmp_path: Path, clock: Callable[[], datetime], capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "state.sqlite3"
    key = open_claim(db_path, clock)
    assert reconcile_main(["--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert key in out
    assert "executions  0 recorded on disk" in out


def test_the_cli_reports_an_empty_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "empty.sqlite3"
    SqliteEffectStore(db_path).close()
    assert reconcile_main(["--db", str(db_path)]) == 0
    assert "no open effect claims" in capsys.readouterr().out


def test_the_cli_releases_a_claim(
    tmp_path: Path, clock: Callable[[], datetime], capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "state.sqlite3"
    key = open_claim(db_path, clock)
    exit_code = reconcile_main(
        ["--db", str(db_path), "--key", key, "--outcome", "not_performed", "--resolver", "ops.dana"]
    )
    assert exit_code == 0
    assert "released" in capsys.readouterr().out

    store = SqliteEffectStore(db_path)
    try:
        assert store.get(key) is None
    finally:
        store.close()


def test_the_cli_closes_a_claim_with_a_result(
    tmp_path: Path, clock: Callable[[], datetime], capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "state.sqlite3"
    key = open_claim(db_path, clock)
    exit_code = reconcile_main(
        [
            "--db",
            str(db_path),
            "--key",
            key,
            "--outcome",
            "performed",
            "--resolver",
            "ops.dana",
            "--result",
            json.dumps({"refunded_cents": 4500}),
        ]
    )
    assert exit_code == 0
    capsys.readouterr()

    store = SqliteEffectStore(db_path)
    try:
        row = store.get(key)
        assert row is not None
        assert row["status"] == "done"
        assert row["receipt"]["result"] == {"refunded_cents": 4500}
    finally:
        store.close()


def test_the_cli_refuses_an_unsigned_resolution(
    tmp_path: Path, clock: Callable[[], datetime], capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "state.sqlite3"
    key = open_claim(db_path, clock)
    assert reconcile_main(["--db", str(db_path), "--key", key, "--outcome", "performed"]) == 1
    assert "resolver" in capsys.readouterr().err


def test_the_cli_refuses_a_key_without_an_outcome(
    tmp_path: Path, clock: Callable[[], datetime], capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "state.sqlite3"
    key = open_claim(db_path, clock)
    assert reconcile_main(["--db", str(db_path), "--key", key]) == 1
    assert "outcome" in capsys.readouterr().err


def test_the_cli_reports_an_unknown_key(
    tmp_path: Path, clock: Callable[[], datetime], capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "state.sqlite3"
    open_claim(db_path, clock)
    exit_code = reconcile_main(
        [
            "--db",
            str(db_path),
            "--key",
            "th-1:nope",
            "--outcome",
            "performed",
            "--resolver",
            "ops.dana",
        ]
    )
    assert exit_code == 1
    assert "no effect claimed" in capsys.readouterr().err


def test_the_cli_reports_a_missing_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert reconcile_main(["--db", str(tmp_path / "absent.sqlite3")]) == 1
    assert "no such database" in capsys.readouterr().err
