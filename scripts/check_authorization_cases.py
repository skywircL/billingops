#!/usr/bin/env python3
"""Four admission gates for the exposed authorization challenge.

--reference uses real local Billing HTTP and independently checked terminal
worlds. It never calls a model, simulates customer approval, or reports an
Agent success rate. Output directories are new-only to preserve prior runs.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any, Mapping

from billingops.billing_client import BillingClient
from billingops.evaluation.authorization_cases import FIXTURE_PATH, SUITE_ID, build_bundle, digest, negative_evidence, validate_bundle
from billingops.evaluation.v2_mutation_reference import run_reference_task
from billingops.testing import billing_server


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_reference(bundle: Mapping[str, Any], out_dir: str | Path) -> dict[str, Any]:
    admission = validate_bundle(bundle)
    if not admission["ok"]:
        raise ValueError("authorization fixture admission failed: " + json.dumps(admission["errors"], ensure_ascii=False))
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=False)
    write_json(destination / "fixtures.json", bundle)
    write_json(destination / "admission.json", admission)
    results = []
    with tempfile.TemporaryDirectory(prefix="billingops-authorization-reference-") as directory:
        with billing_server(Path(directory) / "billing.sqlite3") as server:
            client = BillingClient(server.url, admin_token="demo-admin")
            try:
                for task in bundle["tasks"]:
                    result = run_reference_task(deepcopy(task), client)
                    result["negative_evidence"] = negative_evidence(task)
                    # Real reads/ownership checks must support negative witnesses;
                    # this is still a fixture reference, never an Agent trial.
                    if task["case_id"] in {"AS06", "AS07", "AS10", "AS11"}:
                        observed = {r["args"][1] for r in result["reads"] if r["tool"] == "payment_refunds"}
                        if not {"PAY-1", "PAY-2", "PAY-3"} <= observed:
                            result["errors"].append("negative reference did not inspect all scoped payment refund records")
                    result["fixture_feasibility"] = not result["errors"]
                    result["agent_metrics"] = None
                    filename = task["case_id"] + ".json"
                    write_json(destination / filename, result)
                    results.append({"case_id": task["case_id"], "ok": result["fixture_feasibility"], "errors": result["errors"], "write_count": len(result["writes"]), "reference_path": filename, "reference_sha256": hashlib.sha256((destination / filename).read_bytes()).hexdigest()})
            finally:
                client.close()
    report = {"suite_id": SUITE_ID, "suite_version": bundle["suite_version"], "review_corrections": bundle.get("review_corrections", []), "mode": "model_free_Billing_reference_not_agent_evaluation", "fixture_sha256": digest(bundle), "ok": admission["ok"] and all(row["ok"] for row in results), "admission": admission, "results": results, "positive_reference_count": sum(bool(t["expected_refunds"]) for t in bundle["tasks"]), "negative_reference_count": sum(not t["expected_refunds"] for t in bundle["tasks"]), "total_post_attempts": sum(row["write_count"] for row in results), "agent_metrics": None, "production_frequency_estimate": None, "human_review": "pending", "limitations": ["No live or scripted Agent execution; no claim of end-to-end task success.", "No model interpretation, customer dialogue, operator approval or followup transition is exercised by this reference.", "AS12/AS13 validate final-scope Billing feasibility only; turn-by-turn authorization probes require separate Agent/control tests.", "Static registered annotations and arithmetic do not replace independent semantic human review.", "Purposive risk-oversampled development challenge; lexical variants are grouped, not independent production samples."]}
    write_json(destination / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--build", action="store_true", help="write the registered synthetic fixture asset")
    parser.add_argument("--reference", action="store_true", help="also run model-free real Billing feasibility")
    parser.add_argument("--out", type=Path, help="new output directory for reference artifacts")
    args = parser.parse_args()
    if args.build:
        args.fixtures.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.fixtures, build_bundle())
    bundle = json.loads(args.fixtures.read_text(encoding="utf-8"))
    if args.reference:
        if not args.out:
            parser.error("--reference requires --out (new directory)")
        report = run_reference(bundle, args.out)
        print(json.dumps({"ok": report["ok"], "case_count": len(report["results"]), "positive_reference_count": report["positive_reference_count"], "negative_reference_count": report["negative_reference_count"], "total_post_attempts": report["total_post_attempts"], "agent_metrics": None, "report": str(args.out / "report.json")}, ensure_ascii=False))
    else:
        report = validate_bundle(bundle)
        if args.out:
            args.out.mkdir(parents=True, exist_ok=False)
            write_json(args.out / "admission.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
