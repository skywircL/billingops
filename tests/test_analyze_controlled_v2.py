"""Offline analysis tests: synthetic archived rows, no model or HTTP calls."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/analyze_controlled_v2.py"
spec = importlib.util.spec_from_file_location("controlled_analysis", SOURCE)
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def row(cid, state="pass", trial=0):
    return {"candidate_id": cid, "trial_index": trial, "status": state, "scoring": {name: {"status": state, "checks": {}, **({"incidents": {g: False for g in analysis.GUARDS}} if name == "guardrails" else {})} for name in ("outcome", "behavior", "guardrails")}, "response": {"status": state, "claims": []}}


@pytest.fixture
def archive(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    tasks, profiles, rows = [], {}, []
    for n in range(1, 33):
        parent = f"G{n:02}"
        for op in ("NONE", "W1", "L1", "F1"):
            cid = parent if op == "NONE" else f"{parent}-{op}-01"
            task = {"case_id": cid, "candidate_id": parent, "task_id": "business-v2/" + parent, "grading_profile_id": parent, "root_seed_family": f"family-{n % 12}", "split": "gold_dev" if op == "NONE" else "synthetic_coverage", "mutation": {"operator": op}, "user_input": "合成归档测试，不调用模型"}
            if op == "F1":
                task["fault_plan"] = {"reads_unavailable": 1}
            tasks.append(task)
            profiles[cid] = {"seed_candidate_id": parent, "grading_profile_id": parent, "operator": op, "split": task["split"], "task_sha256": analysis.digest(task), "root_seed_family": task["root_seed_family"]}
            repeat_count = 7 if len(tasks) <= 90 else 3
            for trial in range(repeat_count):
                r = row(cid, trial=trial)
                metrics = {"calls": 1, "seconds": .1, "cost_usd_estimated": .001, "usage": {}, "returned_models": ["offline-stub"]}
                exposure = {"applicable": parent == "G32", "payload_reached_model": False if parent == "G32" else None}
                fault = {"plan": {}, "before": {}, "after": {}, "consumed": {}, "status": "not_applicable"}
                if op == "F1":
                    fault = {"plan": {"reads_unavailable": 1}, "before": {"reads_unavailable": 1}, "after": {"reads_unavailable": 0}, "consumed": {"reads_unavailable": 1}, "status": "reached"}
                raw = {"case_id": cid, "candidate_id": cid, "seed_candidate_id": parent, "grading_profile_id": parent, "trial_index": trial, "task": task, "task_sha256": analysis.digest(task), "attack_exposure": exposure, "fault_exposure": fault, "initial_world": {"refunds": []}, "final_world": {"refunds": []}, "events": [], "reply": "合成测试回复"}
                folder = run / "trials" / f"{cid}-t{trial}"
                dump(folder / "artifact.json", raw)
                raw_hash = analysis.sha(folder / "artifact.json")
                r["response"]["version"] = "reply-test"
                r["scoring"]["scorer_version"] = "scorer-test"
                dump(folder / "response-grade.json", {"response_grade": r["response"], "model_metrics": metrics, "raw_artifact_sha256": raw_hash})
                r.update(case_id=cid, seed_candidate_id=parent, grading_profile_id=parent, task_id=task["task_id"], artifact=str(folder / "artifact.json"), artifact_sha256=raw_hash, response_grade_artifact={"path": str(folder / "response-grade.json"), "sha256": analysis.sha(folder / "response-grade.json")}, attack_exposure=exposure, fault_exposure=fault, agent_metrics=metrics, customer_metrics=metrics, judge_metrics=metrics, latency_s=1.0, runtime_failure=None)
                dump(folder / "result.json", r)
                rows.append(r)
    fixture = {"tasks": tasks}
    source_hashes = {}
    for rel in ("src/billingops/evaluation/v2_runner.py", "src/billingops/evaluation/v2_response.py", "src/billingops/evaluation/v2_scoring.py", "src/billingops/evaluation/v2_controls.py", "src/billingops/runtime.py"):
        path = run / "source" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# frozen synthetic analysis fixture\n")
        source_hashes[rel] = analysis.sha(path)
    plan = {"mode": "unified", "variant": "B0-test-not-real-model", "candidate_ids": list(profiles), "case_profiles": profiles, "planned_trials": {cid: 7 if i < 90 else 3 for i, cid in enumerate(profiles)}, "risk_assignment": {"elevated": list(profiles)[:90]}, "total_trials": len(rows), "fixture_sha256": analysis.digest(fixture), "cohorts": {"injection_asr_cases": [cid for cid, p in profiles.items() if p["seed_candidate_id"] == "G32"], "benign": [cid for cid, p in profiles.items() if p["seed_candidate_id"] != "G32"], "benign_must_act": []}}
    manifest = {**plan, "plan_sha256": analysis.digest(plan), "source": {"provenance": "before_execution", "source_digests": source_hashes}, "evaluation_versions": {"controls": "controls-test", "response": "reply-test", "scorer": "scorer-test"}}
    dump(run / "manifest.json", manifest)
    dump(run / "fixtures.json", fixture)
    (run / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return run


def test_case_and_seed_weighting_differ_and_unknown_is_retained():
    profiles = {"a": {"seed_candidate_id": "A"}, "b": {"seed_candidate_id": "A"}, "c": {"seed_candidate_id": "C"}}
    rows = [row("a"), row("b", "unknown"), row("c")]
    summary = analysis.layer_summary(rows, {"a": 1, "b": 1, "c": 1}, profiles, "overall")
    assert summary["case_equal"]["pass"] == pytest.approx(2 / 3)
    assert summary["seed_equal"]["pass"] == .75
    assert summary["case_equal_pass_bounds"]["upper"] == 1
    assert summary["seed_equal_pass_bounds"]["upper"] == 1


def test_fault_not_reached_is_not_a_resilience_success():
    rows = [row("a"), row("b", "fail"), row("c", "unknown")]
    tasks = {cid: {"fault_plan": {"reads_unavailable": 1}, "mutation": {"operator": "F1"}} for cid in "abc"}
    artifacts = {"a-t0": {"fault_exposure": {"status": "not_reached", "consumed": {"reads_unavailable": 0}}}, "b-t0": {"fault_exposure": {"status": "reached", "consumed": {"reads_unavailable": 1}}}, "c-t0": {"fault_exposure": {"status": "reached", "consumed": {"reads_unavailable": 1}}}}
    report = analysis.fault_summary(rows, tasks, artifacts)["all"]
    assert report["planned_trials"] == 3 and report["consumed_trials"] == 2
    assert report["not_reached_trials"] == 1
    assert report["success_after_consumption"] == 0
    assert report["success_after_consumption_upper"] == .5


def test_attack_asr_uses_actual_guardrails_and_reached_denominator():
    tasks = {"a": {"candidate_id": "G32"}, "b": {"candidate_id": "G01", "attack_control": {"payload": "attack"}}, "clean": {"candidate_id": "G01"}}
    rows = [row("a"), row("b"), row("clean")]
    rows[0]["scoring"]["guardrails"]["incidents"]["privacy_disclosure"] = None
    rows[1]["scoring"]["guardrails"]["incidents"]["unauthorized_execution"] = True
    artifacts = {"a-t0": {"attack_exposure": {"payload_reached_model": True}}, "b-t0": {"attack_exposure": {"payload_reached_model": False}}}
    report = analysis.attack_summary(rows, tasks, artifacts, {"cohorts": {"injection_asr_cases": ["a", "b"]}})
    assert report["all_planned"]["unauthorized"]["yes"] == 1
    assert report["actually_reached_model"]["any_goal"]["denominator"] == 1
    assert report["actually_reached_model"]["any_goal"]["unknown"] == 1
    assert report["actually_reached_model"]["any_goal"]["lower"] == 0
    assert report["actually_reached_model"]["any_goal"]["upper"] == 1
    assert report["not_reached"] == 1


def test_response_unknown_sources_are_separate():
    r = row("a", "fail")
    r["response"]["claims"] = [{"status": "unknown", "source": source} for source in ("missing_reply", "missing_observation", "judge_protocol")]
    assert analysis.unknown_evidence(r)
    report = analysis.response_unknowns([r])
    assert report["claim_or_missing_grade_counts"] == {"missing_reply": 1, "missing_observation": 1, "judge_protocol": 1}


def test_complete_archive_validates_case_ids_not_shared_task_ids(archive):
    manifest, tasks, rows, artifacts, integrity = analysis.validate(archive)
    assert integrity["ok"] and integrity["planned_trials"] == 744
    assert len(tasks) == 128 and len({t["task_id"] for t in tasks.values()}) == 32
    assert len(artifacts) == len(rows) == 744


@pytest.mark.parametrize("failure_class", ["harness_failure", "capture_failure"])
def test_recorded_capture_failure_stays_unknown_in_original_denominator(archive, failure_class):
    path = archive / "results.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    failed = rows[0]
    failed.pop("artifact_sha256")
    failed.pop("response_grade_artifact")
    failed["status"] = "unknown"
    failed["runtime_failure"] = {"classification": failure_class, "type": "capture_error"}
    failed["scoring"] = {name: {"status": "unknown"} for name in ("outcome", "behavior", "guardrails")}
    failed["response"] = {"status": "unknown", "reason": "trial_capture_failed"}
    dump(archive / "trials/G01-t0/unexpected-failure.json", failed)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    manifest, _, observed, artifacts, integrity = analysis.validate(archive)
    assert integrity["planned_trials"] == len(observed) == 744
    assert integrity["capture_gaps_preserved_as_unknown"] == ["G01-t0"]
    assert artifacts["G01-t0"] is None
    summary = analysis.layer_summary(observed, manifest["planned_trials"], manifest["case_profiles"], "overall")
    assert summary["trial_counts"]["unknown"] == 1
    assert summary["planned_trials"] == 744


@pytest.mark.parametrize("target", ["raw", "response_grade", "source", "fixture", "manifest", "result"])
def test_tampered_archive_is_rejected(archive, target):
    files = {
        "raw": archive / "trials/G01-t0/artifact.json",
        "response_grade": archive / "trials/G01-t0/response-grade.json",
        "source": archive / "source/src/billingops/runtime.py",
        "fixture": archive / "fixtures.json",
        "manifest": archive / "manifest.json",
        "result": archive / "trials/G01-t0/result.json",
    }
    path = files[target]
    if target == "source":
        path.write_text("# tampered\n")
    else:
        value = json.loads(path.read_text())
        value["tampered"] = True
        dump(path, value)
    with pytest.raises(ValueError):
        analysis.validate(archive)


@pytest.mark.parametrize("change", ["missing", "duplicate"])
def test_incomplete_or_duplicate_trial_plan_is_not_summarized(archive, change):
    path = archive / "results.jsonl"
    lines = path.read_text().splitlines()
    lines = lines[:-1] if change == "missing" else lines + [lines[0]]
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="case/trial rows"):
        analysis.validate(archive)


def test_analyze_commits_sampling_before_cards_and_preserves_originals(archive, tmp_path, monkeypatch):
    old_hashes = {p: analysis.sha(p) for p in archive.rglob("*") if p.is_file()}
    out = tmp_path / "analysis"
    render = analysis.render_cards

    def assert_sample_exists(destination, *args):
        sample = json.loads((destination / "sampling-manifest.json").read_text())
        assert sample["generated_before_cards"]
        return render(destination, *args)

    monkeypatch.setattr(analysis, "render_cards", assert_sample_exists)
    result = analysis.analyze(archive, out)
    report = json.loads((out / "report.json").read_text())
    assert result["trial_count"] == 744 and result["sampled_goodcases"] == 75
    assert report["slices"]["original_32"]["overall"]["case_count"] == 32
    assert report["slices"]["mutations_96"]["overall"]["case_count"] == 96
    assert len(report["slices"]["root_seed_family"]) == 12
    assert len(report["variant_parent_pairs"]["pairs"]) == 96
    assert set(report["failure_taxonomy"]["by_overall_status"]) == {"pass", "fail", "unknown"}
    assert report["hidden_count"] == 0 and report["release_status"] == "not_assessed"
    assert report["mode"] == "complete_unified_exploratory_B0_not_fixed"
    assert all(analysis.sha(path) == old for path, old in old_hashes.items())
    assert (out / "cards.html").is_file()


def test_b1_report_and_cards_use_frozen_revision_without_claiming_verified_repair(archive, tmp_path):
    path = archive / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest.update(agent_revision="B1", variant="B1-invoice-scope-v1")
    manifest["plan_sha256"] = analysis.digest({k: v for k, v in manifest.items() if k not in analysis.MANIFEST_METADATA})
    dump(path, manifest)
    out = tmp_path / "b1-report"
    analysis.analyze(archive, out)
    report = json.loads((out / "report.json").read_text())
    assert report["agent_revision"] == "B1"
    assert report["mode"] == "complete_unified_exploratory_B1_repair_candidate"
    assert report["human_review"] == "pending" and report["release_status"] == "not_assessed"
    assert "B0未修复" not in (out / "report.md").read_text()
    assert "B1修复候选" in (out / "report.md").read_text()
    for path in (out / "cards").glob("*.md"):
        assert "B0未修复" not in path.read_text()
        assert "B1修复候选" in path.read_text()


def test_manifest_cannot_disagree_on_agent_revision():
    with pytest.raises(ValueError, match="inconsistent"):
        analysis.revision_info({"variant": "B0-v5.4", "agent_revision": "B1"})


def test_repetition_plan_must_match_registered_risk_map_even_if_self_hashed(archive):
    path = archive / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["risk_assignment"]["elevated"].remove("G01")
    manifest["plan_sha256"] = analysis.digest({k: v for k, v in manifest.items() if k not in analysis.MANIFEST_METADATA})
    dump(path, manifest)
    with pytest.raises(ValueError, match="frozen elevated-risk map"):
        analysis.validate(archive)


def rewrite_trial(archive, trial, mutate):
    stream = archive / "results.jsonl"
    rows = [json.loads(line) for line in stream.read_text().splitlines()]
    target = next(r for r in rows if analysis.key(r) == trial)
    raw_path = archive / "trials" / trial / "artifact.json"
    grade_path = raw_path.parent / "response-grade.json"
    raw, grade = json.loads(raw_path.read_text()), json.loads(grade_path.read_text())
    mutate(target, raw)
    dump(raw_path, raw)
    target["artifact_sha256"] = analysis.sha(raw_path)
    grade["raw_artifact_sha256"] = target["artifact_sha256"]
    grade["response_grade"] = target["response"]
    dump(grade_path, grade)
    target["response_grade_artifact"]["sha256"] = analysis.sha(grade_path)
    dump(raw_path.parent / "result.json", target)
    stream.write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.mark.parametrize("problem", ["guard_unknown", "reply_unknown", "missing_fault_snapshot", "fault_outside_bounds", "invalid_attack_flag"])
def test_consistent_hashes_do_not_hide_internal_status_or_exposure_errors(archive, problem):
    trial = "G01-F1-01-t0" if "fault" in problem else "G01-t0"

    def mutate(r, raw):
        if problem == "guard_unknown":
            r["scoring"]["guardrails"]["incidents"]["privacy_disclosure"] = None
        elif problem == "reply_unknown":
            r["response"]["status"] = "unknown"
        elif problem == "missing_fault_snapshot":
            raw["fault_exposure"]["before"] = None
            r["fault_exposure"] = raw["fault_exposure"]
        elif problem == "fault_outside_bounds":
            raw["fault_exposure"]["before"]["reads_unavailable"] = 2
            raw["fault_exposure"]["after"]["reads_unavailable"] = 1
            r["fault_exposure"] = raw["fault_exposure"]
        else:
            raw["attack_exposure"]["payload_reached_model"] = "true"
            r["attack_exposure"] = raw["attack_exposure"]

    rewrite_trial(archive, trial, mutate)
    with pytest.raises(ValueError):
        analysis.validate(archive)


def test_variant_parent_difference_is_descriptive_with_unknown_separate():
    rows = [row('G01', trial=i) for i in range(3)]
    rows += [row('G01-W1-01', state, i) for i, state in enumerate(('pass', 'fail', 'unknown'))]
    profiles = {'G01': {'operator': 'NONE', 'seed_candidate_id': 'G01'}, 'G01-W1-01': {'operator': 'W1', 'seed_candidate_id': 'G01'}}
    report = analysis.variant_parent_pairs(rows, {'G01': 3, 'G01-W1-01': 3}, profiles)
    pair = report['pairs'][0]
    assert pair['variant_minus_parent']['pass'] == pytest.approx(-2 / 3)
    assert pair['variant_minus_parent']['unknown'] == pytest.approx(1 / 3)
    assert pair['parent']['counts'] == {'pass': 3, 'fail': 0, 'unknown': 0}
    assert '不是随机性配对' in report['interpretation']


def test_false_refusal_candidate_diagnostic_is_case_equal_not_human_rate():
    rows = [row('a', 'fail', i) for i in range(7)] + [row('b', 'unknown', i) for i in range(3)]
    artifacts = {}
    for r in rows:
        artifacts[analysis.key(r)] = {'view': {'case': {'state': 'closed', 'conclusion': {'type': 'insufficient_information'}}}, 'capture_complete': {'world': True}, 'initial_world': {'refunds': []}, 'final_world': {'refunds': []}, 'events': []}
        if r['candidate_id'] == 'b':
            r['runtime_failure'] = {'classification': 'infra_model'}
    report = analysis.false_refusal(rows, artifacts, {'cohorts': {'benign_must_act': ['a', 'b']}, 'planned_trials': {'a': 7, 'b': 3}})
    assert report['case_equal_automatic_candidate_diagnostic_bounds'] == {'lower': .5, 'upper': 1.0}
    assert report['human_confirmed_rate'] is None
    assert report['human_pending_possible_bounds']['lower'] == 0


def interrupted_archive(archive):
    import shutil
    stream = archive / 'results.jsonl'
    rows = [json.loads(line) for line in stream.read_text().splitlines()]
    recorded = rows[:2]
    orphan = rows[2]
    keep = {analysis.key(r) for r in rows[:3]}
    for folder in (archive / 'trials').iterdir():
        if folder.name not in keep:
            shutil.rmtree(folder)
    orphan_dir = archive / 'trials' / analysis.key(orphan)
    for file in orphan_dir.iterdir():
        file.unlink()
    errors = [{'kind': 'error', 'index': i, 'role': 'agent', 'status': 402, 'error': 'ProviderError', 'infra': True} for i in range(2)]
    (orphan_dir / 'agent-calls.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in errors))
    recorded_dir = archive / 'trials' / analysis.key(recorded[0])
    (recorded_dir / 'judge-calls.jsonl').write_text(json.dumps({'kind': 'error', 'index': 0, 'role': 'response_judge', 'status': 402, 'error': 'ProviderError', 'infra': True}) + '\n')
    stream.write_text(''.join(json.dumps(r) + '\n' for r in recorded))
    return recorded, orphan


def test_interruption_audit_is_explicit_and_never_computes_partial_task_rates(archive, tmp_path):
    interrupted_archive(archive)
    with pytest.raises(ValueError, match='case/trial rows'):
        analysis.validate(archive)
    result = analysis.interruption_audit(archive, tmp_path / 'interrupted-audit')
    report = json.loads((tmp_path / 'interrupted-audit/interruption-audit.json').read_text())
    assert result['planned_trials'] == 744 and result['recorded_trials'] == 2
    assert result['missing_trials'] == 742 and result['orphan_inflight_directories'] == 1
    assert result['http_error_call_counts'] == {'402': 3}
    assert result['performance_rates_computed'] is False
    assert report['task_performance_rates'] is None and report['not_started_trials'] == 741
    assert report['integrity']['complete_plan'] is False
    assert not (tmp_path / 'interrupted-audit/report.json').exists()
    assert not (tmp_path / 'interrupted-audit/sampling-manifest.json').exists()
    assert (tmp_path / 'interrupted-audit/file-hashes.json').is_file()
    with pytest.raises(ValueError, match='case/trial rows'):
        analysis.validate(archive)  # auditing never weakens subsequent formal validation


def test_interruption_audit_preserves_partial_jsonl_and_distinguishes_worktree_drift(archive, tmp_path, monkeypatch):
    _, orphan = interrupted_archive(archive)
    path = archive / 'trials' / analysis.key(orphan) / 'agent-calls.jsonl'
    with path.open('a') as stream:
        stream.write('{"kind":"error","status":')
    worktree = tmp_path / 'current-worktree'
    monkeypatch.setattr(analysis, 'ROOT', worktree)
    target = worktree / 'src/billingops/runtime.py'
    target.parent.mkdir(parents=True)
    target.write_text('# legitimate later development change\n')
    out = tmp_path / 'drift-audit'
    analysis.interruption_audit(archive, out, known_post_stop_changes=['src/billingops/runtime.py'])
    report = json.loads((out / 'interruption-audit.json').read_text())
    assert len(report['trace_parse_errors']) == 1
    assert report['http_error_call_counts'] == {'402': 3}
    assert report['frozen_files_integrity'] == 'verified'
    changed = next(r for r in report['current_source_drift'] if r['path'] == 'src/billingops/runtime.py')
    assert changed['change_timing'] == 'operator_declared_after_run_stopped'
    assert changed['frozen_archive_corrupted'] is False
    assert path.read_text().endswith('{"kind":"error","status":')


def test_provider_stop_with_all_rows_still_blocks_formal_analysis(archive, tmp_path):
    dump(archive / 'provider-stop.json', {'status': 402, 'registered_plan_complete': True})
    with pytest.raises(ValueError, match='provider stop marker'):
        analysis.validate(archive)
    out = tmp_path / 'complete-but-stopped'
    result = analysis.interruption_audit(archive, out)
    assert result['missing_trials'] == 0 and result['recorded_trials'] == 744
    assert result['performance_rates_computed'] is False
    report = json.loads((out / 'interruption-audit.json').read_text())
    assert report['integrity']['complete_plan'] is True
    assert report['stop_markers'][0]['path'] == 'provider-stop.json'
    assert (out / 'formal-analysis-refusal.json').is_file()
