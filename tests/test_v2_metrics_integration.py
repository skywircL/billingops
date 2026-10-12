"""The reporting boundary must not launder missing, stopped or tampered evidence."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from billingops.evaluation.v2_metrics import digest, load_archive, sha, scientific_metrics, measurement_contract
from billingops.evaluation.v2_release import SAFETY_GUARDS
from billingops.evaluation.trials import TimedBilling

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/report_scientific_v2.py"
spec = importlib.util.spec_from_file_location("scientific_report_test", SCRIPT)
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False))


@pytest.fixture
def archive(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    rows, tasks, profiles = [], [], {}
    for cid in ("T1", "T2"):
        task = {"case_id": cid, "candidate_id": "G31", "grading_profile_id": "G31",
                "task_id": "test/" + cid, "customer_id": "C-1", "user_input": "offline fixture",
                "world": {}, "author_scope": {}, "expected_refunds": []}
        tasks.append(task)
        profiles[cid] = {"task_sha256": digest(task), "root_seed_family": cid, "split": "gold_dev", "operator": "NONE"}
        response = {"version": "test-response", "status": "pass", "claims": [],
                    "semantic_guardrails": {"tool_result_hallucination": {"status": "pass", "incident": False}}}
        raw = {"candidate_id": cid, "trial_index": 0, "task": task, "task_sha256": digest(task),
               "view": {"evidence": [], "trail": []}, "events": [], "control_events": [],
               "snapshot": {"consumed_commands": []}, "reply": "No action.",
               "capture_complete": {"events": True, "evidence": True, "world": True}}
        folder = run / "trials" / f"{cid}-t0"
        dump(folder / "artifact.json", raw)
        metrics = {"calls": 1, "seconds": .1, "cost_usd_estimated": .001,
                   "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}
        dump(folder / "response-grade.json", {"response_grade": response, "model_metrics": metrics,
                                               "raw_artifact_sha256": sha(folder / "artifact.json")})
        row = {"candidate_id": cid, "case_id": cid, "task_id": task["task_id"], "trial_index": 0, "status": "pass",
               "scoring": {"scorer_version": "test-scorer", "task_status": "pass",
                           "outcome": {"status": "pass", "checks": {}},
                           "behavior": {"status": "pass", "checks": {"no_refund_send": True}},
                           "guardrails": {"status": "pass", "incidents": {g: False for g in SAFETY_GUARDS}}},
               "response": response, "artifact": str(folder / "artifact.json"),
               "artifact_sha256": sha(folder / "artifact.json"),
               "response_grade_artifact": {"path": str(folder / "response-grade.json"), "sha256": sha(folder / "response-grade.json")},
               "latency_s": 1., **{r + "_metrics": deepcopy(metrics) for r in ("agent", "customer", "judge")}}
        dump(folder / "result.json", row)
        rows.append(row)
    source = {}
    for name in ("v2_runner", "v2_scoring", "v2_response", "v2_controls"):
        rel = f"src/billingops/evaluation/{name}.py"
        path = run / "source" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic frozen source\n")
        source[rel] = sha(path)
    fixture = {"tasks": tasks}
    plan = {"runner_version": "v2-runner-1.8", "candidate_ids": list(profiles), "case_profiles": profiles,
            "planned_trials": {k: 1 for k in profiles}, "total_trials": 2, "fixture_sha256": digest(fixture),
            "evaluation_versions": {"response": "test-response", "scorer": "test-scorer", "controls": "test-controls"}}
    dump(run / "manifest.json", {**plan, "plan_sha256": digest(plan), "source": {"provenance": "before_execution", "source_digests": source}})
    dump(run / "fixtures.json", fixture)
    (run / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return run


def rewrite_result(run, change):
    rows = [json.loads(line) for line in (run / "results.jsonl").read_text().splitlines()]
    change(rows[0])
    dump(run / "trials/T1-t0/result.json", rows[0])
    (run / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_report_preserves_input_and_exports_pending_review_and_blocking_gate(archive, tmp_path):
    before = {str(p): sha(p) for p in archive.rglob("*") if p.is_file()}
    output = tmp_path / "report"
    result = reporter.report(archive, output)
    assert result["release_status"] == "inconclusive"
    assert result["calibration_status"] == "pending"
    report = json.loads((output / "report.json").read_text())
    assert report["scientific_metrics"]["registration"] == "posthoc_diagnostic_supplement"
    assert report["scientific_metrics"]["resources"]["all_trials"]["total"]["tool_cost_usd"]["missing"] == 2
    assert report["scientific_metrics"]["process"]["successful_and_safe"]["trial_count"] == 2
    assert report["release_gate"]["release_permitted"] is False
    assert before == {str(p): sha(p) for p in archive.rglob("*") if p.is_file()}
    with pytest.raises(FileExistsError):
        reporter.report(archive, output)
    with pytest.raises(ValueError, match="outside"):
        reporter.report(archive, archive / "report")


@pytest.mark.parametrize("field", ["provider-stop.json", "incomplete-run.json"])
def test_full_slot_count_does_not_erase_interruption(archive, field):
    dump(archive / field, {"status": "interrupted"})
    with pytest.raises(ValueError, match="provider-stopped"):
        load_archive(archive)


@pytest.mark.parametrize("mode", ["layer", "guard", "overall", "source", "raw", "grade", "duplicate"])
def test_invalid_evidence_cannot_enter_rates(archive, mode):
    if mode == "layer":
        rewrite_result(archive, lambda r: r["scoring"]["behavior"]["checks"].update(no_refund_send=None))
    elif mode == "guard":
        rewrite_result(archive, lambda r: r["scoring"]["guardrails"]["incidents"].update(over_refund=True))
    elif mode == "overall":
        rewrite_result(archive, lambda r: r.update(status="fail"))
    elif mode == "source":
        (archive / "source/src/billingops/evaluation/v2_runner.py").write_text("changed")
    elif mode in {"raw", "grade"}:
        path = archive / "trials/T1-t0" / ("artifact.json" if mode == "raw" else "response-grade.json")
        path.write_text(path.read_text() + " ")
    else:
        path = archive / "results.jsonl"
        path.write_text(path.read_text() + path.read_text().splitlines()[0] + "\n")
    with pytest.raises(ValueError):
        load_archive(archive)


def test_missing_slot_is_reported_without_formal_aggregate(archive, tmp_path):
    path = archive / "results.jsonl"
    path.write_text(path.read_text().splitlines()[0] + "\n")
    reporter.report(archive, tmp_path / "missing")
    report = json.loads((tmp_path / "missing/report.json").read_text())
    stats = report["scientific_metrics"]["statistics"]
    assert stats["aggregates"] is None and stats["missing_trials"] == 1
    assert report["release_gate"]["release_permitted"] is False


def test_billing_meter_includes_internal_prechecks_and_failed_calls():
    class Billing:
        def get_invoice(self, value):
            return {"id": value}
        def payment_refunds(self, value):
            raise TimeoutError("simulated receipt loss")
    meter = TimedBilling(Billing())
    meter.get_invoice("INV-1")
    with pytest.raises(TimeoutError):
        meter.payment_refunds("PAY-1")
    assert [r["tool"] for r in meter.calls] == ["get_invoice", "payment_refunds"]
    assert [r["ok"] for r in meter.calls] == [True, False]
    assert all(r["seconds"] >= 0 for r in meter.calls)


@pytest.mark.parametrize("state,exit_code", [("inconclusive", 3), ("fail", 3), ("pass", None)])
def test_release_check_cli_blocks_ci_when_gate_does_not_pass(monkeypatch, state, exit_code):
    monkeypatch.setattr(reporter, "report", lambda *args, **kwargs: {"release_status": state})
    monkeypatch.setattr("sys.argv", ["report_scientific_v2.py", "unused-run", "--out", "unused-output", "--require-release-pass"])
    if exit_code is None:
        reporter.main()
    else:
        with pytest.raises(SystemExit) as exc:
            reporter.main()
        assert exc.value.code == exit_code


@pytest.mark.parametrize("key,value", [("version", "future-version"), ("confidence_level", .99),
                                       ("bootstrap_unit", "trial"), ("minimum_families_for_bootstrap", 2)])
def test_unsupported_preregistered_measurement_is_rejected_instead_of_silently_reinterpreted(archive, key, value):
    plan, tasks, rows, artifacts, _ = load_archive(archive)
    plan["measurement_contract"] = measurement_contract()
    plan["measurement_contract"][key] = value
    with pytest.raises(ValueError, match=key):
        scientific_metrics(plan, rows, artifacts, tasks=tasks)
