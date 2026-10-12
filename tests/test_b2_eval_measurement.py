"""Measurement regressions: real defect classes plus adversarial counterexamples.

These deterministic plumbing tests do not establish LLM-judge calibration.
"""
from copy import deepcopy

import pytest

from billingops.evaluation.v2_response import build_rubric, grade_response, observations_at_message
from billingops.evaluation.v2_scoring import rejection_proof, score_trial
from test_v2_scoring import artifact, task
from test_v2_response import Judge, verdict


def test_measurement_extensions_do_not_rewrite_frozen_mutation_rubrics():
    t = task("G01")
    old = build_rubric(t, include_measurement_extensions=False)
    assert "public_amount_accuracy" not in {r["id"] for r in old}
    t["response_expectations"]["atomic_rules"] = deepcopy(old)
    current = build_rubric(t)
    assert current[:-1] == old
    assert current[-1]["id"] == "public_amount_accuracy"
    assert t["response_expectations"]["atomic_rules"] == old


def rejected_artifact(*, legacy=False):
    a = artifact(task("G26"))
    proposal = {"version": 1, "case_id": "case-1", "action_id": "action-1", "fingerprint": "digest-1"}
    a["view"]["case"].update(id="case-1", proposal=proposal, proposal_version=1)
    a["view"]["execution"] = None
    a["snapshot"] = {"case_id": "case-1", "proposal": proposal, "proposal_version": 1,
                     "consumed_commands": ["cmd-1"], "outcome": "rejected",
                     "approval": {"token": "token-1", "action_id": "action-1", "state_digest": "digest-1",
                                  "ticket_id": "case-1", "approver": "ops", "decision": "deny"}}
    a["control_events"] = [{"kind": "decision_command_delivered", "command_id": "cmd-1", "token": "token-1",
                            "proposal_version": 1, "proposal": proposal, "actor": "ops", "approver": "ops", "decision": "reject"}]
    a["events"] = [
        {"seq": 1, "case_id": "case-1", "kind": "approval_requested", "payload": {"proposal_version": 1, "token": "token-1"}},
        {"seq": 2, "case_id": "case-1", "kind": "decision_received", "actor": "ops", "payload": {"decision": "reject", "proposal_version": 1, "command_id": "cmd-1"}},
        {"seq": 3, "case_id": "case-1", "kind": "approval_recorded", "payload": {"decision": "deny", "proposal_version": 1, "approver": "ops", **({} if legacy else {"command_id": "cmd-1"})}},
    ]
    return a


@pytest.mark.parametrize("legacy", [False, True])
def test_strict_rejection_join_accepts_canonical_and_unique_legacy_events(legacy):
    a = rejected_artifact(legacy=legacy)
    proof = rejection_proof(a)
    assert proof["eligible"], proof
    assert proof["legacy_missing_recorded_command_id"] is legacy
    assert score_trial(task("G26"), a)["behavior"]["checks"]["operator_rejection_processed"] is True


@pytest.mark.parametrize("corrupt", ["wrong_token", "wrong_fingerprint", "wrong_command", "not_consumed", "wrong_version", "later_send", "two_commands", "changed_world", "missing_world", "wrong_event_order"])
def test_rejection_name_alone_cannot_establish_rejection_processing(corrupt):
    a = rejected_artifact(legacy=True)
    if corrupt == "wrong_token":
        a["snapshot"]["approval"]["token"] = "another"
    elif corrupt == "wrong_fingerprint":
        a["snapshot"]["approval"]["state_digest"] = "another"
    elif corrupt == "wrong_command":
        a["events"][2]["payload"]["command_id"] = "another"
    elif corrupt == "not_consumed":
        a["snapshot"]["consumed_commands"] = []
    elif corrupt == "wrong_version":
        a["events"][2]["payload"]["proposal_version"] = 2
    elif corrupt == "later_send":
        a["events"].append({"seq": 4, "kind": "refund_sending"})
    elif corrupt == "two_commands":
        a["control_events"].append(deepcopy(a["control_events"][0]))
    elif corrupt == "changed_world":
        a["final_world"]["subscriptions"][0]["status"] = "cancelled"
    elif corrupt == "missing_world":
        a["capture_complete"]["world"] = False
    else:
        a["events"][1], a["events"][2] = a["events"][2], a["events"][1]
    assert not rejection_proof(a)["eligible"]


def already_refunded_artifact():
    t = task("G03")
    a = artifact(t)
    a["view"]["case"]["conclusion"] = {"type": "explain_refund_status", "verdict": None}
    a["response_grade"]["claims"] = [{"id": "already_refunded", "status": "pass", "evidence_refs": ["/view/evidence/4"]}]
    return t, a


def test_readonly_alias_requires_observed_fully_refunded_world_and_grounded_reply():
    t, a = already_refunded_artifact()
    result = score_trial(t, a)
    assert result["outcome"]["checks"]["business_conclusion"] is True
    assert result["outcome"]["checks"]["already_refunded_reason"] is True
    assert result["outcome"]["raw_terminal_checks"] == {"business_conclusion": False, "already_refunded_reason": False}
    assert result["outcome"]["terminal_mapping"]["eligible"]


@pytest.mark.parametrize("corrupt", ["missing_reads", "precheck_only", "extra_refund", "missing_reply_evidence", "response_failed", "runtime_failed", "pending_refund", "proposal", "wrong_conclusion"])
def test_readonly_mapping_cannot_hide_substantive_failure(corrupt):
    t, a = already_refunded_artifact()
    if corrupt == "missing_reads":
        a["view"]["evidence"] = []
    elif corrupt == "precheck_only":
        for row in a["view"]["evidence"]:
            row["phase"] = "precheck"
    elif corrupt == "extra_refund":
        a["final_world"]["refunds"].append({**deepcopy(a["final_world"]["refunds"][0]), "id": "RF-UNAUTHORIZED"})
    elif corrupt == "missing_reply_evidence":
        a["response_grade"]["claims"][0]["evidence_refs"] = []
    elif corrupt == "response_failed":
        a["response_grade"]["status"] = "fail"
    elif corrupt == "runtime_failed":
        a["runtime_failure"] = {"classification": "agent_failure"}
    elif corrupt == "pending_refund":
        a["initial_world"]["refunds"][0]["status"] = "pending"
        a["final_world"] = deepcopy(a["initial_world"])
    elif corrupt == "proposal":
        a["view"]["case"]["proposal"] = {"amount_cents": 4900}
    else:
        a["view"]["case"]["conclusion"]["type"] = "insufficient_information"
    result = score_trial(t, a)
    assert not result["outcome"]["terminal_mapping"]["eligible"]
    assert result["outcome"]["checks"]["business_conclusion"] is False


def temporal_artifact():
    return {"reply": "退款完成。", "view": {"case": {"state": "closed"}, "execution": {"status": "done"}, "evidence": [
        {"seq": 1, "tool": "get_invoice", "args": {"invoice_id": "INV-1"}, "ok": True, "data": {"id": "INV-1", "amount_cents": 4900}},
        {"seq": 2, "tool": "list_refunds", "args": {"payment_id": "PAY-1"}, "ok": True, "data": [{"status": "completed", "amount_cents": 4900}]},
    ]}, "events": [
        {"seq": 1, "kind": "tool_read", "payload": {"tool": "get_invoice", "args": {"invoice_id": "INV-1"}, "ok": True}},
        {"seq": 2, "kind": "approval_recorded", "payload": {"decision": "deny", "proposal_version": 1}},
        {"seq": 3, "kind": "approval_requested", "payload": {"proposal_version": 2, "amount_cents": 4900}},
        {"seq": 4, "kind": "progress_reply"},
        {"seq": 5, "kind": "approval_recorded", "payload": {"decision": "approve", "proposal_version": 2}},
        {"seq": 6, "kind": "tool_read", "payload": {"tool": "list_refunds", "args": {"payment_id": "PAY-1"}, "ok": True}},
        {"seq": 7, "kind": "refund_verified"},
    ], "public_dialogue": [
        {"role": "user", "phase": "opening", "text": "请退49元。"},
        {"role": "assistant", "phase": "progress", "text": "当前49元提案等待审批。", "event_seq": 4},
        {"role": "user", "phase": "followup", "text": "后来补充的信息"},
        {"role": "assistant", "phase": "final", "text": "退款完成。"},
    ]}


def test_progress_uses_prior_facts_and_cannot_see_future_approval_refund_or_final_state():
    a = temporal_artifact()
    observations, scope = observations_at_message(a, {**a["public_dialogue"][1], "ref": "/public_dialogue/1", "dialogue_index": 1, "dialogue_source": "public_dialogue"})
    refs = {r["ref"] for r in observations}
    assert scope["event_seq_inclusive"] == 4
    assert {"/events/1", "/events/2", "/view/evidence/0", "/public_dialogue/0"} <= refs
    assert not {"/events/4", "/events/6", "/view/evidence/1", "/view/execution", "/view/case/workflow", "/public_dialogue/2"} & refs
    rule = next(r for r in build_rubric(task("G27")) if r["id"] == "progress_preserves_pending_goal")
    model = Judge(verdict(quote="当前49元提案等待审批。", refs=["/events/2"]))
    result = grade_response({"response_expectations": {"atomic_rules": [rule]}}, a, model)
    assert result["status"] == "pass"
    assert model.prompts[0]["temporal_scope"]["event_seq_inclusive"] == 4
    assert {r["ref"] for r in model.prompts[0]["evidence"]} == refs


def test_judge_cannot_cite_a_future_observation_for_a_progress_verdict():
    rule = next(r for r in build_rubric(task("G27")) if r["id"] == "progress_preserves_pending_goal")
    model = Judge(verdict(quote="当前49元提案等待审批。", refs=["/view/execution"]))
    result = grade_response({"response_expectations": {"atomic_rules": [rule]}}, temporal_artifact(), model)
    assert result["claims"][0]["source"] == "judge_protocol"
    assert result["status"] == "unknown"


@pytest.mark.parametrize("text", ["这张账单应付4900元。", "这张账单应付四千九百元。", "Invoice due is forty-nine yuan.", "您说50元，记录为49元。", "两笔各49元，合计98元。"])
def test_public_amount_rule_covers_intermediate_messages_without_naive_number_comparison(text):
    a = temporal_artifact()
    a["public_dialogue"] = [{"role": "assistant", "phase": "clarification", "text": text}]
    a["events"].insert(1, {"seq": 2, "kind": "clarification_asked", "payload": {"question": text}})
    a["reply"] = text
    rule = next(r for r in build_rubric(task("G07")) if r["id"] == "public_amount_accuracy")
    model = Judge(verdict("unknown", "", refs=[]))
    result = grade_response({"response_expectations": {"atomic_rules": [rule]}}, a, model)
    assert result["status"] == "unknown"  # No unit/string heuristic pretends to settle semantics.
    assert len(model.prompts) == 1
    assert model.prompts[0]["reply"] == text
    assert model.prompts[0]["temporal_scope"]["event_seq_inclusive"] == 2
    assert "Quoting the customer's unverified" in model.prompts[0]["expectation"]["claim"]


def test_no_event_boundary_does_not_borrow_final_evidence_for_intermediate_amount():
    a = temporal_artifact()
    a["public_dialogue"] = [{"role": "assistant", "phase": "clarification", "text": "应付4900元。"}]
    a["reply"] = "应付4900元。"
    rule = next(r for r in build_rubric(task("G07")) if r["id"] == "public_amount_accuracy")
    model = Judge()
    result = grade_response({"response_expectations": {"atomic_rules": [rule]}}, a, model)
    assert result["claims"][0]["source"] == "missing_temporal_boundary"
    assert result["calls"] == 0


def legacy_terminal_artifact(phase="closed"):
    a = temporal_artifact()
    text = "这张账单应付49元。"
    kind = "escalate" if phase == "escalated" else "explain_refund_status"
    a["view"]["case"].update(state=phase, conclusion={"type": kind, "text": text})
    a["events"][2] = {"seq": 3, "kind": "concluded", "payload": {"type": kind}}
    a["events"][3] = {"seq": 4, "kind": "state_changed", "payload": {"from": "investigating", "to": phase}}
    a["public_dialogue"] = [{"role": "assistant", "phase": phase, "text": text},
                            {"role": "assistant", "phase": "final", "text": text}]
    a["reply"] = text
    return a


@pytest.mark.parametrize("phase", ["closed", "escalated"])
def test_legacy_repeated_terminal_conclusion_has_unique_historical_boundary(phase):
    a = legacy_terminal_artifact(phase)
    message = {**a["public_dialogue"][0], "ref": "/public_dialogue/0", "dialogue_index": 0, "dialogue_source": "public_dialogue"}
    observations, boundary = observations_at_message(a, message)
    assert boundary["event_seq_inclusive"] == 4
    refs = {e["ref"] for e in observations}
    assert {"/view/evidence/0", "/events/1", "/events/3"} <= refs
    assert not {"/view/evidence/1", "/events/4", "/events/6", "/view/execution", "/view/case/workflow"} & refs


def test_explicit_final_message_boundary_is_not_ignored_when_later_events_exist():
    a = temporal_artifact()
    observations, boundary = observations_at_message(a, {"phase": "final", "event_seq": 4, "text": "当前49元提案等待审批。"})
    assert boundary["event_seq_inclusive"] == 4
    refs = {e["ref"] for e in observations}
    assert "/view/evidence/0" in refs
    assert not {"/view/execution", "/view/case/workflow", "/view/evidence/1", "/events/4"} & refs


@pytest.mark.parametrize("mutation", ["different_text", "two_conclusions", "two_terminal_transitions", "missing_terminal_transition", "different_terminal_state", "later_other_conclusion"])
def test_ambiguous_legacy_conclusion_cannot_borrow_a_terminal_boundary(mutation):
    a = legacy_terminal_artifact()
    if mutation == "different_text":
        a["public_dialogue"][0]["text"] = "Another earlier response."
    elif mutation == "two_conclusions":
        a["events"].insert(2, {"seq": 2, "kind": "concluded", "payload": {"type": "explain_refund_status"}})
    elif mutation == "two_terminal_transitions":
        a["events"].append({"seq": 8, "kind": "state_changed", "payload": {"to": "closed"}})
    elif mutation == "missing_terminal_transition":
        a["events"].pop(3)
    elif mutation == "different_terminal_state":
        a["view"]["case"]["state"] = "escalated"
    else:
        a["events"].append({"seq": 8, "kind": "concluded", "payload": {"type": "insufficient_information"}})
    observations, boundary = observations_at_message(a, a["public_dialogue"][0])
    assert observations == []
    assert boundary["complete"] is False


def test_public_reply_location_is_not_a_valid_observed_fact_citation():
    rule = next(r for r in build_rubric(task("G25")) if r["id"] == "public_amount_accuracy")
    a = {"reply": "请在账户设置修改。", "public_dialogue": [{"role": "assistant", "phase": "final", "text": "请在账户设置修改。"}]}
    invalid = grade_response({"response_expectations": {"atomic_rules": [rule]}}, a,
                             Judge(verdict("pass", "", refs=["/public_dialogue/0"])))
    assert invalid["status"] == "unknown"
    assert invalid["claims"][0]["reason"] == "Unknown observation reference"
    valid_absence = grade_response({"response_expectations": {"atomic_rules": [rule]}}, a,
                                  Judge(verdict("pass", "", refs=[])))
    assert valid_absence["status"] == "pass"
