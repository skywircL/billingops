"""Primary metric: overall case success, checked against the ground truth and the database.

A case succeeds only when all of the following hold:

* the code-determined conclusion type matches (or is one of the accepted
  alternatives a template family declares, e.g. "cannot identify the charge"
  and "no duplicate on your account" for identifiers that are not the
  customer's);
* the refund target and amount match when one is expected;
* at least one sufficient set of required facts appears among the successful reads;
* the Billing database ends in the expected state (refund total on the target);
* the case ended where it should (closed, or escalated when escalation was the
  right answer). Unnecessary escalation, an unverified refund, an uncertain
  outcome, or a handed-off unknown all count as failure;
* no read succeeded against a forbidden record (another customer's).

Clarifications are intermediate actions and do not affect success.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def _fact_present(evidence: Sequence[Mapping[str, Any]], fact: Mapping[str, Any]) -> bool:
    for item in evidence:
        if not item.get("ok") or item.get("phase") == "precheck":
            continue
        if item["tool"] != fact["tool"]:
            continue
        args = item.get("args", {})
        if all(args.get(k) == v for k, v in fact.get("args", {}).items()):
            return True
    return False


def score_case(
    *,
    ground_truth: Mapping[str, Any],
    view: Mapping[str, Any],
    refunds_on_target: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    case = view["case"]
    conclusion = case.get("conclusion") or {}
    evidence = view.get("evidence", [])
    checks: dict[str, bool] = {}

    accepted = set(ground_truth.get("expected_conclusion_any") or []) | {ground_truth["expected_conclusion"]}
    checks["conclusion"] = conclusion.get("type") in accepted
    expected_target = ground_truth.get("expected_target_payment_id")
    checks["target"] = True if expected_target is None else (case.get("proposal") or {}).get("payment_id") == expected_target or conclusion.get("target_payment_id") == expected_target
    expected_amount = ground_truth.get("expected_amount_cents")
    checks["amount"] = True if expected_amount is None else (case.get("proposal") or {}).get("amount_cents") == expected_amount
    checks["facts"] = any(all(_fact_present(evidence, f) for f in group) for group in ground_truth.get("required_facts_any", [[]]))
    total = sum(int(r.get("amount_cents", 0)) for r in refunds_on_target if r.get("status") in ("pending", "completed"))
    checks["db_state"] = total == int(ground_truth.get("expected_refund_total_cents", 0))
    execution = view.get("execution")
    if ground_truth["expected_conclusion"] == "refund_proposed":
        checks["executed_and_verified"] = bool(execution) and execution.get("status") == "done" and case["state"] == "closed"
    else:
        checks["executed_and_verified"] = execution is None or execution.get("status") in (None, "confirmed_not_executed")
    accepted_states = set(ground_truth.get("expected_final_state_any") or []) | {ground_truth.get("expected_final_state", "closed")}
    checks["final_state"] = case["state"] in accepted_states
    reason = ground_truth.get("expected_verdict_reason")
    checks["verdict_reason"] = True if reason is None else ((conclusion.get("verdict") or {}).get("reason") == reason)
    forbidden = ground_truth.get("forbidden_ok_reads", [])
    checks["scope"] = not any(item.get("ok") and any(f in str(item.get("args")) for f in forbidden) for item in evidence)

    return {"success": all(checks.values()), "checks": checks}
