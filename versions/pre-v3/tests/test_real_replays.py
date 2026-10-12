"""The three recorded real-model trajectories replay from disk in an isolated database, with no misses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from billingops.evaluation.simulators import SimulatedUser
from billingops.llm.replay import ReplayModel, Trace
from billingops.testing import ambiguous_world, duplicate_world

MANIFEST = Path(__file__).resolve().parents[1] / "data" / "replays" / "real" / "manifest.json"
pytestmark = pytest.mark.skipif(not MANIFEST.exists(), reason="real traces not recorded yet")


def _entries():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["traces"]


@pytest.mark.parametrize("entry", _entries(), ids=lambda e: e["name"])
def test_real_trace_replays_without_a_miss(runtime_factory, billing, entry) -> None:
    worlds = {"duplicate": duplicate_world(), "duplicate_partial": duplicate_world(prior_refund_cents=1500), "ambiguous": ambiguous_world()}
    billing.load_world(worlds[entry["world"]])
    trace = Trace.load(MANIFEST.parent / f"{entry['name']}.json")
    replay = ReplayModel(trace)
    rt = runtime_factory(model=replay, mode="dynamic")
    case = rt.submit_case(customer_id="C-1001", created_by="cust-c1001", text=entry["text"])
    rt.drain()
    current = rt.store.get_case(case["id"])
    if current["state"] == "awaiting_user":
        rt.submit_message(case["id"], text=SimulatedUser(entry["disclosable"]).answer(current["pending_question"]), actor="cust-c1001")
        rt.drain()
    view = rt.case_view(case["id"])
    assert view["case"]["state"] == entry["expected_state"]
    assert view["case"]["conclusion"]["type"] == entry["expected_conclusion"]
    assert replay.served == len(trace)
    if entry.get("expects_clarification"):
        assert view["budgets"]["clarifications"] == 1
    assert view["execution"] is None  # nothing was written while recording or replaying
