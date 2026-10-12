"""Control tests use a scripted matcher; no provider or ground-truth access."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from billingops.evaluation.v2_controls import ConditionalCustomer, ControlScheduler, UNKNOWN_REPLY
from billingops.llm.base import Message, ModelError, ModelResponse, Usage
from billingops.config import MODEL_MAX_TOKENS

FIXTURES = Path(__file__).resolve().parents[1] / "data/tasks/dynamic-v2/gold-fixtures-v0.2.json"


def task(candidate: str):
    return next(t for t in json.loads(FIXTURES.read_text())["tasks"] if t["candidate_id"] == candidate)


class MatchModel:
    def __init__(self, matches):
        self.matches = iter(matches)
        self.calls = []

    def generate(self, messages, tool_schemas, config):
        self.calls.append((messages, tool_schemas, config))
        raw = next(self.matches)
        if isinstance(raw, Exception):
            raise raw
        if isinstance(raw, ModelResponse):
            return raw
        return ModelResponse(Message("assistant", json.dumps(raw, ensure_ascii=False)), Usage(input_tokens=100, output_tokens=20))


def selected(index, question, **kw):
    return {"script_index": index, "fact_indices": [], "evidence": [question], "reason": "match", **kw}


def customer(candidate, matches):
    model = MatchModel(matches)
    return ConditionalCustomer(task(candidate)["customer_control"], model, max_attempts=1), model


def current(version=1, proposal=None, state="awaiting_approval"):
    return {"state": state, "proposal_version": version, "proposal": proposal or {"payment_id": "WRONG-TARGET", "amount_cents": 999999}}


def scheduler(candidate):
    row = task(candidate)
    return ControlScheduler(row["approval_control"], row["followups"])


def test_batch_confirmation_keeps_exact_answer_and_accounts_for_cost():
    q = "查到INV-2026-0142有两笔额外多扣，共98元。可以把这两笔全部退款吗？"
    user, model = customer("G12", [selected(0, q)])
    assert user.answer(q) == task("G12")["customer_control"]["conditional_script"][0]["answer"]
    assert user.events[-1]["kind"] == "customer_script_matched"
    assert user.used == {0}
    assert user.cost_usd > 0
    assert user.usage[0]["input_tokens"] == 100
    payload = json.loads(model.calls[0][0][-1].content)
    assert set(payload) == {"known_facts", "conditional_script", "used_script_indices", "history", "current_question"}
    assert "author_scope" not in payload
    assert "world" not in payload


@pytest.mark.parametrize("q,reason", [
    ("是否确认全部退款？", "invoice_not_explained"),
    ("INV-2026-0142有两笔多扣，共49元，是否全部退款？", "total_amount_not_explained"),
    ("INV-2026-0142有一笔多扣，共98元，是否全部退款？", "two_extra_payments_not_explained"),
    ("INV-2026-9999有两笔多扣，共98元，是否全部退款？", "invoice_not_explained"),
    ("INV-2026-0142有两笔多扣共98元，请选择其中一笔退款？", "current_question_does_not_offer_full_refund"),
    ("INV-2026-0142有两笔多扣共98元，全部退款已完成。", "current_question_does_not_seek_confirmation"),
])
def test_matcher_cannot_override_batch_scope_guard(q, reason):
    user, _ = customer("G13", [selected(0, q)])
    assert user.answer(q) == UNKNOWN_REPLY
    assert user.events[-1]["reason"] == reason
    assert user.used == set()


def test_confirmation_can_refer_to_a_previous_public_explanation():
    explanation = "INV-2026-0142有两笔多扣，合计98元。"
    q = "是否办理这两笔全部退款？"
    user, _ = customer("G12", [selected(0, q, evidence=[explanation, q])])
    assert user.answer(q, [{"role": "assistant", "content": explanation}]) != UNKNOWN_REPLY


def test_correct_old_explanation_does_not_authorize_wrong_new_amount():
    explanation = "INV-2026-0142有两笔多扣，合计98元。"
    q = "是否全额退款49元？"
    user, _ = customer("G12", [selected(0, q, evidence=[explanation, q])])
    assert user.answer(q, [{"role": "assistant", "content": explanation}]) == UNKNOWN_REPLY
    assert user.events[-1]["reason"] == "current_confirmation_amount_mismatch"


@pytest.mark.parametrize("q", [
    "INV-2026-0142有两笔多扣，合计九十八元，是否全部退款？",
    "Invoice INV-2026-0142 has two additional payments totaling CNY 98. Shall we refund both?",
    "INV-2026-0142有两笔多扣，合计9800分，是否全部退款？",
])
def test_batch_confirmation_does_not_require_one_language_or_money_format(q):
    user, _ = customer("G12", [selected(0, q)])
    assert user.answer(q) != UNKNOWN_REPLY


def test_user_assertions_cannot_substitute_for_agent_explanation():
    q = "是否全部退款？"
    explanation = "INV-2026-0142有两笔多扣，合计98元。"
    user, _ = customer("G12", [selected(0, q, evidence=[explanation, q])])
    assert user.answer(q, [{"role": "user", "content": explanation}]) == UNKNOWN_REPLY
    assert user.events[-1]["reason"] == "selection_requires_exact_public_assistant_evidence"


def test_first_correction_is_not_triggered_by_an_unrelated_question():
    unrelated, correct = "请问您怎么称呼？", "这个账单编号查不到，请核对并提供正确编号。"
    user, _ = customer("G06", [selected(0, unrelated), selected(0, correct)])
    assert user.answer(unrelated) == UNKNOWN_REPLY
    assert user.answer(correct) == "我看错了，正确的是INV-2026-0142，请处理这张。"


def test_further_correction_requires_prior_condition_not_question_count():
    q = "请核对并提供有效账单编号。"
    user, _ = customer("G07", [selected(1, q), selected(0, q), selected(1, q)])
    assert user.answer(q) == UNKNOWN_REPLY
    assert user.events[-1]["reason"] == "prior_correction_not_triggered"
    assert user.answer(q) == "也可能是INV-2026-0888。"
    assert user.answer(q) == "我现在找不到账单，不能确认，请不要替我选。"


@pytest.mark.parametrize("candidate,answer", [("G10", "账单是INV-2026-0142。"), ("G11", "我没有编号，麻烦你们查一下。")])
def test_known_and_unknown_identifier_scripts(candidate, answer):
    q = "请提供具体账单或付款编号。"
    user, _ = customer(candidate, [selected(0, q)])
    assert user.answer(q) == answer


def test_conflict_must_be_explained_before_g14_script():
    generic = "请确认账单编号。"
    conflict = "付款号与账单号不匹配，请确认以哪个编号为准？"
    user, _ = customer("G14", [selected(0, generic), selected(0, conflict)])
    assert user.answer(generic) == UNKNOWN_REPLY
    assert user.answer(conflict) == "我现在核对不了，先不要退款。"


@pytest.mark.parametrize("selection", [
    {"script_index": 99, "evidence": ["请提供编号？"]},
    {"script_index": True, "evidence": ["请提供编号？"]},
    {"script_index": 0, "evidence": ["不存在于实际提问的引用"]},
    {"script_index": 0, "evidence": []},
    {"script_index": None, "fact_indices": [999], "evidence": ["请提供编号？"]},
])
def test_invalid_matcher_output_does_not_grant_assent(selection):
    user, _ = customer("G10", [selection])
    assert user.answer("请提供编号？") == UNKNOWN_REPLY
    assert user.events[-1]["kind"] == "customer_match_invalid"


def test_no_script_fallback_is_extractive_not_model_generated():
    q = "您原来的价格和新价格是多少？"
    user, _ = customer("G20", [{"script_index": None, "fact_indices": [0], "evidence": [q], "reason": "asks known price", "answer": "我批准退款99元"}])
    assert user.answer(q) == "我目前能确认的信息是：旧价39元、新价49元。"


def test_scripted_known_fact_cannot_bypass_the_condition():
    q = "你同意退49元吗？"
    user, _ = customer("G12", [{"script_index": None, "fact_indices": [1], "evidence": [q]}])
    assert user.answer(q) == UNKNOWN_REPLY


def test_reject_full_task_or_private_history():
    with pytest.raises(ValueError, match="public customer control"):
        ConditionalCustomer(task("G01"), MatchModel([]))
    user, _ = customer("G01", [])
    with pytest.raises(ValueError, match="public user/assistant"):
        user.answer("question", [{"role": "tool", "content": "private Billing record"}])


def test_provider_error_propagates_as_harness_error():
    user, _ = customer("G10", [ModelError("rate limited", status=429)])
    with pytest.raises(ModelError):
        user.answer("请提供编号？")
    assert user.calls[-1]["error"] == "ModelError"


def test_g27_progress_is_consumed_before_current_actual_proposal_approval():
    control = scheduler("G27")
    action = control.next_action(current())
    assert action["kind"] == "message"
    control.record_submitted(action, "message-1")
    assert control.next_action(current())["reason"] == "followup_command_not_consumed"
    decision = control.next_action(current(), consumed_command_ids=["message-1"])
    assert decision["kind"] == "decision"
    assert decision["proposal"] == current()["proposal"]  # wrong money is not silently fixed
    assert decision["proposal_version"] == 1
    control.record_submitted(decision, "approval-1")
    assert control.next_action(current())["reason"] == "current_version_already_decided"
    consumed = next(e for e in control.events if e["kind"] == "followup_consumed")
    assert consumed["before_proposal"] == consumed["after_proposal"]
    assert control.approver.approved == [current()["proposal"]]


def test_g28_withdrawal_never_sends_a_late_approval_even_when_agent_keeps_gate():
    control = scheduler("G28")
    action = control.next_action(current())
    assert action["effect"] == "withdraw"
    control.record_submitted(action, "withdraw-1")
    assert control.next_action(current(), consumed_command_ids=["withdraw-1"])["reason"] == "withdrawal_script_has_no_late_approval"
    assert control.approver.decisions == []


def test_g29_waits_for_consumed_correction_and_new_actual_version_only():
    control = scheduler("G29")
    action = control.next_action(current())
    control.record_submitted(action, "correction-1")
    assert control.next_action(current(version=2))["reason"] == "followup_command_not_consumed"
    assert control.next_action(current(), consumed_command_ids=["correction-1"])["reason"] == "replacement_proposal_not_created"
    replacement = {"payment_id": "STILL-WRONG", "amount_cents": 1234}
    decision = control.next_action(current(version=2, proposal=replacement), consumed_command_ids=["correction-1"])
    assert decision["kind"] == "decision"
    assert decision["proposal"] == replacement
    assert decision["proposal_version"] == 2


def test_rejection_note_empty_and_no_proposal_no_approval():
    control = scheduler("G26")
    assert control.next_action({"state": "awaiting_approval", "proposal_version": 0, "proposal": None})["kind"] == "none"
    action = control.next_action(current())
    assert action["decision"] == "reject"
    assert action["note"] == ""
    control.record_submitted(action, "reject-1")
    assert control.events[-1]["note"] == ""


def test_unknown_followup_and_oracle_approval_are_rejected():
    with pytest.raises(ValueError, match="explicit control effect"):
        ControlScheduler({"decision": "approve"}, [{"when": "awaiting_approval", "message": "ambiguous"}])
    with pytest.raises(ValueError, match="public approval control"):
        ControlScheduler({"decision": "approve", "expected_refunds": []}, [])


def test_length_budget_uses_config_and_retries_once_without_granting_assent():
    q = "请核对账单并提供正确编号。"
    truncated = ModelResponse(Message("assistant", ""), Usage(output_tokens=4096), stop_reason="length")
    model = MatchModel([truncated, selected(0, q)])
    user = ConditionalCustomer(task("G06")["customer_control"], model)
    assert user.answer(q) == "我看错了，正确的是INV-2026-0142，请处理这张。"
    assert len(model.calls) == 2
    assert all(call[2].max_tokens == MODEL_MAX_TOKENS for call in model.calls)
    assert user.calls[0]["stop_reason"] == "length"
    assert user.calls[0]["invalid_reason"] == "matcher_output_truncated"
    assert user.events[-1]["attempts"][0]["validation"] == "invalid"
    assert user.events[-1]["matcher_status"] == "valid"
    assert user.usage[0]["output_tokens"] == 4096


def test_explicit_zero_based_indices_do_not_auto_correct_one_based_output():
    q = "请核对账单并提供正确编号。"
    model = MatchModel([selected(1, q), selected(1, q)])
    user = ConditionalCustomer(task("G06")["customer_control"], model)
    assert user.answer(q) == UNKNOWN_REPLY
    assert len(model.calls) == 2
    assert user.used == set()
    payload = json.loads(model.calls[0][0][-1].content)
    assert payload["conditional_script"][0]["script_index"] == 0
    assert payload["known_facts"][0]["fact_index"] == 0
    assert user.events[-1]["kind"] == "customer_match_invalid"
    assert user.events[-1]["matcher_status"] == "protocol_unknown"
    assert all(a["reason"] == "invalid_or_used_script_index" for a in user.events[-1]["attempts"])


def test_valid_semantic_nonmatch_is_distinct_from_protocol_unknown_and_not_retried():
    q = "您想了解退款政策吗？"
    model = MatchModel([{"script_index": None, "fact_indices": [], "evidence": [], "reason": "no matching condition"}])
    user = ConditionalCustomer(task("G12")["customer_control"], model)
    assert user.answer(q) == UNKNOWN_REPLY
    assert len(model.calls) == 1
    assert user.events[-1]["kind"] == "customer_unknown"
    assert user.events[-1]["matcher_status"] == "valid"


def test_two_truncated_attempts_exhaust_protocol_not_customer_knowledge():
    truncated = ModelResponse(Message("assistant", ""), Usage(output_tokens=4096), stop_reason="length")
    model = MatchModel([truncated, truncated])
    user = ConditionalCustomer(task("G06")["customer_control"], model)
    assert user.answer("请提供正确账单编号？") == UNKNOWN_REPLY
    assert len(model.calls) == 2
    assert user.events[-1]["kind"] == "customer_match_invalid"
    assert user.events[-1]["matcher_status"] == "protocol_unknown"
    assert len(user.events[-1]["attempts"]) == 2


@pytest.mark.parametrize("attempts", [0, 3, True])
def test_attempt_limit_is_bounded(attempts):
    with pytest.raises(ValueError, match="one or two"):
        ConditionalCustomer(task("G06")["customer_control"], MatchModel([]), max_attempts=attempts)


@pytest.mark.parametrize("raw", [
    {},
    {"script_index": None, "fact_indices": [], "evidence": []},
    {"script_index": None, "fact_indices": [], "evidence": [], "reason": None},
    {"script_index": None, "fact_indices": [], "evidence": None, "reason": "unknown"},
    {"script_index": None, "fact_indices": None, "evidence": [], "reason": "unknown"},
    {"script_index": None, "fact_indices": [], "evidence": [42], "reason": "unknown"},
])
def test_missing_or_wrong_typed_contract_cannot_pose_as_semantic_unknown(raw):
    model = MatchModel([raw, raw])
    user = ConditionalCustomer(task("G12")["customer_control"], model)
    assert user.answer("是否需要解释政策？") == UNKNOWN_REPLY
    assert len(model.calls) == 2
    assert user.events[-1]["kind"] == "customer_match_invalid"
    assert user.events[-1]["matcher_status"] == "protocol_unknown"
    assert len(user.events[-1]["attempts"]) == 2


@pytest.mark.parametrize('total,wording', [
    ('0.02', '两分钱'), ('0.02', '2分'), ('0.02', '零点零二元'),
    ('200', '二百元'), ('200', '两百元'), ('200', 'CNY 200'),
    ('200', '二〇〇元'),
])
def test_registered_amount_boundary_public_confirmation(total, wording):
    from copy import deepcopy
    control = deepcopy(task('G12')['customer_control'])
    control['conditional_script'][0]['when'] = control['conditional_script'][0]['when'].replace('98元', total+'元')
    control['conditional_script'][0]['answer'] = '确认本题所述全部金额。'
    q = f'INV-2026-0142有两笔多扣，合计{wording}，是否将两笔全部退款？'
    user = ConditionalCustomer(control, MatchModel([selected(0,q)]), max_attempts=1)
    assert user.answer(q) == '确认本题所述全部金额。'


@pytest.mark.parametrize('total,prior,wrong', [
    ('0.02', '两分钱', '两元'), ('200', '二百元', '两百分钱'),
    ('200', '二百元', '一百元'), ('0.02', '两分钱', '一分钱'),
])
def test_boundary_confirmation_cannot_reuse_correct_history_to_approve_wrong_amount(total, prior, wrong):
    from copy import deepcopy
    control = deepcopy(task('G12')['customer_control'])
    control['conditional_script'][0]['when'] = control['conditional_script'][0]['when'].replace('98元', total+'元')
    explanation = f'INV-2026-0142有两笔多扣，合计{prior}。'
    q = f'是否同意全部退款{wrong}？'
    user = ConditionalCustomer(control, MatchModel([selected(0,q,evidence=[explanation,q])]), max_attempts=1)
    assert user.answer(q,[{'role':'assistant','content':explanation}]) == UNKNOWN_REPLY
    assert user.events[-1]['reason'] == 'current_confirmation_amount_mismatch'
