"""Developer-authored public-dialogue smoke; not Agent evaluation/human calibration."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import time
from datetime import datetime, timezone
from billingops.config import MODEL_BASE_URL, MODEL_ID, MODEL_TEMPERATURE
from billingops.evaluation.v2_controls import ConditionalCustomer, CONTROLS_VERSION
from billingops.evaluation.v2_runner import AuditedModel, _open_model

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def write(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def plan():
    fixture = ROOT / 'data/tasks/dynamic-v2/gold-fixtures-v0.2.json'
    tasks = {t['candidate_id']: t for t in json.loads(fixture.read_text())['tasks']}
    rows = [
        ('C01', 'G06', 'g06-correct', '您提供的账单编号未查到，请核对后提供正确账单编号。', 0),
        ('C02', 'G07', 'g07-corrections', '这个账单号查不到，请核对并提供您看到的账单编号。', 0),
        ('C03', 'G07', 'g07-corrections', '您刚才提供的INV-2026-0888也未查到，请进一步核对有效账单编号或确认处理目标？', 1),
        ('C04', 'G12', 'g12-valid', '查到INV-2026-0142有两笔多扣，合计98元。是否把这两笔全部退款？', 0),
        ('C05', 'G13', 'g13-wrong49', 'INV-2026-0142有两笔多扣，合计49元。是否把这两笔全部退款？', None),
        ('C06', 'G13', 'g13-pick-one', 'INV-2026-0142有两笔多扣，合计98元。请您从中选择一笔退款，可以吗？', None),
        ('C07', 'G12', 'g12-policy', '退款政策要求客户确认和操作员审批。您是否需要我进一步解释退款政策？', None),
        ('C08', 'G13', 'g13-valid', '已核查INV-2026-0142：三笔付款各49元，其中两笔是额外多扣，共98元。可以将多扣的两笔都退回吗？', 0),
    ]
    target = OUT / 'plan.json'
    if target.exists():
        raise FileExistsError(target)
    data = {
        'registered_at': datetime.now(timezone.utc).isoformat(),
        'purpose': 'developer_synthetic_customer_matcher_smoke_not_agent_success_or_human_calibration',
        'controls_version': CONTROLS_VERSION,
        'source_sha256': {'controls': sha(ROOT/'src/billingops/evaluation/v2_controls.py'), 'runner': sha(ROOT/'src/billingops/evaluation/v2_runner.py'), 'smoke_script': sha(Path(__file__))},
        'fixture_sha256': sha(fixture),
        'model': {'id': MODEL_ID, 'base_url': MODEL_BASE_URL, 'temperature': MODEL_TEMPERATURE, 'max_retries': 0},
        'expected_by': 'developer_before_model_calls',
        'cases': [{'id': ident, 'candidate_id': cid, 'session_id': session, 'question': question, 'expected_script_index': expected, 'customer_control': tasks[cid]['customer_control']} for ident,cid,session,question,expected in rows],
        'scope': 'Only customer_control and public dialogue enter customer matcher; expected_script_index is used after response, never in its prompt.',
    }
    write('plan.json', data)
    print('Registered 8 expected selections before any provider call.')


def run():
    plan_path = OUT / 'plan.json'
    data = json.loads(plan_path.read_text())
    for path, key in [('src/billingops/evaluation/v2_controls.py','controls'), ('src/billingops/evaluation/v2_runner.py','runner')]:
        if sha(ROOT / path) != data['source_sha256'][key]:
            raise RuntimeError('frozen source changed: ' + path)
    if (OUT / 'calls.jsonl').exists() or (OUT / 'results.jsonl').exists():
        raise FileExistsError('refuse to overwrite prior API exchanges')
    wrapper = AuditedModel(_open_model(), OUT / 'calls.jsonl', role='customer_smoke', deadline=time.monotonic()+600)
    users, histories, results = {}, {}, []
    with (OUT/'results.jsonl').open('x') as stream:
        for row in data['cases']:
            sid = row['session_id']
            if sid not in users:
                users[sid] = ConditionalCustomer(row['customer_control'], wrapper)
                histories[sid] = []
            user = users[sid]
            history = histories[sid]
            history.append({'role':'assistant','content':row['question']})
            calls_before = len(wrapper.calls)
            try:
                answer = user.answer(row['question'], history=history)
                event = user.events[-1]
                history.append({'role':'user','content':answer})
                observed = event['script_index'] if event['kind']=='customer_script_matched' else None
                passed = observed == row['expected_script_index'] and event['kind'] != 'customer_match_invalid'
                record = {'id':row['id'], 'candidate_id':row['candidate_id'], 'session_id':sid, 'question':row['question'], 'expected_script_index':row['expected_script_index'], 'observed_script_index':observed, 'check':'pass' if passed else 'fail', 'event':event, 'call_indices':list(range(calls_before,len(wrapper.calls)))}
            except Exception as exc:
                record = {'id':row['id'], 'candidate_id':row['candidate_id'], 'check':'unknown', 'error_type':type(exc).__name__, 'call_indices':list(range(calls_before,len(wrapper.calls)))}
            results.append(record)
            stream.write(json.dumps(record,ensure_ascii=False)+'\n');stream.flush()
            print(row['id'],record['check'],record.get('event',{}).get('kind'),flush=True)
    report = {'purpose':data['purpose'], 'plan_sha256':sha(plan_path), 'finished_at':datetime.now(timezone.utc).isoformat(), 'planned':len(data['cases']), 'recorded':len(results), 'counts':{status:sum(r['check']==status for r in results) for status in ('pass','fail','unknown')}, 'model_metrics':wrapper.metrics(), 'results':results, 'interpretation':'Only 8 developer-authored simulator prompts, one call each, with one two-turn correction session. Not Agent success, human calibration, reliability estimate or hidden evaluation.'}
    write('report.json',report)
    lines=['# 条件客户匹配 smoke v1','', '本次为开发者预先编写的8条公开对话检查；预期匹配序号先登记再调用DeepSeek。不是Agent成功率、人工裁判校准或可靠性结论。','', '| 检查 | 来源控制 | 预期脚本 | 实际脚本 | 结果 |','|---|---|---|---|---|']
    for r in results:
        lines.append(f"| {r['id']} | {r['candidate_id']} | {r.get('expected_script_index')} | {r.get('observed_script_index')} | {r['check']} |")
    lines += ['', '完整请求与响应：`calls.jsonl`；预登记：`plan.json`；逐条匹配证据：`results.jsonl`；机器汇总：`report.json`。', '', '成本按项目冻结的单价配置估算：$'+str(round(report['model_metrics']['cost_usd_estimated'],6))+'。', '', '未修改Agent、评测模块或账务环境；模型仅接收客户控制及公开对话，不接收预期匹配、世界状态或退款标准答案。']
    (OUT/'report.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps(report['counts']))

if __name__ == '__main__':
    (plan if sys.argv[1]=='plan' else run)()
