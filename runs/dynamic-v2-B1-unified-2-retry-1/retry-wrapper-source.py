#!/usr/bin/env python3
"""Explicit user-authorized, single-attempt recovery of nine provider-402 slots.

The original archive is immutable. The copied manifest is its evaluation
contract, not a claim that this retry directory contains 744 new trials.
retry-registration.json describes the actual nine full-trial executions.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("retry_archive_analysis", ROOT / "scripts/analyze_controlled_v2.py")
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)
AUTHORIZATION = "单独重试末尾 9 条试验，然后和之前测完的一起进行统计"
SCHEMA = "controlled-v2-infra-retry-1"
PROTOCOL = "data/eval-design/dynamic-v1/protocol.yaml"


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def provider_errors(row, statuses=(402,)):
    return [{"role": role, **e} for role in ("agent", "customer", "judge")
            for e in row.get(role + "_metrics", {}).get("errors", [])
            if e.get("status") in statuses]


def selection(rows):
    """Select by documented provider cause, including original fail outcomes."""
    selected = sorted(analysis.key(r) for r in rows if provider_errors(r))
    if len(selected) != 9 or len(set(selected)) != 9:
        raise ValueError("this authorization requires exactly nine distinct original 402 trials")
    return selected


def check_runtime(manifest, bundle, allowed_metadata):
    from billingops.evaluation.v2_runner import make_plan
    from billingops.investigation.b1 import EXTRACT_SCHEMA, INVESTIGATION_SCHEMAS
    from billingops.config import MODEL_PRICE_INPUT_PER_M, MODEL_PRICE_OUTPUT_PER_M, MODEL_PRICE_CACHE_READ_PER_M, MODEL_PRICE_CACHE_WRITE_PER_M
    plan = make_plan(bundle, mode="unified", workers=16, agent_revision="B1")
    for k, value in plan.items():
        if manifest.get(k) != value:
            raise ValueError("changed evaluation condition: " + k)
    if manifest["runtime"]["python"] != sys.version:
        raise ValueError("Python runtime changed")
    prices = dict(input=MODEL_PRICE_INPUT_PER_M, output=MODEL_PRICE_OUTPUT_PER_M,
                  cache_read=MODEL_PRICE_CACHE_READ_PER_M, cache_write=MODEL_PRICE_CACHE_WRITE_PER_M)
    if manifest["estimated_usd_per_million_tokens"] != prices:
        raise ValueError("cost estimation configuration changed")
    if analysis.digest([EXTRACT_SCHEMA, INVESTIGATION_SCHEMAS]) != manifest["source"]["tool_schemas_sha256"]:
        raise ValueError("effective B1 tool schemas changed")
    current = {p: sha(ROOT / p) for p in manifest["source"]["source_digests"]}
    differences = [{"path": p, "frozen_sha256": h, "current_sha256": current[p]}
                   for p, h in manifest["source"]["source_digests"].items() if current[p] != h]
    if differences != allowed_metadata:
        raise ValueError("unregistered working source change")
    if any(d["path"] != PROTOCOL for d in differences):
        raise ValueError("only documented operational metadata may differ")
    return current


def prepare(base, out):
    base, out = base.resolve(), out.resolve()
    manifest, _, rows, _, integrity = analysis._validate_archive(base, require_complete=False)
    if not integrity["complete_plan"] or not integrity["stop_markers"] or len(rows) != 744:
        raise ValueError("base must be the 744-row provider-stopped archive")
    if out == base or out.is_relative_to(base) or out.exists():
        raise ValueError("retry needs a new directory outside the original archive")
    chosen = selection(rows)
    raw_selected = set()
    for p in (base / "trials").glob("*/*-calls.jsonl"):
        if any((r := json.loads(line)).get("kind") == "error" and r.get("status") == 402
               for line in p.read_text().splitlines() if line.strip()):
            raw_selected.add(p.parent.name)
    if raw_selected != set(chosen):
        raise ValueError("raw provider evidence disagrees with result selection")
    post_path = ROOT / "analysis/dynamic-eval-v1/B1-unified-2-post-run-status-update.json"
    post = json.loads(post_path.read_text())
    if (ROOT / post["run"]).resolve() != base or not post.get("after_run_exit"):
        raise ValueError("operational metadata record must bind this stopped run")
    bundle = json.loads((base / "fixtures.json").read_text())
    current = check_runtime(manifest, bundle, post["changes"])
    jobs = [f"{cid}-t{i}" for cid in manifest["candidate_ids"] for i in range(manifest["planned_trials"][cid])]
    random.Random(manifest["seed"]).shuffle(jobs)
    jobs = [tid for tid in jobs if tid in chosen]
    out.mkdir(parents=True)
    for name in ("manifest.json", "fixtures.json"):
        shutil.copyfile(base / name, out / name)
    for rel, digest in manifest["source"]["source_digests"].items():
        dst = out / "source" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(base / "source" / rel, dst)
        if sha(dst) != digest:
            raise ValueError("copied source mismatch: " + rel)
    shutil.copyfile(Path(__file__), out / "retry-wrapper-source.py")
    reg = {
        "schema_version": SCHEMA, "mode": "user_authorized_infrastructure_retry",
        "authorized_user_instruction": AUTHORIZATION, "registered_at": now(),
        "base_run": str(base), "base_manifest_sha256": sha(base / "manifest.json"),
        "base_results_sha256": sha(base / "results.jsonl"),
        "base_provider_stop_sha256": sha(base / "provider-stop.json"),
        "base_fixture_sha256": sha(base / "fixtures.json"),
        "selected_trial_ids": chosen, "dispatch_order": jobs,
        "selection_rule": "all_original_http402_trials",
        "retry_policy": "one_full_run_trial_per_selected_slot_no_best_of",
        "execution_kind": "full_run_trial", "executor_workers": 16, "active_trial_count": 9,
        "retry_manifest_sha256": sha(out / "manifest.json"),
        "source_digests": manifest["source"]["source_digests"],
        "working_tree_differences": post["changes"], "working_source_digests": current,
        "post_run_metadata_record": str(post_path), "post_run_metadata_sha256": sha(post_path),
        "wrapper_sha256": sha(Path(__file__)), "wrapper_source": "retry-wrapper-source.py",
        "replacement_rule": "Each of the nine new results replaces its original logical slot regardless of pass/fail/unknown; all other 735 original results remain. No best-of or success-only selection.",
        "protocol_amendment": "User explicitly overrides prior full-restart/no-selective-resume rule. This is a later infrastructure-recovery amendment, not original preregistration.",
        "inherited_manifest_note": "manifest.json is an unchanged copy of the base evaluation contract. This directory executes only the nine registered slots, not a new 744-trial run.",
        "original_error_evidence": [{"trial": analysis.key(r), "status": r["status"], "errors": provider_errors(r)} for r in rows if analysis.key(r) in chosen],
    }
    analysis.write_json(out / "retry-registration.json", reg)
    return {"registered": str(out / "retry-registration.json"), "retry_count": 9, "other_original_slots_retained": 735}


def completion_check(selected, rows):
    ids = [analysis.key(r) for r in rows]
    exact = len(ids) == len(set(ids)) and set(ids) == set(selected)
    errors = [{"trial": analysis.key(r), **e} for r in rows for e in provider_errors(r, (401, 402))]
    return exact, errors


def execute(out):
    from billingops.evaluation.v2_runner import run_trial, _unexpected_trial_failure
    out = out.resolve()
    reg_path = out / "retry-registration.json"
    reg = json.loads(reg_path.read_text())
    base = Path(reg["base_run"])
    if (out / "results.jsonl").exists():
        raise ValueError("retry attempt already started; never overwrite or silently retry")
    if sha(Path(__file__)) != reg["wrapper_sha256"] or sha(out / "retry-wrapper-source.py") != reg["wrapper_sha256"]:
        raise ValueError("registered retry wrapper changed")
    for name, field in (("manifest.json", "base_manifest_sha256"), ("results.jsonl", "base_results_sha256"), ("provider-stop.json", "base_provider_stop_sha256"), ("fixtures.json", "base_fixture_sha256")):
        if sha(base / name) != reg[field]:
            raise ValueError("base archive changed: " + name)
    original_rows = [json.loads(line) for line in (base / "results.jsonl").read_text().splitlines() if line.strip()]
    chosen = selection(original_rows)
    if (reg["schema_version"] != SCHEMA or reg["selected_trial_ids"] != chosen
            or len(reg["dispatch_order"]) != 9 or set(reg["dispatch_order"]) != set(chosen)
            or reg["executor_workers"] != 16
            or reg["retry_policy"] != "one_full_run_trial_per_selected_slot_no_best_of"):
        raise ValueError("retry registration no longer matches authorized nine-slot plan")
    manifest = json.loads((out / "manifest.json").read_text())
    if sha(out / "manifest.json") != reg["retry_manifest_sha256"] or sha(out / "fixtures.json") != reg["base_fixture_sha256"]:
        raise ValueError("retry contract changed")
    bundle = json.loads((out / "fixtures.json").read_text())
    current = check_runtime(manifest, bundle, reg["working_tree_differences"])
    if current != reg["working_source_digests"]:
        raise ValueError("working source changed after retry registration")
    for rel, expected in reg["source_digests"].items():
        if sha(out / "source" / rel) != expected:
            raise ValueError("frozen retry source changed: " + rel)
    tasks = {t["case_id"]: t for t in bundle["tasks"]}
    lookup = {f"{cid}-t{i}": (tasks[cid], i) for cid, n in manifest["planned_trials"].items() for i in range(n)}
    rows = []
    with ThreadPoolExecutor(max_workers=reg["executor_workers"]) as pool, (out / "results.jsonl").open("x") as stream:
        futures = {}
        for tid in reg["dispatch_order"]:
            task, index = lookup[tid]
            folder = out / "trials" / tid
            futures[pool.submit(run_trial, task, index, folder, agent_revision="B1", deadline_s=manifest["deadline_s"])] = (task, index, folder)
        for future in as_completed(futures):
            task, index, folder = futures[future]
            try:
                row = future.result()
            except Exception as exc:
                row = _unexpected_trial_failure(task, index, folder, exc)
            rows.append(row)
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            print(json.dumps({"recorded": len(rows), "planned": 9, "trial": analysis.key(row), "status": row["status"]}, ensure_ascii=False), flush=True)
    exact, terminal = completion_check(reg["selected_trial_ids"], rows)
    final_sources = {p: sha(ROOT / p) for p in reg["working_source_digests"]}
    drift = [p for p, h in reg["working_source_digests"].items() if final_sources[p] != h]
    archive_changed = sha(base / "results.jsonl") != reg["base_results_sha256"]
    complete = exact and not terminal and not drift and not archive_changed
    result = {"schema_version": SCHEMA, "mode": reg["mode"], "finished_at": now(),
              "registration_sha256": sha(reg_path), "selected_trial_ids": reg["selected_trial_ids"],
              "completed_trial_ids": sorted(analysis.key(r) for r in rows), "recorded_trials": len(rows),
              "exact_selected_slots": exact, "no_terminal_provider_error": not terminal,
              "terminal_provider_errors": terminal, "source_drift": drift, "base_archive_changed": archive_changed,
              "post_run_working_source_digests": final_sources,
              "results_sha256": sha(out / "results.jsonl"), "complete": complete,
              "status": "complete_without_terminal_provider_errors" if complete else "retry_incomplete_or_provider_interrupted",
              "note": "complete describes exact selected executions without terminal provider errors; it never means all business outcomes passed."}
    analysis.write_json(out / "retry-completion.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "execute"])
    parser.add_argument("--base", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "prepare" and args.base is None:
        parser.error("prepare requires --base")
    result = prepare(args.base, args.out) if args.action == "prepare" else execute(args.out)
    print(json.dumps(result, ensure_ascii=False))
    if args.action == "execute" and not result["complete"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
