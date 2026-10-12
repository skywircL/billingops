"""B2 protocol regressions use injected failures, not model-scored answers.

These verify repair boundaries and honest failure handling; no real-model
success rate is inferred from them.
"""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from billingops.config import MODEL_MAX_TOKENS, Settings
from billingops.investigation.b2_protocol import extraction_error, question_amount_error, schema_error
from billingops.llm.base import Message, ModelConfig, ModelResponse, ToolCall, Usage
from billingops.llm.capabilities import compatible_tool_choice, provider_capability_profile
from billingops.llm.openai_compat import OpenAICompatibleModel
from billingops.llm.replay import normalise, request_key
from billingops.runtime import CaseRuntime
from billingops.testing import duplicate_world


def fields(**overrides):
    return dict(invoice_ids=['INV-2026-0142'], payment_ids=[], amounts_cents=[4900],
                mentions_duplicate=True, mentions_refund_status=False, mentions_subscription_price=False,
                requests_price_adjustment=False, requests_other_account=False, asks_refund_policy=False,
                requests_out_of_scope=False, summary='Investigate and refund an extra charge',
                request_action='refund', payment_reference_role='unspecified', scope_month=None, scope_plan=None,
                price_adjustment_kind='none', price_adjustment_basis=None, **overrides)


def end(kind='duplicate_charge_refund'):
    return ('end_investigation', dict(conclusion_type=kind, target_invoice_id='INV-2026-0142',
                                     target_payment_id='PAY-88213', summary='Observed duplicate charge', facts=[]))


def reads():
    return [('get_invoice', {'invoice_id': 'INV-2026-0142'}),
            ('list_payments', {'invoice_id': 'INV-2026-0142'}), ('list_customer_refunds', {})]


class Sequence:
    def __init__(self, calls, extractions=None):
        self.calls = iter(calls)
        self.extractions = iter(extractions or [fields()])
        self.seen = []
        self.conclusion_text = '已核对记录，本次仍需人工处理。'

    def generate(self, messages, schemas, config):
        self.seen.append((config, deepcopy(messages)))
        if config.tag == 'conclude':
            return ModelResponse(Message('assistant', self.conclusion_text), Usage())
        item = next(self.extractions) if config.tag == 'extract' else next(self.calls)
        if isinstance(item, ModelResponse):
            return item
        name, args = ('submit_fields', item) if config.tag == 'extract' else item
        return ModelResponse(Message('assistant', tool_calls=[ToolCall(str(len(self.seen)), name, args)]), Usage(10, 10))


@pytest.fixture
def make(tmp_path, billing):
    runtimes = []
    def create(calls, extractions=None, revision='B2'):
        model = Sequence(calls, extractions)
        rt = CaseRuntime(settings=Settings(app_db=str(tmp_path / f'app{len(runtimes)}.db'),
                         mode='dynamic', agent_revision=revision), model=model, billing=billing)
        runtimes.append(rt)
        return rt, model
    yield create
    for rt in runtimes:
        rt.close()


def start(rt, text='INV-2026-0142扣了两次49元，请退多扣的钱。'):
    cid = rt.submit_case(customer_id='C-1001', created_by='cust-c1001', text=text)['id']
    rt.drain()
    return cid, rt.case_view(cid)


@pytest.mark.parametrize('bad', [
    {'__unparseable__': '{"conclusion_type":"duplicate_charge_refund","target_payment_id":PAY-88213}'},
    {'conclusion_type': 'duplicate_charge_refund'},
    {**end()[1], 'facts': 'not an array'},
    {**end()[1], 'conclusion_type': 'invented_success'},
])
def test_invalid_end_is_repaired_without_becoming_business_insufficiency(make, billing, bad):
    rt, model = make([*reads(), ('end_investigation', bad), end()])
    cid, view = start(rt)
    assert view['case']['state'] == 'awaiting_approval'
    assert view['case']['proposal']['amount_cents'] == 4900
    assert view['budgets']['repairs'] == 1
    assert view['protocol_errors'][0]['reason'] == 'invalid_tool_arguments'
    assert billing.customer_refunds('C-1001') == []
    recovery = [c for c, _ in model.seen if c.tag == 'investigate' and c.max_tokens == min(MODEL_MAX_TOKENS * 2, 8192)]
    assert len(recovery) == 1 and recovery[0].max_tokens == min(MODEL_MAX_TOKENS * 2, 8192)
    assert recovery[0].tool_choice == compatible_tool_choice(recovery[0].model, 'required')
    assert not any(e['kind'] == 'concluded' and e['payload'].get('type') == 'insufficient_information'
                   for e in rt.store.events(cid))


def test_permanent_invalid_protocol_preserves_reads_and_explains_escalation(make, billing):
    bad = ('end_investigation', {'__unparseable__': '{bad JSON'})
    rt, _ = make([reads()[0], bad, bad, bad])
    _, view = start(rt)
    assert view['case']['state'] == 'escalated'
    assert view['case']['escalation']['reason'] == 'repair_budget_exhausted'
    assert view['case']['conclusion']['type'] == 'escalate'
    assert '系统处理异常' in view['case']['final_message']
    assert '没有可退金额' in view['case']['final_message']
    assert len(view['evidence']) == 1 and not view['case']['proposal']
    assert billing.customer_refunds('C-1001') == []


def test_repaired_multi_refund_still_requires_customer_scope_and_operator_approval(make, billing):
    world = duplicate_world()
    third = deepcopy(next(p for p in world['payments'] if p['id'] == 'PAY-88213'))
    third.update(id='PAY-88214', paid_at='2026-02-02T09:01:00')
    world['payments'].append(third)
    billing.load_world(world)
    rt, _ = make([*reads(), ('end_investigation', {'__unparseable__': '{bad JSON'}), end(), end()])
    cid, view = start(rt, 'INV-2026-0142扣了三次49元，请退多扣的钱。')
    assert view['case']['state'] == 'awaiting_user'
    assert '98.00' in view['case']['pending_question'] and not view['case']['proposal']
    assert billing.customer_refunds('C-1001') == []
    rt.submit_message(cid, text='确认全部退款。', actor='cust-c1001')
    rt.drain()
    view = rt.case_view(cid)
    assert view['case']['state'] == 'awaiting_approval'
    assert view['case']['proposal']['amount_cents'] == 9800
    assert len(view['case']['proposal']['items']) == 2
    assert billing.customer_refunds('C-1001') == []


def test_truncated_tool_response_executes_nothing_and_requests_recovery(make):
    truncated = ModelResponse(Message('assistant', tool_calls=[ToolCall('partial', 'get_payment',
                              {'payment_id': 'PAY-88213'})]), Usage(10, 4096), stop_reason='length')
    rt, model = make([truncated, *reads(), end()])
    _, view = start(rt)
    assert view['case']['state'] == 'awaiting_approval'
    assert view['protocol_errors'][0]['reason'] == 'output_truncated'
    assert not any(e['tool'] == 'get_payment' for e in view['evidence'])
    configs = [c for c, _ in model.seen if c.tag == 'investigate']
    assert configs[1].tool_choice == compatible_tool_choice(configs[1].model, 'required')
    assert configs[1].max_tokens == min(MODEL_MAX_TOKENS * 2, 8192) and configs[2].tool_choice == 'auto'


def test_repeated_no_tool_output_has_nonempty_failure_reply(make, billing):
    prose = ModelResponse(Message('assistant', 'Thinking, no tool yet'), Usage())
    rt, _ = make([prose, prose, prose])
    _, view = start(rt)
    assert view['case']['state'] == 'escalated' and view['case']['final_message']
    assert not view['case']['proposal'] and billing.customer_refunds('C-1001') == []


def test_unsupported_price_label_requires_reextraction_not_automatic_authorization(make):
    text = '这个月Pro套餐好像多扣了我49元，请查明白，再把多扣的钱退给我。'
    bad = fields()
    bad.update(mentions_duplicate=False, mentions_subscription_price=True, requests_price_adjustment=True,
               price_adjustment_kind='price_increase_credit', price_adjustment_basis='把多扣的钱退给我')
    rt, model = make([*reads(), end()], extractions=[bad, fields()])
    _, view = start(rt, text='INV-2026-0142：' + text)
    assert len([c for c, _ in model.seen if c.tag == 'extract']) == 2
    assert view['case']['state'] == 'awaiting_approval'
    assert rt._snapshot(view['case']['id'])['extracted']['requests_price_adjustment'] is False


def test_real_price_dispute_not_overridden_by_an_incidental_duplicate(make, billing):
    text = 'INV-2026-0142套餐涨价了，我要撤销涨价并退回涨价部分。'
    extracted = fields()
    extracted.update(mentions_duplicate=False, mentions_subscription_price=True, requests_price_adjustment=True,
                     price_adjustment_kind='price_increase_credit', price_adjustment_basis='撤销涨价并退回涨价部分')
    rt, _ = make([*reads(), end()], extractions=[extracted])
    _, view = start(rt, text)
    assert view['case']['state'] == 'escalated'
    assert not view['case']['proposal'] and billing.customer_refunds('C-1001') == []


def test_truncated_extraction_retries_before_regex_fallback(make):
    empty = ModelResponse(Message('assistant', ''), Usage(10, 4096), stop_reason='length')
    rt, model = make([*reads(), end()], extractions=[empty, fields()])
    _, view = start(rt)
    assert view['case']['state'] == 'awaiting_approval'
    extract_calls = [c for c, _ in model.seen if c.tag == 'extract']
    assert len(extract_calls) == 2 and extract_calls[1].tool_choice == compatible_tool_choice(extract_calls[1].model, 'required')
    assert not rt._snapshot(view['case']['id'])['extraction_fallback']


def test_invalid_read_and_empty_question_do_not_reach_tools_or_customer(make):
    rt, _ = make([('get_payment', {'payment_id': None}), ('ask_customer', {'question': ''}), *reads(), end()])
    _, view = start(rt)
    assert view['case']['state'] == 'awaiting_approval'
    assert view['budgets']['clarifications'] == 0
    assert all(e['tool'] != 'get_payment' for e in view['evidence'])


def test_known_invoice_cents_used_as_yuan_is_repaired_before_public_question(make):
    rt, _ = make([reads()[0], ('ask_customer', {'question': '查到INV-2026-0142应付4900元，请确认这张账单？'}),
                  *reads()[1:], end()])
    _, view = start(rt)
    assert view['case']['state'] == 'awaiting_approval'
    assert view['budgets']['clarifications'] == 0
    assert view['protocol_errors'][0]['reason'] == 'currency_unit_mismatch'


def test_amount_check_does_not_turn_customer_claims_or_other_records_into_facts():
    observed = {'INV-2026-0142': {'amount_cents': 4900, 'currency': 'CNY'}}
    for question in ['INV-2026-0142应付49元，请确认？', '您说INV-2026-0142应付4900元，是否记错了？',
                     'INV-2026-0999应付4900元，请确认？']:
        assert question_amount_error(question, observed) is None


def test_schema_rejects_bool_as_integer_and_unparseable_extraction():
    assert schema_error(True, {'type': 'integer'})
    assert extraction_error({'__unparseable__': '{}'}, 'any message')


def test_required_choice_is_sent_and_distinct_from_archived_replay_key():
    seen = []
    def create(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[], content='ok'), finish_reason='stop')],
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1), model='test')
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    model = OpenAICompatibleModel(client=client)
    config = ModelConfig(tag='investigate', model='test')
    required = replace(config, tool_choice='required')
    tools = [{'name': 'list_invoices', 'input_schema': {'type': 'object', 'properties': {}}}]
    model.generate([], tools, required)
    assert seen[0]['tool_choice'] == 'required'
    assert 'tool_choice' not in normalise([], tools, config)[0]['config']
    assert request_key([], tools, config) != request_key([], tools, required)


@pytest.mark.parametrize('text, expected', [
    ('帮我更换下个月扣款的银行卡。', '账户设置 → 付款方式'),
    ('Please change the payment method for next month.', 'Account settings → Payment methods'),
    ('请帮我设计一张产品海报。', '功能范围'),
])
def test_empty_conclusion_does_not_invent_a_human_support_channel(make, text, expected):
    extracted = fields()
    extracted.update(mentions_duplicate=False, requests_out_of_scope=True, request_action='unknown')
    rt, model = make([end('explain_out_of_scope')], extractions=[extracted])
    model.conclusion_text = ''
    _, view = start(rt, text)
    reply = view['case']['final_message']
    assert expected in reply
    assert 'product support' not in reply and '联系产品支持' not in reply
    assert not view['case']['proposal']


@pytest.mark.parametrize('model_id', ['deepseek-flash', 'deepseek-v4-pro'])
def test_deepseek_thinking_capability_uses_actual_auto_without_disabling_thinking(model_id):
    wire = []
    def create(**kwargs):
        wire.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[], content='ok'), finish_reason='stop')],
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1), model=model_id)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    config = ModelConfig(tag='investigate', model=model_id, tool_choice=compatible_tool_choice(model_id, 'required'))
    OpenAICompatibleModel(client=client).generate([], [{'name': 'inspect', 'input_schema': {'type': 'object'}}], config)
    assert config.tool_choice == wire[0]['tool_choice'] == 'auto'
    assert 'extra_body' not in wire[0] and 'thinking' not in wire[0]
    profile = provider_capability_profile(model_id)
    assert profile['required_tool_choice_supported'] is False
    assert profile['thinking'] == 'provider_default_enabled'
    assert compatible_tool_choice('other-model', 'required') == 'required'
