#!/usr/bin/env python3
"""Analyze a user-authorized, one-attempt infrastructure retry composite.

This is NOT a normal complete-run report. The interrupted base remains stopped.
All original HTTP402 slots, irrespective of scores, are replaced once; other
slots and all frozen grading verdicts remain unchanged. No API or regrading.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import importlib.util
import json
import math
from pathlib import Path
import random

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("retry_comparison_helpers", Path(__file__).with_name("compare_controlled_v2.py"))
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)
analysis = comparison.analysis
VERSION = "controlled-v2-authorized-infra-retry-analysis-2"
MODE = "user_authorized_infrastructure_retry"
REGISTRATION_SCHEMA = "controlled-v2-infra-retry-1"


def need(condition, message):
    if not condition:
        raise ValueError(message)


def exact_ids(values, expected, label):
    need(isinstance(values, list) and len(values) == len(set(values)) and set(values) == set(expected), label + " must contain exactly the fixed unique trial IDs")


def utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    need(parsed.tzinfo is not None, "timestamp must have explicit timezone")
    return parsed.astimezone(timezone.utc)


def provider_errors(run):
    records = []
    for role in ("agent", "customer", "judge"):
        for path in sorted((run / "trials").glob(f"*/{role}-calls.jsonl")):
            for line, text in enumerate(path.read_text().splitlines(), 1):
                call = json.loads(text)
                if call.get("kind") == "error" and call.get("status") in (401, 402):
                    records.append({"trial": path.parent.name, "role": role, "http_status": call["status"],
                                    "call_index": call.get("index"), "line": line, "path": str(path),
                                    "file_sha256": analysis.sha(path)})
    return records


def validate_contract(base_manifest, retry_manifest, registration, completion, *, base_hashes, retry_hashes, original_errors, retry_errors, retry_rows):
    """Pure contract checks; frozen archive/tool evidence is validated separately."""
    need(base_manifest == retry_manifest and base_hashes["manifest"] == retry_hashes["manifest"], "retry must carry the byte-identical original frozen manifest")
    need(registration.get("schema_version") == REGISTRATION_SCHEMA and registration.get("mode") == MODE, "unsupported retry registration")
    need(isinstance(registration.get("authorized_user_instruction"), str) and registration["authorized_user_instruction"].strip(), "explicit user retry authorization must be preserved")
    need(registration.get("selection_rule") == "all_original_http402_trials", "selection must include all original HTTP402 slots regardless of status")
    need(registration.get("retry_policy") == "one_full_run_trial_per_selected_slot_no_best_of" and registration.get("execution_kind") == "full_run_trial", "exactly one full trial retry, not judge-only or best-of")
    need(registration.get("executor_workers") == 16 and registration.get("active_trial_count") == 9, "registered full-trial retry population/workers differ")
    expected = {e["trial"] for e in original_errors if e["http_status"] == 402}
    need(len(expected) == 9, "this authorized retry applies to exactly nine original HTTP402 slots")
    exact_ids(registration.get("selected_trial_ids"), expected, "registration selection")
    exact_ids(registration.get("dispatch_order"), expected, "registered dispatch order")
    exact_ids([analysis.key(r) for r in retry_rows], expected, "recorded retries")
    need(not retry_errors, "terminal HTTP401/402 in a retry blocks the composite report")
    need(not any(error.get("status") in (401, 402) for row in retry_rows for role in ("agent", "customer", "judge")
                 for error in row.get(role + "_metrics", {}).get("errors", [])), "retry metrics contain terminal provider errors")
    for field, actual in (("base_manifest_sha256", base_hashes["manifest"]), ("base_results_sha256", base_hashes["results"]),
                          ("base_provider_stop_sha256", base_hashes["provider_stop"]), ("retry_manifest_sha256", retry_hashes["manifest"])):
        need(registration.get(field) == actual, "registration hash mismatch: " + field)
    need(registration.get("base_fixture_sha256") == base_hashes["fixture_bytes"], "registration fixture byte hash differs")
    need(registration.get("source_digests") == base_manifest["source"]["source_digests"], "registration frozen source hashes differ")
    need(completion.get("complete") is True and completion.get("no_terminal_provider_error") is True, "retry completion must confirm all nine attempts and no terminal provider error")
    exact_ids(completion.get("selected_trial_ids"), expected, "completion selection")
    exact_ids(completion.get("completed_trial_ids"), expected, "completion attempts")
    need(completion.get("recorded_trials") == 9 and completion.get("exact_selected_slots") is True, "retry completion must have nine exact rows")
    need(completion.get("source_drift") == [] and completion.get("base_archive_changed") is False, "retry source/base changed during execution")
    need(completion.get("terminal_provider_errors") == [], "completion contains terminal provider errors")
    need(completion.get("registration_sha256") == retry_hashes["registration"] and completion.get("results_sha256") == retry_hashes["results"], "completion hash binding mismatch")
    need(utc(completion["finished_at"]) >= utc(registration["registered_at"]), "completion predates registration")
    return sorted(expected)


def combine(base_rows, retry_rows, selected):
    """Unconditional replacement, never selected according to success/failure."""
    base = {analysis.key(r): r for r in base_rows}
    retry = {analysis.key(r): r for r in retry_rows}
    need(len(base) == len(base_rows) == 744, "base must retain exactly 744 unique logical slots")
    exact_ids(list(retry), selected, "replacement set")
    need(len(retry) == len(retry_rows), "duplicate retry attempts are forbidden")
    need(set(selected) <= set(base), "retry ID outside original plan")
    merged = [retry.get(key, row) for key, row in sorted(base.items())]
    need(len(merged) == 744 and len(base) - len(retry) == 735, "composite must contain 735 original and nine retried slots")
    return merged


def unknown_evidence(row):
    # Diagnostic coverage only; neither assertion nor layer status is changed.
    return analysis.unknown_evidence(row) or any(value is None for layer in ("outcome", "behavior")
        for value in row.get("scoring", {}).get(layer, {}).get("checks", {}).values())


def report_b0(directory):
    """Validate the independent B0 report; never load a retry via old comparator."""
    directory = directory.resolve()
    report = json.loads((directory / "report.json").read_text())
    run = Path(report["run"]).resolve()
    manifest, tasks, rows, artifacts, integrity = analysis.validate(run)
    need(analysis.revision_info(manifest)["agent_revision"] == "B0", "baseline must be frozen B0")
    need(report.get("mode") == "complete_unified_exploratory_B0_not_fixed", "baseline report mode differs")
    need(report.get("case_count") == 128 and report.get("seed_count") == 32 and report.get("hidden_count") == 0 and len(rows) == 744, "baseline report population differs")
    for field in ("manifest_sha256", "results_sha256", "fixture_sha256", "complete_plan", "recorded_trials", "planned_trials"):
        need(report.get("integrity", {}).get(field) == integrity[field], "baseline report binding differs: " + field)
    need(analysis.sha(directory / "analyzer-source.py") == report.get("analysis_program_sha256"), "baseline analyzer snapshot hash differs")
    for layer in analysis.LAYERS:
        need(report.get("metrics", {}).get(layer) == analysis.layer_summary(rows, manifest["planned_trials"], manifest["case_profiles"], layer), "baseline aggregate does not equal frozen verdicts")
    return {"directory": directory, "report": report, "run": run, "manifest": manifest, "tasks": tasks,
            "rows": rows, "artifacts": artifacts, "integrity": integrity, "report_sha256": analysis.sha(directory / "report.json")}


def archive_inventory(run):
    # Includes agent/customer/judge traces without printing model reasoning.
    paths = [run / n for n in ("manifest.json", "fixtures.json", "results.jsonl", "summary.json", "provider-stop.json", "incomplete-run.json", "retry-registration.json", "retry-completion.json") if (run / n).is_file()]
    manifest = json.loads((run / "manifest.json").read_text())
    paths += [run / "source" / name for name in manifest["source"]["source_digests"]]
    for pattern in ("*/result.json", "*/unexpected-failure.json", "*/artifact.json", "*/response-grade.json", "*/*-calls.jsonl"):
        paths.extend((run / "trials").glob(pattern))
    return {str(path): analysis.sha(path) for path in sorted(set(paths))}


def validate_inputs(base, retry, baseline_report, registry_path):
    base, retry = base.resolve(), retry.resolve()
    need(base != retry, "retry archive must be separate")
    m, tasks, base_rows, base_artifacts, base_integrity = analysis._validate_archive(base, require_complete=False)
    rm, rt, retry_rows, retry_artifacts, retry_integrity = analysis._validate_archive(retry, require_complete=False)
    need(base_integrity["complete_plan"] and len(base_rows) == 744 and m["total_trials"] == 744, "base must have all original 744 records")
    need(set(base_integrity["stop_markers"]) == {"provider-stop.json", "incomplete-run.json"} and not (base / "summary.json").exists(), "base must preserve its interrupted identity")
    need(not retry_integrity["stop_markers"] and len(retry_rows) == 9 and not retry_integrity["complete_plan"], "retry is only nine attempts, not an ordinary complete run")
    need(analysis.revision_info(m)["agent_revision"] == "B1", "retry target must be B1")
    need(tasks == rt and (base / "fixtures.json").read_bytes() == (retry / "fixtures.json").read_bytes(), "retry fixtures must be byte-identical")
    registration = json.loads((retry / "retry-registration.json").read_text())
    completion = json.loads((retry / "retry-completion.json").read_text())
    need(Path(registration["base_run"]).resolve() == base, "registration names a different base run")
    errors, new_errors = provider_errors(base), provider_errors(retry)
    selected = validate_contract(m, rm, registration, completion,
        base_hashes={"manifest": analysis.sha(base / "manifest.json"), "results": analysis.sha(base / "results.jsonl"), "provider_stop": analysis.sha(base / "provider-stop.json"), "fixture_bytes": analysis.sha(base / "fixtures.json")},
        retry_hashes={"manifest": analysis.sha(retry / "manifest.json"), "results": analysis.sha(retry / "results.jsonl"), "registration": analysis.sha(retry / "retry-registration.json")},
        original_errors=errors, retry_errors=new_errors, retry_rows=retry_rows)
    metric_selected = {analysis.key(row) for row in base_rows if any(error.get("status") == 402 for role in ("agent", "customer", "judge") for error in row.get(role + "_metrics", {}).get("errors", []))}
    need(metric_selected == set(selected), "original result metrics disagree with raw HTTP402 selection")
    exact_ids([path.name for path in (retry / "trials").iterdir() if path.is_dir()], selected, "retry trial directories (no extra unrecorded attempts)")
    working = registration.get("working_source_digests", {})
    need(set(working) == set(m["source"]["source_digests"]), "working source inventory must match original frozen source inventory")
    actual_drift = comparison.changed_fields(m["source"]["source_digests"], working)
    need(set(actual_drift) <= {"data/eval-design/dynamic-v1/protocol.yaml"}, "retry working implementation differs from frozen B1")
    declared_drift = registration.get("working_tree_differences", [])
    expected_drift = [{"path": path, "frozen_sha256": values["before"], "current_sha256": values["after"]} for path, values in sorted(actual_drift.items())]
    need(isinstance(declared_drift, list) and sorted(declared_drift, key=lambda v: v["path"]) == expected_drift, "retry working metadata changes do not exactly match declared hashes")
    need(completion.get("post_run_working_source_digests") == working, "retry working source drift was not closed out")
    wrapper = (retry / registration["wrapper_source"]).resolve()
    need(wrapper.is_relative_to(retry) and wrapper.is_file() and analysis.sha(wrapper) == registration["wrapper_sha256"], "registered retry wrapper snapshot hash differs")
    metadata_record = Path(registration["post_run_metadata_record"]).resolve()
    need(analysis.sha(metadata_record) == registration["post_run_metadata_sha256"], "post-stop operational metadata record hash differs")
    lineage = {}
    before = {analysis.key(r): r for r in base_rows}
    for row in retry_rows:
        trial = analysis.key(row)
        raw, original = retry_artifacts.get(trial), base_artifacts.get(trial)
        old_case = ((original or {}).get("view") or {}).get("case", {}).get("id")
        new_case = ((raw or {}).get("view") or {}).get("case", {}).get("id")
        observation_gap = bool(row.get("runtime_failure"))
        if raw and raw.get("initial_world") is not None:
            need(original is not None and raw["initial_world"] == original.get("initial_world"), trial + ": retry initial world differs")
        else:
            need(observation_gap, trial + ": full retry initial world missing without runtime failure")
        if old_case and new_case:
            need(old_case != new_case, trial + ": retry must create a fresh case")
        else:
            need(observation_gap, trial + ": full retry case identity missing without runtime failure")
        calls_path = retry / "trials" / trial / "agent-calls.jsonl"
        calls = [json.loads(line) for line in calls_path.read_text().splitlines()] if calls_path.is_file() else []
        requests = [c for c in calls if c.get("kind") == "request"]
        need((bool(requests) or observation_gap) and all(utc(c["at"]) >= utc(registration["registered_at"]) for c in requests), trial + ": no fresh post-registration Agent execution")
        for label, run, record in (("original", base, before[trial]), ("retry", retry, row)):
            result_path = trial_result_path(run, trial, record)
            lineage.setdefault(trial, {})[label] = {"run": str(run), "result_path": str(result_path),
                "result_record_kind": result_path.name, "result_sha256": analysis.sha(result_path), "artifact_path": record.get("_artifact_absolute", record.get("artifact")),
                "artifact_sha256": record.get("artifact_sha256"), "response_grade_artifact": record.get("response_grade_artifact"),
                "status": record["status"], "layers": {layer: analysis.status(record, layer) for layer in analysis.LAYERS},
                "runtime_failure": record.get("runtime_failure"), "failure_taxonomy": record.get("scoring", {}).get("failure_taxonomy", [])}
        lineage[trial].update(selected_by="all_original_http402_trials", used="retry_unconditionally", original_case_id=old_case, retry_case_id=new_case,
            fresh_execution_observation="captured" if raw and old_case and new_case and requests else "runtime_capture_gap_preserved; attempt_bound_by_registered_wrapper")
    rows = combine(base_rows, retry_rows, selected)
    artifacts = {**{key: value for key, value in base_artifacts.items() if key not in selected}, **retry_artifacts}
    baseline = report_b0(baseline_report)
    registry = json.loads(registry_path.read_text())
    comparable = comparison.comparable(baseline["manifest"], m, registry, baseline["integrity"]["manifest_sha256"], base_integrity["manifest_sha256"])
    hashes = {}
    for run in (base, retry, baseline["run"]): hashes.update(archive_inventory(run))
    for path in (registry_path, wrapper, metadata_record, Path(__file__).resolve(), Path(analysis.__file__).resolve(), Path(comparison.__file__).resolve(), baseline["directory"] / "report.json", baseline["directory"] / "analyzer-source.py"):
        hashes[str(path.resolve())] = analysis.sha(path)
    return {"base": base, "retry": retry, "manifest": m, "tasks": tasks, "rows": rows, "artifacts": artifacts,
            "base_rows": base_rows, "retry_rows": retry_rows, "baseline": baseline, "registration": registration,
            "completion": completion, "selected": selected, "lineage": lineage, "base_integrity": base_integrity,
            "retry_integrity": retry_integrity, "comparability": comparable, "registry": registry, "hashes": hashes,
            "original_terminal_error_calls": errors}


def trial_result_path(run, trial, row):
    path = run / "trials" / trial / "result.json"
    if path.is_file():
        return path
    failure = (row.get("runtime_failure") or {}).get("classification")
    alternative = path.with_name("unexpected-failure.json")
    need(row.get("status") == "unknown" and not row.get("artifact_sha256") and failure in {"harness_failure", "capture_failure"} and alternative.is_file(), "missing result must be a validated runtime capture failure")
    return alternative


def metrics(data, ids):
    return comparison.metrics(data, list(ids))


def build_report(data):
    m, rows, tasks, artifacts = (data[k] for k in ("manifest", "rows", "tasks", "artifacts"))
    profiles, planned = m["case_profiles"], m["planned_trials"]
    def measured(ids): return metrics(data, ids)
    slices = {"original_32": measured([c for c in planned if profiles[c]["operator"] == "NONE"]),
              "mutations_96": measured([c for c in planned if profiles[c]["operator"] != "NONE"])}
    slice_members = {}
    for dimension in ("operator", "split", "input_language", "root_seed_family"):
        groups = defaultdict(list)
        for cid in planned:
            value = ("en" if profiles[cid]["operator"] == "L1" else "zh") if dimension == "input_language" else profiles[cid][dimension]
            groups[value].append(cid)
        slices[dimension] = {value: measured(ids) for value, ids in sorted(groups.items())}
        slice_members[dimension] = dict(groups)
    full_success = sorted(analysis.key(r) for r in rows if all(analysis.status(r, layer) == "pass" for layer in analysis.LAYERS) and not unknown_evidence(r))
    sample = {"seed": analysis.SAMPLE_SEED, "fraction": .1, "rounding": "ceil", "candidate_trials": full_success,
              "candidate_list_sha256": analysis.digest(full_success), "selected_trials": sorted(random.Random(analysis.SAMPLE_SEED).sample(full_success, math.ceil(.1 * len(full_success)))),
              "generated_before_cards": True, "human_review": "pending"}
    guards = analysis.guardrail_summary(rows, artifacts)
    costs, latency, infra = analysis.costs_and_latency(rows)
    base_costs, base_latency, _ = analysis.costs_and_latency(data["base_rows"])
    retry_costs, retry_latency, _ = analysis.costs_and_latency(data["retry_rows"])
    actual_costs, actual_latency, actual_infra = analysis.costs_and_latency(data["base_rows"] + data["retry_rows"])
    taxonomy = Counter(label for r in rows for label in set(r.get("scoring", {}).get("failure_taxonomy", [])))
    per_trial = [{"trial": analysis.key(r), "case_id": r["candidate_id"], "seed_candidate_id": profiles[r["candidate_id"]]["seed_candidate_id"],
                  "operator": profiles[r["candidate_id"]]["operator"], "status": r["status"], "has_unknown_evidence": unknown_evidence(r),
                  "assertions": analysis.assertion_issues(r), "failure_taxonomy": r.get("scoring", {}).get("failure_taxonomy", []),
                  "runtime_failure": r.get("runtime_failure"), "artifact": r.get("_artifact_absolute", r.get("artifact")),
                  "source_origin": "retry" if analysis.key(r) in data["selected"] else "base", "human_review": "pending"} for r in rows]
    limitations = ["用户明确授权对全部原402试验各完整重试一次；这是735条原记录+9条重试的组合评测，不是未中断的普通744次运行。",
        "原run保留provider-stop/incomplete-run且仍无效完成；没有修改或覆盖原评分，不使用best-of，不归一deny/reject。",
        "逻辑分母744，实际执行753次；成本包含原失败尝试，报告不把重试成本隐藏为744次成本。",
        "128个case来自32个父种子、12个已曝光根族；Gold逐题人工复核与V2裁判校准待完成，Hidden=0。",
        "同题版本差为描述性工程修复和能力补齐结果，不是线上收益、训练效果或独立泛化结论。",
        "unknown与所有其他业务失败保留；安全门禁、故障触发和攻击触达单列，不与成功率加权。JSON中的guardrails pass是自动证据断言层通过率，不是安全率；unknown不表示已发生事故。",
        "原规则中已定位的审批枚举/终态映射疑点保持原判；本报告不是事后评分修正。"]
    return {"version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(), "mode": MODE,
            "agent_revision": "B1", "variant": m["variant"], "run": None, "source_runs": {"base": str(data["base"]), "retry": str(data["retry"])},
            "case_count": 128, "seed_count": 32, "declared_family_count": 12, "hidden_count": 0, "human_review": "pending", "release_status": "not_assessed",
            "logical_trial_slots": 744, "retained_original_slots": 735, "replaced_retry_slots": 9, "actual_executed_trials": 753,
            "integrity": {"composite_verified": True, "ordinary_complete_run": False, "base": data["base_integrity"], "retry": data["retry_integrity"], "registration_sha256": analysis.sha(data["retry"] / "retry-registration.json"), "completion_sha256": analysis.sha(data["retry"] / "retry-completion.json")},
            "metrics": measured(planned), "slices": slices, "slice_members": slice_members,
            "benign_task_metrics": measured(m["cohorts"]["benign"]), "guardrails": guards,
            "fault_exposure": analysis.fault_summary(rows, tasks, artifacts), "attack_exposure": analysis.attack_summary(rows, tasks, artifacts, m),
            "false_refusal": analysis.false_refusal(rows, artifacts, m), "response_unknowns": analysis.response_unknowns(rows),
            "failure_taxonomy": {"multi_label_trial_counts": dict(taxonomy), "by_overall_status": {state: dict(Counter(label for r in rows if r["status"] == state for label in set(r.get("scoring", {}).get("failure_taxonomy", [])))) for state in analysis.STATES}, "human_review": "pending", "sum_can_exceed_failed_trials": True},
            "repeat_stability": analysis.reliability(rows, planned), "variant_parent_pairs": analysis.variant_parent_pairs(rows, planned, profiles),
            "costs": {"actual_executed_753": actual_costs, "base_744": base_costs, "retry_9": retry_costs, "logical_selected_744_not_total_spend": costs},
            "latency": {"logical_selected_744": latency, "actual_executed_753": actual_latency, "base_744": base_latency, "retry_9": retry_latency},
            "runtime_failures": {"logical_selected_744": infra, "actual_executed_753": actual_infra},
            "per_trial": per_trial, "goodcase_sample": sample, "replacement_lineage": data["lineage"], "limitations": limitations,
            "analysis_program_sha256": analysis.sha(Path(__file__).resolve())}


def build_comparison(data, report):
    before, after = data["baseline"], data
    ids, profiles = data["manifest"]["candidate_ids"], data["manifest"]["case_profiles"]
    batch = [c for c in ids if len(data["tasks"][c].get("expected_refunds", [])) > 1]
    cohorts = {"all_128": ids, "original_32": [c for c in ids if profiles[c]["operator"] == "NONE"],
               "mutations_96": [c for c in ids if profiles[c]["operator"] != "NONE"], "benign": data["manifest"]["cohorts"]["benign"],
               "batch_new_capability": batch, "excluding_batch": [c for c in ids if c not in batch]}
    comparisons = {name: comparison.metric_comparison(before, after, selected) for name, selected in cohorts.items()}
    overall = comparisons["all_128"]["overall"]
    resume = (f"构建32个待人工审核模拟业务种子及96条受控变体，在固定128题、每版744个逻辑试验槽位上，"
              f"工程修复与能力补齐后的任务等权通过率由{overall['before']['case_equal']['pass']:.2%}变为{overall['after']['case_equal']['pass']:.2%}"
              f"（{overall['delta_percentage_points']['case_equal']['pass']:+.2f}个百分点）；B1因基础设施中断经授权重跑9条，共执行753次，安全与未知独立报告。")
    return {"version": VERSION, "mode": "B0_vs_user_authorized_infrastructure_retry_B1", "human_review": "pending", "hidden_count": 0,
            "comparability": {"B0_to_original_B1": data["comparability"], "retry_supplement": data["registration"], "same_frozen_grading": True},
            "baseline_report": str(before["directory"] / "report.json"), "baseline_report_sha256": before["report_sha256"],
            "cohort_members": cohorts, "metrics": comparisons, "failure_taxonomy": comparison.taxonomy_comparison(before, after),
            "guardrails": {"before": comparison._safety(before), "after": comparison._safety(after), "combined_with_optimization": False, "release_gate": "not_assessed"},
            "false_refusal_candidates": {"before": analysis.false_refusal(before["rows"], before["artifacts"], before["manifest"]), "after": report["false_refusal"]},
            "response_unknowns": {"before": analysis.response_unknowns(before["rows"]), "after": report["response_unknowns"]},
            "exposure": {"before": {"attacks": analysis.attack_summary(before["rows"], before["tasks"], before["artifacts"], before["manifest"]), "faults": analysis.fault_summary(before["rows"], before["tasks"], before["artifacts"])}, "after": {"attacks": report["attack_exposure"], "faults": report["fault_exposure"]}},
            "resources": {"before": {"actual_executed_trials": 744, "costs": analysis.costs_and_latency(before["rows"])[0], "latency": analysis.costs_and_latency(before["rows"])[1]}, "after": {"actual_executed_trials": 753, "costs": report["costs"], "latency": report["latency"]}},
            "resume_draft": resume, "limitations": report["limitations"] + ["B0未按B1重试规则追溯重跑；两版基础设施处理流程存在明确登记差异，不能隐藏这一限制。", "重复试验编号不代表随机性配对；case/seed等权差值不作为独立744样本显著性检验。"]}


def analyze(base, retry, baseline_report, registry_path, out):
    base, retry, baseline_report, registry_path, out = [Path(p).resolve() for p in (base, retry, baseline_report, registry_path, out)]
    need(not out.exists() and not any(out.is_relative_to(p) for p in (base, retry, baseline_report)), "output must be new and outside original runs/reports")
    data = validate_inputs(base, retry, baseline_report, registry_path)
    report, comparisons = build_report(data), None
    comparisons = build_comparison(data, report)
    need(data["hashes"] == {p: analysis.sha(Path(p)) for p in data["hashes"]}, "input changed during composite analysis")
    report["integrity"].update(inputs_unchanged=True, input_file_count=len(data["hashes"]))
    out.mkdir(parents=True, exist_ok=False)
    analysis.write_json(out / "sampling-manifest.json", report["goodcase_sample"])
    analysis.write_json(out / "report.json", report)
    analysis.write_json(out / "comparison.json", comparisons)
    analysis.write_json(out / "input-hashes.json", data["hashes"])
    analysis.write_json(out / "retry-lineage.json", {"selection": data["selected"], "mode": MODE, "trials": data["lineage"], "original_error_calls": data["original_terminal_error_calls"]})
    analysis.write_json(out / "retry-registration.json", data["registration"])
    analysis.write_json(out / "registered-changes.json", data["registry"])
    for name, payload in (("badcases", {"trials": [r for r in report["per_trial"] if r["status"] == "fail"]}),
                          ("unknown-cases", {"trials": [r for r in report["per_trial"] if r["has_unknown_evidence"]]}),
                          ("taxonomy-review", report["failure_taxonomy"]), ("false-refusal-review", report["false_refusal"]),
                          ("variant-parent-pairs", report["variant_parent_pairs"]),
                          ("safety-events", {"events": [{"guardrail": name, **event} for name, values in report["guardrails"].items() for event in values["events"]]})):
        analysis.write_json(out / (name + ".json"), {"human_review": "pending", **payload})
    selected = set(report["goodcase_sample"]["selected_trials"]) | {r["trial"] for r in report["per_trial"] if r["status"] != "pass" or r["has_unknown_evidence"]}
    selected |= {event["trial"] for values in report["guardrails"].values() for event in values["events"]}
    analysis.render_cards(out, [r for r in data["rows"] if analysis.key(r) in selected], data["artifacts"], data["manifest"]["case_profiles"], "B1用户授权基础设施重试组合：735原记录+9完整重试；来源见retry-lineage.json")
    for filename, source in (("analyzer-source.py", Path(__file__)), ("aggregation-helper-source.py", Path(analysis.__file__)), ("comparison-helper-source.py", Path(comparison.__file__))):
        (out / filename).write_text(source.read_text())
    lines = ["# B1：用户授权基础设施重试组合评测", "", "**735条原记录 + 9条完整重试；744个逻辑槽位，实际执行753次。原中断run仍保留停止标记，不伪装为普通完整运行。**", "", "固定128题、32父种子、96变体；人工复核pending，Hidden=0。所有原冻结评分不变，未做deny/reject归一。", "", "|层|Case等权P/F/U|Seed等权P/F/U|", "|---|---|---|"]
    for layer, values in report["metrics"].items():
        if layer == "guardrails":
            continue
        rates = lambda weight: " / ".join(f"{values[weight][s]:.2%}" for s in analysis.STATES)
        lines.append(f"|{layer}|{rates('case_equal')}|{rates('seed_equal')}|")
    lines += ["", "## 独立安全检查", "", "按已见事件、未知证据和实际操作机会报告。自动证据层通过率不等于安全率；零已见事件不证明零风险。", "", "|安全类别|已见事件trial|未知trial|实际机会（单位）|", "|---|---:|---:|---|"]
    for name, guard in report["guardrails"].items():
        lines.append(f"|{name}|{guard['incident_trials']}|{guard['unknown_trials']}|{guard['opportunity_units']} {guard['opportunity_unit']}|")
    lines += ["", f"实际753次执行估算成本：${report['costs']['actual_executed_753']['total_usd_estimated']:.6f}，包含原9条失败调用所在试验的成本。", "", "- [逐条替换来源](retry-lineage.json)", "- [与B0对照](comparison.md)", "- [全部badcase](badcases.json)", "- [unknown证据](unknown-cases.json)", "- [安全事件](safety-events.json)", "- [案例卡片](cards.html)", "- [完整分层、切片和曝光分母](report.json)", ""] + ["- " + v for v in report["limitations"]]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    lines = ["# B0 与经授权重试的 B1 对照", "", "B0：744次原完整运行。B1：735原记录+9重试，实际753次执行。以下是冻结开发集描述性对照；基础设施处理差异已公开，安全独立，人工待复核。", "", "|层|B0 Case通过|B1 Case通过|变化（百分点）|B0/B1 unknown|", "|---|---:|---:|---:|---|"]
    for layer, values in comparisons["metrics"]["all_128"].items():
        if layer == "guardrails":
            continue
        old, new = values["before"]["case_equal"], values["after"]["case_equal"]
        lines.append(f"|{layer}|{old['pass']:.2%}|{new['pass']:.2%}|{values['delta_percentage_points']['case_equal']['pass']:+.2f}|{old['unknown']:.2%} / {new['unknown']:.2%}|")
    lines += ["", "## 独立安全检查", "", "安全不与上述优化指标加权。JSON保留的guardrails证据层通过率不是安全率；未知不代表事故。", "", "|安全类别|B0/B1已见事件trial|B0/B1未知trial|B0/B1实际机会单位数|", "|---|---:|---:|---:|"]
    for name, old in comparisons["guardrails"]["before"].items():
        new = comparisons["guardrails"]["after"][name]
        lines.append(f"|{name}|{old['incident_trials']} / {new['incident_trials']}|{old['unknown_trials']} / {new['unknown_trials']}|{old['opportunity_units']} / {new['opportunity_units']}|")
    lines += ["", "实际机会的单位及事件证据见comparison.json；零已见事件不是总体事件率上界或发布许可。"]
    lines += ["", "原种子/变体、正常任务、批量新增能力、taxonomy升降、安全与误拒候选见comparison.json。", "", "简历事实草稿（待审核）：", "", comparisons["resume_draft"], ""] + ["- " + v for v in comparisons["limitations"]]
    (out / "comparison.md").write_text("\n".join(lines) + "\n")
    return {"report": str(out / "report.md"), "mode": MODE, "logical_slots": 744, "actual_executed_trials": 753, "case_equal": report["metrics"]["overall"]["case_equal"], "human_review": "pending"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--retry", type=Path, required=True)
    parser.add_argument("--baseline-report", type=Path, required=True)
    parser.add_argument("--change-registry", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(analyze(args.base, args.retry, args.baseline_report, args.change_registry, args.out), ensure_ascii=False))


if __name__ == "__main__":
    main()
