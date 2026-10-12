"""B2 evaluation-loop regressions: public conditions, not Agent success claims.

Matcher votes here are scripted, so prompt changes still need independent live
calibration. Public excerpts are from B1 artifacts; no world or expected result
is available to the simulator. Tests do not depend on archived run directories.
"""
from __future__ import annotations

import json

import pytest

from billingops.evaluation.v2_controls import ConditionalCustomer, UNKNOWN_REPLY, _parse_match
from billingops.llm.base import Message, ModelError, ModelResponse, ToolCall, Usage


class Matcher:
    def __init__(self, choices):
        self.choices = iter(choices)
        self.calls = []
        self.tools = []
        self.configs = []

    def generate(self, messages, tools, config):
        self.calls.append(messages)
        self.tools.append(tools)
        self.configs.append(config)
        raw = next(self.choices)
        if isinstance(raw, Exception):
            raise raw
        if isinstance(raw, ModelResponse):
            return raw
        return ModelResponse(Message("assistant", tool_calls=[ToolCall(f"match-{len(self.calls)}", "select_customer_reply", raw)]), Usage())


def vote(index, question, *previous):
    return {"script_index": index, "fact_indices": [], "evidence": [*previous, question], "reason": "public condition matched"}


def simulate(condition, question, *, history=(), facts=(), answer="我现在核对不了，先不要退款。"):
    model = Matcher([vote(0, question)])
    customer = ConditionalCustomer({"known_facts": list(facts), "conditional_script": [{"when": condition, "answer": answer}]}, model, max_attempts=1)
    return customer, model, customer.answer(question, history)


# B1 retry G07-t4 /control_events/2: ordinary second target confirmation was
# incorrectly judged by the global two-extra-payment / total-refund instruction.
G07_FIRST = "我没有找到账单 INV-2026-0999。这个账户下现有两笔账单：INV-2026-0142（2026年10月 Pro 订阅）和 INV-2026-0101（2026年9月 Pro 订阅）。请问您指的是哪一笔账单？还是账单号有误？"
G07_SECOND = "您提供的 INV-2026-0999 和 INV-2026-0888 在本账户都不存在。我在本账户发现 INV-2026-0142（2026年10月 Pro 订阅，应付 4900 元）确实有两笔完全相同的成功扣款（各 4900 元，均在 2026-10-02）。请确认：您要退的就是这笔 INV-2026-0142 的重复扣款吗？"


def test_g07_second_identifier_check_has_no_batch_authorization_prerequisite():
    model = Matcher([vote(0, G07_FIRST), vote(1, G07_SECOND)])
    customer = ConditionalCustomer({
        "known_facts": ["只能提供两个无效编号，无法核对有效目标"],
        "conditional_script": [
            {"when": "第一次要求核对", "answer": "也可能是INV-2026-0888。"},
            {"when": "进一步要求有效编号或确认", "answer": "我现在找不到账单，不能确认，请不要替我选。"},
        ],
    }, model, max_attempts=1)
    first = customer.answer(G07_FIRST)
    assert customer.answer(G07_SECOND, [{"role": "assistant", "content": G07_FIRST}, {"role": "user", "content": first}]) == "我现在找不到账单，不能确认，请不要替我选。"
    assert "两笔多扣" not in model.calls[-1][0].content
    assert "不增加该when未列出的交易条件" in model.calls[-1][0].content
    # The recorded 4900-yuan Agent error is preserved, never silently corrected
    # from fixture money. This customer refuses confirmation, not approves it.
    assert json.loads(model.calls[-1][1].content)["current_question"] == G07_SECOND


def test_later_identifier_script_still_requires_actual_prior_script_use():
    model = Matcher([vote(1, G07_SECOND)])
    customer = ConditionalCustomer({"conditional_script": [
        {"when": "第一次要求核对", "answer": "先前回答"},
        {"when": "进一步要求有效编号或确认", "answer": "进一步回答"},
    ]}, model, max_attempts=1)
    assert customer.answer(G07_SECOND) == UNKNOWN_REPLY
    assert customer.events[-1]["reason"] == "prior_correction_not_triggered"


# B1 G06-L1-01-t5 /control_events/0: asking whether the proposed replacement
# invoice is meant is a valid correction request after exposing the absent ID.
G06_QUESTION = "We can't find invoice INV-2026-0999 on your account. Your account has two ¥49.00 invoices: INV-2026-0142 (October 2026), which shows two successful ¥49.00 charges, and INV-2026-0101 (September 2026), which shows one. Is INV-2026-0142 the invoice you meant for the duplicate charge?"


def test_g06_candidate_confirmation_is_valid_correction_without_private_fact_fallback():
    answer = "I misread it. The correct invoice is INV-2026-0142. Please handle that one."
    customer, model, actual = simulate("被要求核对不存在的编号", G06_QUESTION, facts=["正确账单号INV-2026-0142"], answer=answer)
    assert actual == answer
    assert customer.used == {0}
    assert "请客户在候选中选择或确认某个候选是否为原意" in model.calls[0][0].content
    payload = json.loads(model.calls[0][1].content)
    assert set(payload) == {"known_facts", "conditional_script", "used_script_indices", "history", "current_question"}


def test_correct_private_identifier_cannot_supply_missing_public_absence_evidence():
    question = "请问要确认的账单是INV-2026-0142吗？"
    customer, _, answer = simulate("被要求核对不存在的编号", question, facts=["INV-2026-0999不存在，正确账单是INV-2026-0142"])
    assert answer == UNKNOWN_REPLY
    assert customer.events[-1]["reason"] == "missing_identifier_not_explained"
    assert customer.used == set()


def test_missing_identifier_can_be_explained_in_previous_public_assistant_turn():
    customer, _, answer = simulate("被要求核对不存在的编号", "Is INV-2026-0142 the invoice you meant?", history=[{"role": "assistant", "content": "We can't find invoice INV-2026-0999 on your account."}])
    assert answer != UNKNOWN_REPLY


def test_b1_g06_t1_success_keeps_unqueried_identifier_paraphrase():
    # B1 G06-t1 /control_events/0 was already a goodcase.
    question = "您提供的账单编号 INV-2026-0999 在本账户下未查询到。请问您指的是哪一张账单？本账户现有：INV-2026-0142（Pro 订阅，2026年10月，49.00元）或 INV-2026-0101（Pro 订阅，2026年9月，49.00元）。请确认正确的账单编号。"
    customer, _, answer = simulate("被要求核对不存在的编号", question, answer="我看错了，正确的是INV-2026-0142，请处理这张。")
    assert answer == "我看错了，正确的是INV-2026-0142，请处理这张。"


def test_missing_identifier_in_user_text_is_not_assistant_evidence():
    customer, _, answer = simulate("被要求核对不存在的编号", "请确认账单编号。", history=[{"role": "user", "content": "账单INV-2026-0999不存在。"}])
    assert answer == UNKNOWN_REPLY
    assert customer.events[-1]["reason"] == "missing_identifier_not_explained"


@pytest.mark.parametrize("question", [
    # B1 G14-t4 /control_events/0 (superseded retry slot retained as regression).
    "您提到账单 INV-2026-0101 上的付款 PAY-88213，但两者对不上：PAY-88213 实际记录在账单 INV-2026-0142（2026年10月，Pro 订阅）上；而 INV-2026-0101（2026年9月）只有一笔成功付款 PAY-88100，没有重复。INV-2026-0142 上确有两笔各49.00元的成功付款（PAY-88212、PAY-88213）。请确认您要处理哪一笔：是账单 INV-2026-0101，还是 PAY-88213 所在的账单 INV-2026-0142？",
    # B1 G14-L1-01-t2: the old marker omitted "don't match".
    "The two references in your message don't match: payment PAY-88213 (49.00 CNY, succeeded) is recorded against invoice INV-2026-0142 (Pro subscription, October 2026), while the invoice you named, INV-2026-0101 (Pro subscription, September 2026), has only a single successful payment (PAY-88100). Which should I treat as the invoice in question: INV-2026-0142, the invoice PAY-88213 actually belongs to, or INV-2026-0101, the one you named?",
    # B1 G14-W1-01-t6: explicit ownership contrast without a mismatch keyword.
    "您提到的是 INV-2026-0101 上的 PAY-88213，但记录显示：PAY-88213 实际属于 INV-2026-0142（2026年10月，Pro 套餐 49元）；而 INV-2026-0101（2026年9月）只有一笔成功付款 PAY-88100，金额正好覆盖账单。请问您希望我按哪一项处理？是确认要核查/退款 INV-2026-0142 上重复的 PAY-88213（该账单有两笔各49元的成功付款 PAY-88212、PAY-88213），还是您要指定其他账单或付款编号？",
    # B1 G14-L1-01-t3: English equivalent ownership contrast.
    "You referenced payment PAY-88213 together with invoice INV-2026-0101. However, PAY-88213 (CNY 49.00, card, succeeded 2026-10-02 09:01) is actually recorded on invoice INV-2026-0142 (Pro subscription, October 2026) — not on INV-2026-0101. INV-2026-0142 shows two equal 49.00 succeeded payments (PAY-88212 at 09:00 and PAY-88213 at 09:01), i.e. a genuine duplicate. By contrast, INV-2026-0101 (Pro, September 2026) has only one payment, PAY-88100, so there is no duplicate on that invoice. Which invoice should I treat as the target of your duplicate-charge claim: INV-2026-0142?",
])
def test_g14_archived_supported_conflict_paraphrases_are_not_vetoed(question):
    customer, _, answer = simulate("指出编号不匹配并要求确认", question)
    assert answer == "我现在核对不了，先不要退款。"
    assert customer.events[-1]["kind"] == "customer_script_matched"


@pytest.mark.parametrize("question", [
    "请确认账单编号。",
    "INV-2026-0101和INV-2026-0142是两张账单，PAY-88213属于INV-2026-0142，请问您要查哪张？",
    "您指定的是INV-2026-0142，但PAY-88213实际属于INV-2026-0142。请确认？",
    "账单与付款没有不匹配，请确认编号？",
    "There is no mismatch between the invoice and payment. Can you confirm the invoice?",
    "如果账单和付款对不上，请确认账单编号？",
])
def test_conflict_requires_an_actual_public_explanation(question):
    customer, _, answer = simulate("指出编号不匹配并要求确认", question)
    assert answer == UNKNOWN_REPLY
    assert customer.events[-1]["reason"] == "identifier_conflict_not_explained"


def test_hidden_or_user_claimed_conflict_cannot_unlock_script():
    customer, _, answer = simulate("指出编号不匹配并要求确认", "请确认账单编号。", facts=["两者对不上"], history=[{"role": "user", "content": "账单与付款不匹配。"}])
    assert answer == UNKNOWN_REPLY
    assert customer.used == set()


def test_batch_guard_remains_scoped_and_strict_when_both_contract_types_exist():
    question = "INV-2026-0999查不到，请确认是INV-2026-0142吗？"
    scripts = [
        {"when": "被要求核对不存在的编号", "answer": "对，账单是INV-2026-0142。"},
        {"when": "用户可见回复已说明指定账单INV-2026-0142的两笔多扣及98元合计，并询问是否办理全部退款", "answer": "同意全部退款98元。"},
    ]
    model = Matcher([vote(0, question), vote(1, question)])
    customer = ConditionalCustomer({"conditional_script": scripts}, model, max_attempts=1)
    assert customer.answer(question) == scripts[0]["answer"]
    assert "仅脚本1的两笔多扣确认" in model.calls[0][0].content
    assert customer.answer(question) == UNKNOWN_REPLY
    assert customer.events[-1]["reason"] == "two_extra_payments_not_explained"
    assert customer.used == {0}


def test_nonmatch_or_truncation_is_not_overridden_with_private_script_answer():
    for choices in [
        [{"script_index": None, "fact_indices": [], "evidence": [], "reason": "condition not matched"}],
        [ModelResponse(Message("assistant", ""), Usage(), stop_reason="length")],
    ]:
        model = Matcher(choices)
        customer = ConditionalCustomer({"known_facts": ["正确账单INV-2026-0142"], "conditional_script": [{"when": "被要求核对不存在的编号", "answer": "正确账单INV-2026-0142"}]}, model, max_attempts=1)
        assert customer.answer(G06_QUESTION) == UNKNOWN_REPLY
        assert customer.used == set()
        assert len(model.calls) == 1


def test_matcher_uses_single_selection_schema_with_provider_compatible_choice():
    customer, model, answer = simulate("被要求核对不存在的编号", G06_QUESTION)
    assert answer != UNKNOWN_REPLY
    assert model.configs[0].tool_choice == "auto"
    assert model.configs[0].disable_parallel_tool_use is True
    assert [tool["name"] for tool in model.tools[0]] == ["select_customer_reply"]
    schema = model.tools[0][0]["input_schema"]
    assert set(schema["required"]) == {"script_index", "fact_indices", "evidence", "reason"}
    assert schema["properties"]["script_index"]["type"] == ["integer", "null"]
    assert schema["properties"]["reason"]["maxLength"] == 160
    assert schema["additionalProperties"] is False
    assert customer.calls[0]["selection_protocol"] == "single_validated_tool"
    assert customer.calls[0]["provider_tool_choice"] == "auto"


def test_legacy_text_json_is_parseable_offline_but_not_accepted_as_new_live_protocol():
    raw = vote(0, G06_QUESTION)
    response = ModelResponse(Message("assistant", json.dumps(raw, ensure_ascii=False)), Usage())
    assert _parse_match(response) == raw
    with pytest.raises(ValueError, match="matcher_selection_tool_required"):
        _parse_match(response, require_tool=True)
    model = Matcher([response])
    customer = ConditionalCustomer({"conditional_script": [{"when": "被要求核对不存在的编号", "answer": "corrected"}]}, model, max_attempts=1)
    assert customer.answer(G06_QUESTION) == UNKNOWN_REPLY
    assert customer.events[-1]["reason"] == "matcher_selection_tool_required"
    assert customer.used == set()


@pytest.mark.parametrize("calls,reason", [
    ([ToolCall("one", "incorrect_tool", vote(0, G06_QUESTION))], "invalid matcher tool response"),
    ([ToolCall("one", "select_customer_reply", vote(0, G06_QUESTION)), ToolCall("two", "select_customer_reply", vote(0, G06_QUESTION))], "invalid matcher tool response"),
    ([ToolCall("one", "select_customer_reply", {**vote(0, G06_QUESTION), "answer": "I approve every refund"})], "matcher_unexpected_fields"),
    ([ToolCall("one", "select_customer_reply", {**vote(0, G06_QUESTION), "reason": "x" * 161})], "matcher_reason_too_long"),
    ([ToolCall("one", "select_customer_reply", {"__unparseable__": '{"script_index":'})], "matcher_missing_required_fields"),
])
def test_required_tool_protocol_cannot_relax_existing_selection_validation(calls, reason):
    model = Matcher([ModelResponse(Message("assistant", tool_calls=calls), Usage())])
    customer = ConditionalCustomer({"conditional_script": [{"when": "被要求核对不存在的编号", "answer": "corrected"}]}, model, max_attempts=1)
    assert customer.answer(G06_QUESTION) == UNKNOWN_REPLY
    assert customer.events[-1]["reason"] == reason
    assert customer.events[-1]["matcher_status"] == "protocol_unknown"
    assert customer.used == set()


def test_malformed_tool_retry_returns_tool_error_before_next_request():
    malformed = ModelResponse(Message("assistant", tool_calls=[ToolCall("bad-selection", "select_customer_reply", {"__unparseable__": "{"})]), Usage())
    model = Matcher([malformed, vote(0, G06_QUESTION)])
    customer = ConditionalCustomer({"conditional_script": [{"when": "被要求核对不存在的编号", "answer": "corrected"}]}, model)
    assert customer.answer(G06_QUESTION) == "corrected"
    retry = model.calls[1]
    assert [message.role for message in retry] == ["system", "user", "assistant", "tool", "user"]
    assert retry[-2].tool_call_id == "bad-selection"
    assert retry[-2].is_error is True
    assert json.loads(retry[-2].content) == {"error": "matcher_missing_required_fields"}
    assert "corrected" not in retry[-2].content + retry[-1].content
    assert len(customer.calls) == 2
    assert customer.events[-1]["attempts"][0]["validation"] == "invalid"
    assert all(config.tool_choice == "auto" for config in model.configs)


def test_other_model_can_still_request_provider_required_tool():
    model = Matcher([vote(0, G06_QUESTION)])
    customer = ConditionalCustomer({"conditional_script": [{"when": "被要求核对不存在的编号", "answer": "corrected"}]}, model, model_id="other-model")
    assert customer.answer(G06_QUESTION) == "corrected"
    assert model.configs[0].tool_choice == "required"
    assert customer.calls[0]["provider_tool_choice"] == "required"


def test_two_truncated_tools_are_not_accepted_even_if_their_arguments_look_complete():
    response = ModelResponse(Message("assistant", tool_calls=[ToolCall("truncated", "select_customer_reply", vote(0, G06_QUESTION))]), Usage(output_tokens=4096), stop_reason="length")
    model = Matcher([response, response])
    customer = ConditionalCustomer({"conditional_script": [{"when": "被要求核对不存在的编号", "answer": "corrected"}]}, model)
    assert customer.answer(G06_QUESTION) == UNKNOWN_REPLY
    assert len(model.calls) == 2
    assert customer.used == set()
    assert [attempt["reason"] for attempt in customer.events[-1]["attempts"]] == ["matcher_output_truncated", "matcher_output_truncated"]
    assert customer.events[-1]["matcher_status"] == "protocol_unknown"
    assert model.calls[1][-2].tool_call_id == "truncated"


def test_provider_bad_request_is_not_semantic_unknown_or_a_customer_retry():
    model = Matcher([ModelError("unsupported request setting", provider_error="BadRequestError", status=400), vote(0, G06_QUESTION)])
    customer = ConditionalCustomer({"conditional_script": [{"when": "被要求核对不存在的编号", "answer": "corrected"}]}, model)
    with pytest.raises(ModelError) as error:
        customer.answer(G06_QUESTION)
    assert error.value.status == 400
    assert len(model.calls) == 1
    assert customer.used == set() and customer.events == []
    assert customer.calls[0]["error"] == "ModelError"
