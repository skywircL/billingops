"""B1 regressions exercise the real graph pauses/guards; model choices are scripted.

These tests prove integration and safety boundaries, not DeepSeek success rates.
"""
from copy import deepcopy

import pytest

from billingops.config import Settings
from billingops.runtime import CaseRuntime
from billingops.llm.base import Message, ModelResponse, ToolCall, Usage
from billingops.investigation.b1 import confirmation_answer
from billingops.testing import duplicate_world


class SequenceModel:
    def __init__(self, calls, **intent):
        self.calls = iter(calls)
        self.intent = {'mentions_duplicate': True, 'request_action': 'refund', 'payment_reference_role': 'locator', **intent}
        self.n = 0

    def generate(self, messages, schemas, config):
        self.n += 1
        if config.tag == 'extract':
            name, args = 'submit_fields', self.intent
        elif config.tag == 'conclude':
            return ModelResponse(Message('assistant', '已核实所查询记录；退款提案仍需审批。'), Usage())
        elif config.tag == 'followup':
            name, args = 'classify_followup', {'kind': 'withdraw', 'reason': 'Customer cancels'}
        else:
            name, args = next(self.calls)
        return ModelResponse(Message('assistant', tool_calls=[ToolCall(str(self.n), name, args)]), Usage())


def end(kind='duplicate_charge_refund', invoice='INV-2026-0142', payment='PAY-88213'):
    return ('end_investigation', {'conclusion_type': kind, 'target_invoice_id': invoice, 'target_payment_id': payment, 'summary': '', 'facts': []})


def reads():
    return [('get_invoice', {'invoice_id': 'INV-2026-0142'}), ('list_payments', {'invoice_id': 'INV-2026-0142'}), ('list_customer_refunds', {})]


@pytest.fixture
def make_b1(tmp_path, billing):
    runtimes = []
    def make(calls, **intent):
        rt = CaseRuntime(settings=Settings(app_db=str(tmp_path/f'app{len(runtimes)}.db'), agent_revision='B1', mode='dynamic'), model=SequenceModel(calls, **intent), billing=billing)
        runtimes.append(rt)
        return rt
    yield make
    for rt in runtimes:
        rt.close()


def start(rt, text):
    cid = rt.submit_case(customer_id='C-1001', created_by='cust-c1001', text=text)['id']
    rt.drain()
    return cid, rt.case_view(cid)


def approve(rt, cid):
    case = rt.store.get_case(cid)
    rt.submit_decision(cid, decision='approve', approver='ops-dana', note='checked full scope', proposal_version=case['proposal_version'], token=None)
    rt.drain()
    return rt.case_view(cid)


def test_receipt_is_locator_but_approval_still_required(make_b1, billing):
    rt = make_b1([('get_payment', {'payment_id': 'PAY-88212'}), *reads(), end()])
    cid, view = start(rt, 'PAY-88212是收据编号，这张账单扣了两次，请退多扣的钱。')
    assert view['case']['state'] == 'awaiting_approval'
    assert billing.customer_refunds('C-1001') == []
    assert view['case']['proposal']['items'][0]['payment_id'] == 'PAY-88213'
    assert approve(rt, cid)['case']['state'] == 'closed'
    assert len(billing.customer_refunds('C-1001')) == 1


def test_inquiry_cannot_become_refund_even_if_model_proposes(make_b1, billing):
    rt = make_b1([*reads(), end()], request_action='inquire')
    _, view = start(rt, '只核查INV-2026-0142是否重复扣款，不要退款。')
    assert view['case']['state'] == 'closed'
    assert view['case']['conclusion']['type'] == 'inquiry_completed'
    assert not view['case']['proposal']
    assert billing.customer_refunds('C-1001') == []


def test_invalid_id_enters_real_customer_pause_and_corrects(make_b1):
    rt = make_b1([('get_invoice', {'invoice_id': 'INV-2026-0999'}), end('insufficient_information', 'INV-2026-0999', None), *reads(), end()])
    cid, view = start(rt, 'INV-2026-0999扣了两次，请退多扣部分。')
    assert view['case']['state'] == 'awaiting_user'
    rt.submit_message(cid, text='我看错了，正确的是INV-2026-0142。', actor='cust-c1001')
    rt.drain()
    assert rt.case_view(cid)['case']['state'] == 'awaiting_approval'
    assert any(e['kind'] == 'clarification_asked' for e in rt.store.events(cid))


def test_foreign_account_request_refuses_without_invented_handoff(make_b1):
    rt = make_b1([end('escalate', None, None)], requests_other_account=True)
    _, view = start(rt, '查询别的客户C-2002的付款。')
    assert view['case']['state'] == 'closed'
    assert view['case']['conclusion']['type'] == 'permission_refused'
    assert not view['case'].get('escalation')


def test_whole_invoice_evidence_missing_cannot_execute(make_b1, billing):
    rt = make_b1([*reads()[:2], ('list_refunds', {'payment_id': 'PAY-88213'}), end(), end('insufficient_information')])
    _, view = start(rt, '请退INV-2026-0142的多扣款。')
    assert not view['case']['proposal']
    assert billing.customer_refunds('C-1001') == []


def three_payments(billing):
    world = duplicate_world()
    third = deepcopy(next(p for p in world['payments'] if p['id'] == 'PAY-88213'))
    third.update(id='PAY-88214', paid_at='2026-02-02T09:01:00')
    world['payments'].append(third)
    billing.load_world(world)


def test_batch_requires_actual_customer_command_then_one_bound_approval(make_b1, billing):
    three_payments(billing)
    rt = make_b1([*reads(), end(), end()])
    cid, view = start(rt, 'INV-2026-0142扣了三次，请退多扣的钱。')
    assert view['case']['state'] == 'awaiting_user'
    assert '98.00' in view['case']['pending_question']
    assert not view['case']['proposal']
    cmd = rt.submit_message(cid, text='确认，两笔多扣的钱全部退回。', actor='cust-c1001')
    rt.drain()
    proposal = rt.case_view(cid)['case']['proposal']
    assert proposal['amount_cents'] == 9800 and len(proposal['items']) == 2
    assert proposal['target_resolution']['customer_confirmation']['command_id'] == cmd
    view = approve(rt, cid)
    assert view['case']['state'] == 'closed'
    assert len(billing.customer_refunds('C-1001')) == 2
    assert all(x['status'] == 'done' for x in view['execution']['items'])


def test_batch_customer_decline_never_requests_operator_approval(make_b1, billing):
    three_payments(billing)
    rt = make_b1([*reads(), end(), end()])
    cid, _ = start(rt, '请处理INV-2026-0142多扣的钱。')
    rt.submit_message(cid, text='不同意，先不要退款。', actor='cust-c1001')
    rt.drain()
    assert not rt.case_view(cid)['case']['proposal']
    assert billing.customer_refunds('C-1001') == []
    assert not any(e['kind'] == 'approval_requested' for e in rt.store.events(cid))


@pytest.mark.parametrize('answer', ['确认只退一笔', 'Yes, but only one', 'not approved', '改成另一张账单', '同意除了第一笔', '确认金额改为1元'])
def test_modified_scope_is_not_confirmation(answer):
    assert confirmation_answer(answer) != 'confirm'


def test_customer_hold_overrides_wrong_extraction_label(make_b1, billing):
    rt = make_b1([*reads(), end()], request_action='refund')
    _, view = start(rt, '只核查INV-2026-0142是否多扣，先不要退款。')
    assert view['case']['conclusion']['type'] == 'inquiry_completed'
    assert not view['case']['proposal']
    assert billing.customer_refunds('C-1001') == []


def test_batch_withdrawal_after_confirmation_invalidates_operator_gate(make_b1, billing):
    from billingops.runtime import StateConflict
    three_payments(billing)
    rt = make_b1([*reads(), end(), end()])
    cid, _ = start(rt, '请退INV-2026-0142的多扣款。')
    rt.submit_message(cid, text='确认全部退款。', actor='cust-c1001')
    rt.drain()
    old = deepcopy(rt.store.get_case(cid))
    rt.submit_message(cid, text='取消退款，暂时不要处理。', actor='cust-c1001')
    rt.drain()
    assert rt.store.get_case(cid)['state'] == 'closed'
    with pytest.raises(StateConflict):
        rt.submit_decision(cid, decision='approve', approver='ops-dana', note='stale', proposal_version=old['proposal_version'], token=old['gate_token'])
    assert billing.customer_refunds('C-1001') == []


def test_new_external_refund_after_batch_approval_request_invalidates_amount(make_b1, billing):
    from tests.test_b1_invoice_cap import request
    three_payments(billing)
    rt = make_b1([*reads(), end(), end()])
    cid, _ = start(rt, '请退INV-2026-0142的多扣款。')
    rt.submit_message(cid, text='确认全部退款。', actor='cust-c1001')
    rt.drain()
    assert billing.post_refund(request('PAY-88213', 'external-concurrent-case')).status == 201
    view = approve(rt, cid)
    assert view['case']['state'] == 'closed'
    assert view['case']['conclusion']['type'] == 'insufficient_information'
    assert len(billing.customer_refunds('C-1001')) == 1
    assert not any(e['kind'] == 'refund_sending' for e in rt.store.events(cid))


def test_generated_batch_question_meets_public_customer_confirmation_contract():
    from billingops.investigation.b1 import confirmation_scope, confirmation_question
    from billingops.evaluation.v2_controls import _condition_guard
    scope = confirmation_scope('INV-CHECK-1', [{'payment_id': 'PAY-CHECK-2', 'amount_cents': 4900, 'currency': 'CNY'}, {'payment_id': 'PAY-CHECK-3', 'amount_cents': 4900, 'currency': 'CNY'}])
    question = confirmation_question(scope)
    assert _condition_guard('说明INV-CHECK-1有两笔多扣，98元合计，并询问是否全部退款', question, question, set(), 0) is None


def test_model_omitting_redundant_target_still_uses_customer_verified_invoice(make_b1):
    rt = make_b1([*reads(), end(invoice=None, payment=None)])
    _, view = start(rt, '请退INV-2026-0142的多扣款。')
    assert view['case']['state'] == 'awaiting_approval'
    assert view['case']['proposal']['invoice_id'] == 'INV-2026-0142'


def test_explicit_two_payment_request_cannot_be_truncated_by_scalar_model_hint(make_b1, billing):
    three_payments(billing)
    rt = make_b1([*reads(), end(), end()], payment_reference_role='exclusive')
    cid, view = start(rt, '只退INV-2026-0142的PAY-88213和PAY-88214这两笔多扣款，其他付款不要动。')
    assert view['case']['state'] == 'awaiting_user'
    rt.submit_message(cid, text='确认全部退款。', actor='cust-c1001')
    rt.drain()
    proposal = rt.case_view(cid)['case']['proposal']
    assert {x['payment_id'] for x in proposal['items']} == {'PAY-88213', 'PAY-88214'}
