"""Prefix diagnostics: strict replay up to the switch point, refusal on divergence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from billingops.billing_client import BillingClient
from billingops.evaluation.prefix import PrefixModel, prefix_run
from billingops.evaluation.trials import run_trial
from tests.test_eval_tasks import d10_task


def _record(billing: BillingClient, tmp_path: Path) -> Path:
    t = d10_task(task_id="example/D10-named-invoice/en-2")
    result = run_trial(t, "dynamic", model_spec="fake", billing=billing, workdir=tmp_path / "rec" / "dbs")
    assert result.status == "pass"
    return tmp_path / "rec" / result.artifact["path"]


def test_prefix_replays_then_switches(billing: BillingClient, tmp_path: Path) -> None:
    artifact = _record(billing, tmp_path)
    record = prefix_run(artifact=artifact, switch_at=1, model_spec="fake", out_dir=tmp_path / "prefix-1", billing=billing)
    assert record["status"] == "completed"
    assert record["prefix"]["replayed_turns"] == 1 and record["prefix"]["switched"] and record["prefix"]["verified_tool_results"] == 1
    assert record["suffix"]["status"] == "pass" and record["suffix"]["cost_usd"] >= 0


def test_prefix_refuses_switch_after_end_and_fault_plans(billing: BillingClient, tmp_path: Path) -> None:
    artifact = _record(billing, tmp_path)
    art = json.loads(artifact.read_text())
    with pytest.raises(ValueError):
        PrefixModel(snapshot=art["snapshot"], switch_at=99, live=None)
    art["task"]["fault_plan"] = {"reads_unavailable": 1}
    faulty = tmp_path / "faulty.json"
    faulty.write_text(json.dumps(art))
    with pytest.raises(ValueError):
        prefix_run(artifact=faulty, switch_at=1, model_spec="fake", out_dir=tmp_path / "prefix-f", billing=billing)


def test_prefix_is_unreproducible_when_the_world_diverges(billing: BillingClient, tmp_path: Path) -> None:
    artifact = _record(billing, tmp_path)
    art = json.loads(artifact.read_text())
    for p in art["task"]["world"]["payments"]:
        if p["id"] == "PAY-88213":
            p["amount_cents"] = 4800  # the recorded list_payments result can no longer be reproduced
    art["task"]["expected"]["refund_delta"]["amount_cents"] = 4800
    art["task"]["expected"]["amount_cents"] = 4800
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(art))
    record = prefix_run(artifact=changed, switch_at=2, model_spec="fake", out_dir=tmp_path / "prefix-x", billing=billing)
    assert record["status"] == "unreproducible" and "differs from the recording" in (record["error"] or "")
    assert not record["prefix"]["switched"]
