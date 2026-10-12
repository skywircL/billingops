"""Offline retry contract/composite tests; no API, live run, or ports."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("retry_analysis_test", ROOT / "scripts/analyze_controlled_retry_v2.py")
retry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retry)
fixture_spec = importlib.util.spec_from_file_location("retry_synthetic_archive", ROOT / "tests/test_analyze_controlled_v2.py")
fixture = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(fixture)


def contract():
    ids = [f"case-{i}-t0" for i in range(9)]
    m = {"fixture_sha256": "canonical-fixture", "source": {"source_digests": {"agent.py": "frozen"}}}
    base = {"manifest": "m", "results": "br", "provider_stop": "stop", "fixture_bytes": "fixture-file"}
    new = {"manifest": "m", "results": "rr", "registration": "registration-hash"}
    reg = {"schema_version": retry.REGISTRATION_SCHEMA, "mode": retry.MODE, "authorized_user_instruction": "仅重试全部9条受402影响的试验，并合并统计",
           "selection_rule": "all_original_http402_trials", "retry_policy": "one_full_run_trial_per_selected_slot_no_best_of", "execution_kind": "full_run_trial",
           "executor_workers": 16, "active_trial_count": 9, "selected_trial_ids": ids, "dispatch_order": list(reversed(ids)), "base_manifest_sha256": "m",
           "base_results_sha256": "br", "base_provider_stop_sha256": "stop", "retry_manifest_sha256": "m", "base_fixture_sha256": "fixture-file",
           "source_digests": m["source"]["source_digests"], "registered_at": "2026-10-09T01:00:00+00:00"}
    completion = {"complete": True, "no_terminal_provider_error": True, "selected_trial_ids": ids, "completed_trial_ids": ids,
                  "recorded_trials": 9, "exact_selected_slots": True, "source_drift": [], "base_archive_changed": False, "terminal_provider_errors": [],
                  "registration_sha256": "registration-hash", "results_sha256": "rr", "finished_at": "2026-10-09T01:05:00+00:00"}
    rows = [{"candidate_id": f"case-{i}", "trial_index": 0, "status": "fail" if i == 0 else "unknown"} for i in range(9)]
    return {"base_manifest": m, "retry_manifest": deepcopy(m), "registration": reg, "completion": completion,
            "base_hashes": base, "retry_hashes": new, "original_errors": [{"trial": tid, "http_status": 402} for tid in ids], "retry_errors": [], "retry_rows": rows}


def test_exact_contract_accepts_all_business_failures_and_unknowns():
    args = contract()
    assert retry.validate_contract(**args) == sorted(args["registration"]["selected_trial_ids"])


@pytest.mark.parametrize("field,value", [("execution_kind", "judge_only"), ("retry_policy", "best_of"), ("selection_rule", "unknown_only"), ("authorized_user_instruction", ""), ("active_trial_count", 8), ("executor_workers", 9), ("base_results_sha256", "changed"), ("base_fixture_sha256", "canonical-fixture")])
def test_registration_must_preserve_authorization_selection_and_hashes(field, value):
    args = contract(); args["registration"][field] = value
    with pytest.raises(ValueError): retry.validate_contract(**args)


@pytest.mark.parametrize("field,value", [("complete", False), ("no_terminal_provider_error", False), ("source_drift", ["agent.py"]), ("base_archive_changed", True), ("registration_sha256", "wrong"), ("results_sha256", "wrong"), ("exact_selected_slots", False), ("recorded_trials", 8)])
def test_incomplete_or_changed_retry_is_rejected(field, value):
    args = contract(); args["completion"][field] = value
    with pytest.raises(ValueError): retry.validate_contract(**args)


@pytest.mark.parametrize("which", ["raw", "metrics"])
def test_terminal_error_in_either_evidence_source_is_rejected(which):
    args = contract()
    if which == "raw": args["retry_errors"] = [{"trial": "case-0-t0", "http_status": 402}]
    else: args["retry_rows"][0]["judge_metrics"] = {"errors": [{"status": 401}]}
    with pytest.raises(ValueError): retry.validate_contract(**args)


@pytest.mark.parametrize("target", ["selected_trial_ids", "dispatch_order"])
def test_omitting_original_fail_slot_or_adding_duplicate_is_rejected(target):
    args = contract(); args["registration"][target] = args["registration"][target][1:]
    with pytest.raises(ValueError): retry.validate_contract(**args)


def test_retry_contract_cannot_change_model_or_other_manifest_fields():
    args = contract(); args["retry_manifest"]["model"] = "changed-model"
    with pytest.raises(ValueError): retry.validate_contract(**args)


def test_status_never_controls_selection_or_replacement():
    base = [{"candidate_id": f"c-{i}", "trial_index": 0, "status": "pass"} for i in range(744)]
    base[0]["status"] = "fail"  # An original business fail with judge 402 is selected too.
    base[500]["status"] = "fail"  # Unselected business fail must remain.
    base[501]["status"] = "unknown"
    selected = [retry.analysis.key(r) for r in base[:9]]
    repeated = deepcopy(base[:9]); repeated[0]["status"] = "pass"; repeated[1]["status"] = "fail"; repeated[2]["status"] = "unknown"
    original = deepcopy(base)
    combined = {retry.analysis.key(r): r for r in retry.combine(base, repeated, selected)}
    assert combined["c-0-t0"]["status"] == "pass"
    assert combined["c-1-t0"]["status"] == "fail"  # Even if old was pass.
    assert combined["c-2-t0"]["status"] == "unknown"
    assert combined["c-500-t0"] is base[500] and combined["c-501-t0"] is base[501]
    assert base == original and len(combined) == 744


@pytest.mark.parametrize("change", ["missing", "duplicate", "unregistered"])
def test_replacement_set_is_exact_once(change):
    base = [{"candidate_id": f"c-{i}", "trial_index": 0, "status": "fail"} for i in range(744)]
    selected = [retry.analysis.key(r) for r in base[:9]]; rows = deepcopy(base[:9])
    if change == "missing": rows.pop()
    elif change == "duplicate": rows.append(deepcopy(rows[0]))
    else: rows[-1]["candidate_id"] = "different"
    with pytest.raises(ValueError): retry.combine(base, rows, selected)


def test_unknown_None_inside_failed_layer_is_retained_for_review():
    row = fixture.row("G01", state="fail")
    row["scoring"]["behavior"]["checks"] = {"observed_failure": False, "missing_read": None}
    assert not retry.analysis.unknown_evidence(row)
    assert retry.unknown_evidence(row)
    assert row["status"] == "fail"


def test_runtime_capture_failure_lineage_uses_actual_failure_file(tmp_path):
    folder = tmp_path / "trials" / "G01-t0"; folder.mkdir(parents=True)
    (folder / "unexpected-failure.json").write_text("{}")
    row = {"status": "unknown", "runtime_failure": {"classification": "harness_failure"}, "artifact_sha256": None}
    assert retry.trial_result_path(tmp_path, "G01-t0", row).name == "unexpected-failure.json"
    row["status"] = "pass"
    with pytest.raises(ValueError): retry.trial_result_path(tmp_path, "G01-t0", row)


def test_report_keeps_744_logical_slots_and_charges_all_753_executions(tmp_path):
    run = fixture.archive.__wrapped__(tmp_path)
    m, tasks, rows, artifacts, integrity = retry.analysis.validate(run)
    repeated = deepcopy(rows[:9]); chosen = [retry.analysis.key(r) for r in repeated]
    for row in repeated:
        for role in ("agent", "customer", "judge"): row[role + "_metrics"]["cost_usd_estimated"] = .002
    repeated[0]["status"] = "fail"; repeated[0]["scoring"]["outcome"]["status"] = "fail"
    rows[500]["status"] = "unknown"; rows[500]["response"]["status"] = "unknown"
    merged = retry.combine(rows, repeated, chosen)
    for name in ("retry-registration.json", "retry-completion.json"): (run / name).write_text("{}")
    data = {"manifest": m, "tasks": tasks, "rows": merged, "artifacts": artifacts, "base_rows": rows, "retry_rows": repeated,
            "base": run, "retry": run, "selected": chosen, "lineage": {}, "base_integrity": integrity, "retry_integrity": {"complete_plan": False}}
    report = retry.build_report(data)
    assert report["mode"] == "user_authorized_infrastructure_retry"
    assert report["run"] is None and report["logical_trial_slots"] == 744 and report["actual_executed_trials"] == 753
    assert report["retained_original_slots"] == 735 and report["replaced_retry_slots"] == 9
    assert report["metrics"]["overall"]["trial_counts"] == {"pass": 742, "fail": 1, "unknown": 1}
    assert report["costs"]["actual_executed_753"]["total_usd_estimated"] == pytest.approx(744 * .003 + 9 * .006)
    assert report["costs"]["logical_selected_744_not_total_spend"]["total_usd_estimated"] == pytest.approx(735 * .003 + 9 * .006)
    assert report["integrity"]["ordinary_complete_run"] is False
    assert {r["source_origin"] for r in report["per_trial"]} == {"base", "retry"}
    assert sum(r["source_origin"] == "retry" for r in report["per_trial"]) == 9
    assert len(report["slices"]["root_seed_family"]) == 12
