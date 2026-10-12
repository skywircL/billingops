"""Customer-derived scope checks and real graph pauses, with scripted models.

Assertions come from the stated customer request, not the parser's own output.
These are deterministic regressions, not claims about live-model performance.
"""
from copy import deepcopy

import pytest

from billingops.investigation.authorization import authorizes_items, resolve_customer_authorization
from billingops.investigation.b1 import assess_refund, confirmation_is_authorized
from billingops.runtime import StateConflict
from test_b1_money_target_proposal import index, state
from test_b1_graph import three_payments, approve
from test_b2_agent_protocol import make, fields, reads, end, start


@pytest.mark.parametrize('text,target', [
    ('PAY-2和PAY-3都重复了，但只退PAY-2。', 'PAY-2'),
    ('PAY-2不要退，只退PAY-3。', 'PAY-3'),
    ('不要退PAY-2，请退PAY-3。', 'PAY-3'),
    ('PAY-2不要退款，只退PAY-3。', 'PAY-3'),
    ('PAY-2 and PAY-3 are duplicates, but only refund PAY-2.', 'PAY-2'),
    ("Don't refund PAY-2, only refund PAY-3.", 'PAY-3'),
    ('Do not refund PAY-2 and refund PAY-3.', 'PAY-3'),
    ('只退PAY-2，其他付款不要动。', 'PAY-2'),
    ('请退PAY-2。', 'PAY-2'),
    ('这次只退PAY-2，PAY-3不要动。', 'PAY-2'),
])
@pytest.mark.parametrize('role', ['locator', 'exclusive', 'unspecified'])
def test_background_or_negative_ids_never_become_authorized_targets(text, target, role):
    resolution, verdict = assess_refund(state(text, role=role), index(3), {'target_invoice_id': 'INV-1', 'target_payment_id': 'PAY-3'})
    assert resolution['ok']
    assert resolution['requested_payment_ids'] == [target]
    assert [item.payment_id for item in verdict.items] == [target]
    assert verdict.amount_cents == 4900


@pytest.mark.parametrize('text', [
    'INV-1两笔重复里只退一笔。',
    'INV-1: refund only one of the two duplicate payments.',
    'INV-1: only one, please.',
    'PAY-2和PAY-3两笔只退其中一笔。',
    'INV-1最多退一笔，PAY-2和PAY-3都重复了。',
    'INV-1就退一笔。',
])
@pytest.mark.parametrize('answer', ['yes', '确认', '确认全部退款。'])
def test_unselected_single_payment_limit_survives_ordinary_confirmation(text, answer):
    s = state(text, role='locator')
    s['clarifications'] = [{'answer': answer, 'command_id': 'CMD-YES'}]
    resolution, verdict = assess_refund(s, index(3), {'target_invoice_id': 'INV-1'})
    assert not resolution['ok']
    assert resolution['authorization_scope']['max_payment_count'] == 1
    assert not verdict.eligible
    assert not confirmation_is_authorized(s, {'items': [{'payment_id': 'PAY-2'}, {'payment_id': 'PAY-3'}]})


def test_explicit_selection_resolves_count_without_implicitly_expanding_it():
    rows = [{'text': 'INV-1只退其中一笔。'}, {'text': 'PAY-3', 'command_id': 'CMD-SELECT'}]
    scope = resolve_customer_authorization(rows, 'locator')
    assert scope['requested_payment_ids'] == ['PAY-3']
    assert authorizes_items(scope, [{'payment_id': 'PAY-3'}])
    assert not authorizes_items(scope, [{'payment_id': 'PAY-2'}, {'payment_id': 'PAY-3'}])
    for answer in ['确认退PAY-2和PAY-3', 'Yes, refund PAY-2 and PAY-3']:
        assert not authorizes_items(resolve_customer_authorization(rows + [{'text': answer}]), [{'payment_id': 'PAY-2'}, {'payment_id': 'PAY-3'}])


def test_unresolved_expansion_preserves_the_earlier_selection_and_exclusion():
    rows = [{'text': '只退PAY-2，不退PAY-3。'}, {'text': '退款PAY-2和PAY-3。'}, {'text': '确认退款PAY-2和PAY-3。'}, {'text': 'yes'}]
    scope = resolve_customer_authorization(rows, 'locator')
    assert scope['resolution_status'] == 'needs_clarification'
    assert scope['retained_payment_ids'] == ['PAY-2']
    assert scope['explicitly_excluded_payment_ids'] == ['PAY-3']
    changed = resolve_customer_authorization(rows + [{'text': '我改变主意，改为退PAY-2和PAY-3。'}], 'locator')
    assert changed['requested_payment_ids'] == ['PAY-2', 'PAY-3']
    assert changed['explicitly_excluded_payment_ids'] == []


@pytest.mark.parametrize('text', [
    'PAY-2和PAY-3只处理前者。',
    'Refund PAY-2 unless PAY-3 is eligible.',
    '只退一笔PAY-2和PAY-3。',
    'PAY-2不要退只退PAY-3。',
])
def test_unsupported_or_contradictory_selection_stays_unresolved(text):
    resolution, verdict = assess_refund(state(text, role='exclusive'), index(3), {'target_invoice_id': 'INV-1'})
    assert not resolution['ok']
    assert not verdict.eligible


@pytest.mark.parametrize('text,role', [
    ('INV-1扣了三次，请退多扣的钱。', 'unspecified'),
    ('付款凭证PAY-1，这张账单扣了三次，请退多扣款。', 'locator'),
    ('只退INV-1的PAY-2和PAY-3这两笔多扣款，其他付款不要动。', 'exclusive'),
    ('请把PAY-2和PAY-3这两笔重复付款都退回，共98元。', 'locator'),
])
def test_invoice_all_locator_and_explicit_multiple_scope_are_preserved(text, role):
    resolution, verdict = assess_refund(state(text, role=role), index(3), {'target_invoice_id': 'INV-1'})
    assert resolution['ok']
    assert [item.payment_id for item in verdict.items] == ['PAY-2', 'PAY-3']


def _make(make, revision, calls):
    return make(calls, extractions=[{**fields(), 'payment_reference_role': 'locator'}], revision=revision)[0]


@pytest.mark.parametrize('revision', ['B1', 'B2'])
@pytest.mark.parametrize('text,target', [
    ('PAY-88213和PAY-88214都重复了，但只退PAY-88213。', 'PAY-88213'),
    ('PAY-88213不要退，只退PAY-88214。', 'PAY-88214'),
])
def test_graph_proposes_and_executes_only_the_requested_payment(make, billing, revision, text, target):
    three_payments(billing)
    rt = _make(make, revision, [*reads(), end()])
    cid, view = start(rt, text)
    assert view['case']['state'] == 'awaiting_approval'
    assert [x['payment_id'] for x in view['case']['proposal']['items']] == [target]
    assert billing.customer_refunds('C-1001') == []
    view = approve(rt, cid)
    assert view['case']['state'] == 'closed'
    assert [(r['payment_id'], r['amount_cents']) for r in billing.customer_refunds('C-1001')] == [(target, 4900)]


@pytest.mark.parametrize('revision', ['B1', 'B2'])
def test_graph_asks_for_selection_and_yes_does_not_erase_the_limit(make, billing, revision):
    three_payments(billing)
    rt = _make(make, revision, [*reads(), end(), end(), end()])
    cid, view = start(rt, 'INV-2026-0142扣了三次，但本次只退其中一笔。')
    assert view['case']['state'] == 'awaiting_user'
    assert '只退一笔' in view['case']['pending_question']
    rt.submit_message(cid, text='确认', actor='cust-c1001')
    rt.drain()
    view = rt.case_view(cid)
    assert view['case']['state'] == 'awaiting_user'
    assert not view['case']['proposal']
    rt.submit_message(cid, text='PAY-88214', actor='cust-c1001')
    rt.drain()
    proposal = rt.case_view(cid)['case']['proposal']
    assert [x['payment_id'] for x in proposal['items']] == ['PAY-88214']
    assert not any(e['kind'] == 'customer_scope_confirmed' for e in rt.store.events(cid))
    assert billing.customer_refunds('C-1001') == []


@pytest.mark.parametrize('revision', ['B1', 'B2'])
def test_approval_followup_then_clarification_uses_latest_customer_scope(make, billing, revision):
    three_payments(billing)
    rt = _make(make, revision, [*reads(), end(), end(), end()])
    cid, view = start(rt, '只退INV-2026-0142的PAY-88213，其他付款不要动。')
    old = deepcopy(view['case'])
    assert old['state'] == 'awaiting_approval'
    rt.submit_message(cid, text='INV-2026-0142还是只退一笔，先重新确认目标。', actor='cust-c1001')
    rt.drain()
    assert rt.case_view(cid)['case']['state'] == 'awaiting_user'
    with pytest.raises(StateConflict):
        rt.submit_decision(cid, decision='approve', approver='ops-dana', note='old', proposal_version=old['proposal_version'], token=old['gate_token'])
    selected = rt.submit_message(cid, text='改为只退PAY-88214。', actor='cust-c1001')
    rt.drain()
    current = rt.case_view(cid)['case']
    assert current['state'] == 'awaiting_approval'
    assert current['proposal_version'] > old['proposal_version']
    assert current['gate_token'] != old['gate_token']
    assert [x['payment_id'] for x in current['proposal']['items']] == ['PAY-88214']
    assert current['proposal']['target_resolution']['authorization_scope']['source_utterance']['command_id'] == selected
    with pytest.raises(StateConflict):
        rt.submit_decision(cid, decision='approve', approver='ops-dana', note='old', proposal_version=old['proposal_version'], token=old['gate_token'])
    assert billing.customer_refunds('C-1001') == []
    assert approve(rt, cid)['case']['state'] == 'closed'
    assert [r['payment_id'] for r in billing.customer_refunds('C-1001')] == ['PAY-88214']
