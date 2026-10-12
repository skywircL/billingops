"""Model-free Billing feasibility for a validated V2 controlled mutation bundle.

The effective scope and customer confirmations are fixture premises. This does
not execute an Agent, customer conversation, operator approval, or handoff.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping

import httpx

from billingops.billing_client import BillingClient, BillingUnavailable, NotFound
from billingops.evaluation.business_v2 import invoice_oracle, verify_world_change
from billingops.testing import billing_server

REFERENCE_VERSION = "controlled-mutation-reference-1"
LIMITATIONS = [
    "agent_not_exercised", "customer_dialogue_and_semantic_matching_not_exercised",
    "operator_approval_not_exercised", "followup_state_transitions_not_exercised",
    "handoff_not_exercised", "response_quality_not_exercised",
    "effective_scope_and_confirmations_are_fixture_premises",
    "sequential_reference_is_not_invoice_atomic_concurrency_validation",
    "reference_recovery_does_not_prove_agent_recovery",
]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _refunds(value: list[dict[str, Any]]) -> Counter:
    return Counter((r["payment_id"], r["amount_cents"]) for r in value)


def verify_post_attempts(writes: list[dict[str, Any]], oracle: list[dict[str, Any]]) -> dict[str, Any]:
    """Idempotent duplicate POSTs still fail, even if the world has one refund."""
    errors = []
    if len(writes) != len(oracle):
        errors.append("POST attempt count differs from authorized refund action count")
    keys = [w["request"]["idempotency_key"] for w in writes]
    if len(set(keys)) != len(keys):
        errors.append("duplicate POST attempt for the same idempotency key")
    actual = [{"payment_id": w["request"]["payment_id"], "amount_cents": w["request"]["amount_cents"]} for w in writes]
    if _refunds(actual) != _refunds(oracle):
        errors.append("POST action multiset differs from independent oracle")
    return {"ok": not errors, "errors": errors, "post_attempt_count": len(writes)}


def run_reference_task(task: Mapping[str, Any], client: BillingClient) -> dict[str, Any]:
    """Reset a single world and exercise real HTTP; callers validate the bundle first.

    The independently recomputed answer and supported fault plan are also checked
    here, before any reset or write, to reject accidental direct misuse.
    """
    oracle = invoice_oracle(task)
    if _refunds(oracle) != _refunds(task["expected_refunds"]):
        raise ValueError("expected_refunds differs from independent invoice_oracle")
    fault_plan = dict(task.get("fault_plan") or {})
    if any(key not in {"reads_unavailable", "lost_response_after_commit"} or type(value) is not int or value != 1 for key, value in fault_plan.items()) or len(fault_plan) > 1:
        raise ValueError("reference supports one registered read failure or lost write response only")
    if fault_plan.get("lost_response_after_commit") and not oracle:
        raise ValueError("lost-response fixture must contain an authorized refund action")
    scope = task["author_scope"]
    if oracle and (scope["authorization"] != "refund_requested" or scope["kind"] == "read_only" or task["approval_control"]["decision"] == "reject"):
        raise ValueError("read-only, unresolved, rejected or forbidden scope cannot write")
    client.load_world(deepcopy(task["world"]))
    before = client.dump_world()  # Capture before arming: admin reads cannot consume a fault.
    if _refunds(invoice_oracle({**task, "world": before})) != _refunds(oracle):
        raise ValueError("loaded Billing world changes independent refund oracle")
    fault_before = client.fault_snapshot()
    if any(fault_before.values()):
        raise AssertionError("fresh world retained an earlier fault")
    armed = client.arm_faults(**fault_plan) if fault_plan else fault_before
    reads: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []
    errors: list[str] = []
    derived: dict[str, Any] = {}
    customer = task["customer_id"]
    profile = task.get("grading_profile_id", task["candidate_id"])
    read_retry_budget = int(bool(fault_plan.get("reads_unavailable")))

    def read(tool: str, fn: Callable[..., Any], *args: Any, allow_not_found: bool = False) -> Any:
        nonlocal read_retry_budget
        # Exactly one retry, and only for the declared transient business-read fault.
        for attempt in range(1, 3 if fault_plan.get("reads_unavailable") else 2):
            event: dict[str, Any] = {"tool": tool, "args": list(args), "attempt": attempt}
            reads.append(event)
            try:
                result = fn(*args)
                event.update(status=200, result=result, recovered_after_retry=attempt > 1)
                return result
            except NotFound:
                event.update(status=404, result="not_found_in_bound_customer_scope")
                if allow_not_found:
                    return None
                raise
            except BillingUnavailable as exc:
                event.update(status=exc.status, error=str(exc), error_type=type(exc).__name__)
                if not (read_retry_budget and attempt == 1 and exc.status == 503):
                    raise
                read_retry_budget -= 1
        raise AssertionError("unreachable reference read retry state")

    try:
        policy = next(p for p in task["world"]["policies"] if p["name"] == "refunds")
        read("get_policy", client.get_policy, "refunds", policy["version"])
        selected = list(scope.get("invoice_ids", []))
        if scope["kind"] == "month_plan":
            invoices = read("list_invoices", client.list_invoices, customer)
            selected = [i["id"] for i in invoices if i.get("subscription_id") == scope["subscription_id"] and i["period_start"].startswith(scope["month"])]
            if len(selected) != 1:
                raise AssertionError("actual month/plan lookup did not resolve one invoice")
            derived["month_plan_invoice_ids"] = selected
        # Include user-visible invalid/conflicting identifiers; they remain scoped
        # reads, and customer-script timing is expressly not being simulated.
        public_texts = [task["user_input"]] + [s["answer"] for s in task["customer_control"].get("conditional_script", [])]
        public_texts += [f["message"] for f in task.get("followups", [])]
        mentioned_invoices = sorted(set(re.findall(r"INV-[A-Za-z0-9-]+", " ".join(public_texts))))
        for invoice_id in mentioned_invoices:
            if invoice_id not in selected:
                read("get_invoice", client.get_invoice, customer, invoice_id, allow_not_found=True)
        observed_payments = set()
        for invoice_id in selected:
            invoice = read("get_invoice", client.get_invoice, customer, invoice_id)
            if invoice["customer_id"] != customer:
                raise AssertionError("invoice read crossed bound customer")
            payments = read("invoice_payments", client.invoice_payments, customer, invoice_id)
            for payment in payments:
                read("payment_refunds", client.payment_refunds, customer, payment["id"])
                observed_payments.add(payment["id"])
        for payment_id in scope.get("payment_ids", []):
            read("get_payment", client.get_payment, customer, payment_id)
            if payment_id not in observed_payments:
                read("payment_refunds", client.payment_refunds, customer, payment_id)
        if profile == "G14":
            for payment_id in sorted(set(re.findall(r"PAY-[A-Za-z0-9-]+", task["user_input"]))):
                record = read("get_payment", client.get_payment, customer, payment_id)
                derived.setdefault("identifier_conflicts", []).append({"payment_id": payment_id, "actual_invoice_id": record["invoice_id"], "mentioned_invoice_ids": mentioned_invoices, "mismatch": record["invoice_id"] not in mentioned_invoices})
        if profile in {"G04", "G10", "G11", "G20", "G21", "G22", "G23"}:
            read("subscription_history", client.subscription_history, customer)
            read("get_policy", client.get_policy, "pricing", 3)
        if profile == "G04":
            read("get_policy", client.get_policy, "billing_basis", 1)
        if profile == "G25":
            read("get_policy", client.get_policy, "support_channels", 2)
        if scope.get("refund_created_at_window"):
            refunds = read("customer_refunds", client.customer_refunds, customer)
            window = scope["refund_created_at_window"]
            start = datetime.fromisoformat(window["start"])
            end = datetime.fromisoformat(window["end_exclusive"])
            found = [r for r in refunds if start <= datetime.fromisoformat(r["created_at"]) < end]
            derived["refund_time_window"] = {"as_of": task["as_of"], "window": window, "refund_ids": [r["id"] for r in found]}
            if not found:
                raise AssertionError("actual Billing refund timestamps do not satisfy declared query window")
        if profile == "G31" or scope["authorization"] == "permission_refused":
            targets = [("get_payment", client.get_payment, scope.get("requested_foreign_payment")),
                       ("get_invoice", client.get_invoice, scope.get("requested_foreign_invoice"))]
            if not any(identifier for _, _, identifier in targets):
                raise AssertionError("permission fixture has no concrete foreign target")
            for tool, fn, foreign_id in targets:
                if foreign_id and read(tool, fn, customer, foreign_id, allow_not_found=True) is not None:
                    raise AssertionError("Billing exposed another customer's record")
            derived["permission_boundary_verified"] = True
        for expected in oracle:
            key = "reference-" + _digest([task.get("case_id", task["task_id"]), expected["payment_id"]])[:40]
            payload = {"idempotency_key": key, "dispute_key": f"duplicate/{customer}/{expected['payment_id']}", "customer_id": customer, "payment_id": expected["payment_id"], "amount_cents": expected["amount_cents"], "currency": "CNY", "reason": "model-free fixture feasibility; no Agent or approval claim"}
            event = {"request": payload, "attempt": 1}
            writes.append(event)  # Record the single attempt before sending it.
            try:
                sent = client.post_refund(payload)
                event.update(response_status=sent.status, response=sent.body)
            except httpx.HTTPError as exc:
                event.update(response_status=None, error_type=type(exc).__name__, error=str(exc))
            # Always verify independently. A dropped response must never cause
            # a second POST, including a POST with the same idempotency key.
            receipt = read("refund_by_key", client.refund_by_key, key)
            event["independent_verification"] = receipt
            if not receipt or receipt.get("status") != "succeeded" or any(receipt.get(f) != payload[f] for f in ("idempotency_key", "dispute_key", "customer_id", "payment_id", "amount_cents", "currency")):
                raise AssertionError("independent refund receipt does not verify exact action")
            if event["response_status"] not in (200, 201) and not fault_plan.get("lost_response_after_commit"):
                raise AssertionError("unexpected refund response failure outside the declared fault plan")
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    after = client.dump_world()
    fault_after = client.fault_snapshot()
    consumed = {k: armed.get(k, 0) - fault_after.get(k, 0) for k in fault_plan}
    if any(consumed.get(k) != v for k, v in fault_plan.items()):
        errors.append("declared fault was not consumed exactly as registered")
    if fault_plan.get("reads_unavailable") and not (any(r.get("status") == 503 for r in reads) and any(r.get("recovered_after_retry") for r in reads)):
        errors.append("transient read failure and bounded recovery were not both observed")
    if fault_plan.get("lost_response_after_commit") and not any(w.get("response_status") == 500 and (w.get("independent_verification") or {}).get("status") == "succeeded" for w in writes):
        errors.append("lost write response followed by verified commit was not observed")
    change = verify_world_change(before, after, list(task["expected_refunds"]), customer)
    posts = verify_post_attempts(writes, oracle)
    errors.extend(change["errors"])
    errors.extend(posts["errors"])
    return {"reference_version": REFERENCE_VERSION, "case_id": task.get("case_id", task["task_id"]), "candidate_id": task["candidate_id"], "grading_profile_id": profile, "task_sha256": _digest(task), "fixture_feasibility": not errors, "errors": errors, "before": before, "after": after, "fault_plan": fault_plan, "fault_before": fault_before, "fault_armed": armed, "fault_after": fault_after, "fault_consumed": consumed, "reads": reads, "writes": writes, "derived_evidence": derived, "oracle_refunds": oracle, "world_change": change, "post_attempts": posts, "agent_metrics": None, "reference_limitations": LIMITATIONS}


def run_reference(bundle: Mapping[str, Any], out_dir: str | Path) -> dict[str, Any]:
    from billingops.evaluation.v2_mutation import validate_mutation_bundle

    validation = validate_mutation_bundle(bundle)
    if not validation["ok"]:
        raise ValueError("controlled mutation validation failed: " + json.dumps(validation, ensure_ascii=False))
    # Check every answer before touching Billing, even if validator evolves.
    for task in bundle["tasks"]:
        if _refunds(invoice_oracle(task)) != _refunds(task["expected_refunds"]):
            raise ValueError(f"{task['case_id']}: expected_refunds differs from independent oracle")
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "bundle.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    results = []
    with billing_server(destination / "billing.sqlite") as server:
        client = BillingClient(server.url, admin_token=server.admin_token)
        try:
            for index, task in enumerate(bundle["tasks"], 1):
                artifact = run_reference_task(task, client)
                name = f"{index:03d}-" + re.sub(r"[^A-Za-z0-9_.-]", "_", task["case_id"])
                case_dir = destination / name
                case_dir.mkdir()
                (case_dir / "reference.json").write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                results.append({"case_id": task["case_id"], "fixture_feasibility": artifact["fixture_feasibility"], "new_refund_count": len(artifact["world_change"]["added_refunds"]), "post_attempt_count": len(artifact["writes"]), "errors": artifact["errors"], "artifact": f"{name}/reference.json"})
        finally:
            client.close()
    report = {"reference_version": REFERENCE_VERSION, "bundle_sha256": _digest(bundle), "contract_validation": validation, "fixture_feasibility": {"passed": sum(r["fixture_feasibility"] for r in results), "total": len(results), "cases": results}, "agent_metrics": None, "reference_limitations": LIMITATIONS, "interpretation": "Schema and simulated Billing feasibility only; not Agent dialogue, approval, success, or release evidence."}
    (destination / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("reference")
    command.add_argument("bundle")
    command.add_argument("--out", required=True)
    args = parser.parse_args()
    report = run_reference(json.loads(Path(args.bundle).read_text(encoding="utf-8")), args.out)
    print(json.dumps({"reference_version": REFERENCE_VERSION, "passed": report["fixture_feasibility"]["passed"], "total": report["fixture_feasibility"]["total"], "agent_metrics": None}, ensure_ascii=False))
    raise SystemExit(0 if report["fixture_feasibility"]["passed"] == report["fixture_feasibility"]["total"] else 1)


if __name__ == "__main__":
    main()
