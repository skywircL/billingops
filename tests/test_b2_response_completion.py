"""Necessary business facts must survive final receipt rendering, without GT."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from billingops.execution.refund import execute_approved
from billingops.investigation.b2_response import complete_reply, response_context
from billingops.investigation.evidence import build_index
from billingops.investigation.graph import _evidence_bundle
from billingops.investigation.prompts import B2_CONCLUDE_SYSTEM, B2_CONCLUDE_INSTRUCTIONS, CONCLUDE_SYSTEM
from test_b1_batch_execution import prepared_batch

ROOT = Path(__file__).resolve().parents[1]


def archived_bundle(trial):
    artifact = json.loads((ROOT / 'runs/dynamic-v2-B1-unified-2/trials' / trial / 'artifact.json').read_text())
    state = artifact['snapshot']
    case = artifact['view']['case']
    draft = case['conclusion']
    bundle = _evidence_bundle(state, build_index(artifact['view']['evidence']), draft['type'], None,
                              {'target_invoice_id': draft.get('target_invoice_id'),
                               'target_payment_id': draft.get('target_payment_id')}, None)
    bundle['customer_request'] = state['text']
    return artifact, bundle


@pytest.mark.parametrize('trial,claim', [('G01-U1-48-t0', '48.00'), ('G01-U1-50-t0', '50.00')])
def test_execution_receipt_retains_actual_archived_amount_discrepancy(runtime, billing, trial, claim):
    _, bundle = archived_bundle(trial)
    context = response_context(bundle)
    assert context['amount_conflict']['recorded_invoice_cents'] == 4900
    case, proposal = prepared_batch(runtime, billing, count=1)
    runtime.execution.agent_revision = 'B2'
    runtime.store.update_case(case['id'], conclusion={'response_context': context})
    result = execute_approved(runtime.execution, case, proposal)
    view = runtime.case_view(case['id'])
    reply = view['case']['final_message']
    assert result['status'] == 'done'
    assert claim in reply and '49.00' in reply and '不一致' in reply
    assert view['execution']['items'][0]['receipt']['refund']['id'] in reply
    assert runtime.effects.total_executions() == 1
    assert 'awaits approval' not in reply


def test_claims_never_become_observed_amounts():
    _, bundle = archived_bundle('G01-U1-48-t0')
    for bad in ('missing_amount', 'different_customer', 'derived_invoice'):
        altered = deepcopy(bundle)
        if bad == 'missing_amount':
            altered['invoice'].pop('amount_cents')
        elif bad == 'different_customer':
            altered['invoice']['customer_id'] = 'other'
        else:
            altered['invoice']['derived_from'] = 'list_payments'
        assert response_context(altered) == {}


@pytest.mark.parametrize('text,amount', [
    ('这张账单多扣98元，请把多扣的钱退回。', 9800),
    ('三笔付款合计147元，请处理重复扣费。', 14700),
    ('历史退过17元，请退剩余部分。', 1700),
    ('Refund the extra 98 yuan from this invoice.', 9800),
])
def test_different_monetary_roles_are_not_fabricated_conflicts(text, amount):
    _, bundle = archived_bundle('G01-U1-48-t0')
    bundle['customer_request'] = text
    bundle['extracted']['amounts_cents'] = [amount]
    assert response_context(bundle) == {}
    assert '不一致' not in complete_reply('已核实账单记录。', bundle)


def test_refund_status_retains_creation_completion_and_amount_separately():
    artifact, bundle = archived_bundle('G15-W1-01-t1')
    reply = complete_reply(artifact['reply'], bundle)
    assert '退款金额 49.00 元' in reply
    assert '申请/创建时间 2026-10-04' in reply
    assert '账务完成时间 2026-10-05' in reply
    assert '不代表银行已到账' in reply
    bundle['refunds'] = None
    reply = complete_reply('无法确认退款记录。', bundle)
    assert '49.00' not in reply and '2026-10-05' not in reply


def test_inquiry_keeps_customer_consent_distinct_from_operator_approval():
    artifact, bundle = archived_bundle('G09-W1-01-t3')
    reply = complete_reply(artifact['reply'], bundle)
    assert '未发起退款' in reply and '您另行确认授权' in reply and '独立审批' in reply
    bundle['proposal'] = {'version': 1}
    assert complete_reply('提案待审批。', bundle) == '提案待审批。'


def test_corrected_not_found_lookup_is_not_erased_or_widened():
    bundle = {'conclusion_type': 'insufficient_information', 'read_observations': [
        {'tool': 'get_invoice', 'args': {'invoice_id': 'INV-OLD'}, 'ok': False, 'error': 'not_found'},
        {'tool': 'get_invoice', 'args': {'invoice_id': 'INV-NEW'}, 'ok': False, 'error': 'not_found'},
        {'tool': 'get_invoice', 'args': {'invoice_id': 'INV-UNKNOWN'}, 'ok': False, 'error': 'unavailable'},
    ]}
    reply = complete_reply('目标仍无法核实。', bundle)
    assert 'INV-OLD' in reply and 'INV-NEW' in reply
    assert 'INV-UNKNOWN' not in reply
    assert '在当前客户范围内' in reply and '不代表不存在多扣款' in reply
    bundle['read_observations'].append({'tool': 'get_invoice', 'args': {'invoice_id': 'INV-NEW'}, 'ok': True, 'data': {'id': 'INV-NEW'}})
    reply = complete_reply('已查到更正后的账单。', bundle)
    assert '未找到账单 INV-NEW' not in reply


def test_new_prompt_removes_unsupported_channel_without_changing_old_pin():
    assert 'or to product support' in CONCLUDE_SYSTEM
    assert 'or to product support' not in B2_CONCLUDE_SYSTEM
    assert '账户设置 → 付款方式' in B2_CONCLUDE_INSTRUCTIONS
