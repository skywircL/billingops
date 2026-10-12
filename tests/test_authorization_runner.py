"""New challenge cases run without pretending to be the frozen Gold suites."""
from copy import deepcopy
import json
from pathlib import Path
import pytest

from billingops.evaluation.authorization_cases import build_bundle
from billingops.evaluation.v2_runner import make_plan, grading_task
from billingops.evaluation.v2_runner import run_trial
from billingops.llm.base import Message, ModelResponse, ToolCall, Usage
from test_b1_graph import SequenceModel


def test_authorization_suite_registers_own_cases_risk_and_exposure():
    bundle = build_bundle()
    plan = make_plan(bundle, mode='baseline', agent_revision='B2')
    assert plan['candidate_ids'] == [task['case_id'] for task in bundle['tasks']]
    assert plan['planned_trials']['AS02'] == 7
    assert plan['planned_trials']['AS11'] == 3
    assert plan['declared_seed_count'] == 2
    assert plan['declared_root_families'] == 2
    assert plan['human_gold_review'] == 'not_independently_completed'
    assert plan['suite_id'] == 'authorization-scope-v1'
    assert plan['evaluation_versions']['scorer'] == 'business-v2-trace-1.4'
    assert 'source hashes' in plan['baseline_policy_note']
    pilot = make_plan(bundle, mode='pilot', agent_revision='B1')
    assert pilot['total_trials'] == 14
    assert set(pilot['candidate_ids']) == set(plan['candidate_ids'])
    selected = make_plan(bundle, mode='pilot', selected=['AS02'], agent_revision='B1')
    assert selected['candidate_ids'] == ['AS02'] and selected['total_trials'] == 1


def test_admission_rejects_wrong_answer_before_any_model_is_created():
    bad = deepcopy(build_bundle())
    bad['tasks'][1]['expected_refunds'].append({'payment_id': 'PAY-3', 'amount_cents': 10000})
    with pytest.raises(ValueError, match='fixture validation failed'):
        make_plan(bad, mode='pilot')


def test_grading_profile_does_not_erase_independent_author_scope():
    task = build_bundle()['tasks'][1]
    view = grading_task(task)
    assert task['candidate_id'] == 'AS02' and view['candidate_id'] == 'G02'
    assert view['author_scope']['payment_ids'] == ['PAY-2']
    assert view['authorization_expectations'] == task['authorization_expectations']


@pytest.mark.parametrize('cid', ['AS02', 'AS12', 'AS13'])
def test_new_suite_actual_graph_trace_satisfies_scope_and_followup_grading(tmp_path, cid):
    task = next(t for t in build_bundle()['tasks'] if t['candidate_id'] == cid)
    read_calls = [
        ('get_invoice', {'invoice_id': 'INV-2026-0142'}),
        ('list_payments', {'invoice_id': 'INV-2026-0142'}),
        ('list_customer_refunds', {}),
        ('end_investigation', {'conclusion_type': 'duplicate_charge_refund',
          'target_invoice_id': 'INV-2026-0142', 'target_payment_id': 'PAY-2', 'summary': '', 'facts': []}),
    ]

    class Model(SequenceModel):
        def generate(self, messages, schemas, config):
            if config.tag == 'followup':
                return ModelResponse(Message('assistant', tool_calls=[ToolCall('followup', 'classify_followup',
                    {'kind': 'progress', 'reason': 'acknowledgement of the same requested payment'})]), Usage())
            return super().generate(messages, schemas, config)

    result = run_trial(task, 0, tmp_path/cid,
                       model_factory=lambda: Model(read_calls * 2), grade=False, agent_revision='B1')
    trace = json.loads(Path(result['artifact']).read_text())
    assert result['scoring']['outcome']['status'] == 'pass', result['scoring']
    assert result['scoring']['behavior']['status'] == 'pass', result['scoring']
    incidents = result['scoring']['guardrails']['incidents']
    assert incidents['privacy_disclosure'] is None  # Response judge intentionally absent.
    assert incidents['tool_result_hallucination'] is None
    assert all(value is False for name, value in incidents.items() if name not in {'privacy_disclosure', 'tool_result_hallucination'})
    old_ids = {r['id'] for r in trace['initial_world']['refunds']}
    new_refunds = [r for r in trace['final_world']['refunds'] if r['id'] not in old_ids]
    assert [r['payment_id'] for r in new_refunds] == (['PAY-3'] if cid == 'AS13' else ['PAY-2'])
    assert result['status'] == 'unknown'  # No response judge or model success-rate claim.
