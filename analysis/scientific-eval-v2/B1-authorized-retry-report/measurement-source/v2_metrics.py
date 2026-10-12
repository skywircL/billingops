"""Versioned measurement supplements; never re-score or modify frozen trials."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

VERSION = "scientific-v2-metrics-1"
ROOT = Path(__file__).resolve().parents[3]
MANIFEST_METADATA = {"registered_at", "source", "plan_sha256", "runtime", "estimated_usd_per_million_tokens"}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def trial_key(row: dict) -> str:
    return f"{row.get('case_id') or row['candidate_id']}-t{row['trial_index']}"


def measurement_contract() -> dict:
    return {"version": VERSION, "ks": [1, 3], "bootstrap_iterations": 2000,
            "bootstrap_seed": 20261012, "bootstrap_unit": "root_seed_family",
            "minimum_families_for_bootstrap": 8, "confidence_level": .95,
            "efficiency_cohort": "all necessary layers pass and every required safety incident explicitly false",
            "tool_pricing": {"mode": "local_simulator", "usd_per_call": 0.0, "currency": "USD",
                             "scope": "tool_service_fee_only_excludes_host_compute"},
            "best_at_k": "not_applicable_no_registered_selector",
            "release": "blocked_until_independent_calibration_and_preregistered_hidden_validation"}


def _archive_path(value: str, run: Path) -> Path:
    path = Path(value)
    candidates = [path] if path.is_absolute() else [run / path, ROOT / path]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_relative_to(run) and resolved.is_file():
            return resolved
    raise ValueError(f"missing or out-of-run artifact: {value}")


def load_archive(run: Path) -> tuple[dict, dict, list[dict], dict, dict]:
    """Check frozen identity/hash chains for any registered V2 suite size.

    Missing registered slots remain missing. Duplicate, unexpected, corrupted
    or mismatched records are rejected before computing any rates.
    """
    run = run.resolve()
    if any((run / marker).exists() for marker in ("provider-stop.json", "incomplete-run.json")):
        raise ValueError("provider-stopped archive requires interruption audit, not a scientific success-rate report")
    manifest = json.loads((run / "manifest.json").read_text())
    fixture = json.loads((run / "fixtures.json").read_text())
    rows = [json.loads(line) for line in (run / "results.jsonl").read_text().splitlines() if line.strip()]
    metadata = MANIFEST_METADATA | ({"evaluation_versions"} if manifest.get("runner_version") in {"v2-runner-1.2", "v2-runner-1.3", "v2-runner-1.4"} else set())
    if digest({k: v for k, v in manifest.items() if k not in metadata}) != manifest.get("plan_sha256"):
        raise ValueError("manifest plan digest mismatch")
    if digest(fixture) != manifest.get("fixture_sha256"):
        raise ValueError("fixture digest mismatch")
    tasks = {t.get("case_id") or t["candidate_id"]: t for t in fixture["tasks"]}
    if len(tasks) != len(fixture["tasks"]):
        raise ValueError("duplicate fixture identity")
    planned = manifest["planned_trials"]
    if not planned or any(type(n) is not int or n <= 0 for n in planned.values()):
        raise ValueError("invalid registered repetitions")
    if set(planned) != set(manifest["candidate_ids"]) or not set(planned) <= tasks.keys() or manifest["total_trials"] != sum(planned.values()):
        raise ValueError("registered plan identity/count mismatch")
    profiles = manifest["case_profiles"]
    for cid in planned:
        if profiles.get(cid, {}).get("task_sha256") != digest(tasks[cid]):
            raise ValueError(f"{cid}: task profile digest mismatch")
    source = manifest.get("source", {})
    required = {f"src/billingops/evaluation/{name}.py" for name in ("v2_runner", "v2_scoring", "v2_response", "v2_controls")}
    hashes = source.get("source_digests", {})
    if source.get("provenance") != "before_execution" or not required <= hashes.keys():
        raise ValueError("missing source freeze before execution")
    for rel, expected in hashes.items():
        path = (run / "source" / rel).resolve()
        if not path.is_relative_to(run / "source") or not path.is_file() or sha(path) != expected:
            raise ValueError(f"frozen source mismatch: {rel}")
    versions = manifest.get("evaluation_versions", {})
    if not all(versions.get(k) for k in ("scorer", "response", "controls")):
        raise ValueError("missing registered evaluation versions")
    expected = {(cid, i) for cid, n in planned.items() for i in range(n)}
    seen, artifacts, gaps = set(), {}, []
    for row in rows:
        identity = (row["candidate_id"], row["trial_index"])
        if type(row["trial_index"]) is not int or identity not in expected or identity in seen:
            raise ValueError("duplicate or unexpected trial slot")
        seen.add(identity)
        cid, _ = identity
        task, key = tasks[cid], trial_key(row)
        if row.get("case_id", cid) != cid or row.get("task_id") != task["task_id"] or row.get("status") not in {"pass", "fail", "unknown"}:
            raise ValueError(f"{key}: trial identity/status mismatch")
        if not row.get("artifact_sha256"):
            failure_path = run / "trials" / key / "unexpected-failure.json"
            if row["status"] != "unknown" or (row.get("runtime_failure") or {}).get("classification") not in {"harness_failure", "capture_failure"} or not failure_path.is_file() or json.loads(failure_path.read_text()) != row:
                raise ValueError(f"{key}: unbound capture failure")
            artifacts[key] = None
            gaps.append(key)
            continue
        scoring = row.get("scoring") or {}
        layers = [scoring.get(name) or {} for name in ("outcome", "behavior", "guardrails")] + [row.get("response") or {}]
        states = [layer.get("status", "unknown") for layer in layers]
        if any(state not in {"pass", "fail", "unknown"} for state in states):
            raise ValueError(f"{key}: invalid layer status")
        for layer in layers:
            if layer.get("status") == "pass" and (any(v is not True for v in (layer.get("checks") or {}).values()) or any(c.get("status") != "pass" for c in layer.get("claims") or [])):
                raise ValueError(f"{key}: passing layer contains failed or unknown assertions")
        guard = scoring.get("guardrails") or {}
        if guard.get("status") == "pass" and (not guard.get("incidents") or any(value is not False for value in guard["incidents"].values())):
            raise ValueError(f"{key}: passing safety layer contains incidents or unknown evidence")
        failure = row.get("runtime_failure")
        combined = ("fail" if guard.get("status") == "fail" else
                    "unknown" if failure and failure.get("classification") in {"infra_model", "infra_billing", "harness_failure", "capture_failure"} else
                    "fail" if failure or "fail" in states else "pass" if all(s == "pass" for s in states) else "unknown")
        if row["status"] != combined or scoring.get("task_status", combined) != combined:
            raise ValueError(f"{key}: frozen overall verdict disagrees with layers/runtime failure")
        path = _archive_path(row["artifact"], run)
        artifact = json.loads(path.read_text())
        if sha(path) != row["artifact_sha256"] or artifact.get("task") != task or artifact.get("task_sha256") != digest(task):
            raise ValueError(f"{key}: artifact/task digest mismatch")
        if artifact.get("candidate_id") != cid or artifact.get("trial_index") != row["trial_index"]:
            raise ValueError(f"{key}: artifact identity mismatch")
        if row.get("scoring", {}).get("scorer_version") != versions["scorer"]:
            raise ValueError(f"{key}: scorer version mismatch")
        response = row.get("response", {})
        if response.get("version") != versions["response"] and not (response.get("version") is None and response.get("status") == "unknown"):
            raise ValueError(f"{key}: response version mismatch")
        ref = row["response_grade_artifact"]
        grade_path = _archive_path(ref["path"], run)
        grade = json.loads(grade_path.read_text())
        if sha(grade_path) != ref.get("sha256") or grade.get("raw_artifact_sha256") != row["artifact_sha256"] or grade.get("response_grade") != response or grade.get("model_metrics") != row.get("judge_metrics"):
            raise ValueError(f"{key}: grade/raw binding mismatch")
        if json.loads((path.parent / "result.json").read_text()) != row:
            raise ValueError(f"{key}: stream/result mismatch")
        if "process_metrics" in row:
            contract = manifest.get("process_contracts", {}).get(cid)
            process = row["process_metrics"]
            contract_path = path.parent / "process-contract.json"
            if not contract or process.get("version") != contract.get("version") or process.get("contract_sha256") != digest(contract) or artifact.get("process_metrics_contract") != contract or not contract_path.is_file() or json.loads(contract_path.read_text()) != contract:
                raise ValueError(f"{key}: process measurement registration mismatch")
            if row.get("tool_metrics") != artifact.get("tool_metrics"):
                raise ValueError(f"{key}: tool telemetry/raw mismatch")
        artifacts[key] = artifact
    missing = sorted(expected - seen)
    return manifest, tasks, rows, artifacts, {
        "ok": True, "complete_plan": not missing, "missing": [list(x) for x in missing],
        "planned_trials": len(expected), "recorded_trials": len(rows), "capture_gaps": gaps,
        "manifest_sha256": sha(run / "manifest.json"), "results_sha256": sha(run / "results.jsonl"),
        "fixture_sha256": sha(run / "fixtures.json"), "frozen_source_files": len(hashes),
        "scores": "original_frozen_verdicts_not_regraded"}


def scientific_metrics(plan: dict, rows: list[dict], artifacts: dict | None = None, *, tasks: dict | None = None) -> dict:
    from .v2_process_metrics import process_metrics, summarize_process
    from .v2_statistics import resource_distributions, summarize_statistics
    artifacts, tasks = artifacts or {}, tasks or {}
    contract = plan.get("measurement_contract") or measurement_contract()
    supported = measurement_contract()
    for key in ("version", "bootstrap_unit", "minimum_families_for_bootstrap", "confidence_level", "efficiency_cohort", "best_at_k"):
        if contract.get(key) != supported[key]:
            raise ValueError(f"unsupported measurement contract field: {key}")
    processes, measured = [], []
    for row in rows:
        copied = deepcopy(row)
        if "process_metrics" in row:
            process = row["process_metrics"]
        else:
            raw = deepcopy(artifacts.get(trial_key(row)) or {})
            task = deepcopy(tasks.get(row["candidate_id"]) or raw.get("task") or {})
            task["candidate_id"] = task.get("grading_profile_id", task.get("candidate_id"))
            registered = plan.get("process_contracts", {}).get(row["candidate_id"])
            if registered:
                task["process_metrics_contract"] = registered
            raw["response_grade"] = row.get("response", {})
            process = process_metrics(task, raw, row.get("scoring"))
        processes.append(process)
        copied.setdefault("tool_metrics", process.get("tool_metrics", {"capture": "unknown"}))
        measured.append(copied)
    profiles = plan.get("case_profiles", {})
    operators = sorted({profiles.get(cid, {}).get("operator", "UNKNOWN") for cid in plan["planned_trials"]})
    slices = {}
    for operator in operators:
        selected = {cid: n for cid, n in plan["planned_trials"].items() if profiles.get(cid, {}).get("operator", "UNKNOWN") == operator}
        slices[operator] = summarize_statistics([r for r in rows if r["candidate_id"] in selected], selected, profiles,
            ks=tuple(contract["ks"]), bootstrap_iterations=contract["bootstrap_iterations"], seed=contract["bootstrap_seed"])
    return {"version": VERSION,
            "registration": "before_execution" if plan.get("measurement_contract") else "posthoc_diagnostic_supplement",
            "statistics": summarize_statistics(rows, plan["planned_trials"], plan.get("case_profiles", {}),
                ks=tuple(contract["ks"]), bootstrap_iterations=contract["bootstrap_iterations"], seed=contract["bootstrap_seed"]),
            "statistics_by_operator": slices,
            "population_interpretation": "Overall includes the registered mixture. NONE denotes original cases; each perturbation operator is reported separately, never interpreted as the original fixed-condition score.",
            "resources": resource_distributions(measured, tool_pricing=plan.get("measurement_contract", {}).get("tool_pricing")),
            "process": summarize_process(processes), "per_trial_process": processes,
            "limitations": ["Frozen trial verdicts are preserved; added measurements are not Agent improvement.",
                "Bootstrap intervals describe sensitivity across sampled development families; they do not validate a production population.",
                "No hidden-test or independent human calibration completion is inferred from local execution."]}
