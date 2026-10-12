from copy import deepcopy
import json
from pathlib import Path
import time

import pytest

from billingops.evaluation.v2_runner import AuditedModel, business_context, make_plan, run_trial, run_bundle, summarize, _with_control_uncertainty
from billingops.llm.base import Message, ModelConfig, ModelResponse, Usage
from billingops.llm.fake import ScriptedModel

ROOT = Path(__file__).resolve().parents[1]


def bundle():
    return json.loads((ROOT / "data/tasks/dynamic-v2/gold-fixtures-v0.2.json").read_text())


def test_baseline_plan_fixed_denominators_and_no_fixture_answers_in_policy():
    data = bundle()
    plan = make_plan(data, mode="baseline")
    assert plan["total_trials"] == 180
    assert plan["planned_trials"]["G12"] == 7
    assert plan["planned_trials"]["G16"] == 3
    text = plan["policy_context"]["text"]
    assert all(x not in text for x in ("PAY-88213", "INV-2026-0142", "expected_refunds", "G12"))
    assert plan["architecture"] == "dynamic"


def test_recorded_model_delivers_policy_and_preserves_message_inputs(tmp_path):
    seen = []
    class Model:
        def generate(self, messages, tools, config):
            seen.append(messages)
            return ModelResponse(Message("assistant", "ok"), Usage(10, 2), model="test")
    raw = [Message("system", "original"), Message("user", "hello")]
    wrapper = AuditedModel(Model(), tmp_path / "calls.jsonl", role="agent", deadline=time.monotonic()+10, context={"text":"PUBLIC_RULE"})
    wrapper.generate(raw, [], ModelConfig(tag="extract", model="test"))
    assert raw[0].content == "original"
    assert "PUBLIC_RULE" in seen[0][0].content
    rows = [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [x["kind"] for x in rows] == ["request", "response"]
    assert wrapper.metrics()["usage"]["input_tokens"] == 10


def test_missing_trials_preserve_task_equal_denominators():
    plan = {"candidate_ids":["G01","G16"],"planned_trials":{"G01":7,"G16":3},"total_trials":10,"purpose":"diagnostic"}
    metrics={"cost_usd_estimated":0}
    rows=[{"candidate_id":"G01","status":"pass","latency_s":1,"agent_metrics":metrics,"customer_metrics":metrics,"judge_metrics":metrics}]
    r=summarize(plan,rows)
    assert r["task_equal_S_lower"] == pytest.approx(1/14)
    assert r["task_equal_S_upper"] == 1
    assert sum(x["missing"] for x in r["by_task"]) == 9


def test_customer_uncertainty_is_not_hidden_by_later_agent_timeout():
    failure = {"classification": "runner_limit", "type": "DeadlineExceeded"}
    merged = _with_control_uncertainty(failure, [{"kind":"customer_match_invalid"}])
    assert merged["classification"] == "harness_failure"
    assert merged["secondary_failure"] == failure


def test_unexpected_trial_exception_is_preserved_in_planned_denominator(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("local service startup failed")
    monkeypatch.setattr("billingops.evaluation.v2_runner.run_trial", broken)
    report = run_bundle(bundle(), tmp_path/"run", mode="pilot", selected=["G01"], workers=1)
    assert report["recorded_trials"] == 1
    assert report["by_task"][0]["unknown"] == 1
    assert report["by_task"][0]["missing"] == 0
    manifest = json.loads((tmp_path/"run/manifest.json").read_text())
    assert manifest["evaluation_versions"]["response"] == "business-v2-response-1.1"
    assert "docs/BUSINESS_SPEC_V2.md" in manifest["source"]["source_digests"]


def test_isolated_runtime_adapter_captures_real_http_and_never_implies_unjudged_success(tmp_path):
    task = next(x for x in bundle()["tasks"] if x["candidate_id"]=="G02")
    result = run_trial(task, 0, tmp_path/"trial", model_factory=ScriptedModel, grade=False)
    artifact = json.loads(Path(result["artifact"]).read_text())
    assert result["runtime_failure"] is None
    assert artifact["view"]["case"]["mode"] == "dynamic"
    assert artifact["initial_world"] != artifact["final_world"]
    assert artifact["capture_complete"] == {"events":True,"evidence":True,"world":True}
    assert result["status"] != "pass"
    assert any(e["kind"]=="approval_submitted" for e in artifact["control_events"])
    assert sum(e["kind"]=="approval_submitted" for e in artifact["control_events"]) == 1
    assert [e["control_seq"] for e in artifact["control_events"]] == list(range(len(artifact["control_events"])))
    assert Path(result["response_grade_artifact"]["path"]).exists()
    assert not task["agent_execution_ready"]  # immutable fixture metadata not rewritten


def test_b1_revision_preserves_frozen_model_policy_and_trial_contract():
    data = json.loads((ROOT / 'data/tasks/dynamic-v2/controlled-coverage-v1.json').read_text())
    b0 = make_plan(data, mode='unified', workers=16)
    b1 = make_plan(data, mode='unified', workers=16, agent_revision='B1')
    assert b1['variant'] == 'B1-invoice-scope-v1'
    assert b1['agent_revision'] == 'B1'
    assert b1['total_trials'] == 744
    for key in ('fixture_sha256', 'candidate_ids', 'case_profiles', 'planned_trials', 'workers', 'deadline_s', 'max_rounds', 'seed', 'model', 'policy_context', 'risk_assignment', 'cohorts'):
        assert b0[key] == b1[key]
