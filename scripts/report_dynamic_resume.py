"""Recompute B-only resume evidence from fixed historical inputs; no model calls.

Run with Python 3 from any directory. Only summary.json is written. Historical
inputs are read-only; sealed tasks, individual results and artifacts are never
opened. --verify-grader uses the project environment to recheck only the four
paired development runs' B artifacts in memory. No model or network calls occur.
Task text and responses are never copied into the output or printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "analysis/dynamic-agent-eval/summary.json"
KEY_RISKS = {"fault", "approval", "permission"}
SAFETY_KEYS = (
    "unauthorized_refund", "wrong_target_refund", "over_refund",
    "duplicate_write", "cross_customer",
)
INPUT_HASHES: dict[str, str] = {}


def read(relative: str, *, jsonl: bool = False):
    raw = (ROOT / relative).read_bytes()
    INPUT_HASHES[relative] = hashlib.sha256(raw).hexdigest()
    return [json.loads(line) for line in raw.splitlines() if line.strip()] if jsonl else json.loads(raw)


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def planned(manifest: dict, task: dict) -> int:
    key = "key_risk_trials" if KEY_RISKS.intersection(task.get("risk", [])) else "trials"
    n = manifest[key]
    if not isinstance(n, int) or n < 1:
        raise ValueError("Invalid planned trial count")
    return n


def load_run(run: str, results_subdir: str = "") -> dict:
    # Only the five explicitly selected development runs use this loader.
    if "sealed" in run or "hidden" in run:
        raise ValueError("Sealed/hidden runs must use aggregate summaries only")
    base = f"{run}/{results_subdir}" if results_subdir else run
    manifest = read(f"{base}/manifest.json")
    task_list = read(f"{run}/tasks.json")
    tasks = {t["task_id"]: t for t in task_list}
    if len(tasks) != len(task_list) or manifest["tasks"] != len(tasks):
        raise ValueError("Duplicate tasks or manifest task count mismatch")
    if any(t.get("exposure", {}).get("status") == "sealed" for t in task_list):
        raise ValueError("Unexpected sealed task in development run")
    rows = [r for r in read(f"{base}/results.jsonl", jsonl=True) if r["group"] == "B"]
    seen = set()
    for row in rows:
        tid, index = row["task_id"], row["trial_index"]
        if tid not in tasks or row["status"] not in {"pass", "fail", "unknown"}:
            raise ValueError("Unexpected task or trial status")
        if not isinstance(index, int) or not 0 <= index < planned(manifest, tasks[tid]):
            raise ValueError("Recorded trial index outside plan")
        if (tid, index) in seen:
            raise ValueError("Duplicate B trial slot")
        seen.add((tid, index))
    return {"run": run, "results_source": base, "manifest": manifest, "tasks": tasks, "rows": rows}


def mean(values):
    values = list(values)
    return statistics.fmean(values) if values else None


def numeric_summary(rows: list[dict], key: str) -> dict:
    values = sorted(float(r[key]) for r in rows if isinstance(r.get(key), (int, float)))
    def quantile(p):
        if not values:
            return None
        k = (len(values) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(values) - 1)
        return values[lo] + (values[hi] - values[lo]) * (k - lo)
    return {"n": len(values), "sum": sum(values), "mean": mean(values), "p50": quantile(.5), "p95": quantile(.95)}


def summarize(run: dict, ids: set[str]) -> dict:
    rows = [r for r in run["rows"] if r["task_id"] in ids]
    by_task = defaultdict(list)
    for row in rows:
        by_task[row["task_id"]].append(row)
    per_task = {}
    for tid in sorted(ids):
        n = planned(run["manifest"], run["tasks"][tid])
        counts = Counter(r["status"] for r in by_task[tid])
        missing = n - len(by_task[tid])
        known = counts["pass"] + counts["fail"]
        per_task[tid] = {
            "family": run["tasks"][tid]["family"], "planned": n,
            "pass": counts["pass"], "fail": counts["fail"], "unknown": counts["unknown"], "missing": missing,
            "s_lower": counts["pass"] / n,
            "s_upper": (counts["pass"] + counts["unknown"] + missing) / n,
            "s_known_only": counts["pass"] / known if known else None,
        }
    trials = {k: sum(t[k] for t in per_task.values()) for k in ("planned", "pass", "fail", "unknown", "missing")}
    trials["recorded"] = len(rows)
    known_n = trials["pass"] + trials["fail"]
    complete = [r for r in rows if r.get("safety", {}).get("evidence") == "complete"]
    complete_known = [r for r in complete if r["status"] != "unknown"]
    safety = {
        "recorded_trials": len(rows), "evidence_complete": len(complete),
        "evidence_incomplete": len(rows) - len(complete), "missing_trials": trials["missing"],
        "evidence_complete_known_trials": len(complete_known),
        "any_observed_event_trials": sum(any(r.get("safety", {}).get(k) is True for k in SAFETY_KEYS) for r in rows),
        "events": {k: {
            "observed_events": sum(r.get("safety", {}).get(k) is True for r in rows),
            "boolean_recorded_denominator": sum(isinstance(r.get("safety", {}).get(k), bool) for r in rows),
            "complete_evidence_denominator": sum(isinstance(r.get("safety", {}).get(k), bool) for r in complete),
            "complete_known_denominator": sum(isinstance(r.get("safety", {}).get(k), bool) for r in complete_known),
        } for k in SAFETY_KEYS},
    }
    failed_checks = Counter(
        f"{layer}.{check}" for r in rows if r["status"] == "fail"
        for layer in ("outcome", "behaviour")
        for check, value in r.get("layers", {}).get(layer, {}).get("checks", {}).items() if value is False
    )
    families = {}
    for family in sorted({t["family"] for t in per_task.values()}):
        members = [t for t in per_task.values() if t["family"] == family]
        families[family] = {"tasks": len(members), "s_lower": mean(t["s_lower"] for t in members), "s_upper": mean(t["s_upper"] for t in members)}
    return {
        "run": run["run"], "results_source": run["results_source"], "group": "B", "tasks": len(ids), "trials": trials,
        "task_equal": {
            "s_lower": mean(t["s_lower"] for t in per_task.values()),
            "s_upper": mean(t["s_upper"] for t in per_task.values()),
            "s_known_only": mean(t["s_known_only"] for t in per_task.values() if t["s_known_only"] is not None),
            "known_only_task_denominator": sum(t["s_known_only"] is not None for t in per_task.values()),
        },
        "raw_trial_rates": {
            "lower": trials["pass"] / trials["planned"] if trials["planned"] else None,
            "upper": (trials["pass"] + trials["unknown"] + trials["missing"]) / trials["planned"] if trials["planned"] else None,
            "known_only": trials["pass"] / known_n if known_n else None,
            "known_denominator": known_n,
        },
        "safety": safety, "failed_checks_multilabel_fail_trials_only": dict(sorted(failed_checks.items())),
        "failure_classes_by_status": {status: dict(Counter(r.get("failure_class") or "none" for r in rows if r["status"] == status)) for status in ("fail", "unknown")},
        "optimization": {name: {
            "trials": len(subset),
            **{key: numeric_summary(subset, key) for key in ("cost_usd", "latency_s", "reads", "model_calls", "clarifications", "repairs")},
            "trials_with_repairs": sum((r.get("repairs") or 0) > 0 for r in subset),
        } for name, subset in (("all_recorded", rows), ("known_pass_fail", [r for r in rows if r["status"] != "unknown"]))},
        "families": families, "per_task_scores": per_task,
    }


def metadata(run: dict) -> dict:
    m = run["manifest"]
    source = m.get("provenance", {}).get("source_digests", {})
    return {
        **{key: m.get(key) for key in ("started_at", "model", "model_spec", "model_config", "prices_usd_per_m", "workers", "seed", "trials", "key_risk_trials", "budget_usd", "task_set_sha256")},
        "graders": sorted({r.get("versions", {}).get("grader", "unrecorded") for r in run["rows"]}),
        "grader_source_sha256": {key: source.get(key) for key in ("src/billingops/evaluation/trials.py", "src/billingops/evaluation/projection.py")},
        "tool_schemas_sha256": m.get("provenance", {}).get("tool_schemas_sha256"),
    }


def compare(before: dict, after: dict) -> dict:
    bt, at = before["tasks"], after["tasks"]
    selected, excluded = set(), []
    normalized = lambda t: {k: v for k, v in t.items() if k != "dataset"}
    for tid in sorted(set(bt) | set(at)):
        reasons = []
        if tid not in bt or tid not in at:
            reasons.append("only_in_after" if tid not in bt else "only_in_before")
        else:
            if normalized(bt[tid]) != normalized(at[tid]):
                reasons.append("task_content_differs_except_dataset")
            if planned(before["manifest"], bt[tid]) != planned(after["manifest"], at[tid]):
                reasons.append("planned_trials_differ")
        if reasons:
            excluded.append({"task_id": tid, "reasons": reasons})
        else:
            selected.add(tid)
    if not selected:
        raise ValueError("No comparable tasks")
    bs, ass = summarize(before, selected), summarize(after, selected)
    families = [{"family": f, "before": bs["families"][f]["s_lower"], "after": ass["families"][f]["s_lower"],
                 "delta": ass["families"][f]["s_lower"] - bs["families"][f]["s_lower"]} for f in bs["families"]]
    bm, am = metadata(before), metadata(after)
    return {
        "comparable_tasks": len(selected), "task_content_comparison_ignored_fields": ["dataset"],
        "comparable_content_sha256": canonical_hash([normalized(bt[k]) for k in sorted(selected)]),
        "excluded": excluded, "before": bs, "after": ass,
        "task_equal_s_lower_delta": ass["task_equal"]["s_lower"] - bs["task_equal"]["s_lower"],
        "families": {"improved": [f for f in families if f["delta"] > 0], "regressed": [f for f in families if f["delta"] < 0], "unchanged_count": sum(f["delta"] == 0 for f in families)},
        "metadata": {"before": bm, "after": am, "differences": {k: {"before": bm[k], "after": am[k]} for k in bm if bm[k] != am[k]}},
    }


def sealed_history() -> dict:
    # Existing aggregates only. Do not open the sealed runs' tasks/results.
    before = read("analysis/v4-round1/release.json")
    after = read("analysis/v4-round2/release.json")
    comparison = read("analysis/v4-round1/compare-sealed-B.json")
    allowed = ("group", "tasks", "trials", "s_lower", "s_upper", "slices", "pass_at_3", "pass_pow_3", "safety_evidence_incomplete", "cost_usd", "latency_s")
    groups = {}
    for name, report in (("before", before), ("after", after)):
        b = report["groups"]["B"]
        groups[name] = {k: b[k] for k in allowed if k in b}
        groups[name]["safety_incident_count"] = len(b.get("safety_incidents", []))
        groups[name]["grader_summary_label"] = report.get("grader")
        groups[name]["report_gate_group"] = report["gate_group"]
    return {
        "status": "exposed historical measurement; not a current hidden release gate",
        "verification": "Copied B aggregates; task equality and raw records not independently inspected",
        **groups,
        "task_equal_s_delta": groups["after"]["s_lower"] - groups["before"]["s_lower"],
        "family_improved_count": len(comparison["improved"]),
        "family_regressed_count": len(comparison["regressed"]),
        "raw_trial_rate_before": comparison["trial_mean_before"],
        "raw_trial_rate_after": comparison["trial_mean_after"],
    }


def verify_grader(pairs: list[tuple[dict, dict, dict]]) -> dict:
    """Verify only paired development B artifacts; never write rescore files."""
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / "src"))
    from billingops.evaluation.tasks import EvalTask, apply_patches
    from billingops.evaluation.trials import (
        GRADER_VERSION, INCONCLUSIVE_CLASSES, _TASK_DELTA,
        score_outcome, score_behaviour, safety_assertions,
    )

    allowed = {
        "runs/v4r2e-canary-v2", "runs/v4v12-canary-v3",
        "runs/v4r2e-regression-v1.1", "runs/v4v12-regression-v1.1",
    }
    source_sha256 = {}
    for name in ("trials.py", "projection.py", "tasks.py"):
        relative = f"src/billingops/evaluation/{name}"
        source_sha256[relative] = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        INPUT_HASHES[relative] = source_sha256[relative]
    count_keys = (
        "planned_trials", "rows_requested", "artifacts_matched", "artifacts_missing", "artifact_hash_mismatches",
        "status_matched", "status_changed", "outcome_checks_matched", "outcome_checks_changed",
        "behaviour_checks_matched", "behaviour_checks_changed", "safety_matched", "safety_changed",
    )
    per_run, inventory = {}, []
    for before, after, comparison in pairs:
        ids = set(comparison["before"]["per_task_scores"])
        for run in (before, after):
            name = run["run"]
            if name not in allowed or name in per_run:
                raise ValueError("Grader verification must use the four distinct development runs")
            counts = dict.fromkeys(count_keys, 0)
            counts["planned_trials"] = sum(planned(run["manifest"], run["tasks"][tid]) for tid in ids)
            statuses = Counter()
            artifact_root = ROOT / name / "artifacts"
            for row in run["rows"]:
                if row["group"] != "B" or row["task_id"] not in ids:
                    continue
                counts["rows_requested"] += 1
                ref = row.get("artifact") or {}
                relative = ref.get("path")
                if not relative:
                    counts["artifacts_missing"] += 1
                    continue
                path = (ROOT / name / relative).resolve()
                if not path.is_relative_to(artifact_root):
                    raise ValueError("Artifact path outside the selected development run")
                if not path.is_file():
                    counts["artifacts_missing"] += 1
                    continue
                raw = path.read_bytes()
                actual_sha = hashlib.sha256(raw).hexdigest()
                inventory.append((str(path.relative_to(ROOT)), ref.get("sha256"), actual_sha))
                if actual_sha != ref.get("sha256"):
                    counts["artifact_hash_mismatches"] += 1
                    continue
                counts["artifacts_matched"] += 1
                artifact = json.loads(raw)
                task = EvalTask.from_dict(run["tasks"][row["task_id"]])
                view, events = artifact.get("view"), artifact.get("events", [])
                patches = [f["patch"] for f in row.get("followups_applied") or []
                           if f.get("command") == "billing_patch" and not f.get("rejected")]
                outcome = score_outcome(task, view, artifact.get("billing_final"),
                                        baseline_world=apply_patches(task.world, patches) if patches else None)
                behaviour = score_behaviour(task, view, events)
                safety = safety_assertions(
                    task, view, events, outcome.get("changes"),
                    expected_delta=outcome.get("expected_delta", task.expected.refund_delta)
                    if "expected_delta" in outcome else _TASK_DELTA,
                )
                if row.get("failure_class") in INCONCLUSIVE_CLASSES:
                    status = "unknown"
                elif row.get("failure_class"):
                    status = "fail"
                elif outcome["status"] == behaviour["status"] == "pass":
                    status = "pass"
                elif "fail" in (outcome["status"], behaviour["status"]):
                    status = "fail"
                else:
                    status = "unknown"
                statuses[status] += 1
                for key, matches in (
                    ("status", status == row["status"]),
                    ("outcome_checks", outcome.get("checks") == row["layers"]["outcome"].get("checks")),
                    ("behaviour_checks", behaviour.get("checks") == row["layers"]["behaviour"].get("checks")),
                    ("safety", safety == row["safety"]),
                ):
                    counts[f"{key}_{'matched' if matches else 'changed'}"] += 1
            per_run[name] = {**counts, "recomputed_status_counts": dict(sorted(statuses.items()))}
    totals = {key: sum(value[key] for value in per_run.values()) for key in count_keys}
    passed = totals["artifacts_matched"] == totals["rows_requested"] == totals["planned_trials"] and all(
        totals[key] == 0 for key in count_keys if key.endswith("_changed")
    )
    return {
        "status": "pass" if passed else "fail", "grader": GRADER_VERSION,
        "scope": "Only common B trials in the four development runs; no sealed artifacts or model calls.",
        "method": "Validate recorded artifact SHA256, then recompute outcome/behaviour checks, safety and status in memory; preserve recorded runtime failure classes.",
        "current_scoring_source_sha256": source_sha256,
        "artifact_inventory_sha256": canonical_hash(sorted(inventory)),
        "totals": totals, "runs": per_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-grader", action="store_true",
                        help="Recheck paired development B artifacts with current scoring functions (no model calls)")
    args = parser.parse_args()
    canary_runs = (load_run("runs/v4r2e-canary-v2"), load_run("runs/v4v12-canary-v3"))
    regression_runs = (load_run("runs/v4r2e-regression-v1.1"), load_run("runs/v4v12-regression-v1.1"))
    canary = compare(*canary_runs)
    regression = compare(*regression_runs)
    coverage_run = load_run("runs/cov1-coverage-v1", "reclass-infra-1")
    coverage = summarize(coverage_run, set(coverage_run["tasks"]))
    coverage["metadata"] = metadata(coverage_run)
    verification = verify_grader([(*canary_runs, canary), (*regression_runs, regression)]) if args.verify_grader else {
        "status": "not_requested", "enable_with": "--verify-grader",
    }
    output = {
        "schema_version": 1, "scope": "Historical dynamic Agent B only; no new model evaluation",
        "methodology": {
            "task_equal": "Mean over tasks of pass/planned trials. Bounds count unknown+missing as failure/success; bounds are not confidence intervals.",
            "known_only": "Diagnostic only; excludes unknown/missing within each task, omitting tasks with no known trial. Never substitute for release bounds.",
            "failed_checks": "Multi-label counts on fail trials only; labels overlap and are not root-cause taxonomy. Unknown trials excluded.",
            "safety": "Five historical flags; all recorded statuses retained. Denominators separately show complete evidence and known trials. Zero observations do not prove zero risk.",
            "optimization": "Recorded trials include failed and infrastructure trials; known-pass/fail subset also supplied. Prices come from historical manifests; latency scope is CaseRuntime plus Billing HTTP, excluding production ingress and real human waiting.",
            "quantile": "Linear interpolation at (n-1)*p, matching evaluation/release.py.",
            "comparability": "Development task objects must match exactly except dataset and have equal planned repeat counts. Model, workers, grader source and other configuration differences remain explicit; this is exploratory historical evidence, not a controlled causal experiment.",
            "tools": "reads measures investigation reads; it is not a count of every tool invocation.",
            "latest_v5_4": "No post-fix live run is included; these comparisons predate v5.4.",
        },
        "canary_comparison": canary, "regression_comparison": regression,
        "coverage_baseline": coverage, "exposed_sealed_history": sealed_history(),
        "uniform_grader_verification": verification,
        "input_sha256": dict(sorted(INPUT_HASHES.items())),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUT}; B-only paired tasks: Canary={canary['comparable_tasks']}, Regression={regression['comparable_tasks']}; Coverage={coverage['tasks']}")
    if args.verify_grader:
        print(f"Uniform grader verification: {verification['status']}; {verification['totals']}")
        if verification["status"] != "pass":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
