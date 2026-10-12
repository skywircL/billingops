"""Bad-case mechanisms and their counterexamples; scripted tests are not model quality scores."""
from __future__ import annotations

import pytest

from billingops.investigation.target import resolve_target
from billingops.investigation.evidence import build_index
from billingops.llm.fake import ScriptedModel
from billingops.tools import END_TOOL
from billingops.testing import World, ambiguous_world, duplicate_world
from billingops.evaluation.challenges import build_challenges
from conftest import run_case, approve


def answer_unknown(rt, view):
    for _ in range(2):
        if view['case']['state'] != 'awaiting_user':
            break
        rt.submit_message(view['case']['id'], text="I don't have that information", actor='cust-c1001')
        rt.drain()
        view = rt.case_view(view['case']['id'])
    return view


@pytest.mark.parametrize('mode', ['fixed', 'dynamic'])
@pytest.mark.parametrize('world,expected', [(duplicate_world(), 'awaiting_approval'), (ambiguous_world(), 'closed')])
def test_unknown_identifier_requires_unique_target(runtime_factory, billing, mode, world, expected):
    billing.load_world(world)
    rt = runtime_factory(mode=mode)
    view = answer_unknown(rt, run_case(rt, 'I was charged twice. Please find the duplicate; I do not know the invoice.'))
    assert view['case']['state'] == expected
    if expected == 'closed':
        assert view['case']['conclusion']['type'] == 'insufficient_information'
        assert view['case']['proposal'] is None
    else:
        assert view['case']['proposal']['target_resolution']['scope'] == 'account'
    assert billing.customer_refunds('C-1001') == []


@pytest.mark.parametrize('mode', ['fixed', 'dynamic'])
def test_noise_is_scanned_instead_of_stopping_at_first_extra(runtime_factory, billing, mode):
    c = build_challenges()[0]
    billing.load_world(c.world)
    rt = runtime_factory(mode=mode)
    view = answer_unknown(rt, run_case(rt, c.user_text))
    assert view['case']['state'] == 'awaiting_approval'
    assert view['budgets']['reads'] == 7
    view = approve(rt, view['case']['id'])
    assert view['execution']['status'] == 'done'


@pytest.mark.parametrize('mode', ['fixed', 'dynamic'])
def test_budget_cannot_turn_partial_scan_into_refund(runtime_factory, billing, mode):
    world = duplicate_world()
    noise = World()
    for i in range(8):
        noise.invoice(f'INV-N-{i}', 'C-1001', 9000, '2026-03-01', '2026-03-31')
    world['invoices'].extend(noise.build()['invoices'])
    billing.load_world(world)
    rt = runtime_factory(mode=mode)
    view = answer_unknown(rt, run_case(rt, 'I was charged twice for 49 yuan. I cannot identify it.'))
    assert view['case']['proposal'] is None
    assert view['budgets']['reads'] == 8
    assert billing.customer_refunds('C-1001') == []


def test_new_duplicate_after_approval_invalidates_inferred_target(runtime_factory, billing):
    rt = runtime_factory(mode='fixed')
    view = answer_unknown(rt, run_case(rt, 'I was charged twice. Please find the extra payment.'))
    assert view['case']['state'] == 'awaiting_approval'
    billing.load_world(ambiguous_world())
    view = approve(rt, view['case']['id'])
    assert view['case']['proposal'] is None
    assert view['case']['conclusion']['type'] == 'insufficient_information'
    assert rt._snapshot(view['case']['id'])['precheck']['ok'] is False
    assert view['case']['gate_token'] is None
    assert billing.customer_refunds('C-1001') == []


class GuessingModel(ScriptedModel):
    """Looks up a real duplicate but pretends the user selected it."""
    def _investigate(self, t, messages):
        for name, args in [('list_payments', {'invoice_id': 'INV-2026-0142'}), ('list_refunds', {'payment_id': 'PAY-88213'})]:
            if t.result_for(name, **args) is None:
                return self._tool(name, args, messages)
        return self._tool(END_TOOL, {'conclusion_type': 'duplicate_charge_refund', 'target_invoice_id': 'INV-2026-0142', 'target_payment_id': 'PAY-88213', 'summary': 'I selected this one', 'facts': []}, messages)


def test_code_blocks_model_guess_without_complete_scope(runtime_factory, billing):
    billing.load_world(ambiguous_world())
    rt = runtime_factory(model=GuessingModel())
    view = run_case(rt, 'I was charged twice but cannot identify the charge.')
    assert view['case']['proposal'] is None
    assert view['blocked_proposals'][0]['reason'] == 'target_scope_not_read'
    assert view['case']['conclusion']['type'] == 'insufficient_information'


@pytest.mark.parametrize('mode', ['fixed', 'dynamic'])
@pytest.mark.parametrize('text,kind', [
    ('My subscription increased. Please credit the increase back to my card.', 'escalate'),
    ('My subscription price increased. Please reimburse the difference.', 'escalate'),
    ("My subscription price increased. I do not want a refund or credit, just explain it.", 'explain_subscription'),
    ('Why did my subscription price increase?', 'explain_subscription'),
])
def test_price_intent_positive_and_negative(runtime_factory, billing, mode, text, kind):
    view = run_case(runtime_factory(mode=mode), text)
    assert view['case']['conclusion']['type'] == kind
    assert view['case']['proposal'] is None


def test_named_payment_cannot_be_replaced_by_another_extra(runtime_factory, billing):
    rt = runtime_factory(model=GuessingModel())
    view = run_case(rt, 'I was charged twice; please handle payment PAY-88212.')
    assert view['case']['proposal'] is None
    assert view['blocked_proposals'][0]['reason'] == 'customer_payment_mismatch'


def test_two_extras_on_one_invoice_need_explicit_payment(runtime_factory, billing):
    world = duplicate_world()
    world['payments'].extend(World().payment('PAY-THIRD', 'C-1001', 'INV-2026-0142', 4900, '2026-02-02T09:02:00').build()['payments'])
    billing.load_world(world)
    rt = runtime_factory(mode='fixed')
    view = answer_unknown(rt, run_case(rt))
    assert view['case']['proposal'] is None
    view = run_case(rt, 'I was charged twice on INV-2026-0142. Refund extra payment PAY-THIRD.')
    assert view['case']['proposal']['payment_id'] == 'PAY-THIRD'

class EarlyFinishModel(ScriptedModel):
    def _investigate(self, t, messages):
        for name, args in [('list_invoices', {}), ('list_payments', {'invoice_id': 'INV-2026-0142'}), ('list_refunds', {'payment_id': 'PAY-88213'})]:
            if t.result_for(name, **args) is None:
                return self._tool(name, args, messages)
        rejected = next((r for c,r in t.calls if c.name == END_TOOL and r and r.get('missing_invoice_ids')),None)
        if rejected:
            for iid in rejected['missing_invoice_ids']:
                if t.result_for('list_payments',invoice_id=iid) is None:
                    return self._tool('list_payments',{'invoice_id':iid},messages)
        return self._tool(END_TOOL, {'conclusion_type':'duplicate_charge_refund','target_invoice_id':'INV-2026-0142','target_payment_id':'PAY-88213','summary':'extra payment','facts':[]},messages)


def test_rejected_early_proposal_returns_bounded_actionable_feedback(runtime_factory,billing):
    rt=runtime_factory(model=EarlyFinishModel())
    view=run_case(rt,'I was charged twice but have no invoice number.')
    assert view['case']['state']=='awaiting_approval'
    assert view['budgets']['reads']==4
    assert view['budgets']['repairs']==1
    assert view['protocol_errors'][0]['reason']=='target_scope_incomplete'
    assert billing.customer_refunds('C-1001')==[]


def test_price_adjustment_intent_takes_precedence_over_status_flag():
    from billingops.investigation.rules import category_of
    assert category_of({'requests_price_adjustment':True,'mentions_subscription_price':False,'mentions_refund_status':True})=='subscription'


def test_account_evidence_survives_without_target():
    from billingops.investigation.graph import _evidence_bundle
    world=duplicate_world()
    evidence=[{'ok':True,'tool':'list_invoices','data':world['invoices']}]
    for inv in world['invoices']:
        evidence.append({'ok':True,'tool':'list_payments','args':{'invoice_id':inv['id']},'data':[p for p in world['payments'] if p['invoice_id']==inv['id']]})
    bundle=_evidence_bundle({'customer_id':'C-1001'},build_index(evidence),'insufficient_information',None,{},None)
    assert len(bundle['invoice_list'])==2
    assert len(bundle['payments_by_invoice']['INV-2026-0142'])==2
    assert bundle['invoice'] is None
