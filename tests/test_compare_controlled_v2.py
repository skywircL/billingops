"""Strict report comparison uses synthetic frozen scores; never an API or grader."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from test_analyze_controlled_v2 import archive, dump

SOURCE = Path(__file__).resolve().parents[1] / "scripts/compare_controlled_v2.py"
spec = importlib.util.spec_from_file_location("controlled_comparison", SOURCE)
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)
analysis = comparison.analysis


def metadata():
    ids = [f"case-{n}" for n in range(128)]
    return {"mode": "unified", "variant": "B0-offline-test", "runner_version": "v2-runner-1.3",
            "candidate_ids": ids, "case_profiles": {cid: {} for cid in ids},
            "planned_trials": {cid: 7 if n < 90 else 3 for n, cid in enumerate(ids)},
            "total_trials": 744, "fixture_sha256": "same-fixture", "risk_assignment": {"elevated": ids[:90]},
            "cohorts": {"benign": ids, "benign_must_act": []}, "model": {"id": "offline-test"},
            "policy_context": {"sha256": "same-context"}, "evaluation_versions": {"scorer": "same", "response": "same", "controls": "same"},
            "runtime": {"agent_deadline_s": 300}, "deadline_s": 300, "max_rounds": 16,
            "seed": 20261008, "workers": 16, "estimated_usd_per_million_tokens": {"input": .3},
            "source": {"provenance": "before_execution", "model_config": {"model": "offline-test"},
                       "tool_schemas_sha256": "same-tools", "source_digests": {
                           "src/billingops/runtime.py": "before-agent", "src/billing_service/refunds.py": "before-billing",
                           "src/billingops/evaluation/v2_runner.py": "before-runner",
                           "src/billingops/evaluation/v2_scoring.py": "same-scorer"}}}


def register(before, after, old_hash="before-manifest", new_hash="after-manifest"):
    def entries(changes, category=False):
        return [{"path": path, **values, "reason": "Explicit synthetic test implementation change",
                 **({"category": comparison.source_category(path)} if category else {})}
                for path, values in changes.items()]
    return {"schema_version": comparison.REGISTRY_VERSION,
            "before_manifest_sha256": old_hash, "after_manifest_sha256": new_hash,
            "manifest_changes": entries(comparison.changed_fields(before, after, ignored=comparison.IGNORED_MANIFEST_FIELDS)),
            "source_changes": entries(comparison.changed_fields(before["source"]["source_digests"], after["source"]["source_digests"]), True),
            "source_metadata_changes": entries(comparison.changed_fields(before["source"], after["source"], ignored={"captured_at", "source_digests"}))}


def revised(before):
    after = deepcopy(before)
    after.update(agent_revision="B1", variant="B1-offline-test", runner_version="v2-runner-1.4")
    for path in ("src/billingops/runtime.py", "src/billing_service/refunds.py", "src/billingops/evaluation/v2_runner.py"):
        after["source"]["source_digests"][path] = "after-" + path
    return after


def test_exactly_registered_agent_billing_and_runner_changes_are_comparable():
    before = metadata()
    after = revised(before)
    report = comparison.comparable(before, after, register(before, after), "before-manifest", "after-manifest")
    assert report["case_count"] == 128 and report["planned_trials_per_revision"] == 744
    assert len(report["source_changes"]) == 3


@pytest.mark.parametrize("field", ["fixture_sha256", "case_profiles", "planned_trials", "risk_assignment", "cohorts", "model", "policy_context", "evaluation_versions", "deadline_s", "workers"])
def test_explicit_registration_cannot_waive_changed_evaluation_conditions(field):
    before = metadata()
    after = revised(before)
    if field == "planned_trials":
        after[field]["case-0"], after[field]["case-127"] = 3, 7
    elif isinstance(after[field], dict):
        after[field]["changed"] = True
    else:
        after[field] = "changed"
    with pytest.raises(ValueError, match="fixed evaluation condition"):
        comparison.comparable(before, after, register(before, after), "before-manifest", "after-manifest")


@pytest.mark.parametrize("path", ["src/billingops/evaluation/v2_scoring.py", "src/billingops/evaluation/v2_controls.py", "data/tasks/dynamic-v2/gold-fixtures-v0.2.json", "docs/BUSINESS_SPEC_V2.md"])
def test_registered_implementation_exception_cannot_hide_grader_control_or_business_changes(path):
    before = metadata()
    after = revised(before)
    after["source"]["source_digests"][path] = "different"
    with pytest.raises(ValueError, match="cannot be waived"):
        comparison.comparable(before, after, register(before, after), "before-manifest", "after-manifest")


@pytest.mark.parametrize("problem", ["unregistered", "wrong_hash", "wrong_reason", "extra_entry", "partial"])
def test_stale_or_incomplete_comparison_declarations_are_rejected(problem):
    before = metadata()
    after = revised(before)
    registry = register(before, after)
    if problem == "unregistered":
        registry["source_changes"].pop()
    elif problem == "wrong_hash":
        registry["after_manifest_sha256"] = "other-run"
    elif problem == "wrong_reason":
        registry["source_changes"][0]["reason"] = ""
    elif problem == "extra_entry":
        registry["source_changes"].append(deepcopy(registry["source_changes"][0]))
    else:
        after["total_trials"] = 743
    with pytest.raises(ValueError):
        comparison.comparable(before, after, registry, "before-manifest", "after-manifest")


def enrich_manifest(run, revision):
    path = run / "manifest.json"
    manifest = json.loads(path.read_text())
    for key, value in metadata().items():
        manifest.setdefault(key, value)
    manifest["runner_version"] = "v2-runner-1.3" if revision == "B0" else "v2-runner-1.4"
    if revision == "B1":
        manifest.update(agent_revision="B1", variant="B1-offline-test")
    manifest["plan_sha256"] = analysis.digest({k: v for k, v in manifest.items() if k not in analysis.MANIFEST_METADATA})
    dump(path, manifest)
    return manifest


def test_complete_comparison_binds_reports_and_preserves_unknowns_without_rescoring(archive, tmp_path):
    before_run = archive
    nested = tmp_path / "after"
    nested.mkdir()
    after_run = globals()["archive"].__wrapped__(nested)
    old = enrich_manifest(before_run, "B0")
    new = enrich_manifest(after_run, "B1")
    before_report, after_report = tmp_path / "b0-report", tmp_path / "b1-report"
    analysis.analyze(before_run, before_report)
    analysis.analyze(after_run, after_report)
    registry = register(old, new, analysis.sha(before_run / "manifest.json"), analysis.sha(after_run / "manifest.json"))
    registry_path = tmp_path / "changes.json"
    dump(registry_path, registry)
    old_hashes = {p: analysis.sha(p) for run in (before_run, after_run) for p in run.rglob("*") if p.is_file()}
    out = tmp_path / "comparison"
    result = comparison.compare(before_report, after_report, registry_path, out)
    report = json.loads((out / "comparison.json").read_text())
    assert result["trials_per_revision"] == 744
    assert result["overall_case_equal_delta_percentage_points"] == 0
    assert report["metrics"]["original_32"]["overall"]["before"]["case_count"] == 32
    assert report["metrics"]["mutations_96"]["overall"]["after"]["case_count"] == 96
    assert report["guardrails"]["combined_with_optimization"] is False
    assert report["human_review"] == "pending" and report["hidden_count"] == 0
    assert report["false_refusal_candidates"]["after"]["human_confirmed_rate"] is None
    assert report["metrics"]["batch_new_capability"]["overall"]["before"]["case_count"] == 0
    assert all(analysis.sha(path) == digest for path, digest in old_hashes.items())
    corrupted = json.loads((after_report / "report.json").read_text())
    corrupted["metrics"]["overall"]["case_equal"]["pass"] = .5
    dump(after_report / "report.json", corrupted)
    with pytest.raises(ValueError, match="aggregate differs"):
        comparison.load_report(after_report)


def test_unknown_remains_a_separate_delta_and_taxonomy_does_not_claim_confirmed_errors():
    profiles = {"a": {"seed_candidate_id": "A"}}
    def data(state):
        return {"manifest": {"planned_trials": {"a": 3}, "case_profiles": profiles},
                "rows": [{"candidate_id": "a", "status": state, "scoring": {"failure_taxonomy": ["clarification"]}}
                         for _ in range(3)]}
    before, after = data("fail"), data("unknown")
    result = comparison.metric_comparison(before, after, ["a"])["overall"]
    assert result["delta_percentage_points"]["case_equal"] == {"pass": 0, "fail": -100, "unknown": 100}
    taxonomy = comparison.taxonomy_comparison(before, after)
    assert taxonomy["labels"][0]["after"]["by_overall_status"]["unknown"] == 3
    assert taxonomy["human_review"] == "pending"
