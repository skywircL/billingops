"""Real HTTP reference checks do not claim customer/Agent success."""
from copy import deepcopy
import json

import pytest

from billingops.billing_client import BillingUnavailable
from billingops.evaluation.business_v2 import ROOT
from billingops.evaluation.v2_mutation import build_bundle
from billingops.evaluation.v2_mutation_reference import run_reference, run_reference_task, verify_post_attempts


@pytest.fixture(scope="module")
def mutation_bundle():
    return build_bundle(
        json.loads((ROOT / "data/tasks/dynamic-v2/gold-fixtures-v0.2.json").read_text()),
        json.loads((ROOT / "data/eval-design/dynamic-v2/controlled-text-rewrites-v2.json").read_text()),
    )


def select(bundle, case_id=None, operator=None):
    return deepcopy(next(t for t in bundle["tasks"] if (case_id is None or t["case_id"] == case_id) and (operator is None or t["mutation"]["operator"] == operator)))


def test_transient_read_failure_is_observed_and_retried_once(billing, mutation_bundle):
    task = select(mutation_bundle, operator="F1")
    artifact = run_reference_task(task, billing)
    assert artifact["fixture_feasibility"], artifact["errors"]
    assert artifact["fault_before"]["reads_unavailable"] == 0
    assert artifact["fault_armed"]["reads_unavailable"] == 1
    assert artifact["fault_after"]["reads_unavailable"] == 0
    assert artifact["fault_consumed"] == {"reads_unavailable": 1}
    failures = [r for r in artifact["reads"] if r.get("status") == 503]
    retries = [r for r in artifact["reads"] if r.get("recovered_after_retry")]
    assert len(failures) == len(retries) == 1
    assert failures[0]["args"] == retries[0]["args"]
    assert failures[0]["attempt"] == 1 and retries[0]["attempt"] == 2
    assert artifact["agent_metrics"] is None


def test_lost_write_response_is_verified_without_a_second_post(billing, mutation_bundle, monkeypatch):
    task = select(mutation_bundle, operator="F3")
    post = billing.post_refund
    actual_calls = []

    def counted(payload):
        actual_calls.append(deepcopy(payload))
        return post(payload)

    monkeypatch.setattr(billing, "post_refund", counted)
    artifact = run_reference_task(task, billing)
    assert artifact["fixture_feasibility"], artifact["errors"]
    assert len(actual_calls) == len(task["expected_refunds"]) == 1
    assert len(artifact["writes"]) == 1
    event = artifact["writes"][0]
    assert event["response_status"] == 500
    assert event["independent_verification"]["status"] == "succeeded"
    assert artifact["fault_consumed"] == {"lost_response_after_commit": 1}
    assert artifact["world_change"]["new_request_count"] == 1
    assert len([r for r in artifact["reads"] if r["tool"] == "refund_by_key"]) == 1


def test_idempotent_duplicate_post_is_a_reference_failure_even_with_same_final_world(billing, mutation_bundle):
    artifact = run_reference_task(select(mutation_bundle, operator="F3"), billing)
    assert artifact["world_change"]["ok"]
    # Even a same-key retry that would leave the ledger unchanged is forbidden.
    duplicated = artifact["writes"] + deepcopy(artifact["writes"])
    check = verify_post_attempts(duplicated, artifact["oracle_refunds"])
    assert not check["ok"]
    assert any("duplicate POST" in e for e in check["errors"])


def test_failed_verification_never_triggers_write_replay(billing, mutation_bundle, monkeypatch):
    monkeypatch.setattr(billing, "refund_by_key", lambda _key: None)
    artifact = run_reference_task(select(mutation_bundle, operator="F3"), billing)
    assert not artifact["fixture_feasibility"]
    assert len(artifact["writes"]) == 1
    assert artifact["world_change"]["new_request_count"] == 1
    assert any("independent refund receipt" in e for e in artifact["errors"])


def test_declared_read_retry_budget_is_not_reused_for_later_failures(billing, mutation_bundle, monkeypatch):
    calls = []

    def fail(*args):
        calls.append(args)
        raise BillingUnavailable("unexpected later outage", status=503)

    monkeypatch.setattr(billing, "payment_refunds", fail)
    artifact = run_reference_task(select(mutation_bundle, operator="F1"), billing)
    assert not artifact["fixture_feasibility"]
    assert len(calls) == 1
    assert artifact["writes"] == []
    assert len([r for r in artifact["reads"] if r.get("recovered_after_retry")]) == 1


def test_tampered_expected_money_is_rejected_before_world_reset(billing, mutation_bundle):
    task = select(mutation_bundle, "G01")
    before = billing.dump_world()
    task["expected_refunds"][0]["amount_cents"] += 1
    with pytest.raises(ValueError, match="independent invoice_oracle"):
        run_reference_task(task, billing)
    assert billing.dump_world() == before


def test_bundle_validator_rejects_tampered_gt_before_creating_output(mutation_bundle, tmp_path):
    bundle = deepcopy(mutation_bundle)
    bundle["tasks"][0]["expected_refunds"][0]["amount_cents"] += 1
    with pytest.raises(ValueError, match="controlled mutation validation failed"):
        run_reference(bundle, tmp_path / "must-not-exist")
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize("case_id", ["G07", "G14", "G26", "G28", "G31", "G01-P1-01", "G02-P1-01"])
def test_rejection_unresolved_withdrawal_and_foreign_scope_have_zero_writes(billing, mutation_bundle, case_id):
    artifact = run_reference_task(select(mutation_bundle, case_id), billing)
    assert artifact["fixture_feasibility"], artifact["errors"]
    assert artifact["writes"] == []
    assert artifact["before"] == artifact["after"]
    if artifact["grading_profile_id"] == "G31":
        assert artifact["derived_evidence"]["permission_boundary_verified"]
        assert any(r["status"] == 404 for r in artifact["reads"])


def test_last_week_is_read_from_unchanged_billing_dates(billing, mutation_bundle):
    task = select(mutation_bundle, "G19")
    artifact = run_reference_task(task, billing)
    assert artifact["fixture_feasibility"], artifact["errors"]
    assert artifact["before"] == artifact["after"]
    evidence = artifact["derived_evidence"]["refund_time_window"]
    assert evidence["as_of"] == task["as_of"]
    assert evidence["refund_ids"] == ["RF-0102"]
    assert evidence["window"] == task["author_scope"]["refund_created_at_window"]


def test_multiple_extra_payments_are_distinct_actions_and_all_refunds_read(billing, mutation_bundle):
    artifact = run_reference_task(select(mutation_bundle, "G12"), billing)
    assert artifact["fixture_feasibility"], artifact["errors"]
    assert len(artifact["writes"]) == 2
    assert {r["args"][1] for r in artifact["reads"] if r["tool"] == "payment_refunds"} == {"PAY-88212", "PAY-88213", "PAY-88214"}
    assert artifact["post_attempts"]["ok"]


def test_unregistered_fault_or_lost_response_without_write_is_refused(billing, mutation_bundle):
    task = select(mutation_bundle, "G01")
    task["fault_plan"] = {"reads_unavailable": 2}
    with pytest.raises(ValueError, match="one registered"):
        run_reference_task(task, billing)
    task = select(mutation_bundle, "G26")
    task["fault_plan"] = {"lost_response_after_commit": 1}
    with pytest.raises(ValueError, match="authorized refund action"):
        run_reference_task(task, billing)
