"""Meaningful mutation adapter checks: identity, scoring condition and faults."""
from copy import deepcopy
import json
from pathlib import Path

from billingops.evaluation.v2_mutation import build_bundle
from billingops.evaluation.v2_runner import case_key, grading_task, make_plan, run_trial, _attack_exposure
from billingops.evaluation.v2_scoring import score_trial
from billingops.llm.fake import ScriptedModel

ROOT = Path(__file__).resolve().parents[1]


def suite():
    return build_bundle(json.loads((ROOT/'data/tasks/dynamic-v2/gold-fixtures-v0.2.json').read_text()), json.loads((ROOT/'data/eval-design/dynamic-v2/controlled-text-rewrites-v2.json').read_text()))


def item(cid):
    return next(t for t in suite()['tasks'] if t['case_id']==cid)


def test_unified_plan_preserves_each_variant_and_registered_repeat_denominator():
    p=make_plan(suite(),mode='unified',workers=8)
    assert len(p['candidate_ids'])==128 and len(set(p['candidate_ids']))==128
    assert p['total_trials']==744
    assert p['planned_trials']['G01-B1-1']==7
    assert p['planned_trials']['G15-S1-01']==3
    assert len(p['cohorts']['injection_asr_cases'])==7
    assert 'G01-P1-01' not in p['cohorts']['benign_must_act']
    assert 'G01-D1-01' not in p['cohorts']['benign_must_act']
    assert 'G26-D1-01' in p['cohorts']['benign_must_act']


def test_permission_condition_scores_changed_profile_without_relabeling_parent():
    task=item('G01-P1-01')
    proxy=grading_task(task)
    artifact={'initial_world':task['world'],'final_world':task['world'],'events':[], 'control_events':[],
              'view':{'case':{'state':'closed','conclusion':{'type':'permission_refused'}},'evidence':[]},
              'capture_complete':{'world':True,'events':True,'evidence':True},'response_grade':{'status':'unknown'}}
    scored=score_trial(proxy,artifact)
    assert task['candidate_id']=='G01' and proxy['candidate_id']=='G31'
    assert scored['outcome']['status']=='pass'
    assert not any(k.startswith('invoice_amount') for k in scored['behavior']['checks'])
    assert case_key(task)=='G01-P1-01'


def test_lost_response_after_commit_is_real_and_does_not_duplicate_write(tmp_path):
    task=item('G02-F3-01')
    result=run_trial(task,0,tmp_path/'f3',model_factory=ScriptedModel,grade=False)
    a=json.loads(Path(result['artifact']).read_text())
    assert result['candidate_id']=='G02-F3-01' and a['seed_candidate_id']=='G02'
    assert a['task']['candidate_id']=='G02'
    assert a['fault_exposure']['status']=='reached'
    assert a['fault_exposure']['consumed']=={'lost_response_after_commit':1}
    assert result['scoring']['outcome']['status']=='pass'
    assert len(a['final_world']['refunds'])-len(a['initial_world']['refunds'])==1
    assert sum(e['kind']=='refund_sending' for e in a['events'])==1
    assert result['scoring']['guardrails']['incidents']['duplicate_action'] is False


def test_read_fault_is_armed_after_world_reset_and_consumed_by_agent(tmp_path):
    result=run_trial(item('G01-F1-01'),0,tmp_path/'f1',model_factory=ScriptedModel,grade=False)
    a=json.loads(Path(result['artifact']).read_text())
    assert a['fault_exposure']['consumed']=={'reads_unavailable':1}
    assert a['capture_complete']['world']
    assert any(e.get('ok') is False for e in a['view']['evidence'])


def test_injection_marker_detects_nested_json_in_actual_requests(tmp_path):
    t=item('G01-A1-01');payload=t['attack_control']['payload']
    log=tmp_path/'agent-calls.jsonl'
    log.write_text(json.dumps({'kind':'request','index':2,'messages':[{'role':'tool','content':json.dumps({'description':payload})}]})+'\n')
    exposure=_attack_exposure(t,log)
    assert exposure['payload_reached_model'] is True
    assert exposure['hits'][0]['role']=='tool'
    log.write_text(json.dumps({'kind':'response','response':{'description':payload}})+'\n')
    assert _attack_exposure(t,log)['payload_reached_model'] is None
