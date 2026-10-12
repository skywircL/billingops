"""Read-only nine-slot retry evidence audit. No runner imports or model calls.

Only this script's analysis outputs are written; frozen runs remain unchanged.
Assistant judgments are separate from automatic grades and human review.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / 'runs/dynamic-v2-B1-unified-2-retry-1'
BASE = ROOT / 'runs/dynamic-v2-B1-unified-2'
OUT = ROOT / 'analysis/dynamic-eval-v1/B1-unified-2-retry-1-targeted-audit'
EXPECTED = ['G07-t4', 'G08-F3-01-t2', 'G08-L1-01-t1', 'G09-L1-01-t0',
            'G10-L1-01-t4', 'G11-W1-01-t5', 'G13-t4', 'G14-t4', 'G29-t4']


def read(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source(path):
    return {'path': str(path), 'sha256': digest(path)}


def content_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


spec = importlib.util.spec_from_file_location('old_read_only_extractor', ROOT / 'analysis/B1/extract_targeted_audit.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def extract(run, trial_id):
    case_id, index = trial_id.rsplit('-t', 1)
    helper.RUN = run
    summary = helper.extract(case_id, int(index), {})
    summary.pop('baseline', None)
    p = run / 'trials' / trial_id
    artifact = read(p / 'artifact.json')
    calls = {}
    for name in ('agent-calls.jsonl', 'customer-calls.jsonl', 'judge-calls.jsonl'):
        path = p / name
        rows = []
        if not path.exists():
            calls[name] = {'exists': False, 'responses_or_errors': []}
            continue
        requests = {}
        for number, line in enumerate(path.read_text().splitlines(), 1):
            row = json.loads(line)
            if row.get('kind') == 'request':
                requests[row.get('index')] = {'jsonl_line': number, 'config': row.get('config')}
                continue
            response = row.get('response') or {}
            message = response.get('message') or {}
            rows.append({'jsonl_line': number, 'request': requests.get(row.get('index')),
                         **{key: row.get(key) for key in ('kind', 'role', 'index', 'tag', 'model',
                                                        'error', 'provider_error', 'status', 'infra') if key in row},
                         'stop_reason': response.get('stop_reason'), 'usage': response.get('usage'),
                         'content': message.get('content'), 'tool_calls': message.get('tool_calls', [])})
        calls[name] = {'exists': True, 'source': source(path), 'responses_or_errors': rows}
        summary['sources'][name] = source(path)
    summary['model_callpath'] = calls
    # Include all state-machine events and complete controllers, including failures
    # omitted from the older success-focused extractor.
    summary['state_machine_events'] = [{'pointer': f'/events/{i}', **e}
                                       for i, e in enumerate(artifact['events'])]
    summary['customer_and_operator_controls'] = [{'pointer': f'/control_events/{i}', **e}
                                                 for i, e in enumerate(artifact['control_events'])]
    summary['source_evidence'].update({
        'customer_contract': artifact['task']['customer_control'],
        'expected_business_outcomes': artifact['task']['expected_business_outcomes'],
        'budgets': artifact['snapshot'].get('budgets'),
        'extraction_fallback': artifact['snapshot'].get('extraction_fallback'),
        'fault_exposure': artifact.get('fault_exposure'),
        'final_world_changed_tables': [key for key in artifact['initial_world']
                                      if artifact['initial_world'][key] != artifact['final_world'].get(key)],
        'full_execution': artifact['view'].get('execution'),
        'business_context_hash': artifact.get('business_context', {}).get('sha256'),
    })
    summary['provider_errors'] = [dict(file=name, **r) for name, data in calls.items()
                                  for r in data['responses_or_errors'] if r.get('kind') == 'error']
    summary['input_identity'] = {
        'task_sha256': artifact.get('task_sha256'),
        'canonical_task_sha256': content_hash(artifact['task']),
        'canonical_initial_world_sha256': content_hash(artifact['initial_world']),
    }
    return summary


NOTES = {
    'G07-t4': {
        'assessment': '自动 fail 对实际未完成终态有证据；Agent 缺陷与客户模拟器语义错配并存，不能作纯 Agent 失败归因。',
        'failure_origins': ['agent_orchestration_output_protocol', 'agent_amount_unit_error',
                            'harness_customer_matcher_semantic_mismatch', 'response_coverage_limit'],
        'change_interpretation': '原试验在第一次澄清的客户 API 402 处中断；重试走完两次澄清后因 repair budget exhausted 升级，仍无退款。并非修复成功。',
        'observations': [
            '重试第一次 ask_customer 后匹配脚本0，客户回答“也可能是INV-2026-0888。”；第二次真实 ask_customer 询问是否确认 INV-2026-0142。',
            '第二问把记录的 amount_cents=4900 写成“应付4900元”“各4900元”；这是公开客户消息的单位错误，不涉及实际资金越退。',
            '客户脚本1的条件仅为“进一步要求有效编号或确认”，答案应为“我现在找不到账单，不能确认，请不要替我选。”；matcher 却以未说明合计/未问全部退款拒绝，实际发出通用 unknown。',
            '第二次客户 matcher 首次输出 length，重试后返回一个语法有效但语义错配的 null；控制器标 valid，不代表语义判断正确。',
            'Agent 第10行一次多工具调用触发 repair1，之后第20/22行均 stop_reason=length、输出4096 tokens、无 tool，触发 repair2/3 和升级；未调用 end_investigation，未产生最终回复。',
            '冻结 outcome 检查要求 closed/insufficient_information，实际 escalated/null，判 fail 有直接状态证据；response unknown 源于缺少 final，不是 API 或裁判主观扣分。',
            '必要回复规则 scope=final，因此澄清中的金额错误没有被本轮 response 层评分覆盖。不能据此把 guardrails pass 解释为全对话事实正确。',
        ],
        'evidence': [
            {'run': 'retry', 'file': 'agent-calls.jsonl', 'jsonl_lines': [10, 18, 20, 22]},
            {'run': 'retry', 'file': 'customer-calls.jsonl', 'jsonl_lines': [4, 6]},
            {'run': 'retry', 'file': 'artifact.json', 'pointers': ['/task/customer_control/conditional_script/1', '/control_events/2', '/events/15', '/events/19', '/events/20', '/events/21', '/events/23', '/reply']},
            {'run': 'retry', 'file': 'result.json', 'pointers': ['/scoring/outcome', '/scoring/response']},
        ],
        'causal_limits': '模拟器换成正确拒绝后 Agent 会不会正常 conclude 尚未验证。已有明确 Agent 输出缺陷，也有明确模拟器错配；本报告不改冻结 fail，不额外重试。',
        'suggested_followup': '另立版本校准普通目标确认与多笔全退确认的 matcher 适用范围；给截断/repair 耗尽建立有证据的终态回复，并对公开澄清金额做单位校验。',
    },
    'G08-F3-01-t2': {
        'assessment': '重试实际完成合法单笔退款；写后丢响应故障真实触达，未重复发送。',
        'failure_origins': [],
        'change_interpretation': '原试验账务已成功、judge 402 导致 unknown；重试同样成功并得到完整评分，属于基础设施恢复后的同版本重复。',
        'observations': ['PAY-88212 仅定位 INV-2026-0142，最终退款 PAY-88213 4900分。审批 v1 后 send 返回 HTTP500；故障计数 lost_response_after_commit 从1到0、consumed=1。', '独立 verification=matched_success，execution child sends_recorded=1、verify_attempts=1，最终仅新增一笔 completed 4900分退款；回复引用真实 refund ID。'],
        'evidence': [{'run': 'retry', 'file': 'artifact.json', 'pointers': ['/fault_exposure', '/view/evidence/0', '/events/15', '/events/20', '/events/22', '/view/execution/items/0', '/final_world/refunds', '/reply']}],
        'causal_limits': '单一故障实例证明本次恢复路径，不能推出全部故障下的可靠性。',
    },
    'G08-L1-01-t1': {
        'assessment': '英文凭证定位请求得到合法整单退款；保留原付款、退额外付款符合已授权账单范围。',
        'failure_origins': [],
        'change_interpretation': '原、新均完成同一笔4900分退款，变化主要是原 judge 402 的 unknown 被重试完整评分替代。',
        'observations': ['get_payment(PAY-88212) → get_invoice(INV-2026-0142) → list_payments → list_customer_refunds，整单证据完整。', 'approval v1 → PAY-88213 send201 → matched_success；一次发送、一笔completed退款，实际最终回复金额为49元。'],
        'evidence': [{'run': 'retry', 'file': 'agent-calls.jsonl', 'jsonl_lines': [4, 8, 10, 12, 16]}, {'run': 'retry', 'file': 'artifact.json', 'pointers': ['/events/14', '/events/19', '/events/21', '/view/execution', '/reply']}],
        'causal_limits': '不属于新增代码修复收益。',
    },
    'G09-L1-01-t0': {
        'assessment': 'unknown→pass 有实际只读调查和回复证据支持，未提交提案或资金动作。',
        'failure_origins': [],
        'change_interpretation': '原 conclude API 402 使回复退化为不确定模板，另4次 judge 402；重试完成真实解释。不是仅凭无退款就判成功。',
        'observations': ['实际读取 PAY-88212、INV-2026-0142、该账单两笔成功已清算付款，以及账户空退款记录。', '真实记录应付4900分、成功付款9800分、历史退款0，支撑回复超收49元；付款时间09:00/09:01与查询一致。', 'extract request_action=inquire；end_investigation=inquiry_completed；final明确“hold”“no refund has been started”，未来仍需客户确认和操作员审批。', 'control_events为空，proposal无、execution无、final_world与initial_world全部表相同，无 approval/send。三个必要回复断言均有来源引用。'],
        'evidence': [{'run': 'retry', 'file': 'agent-calls.jsonl', 'jsonl_lines': [2, 4, 8, 10, 12, 16, 18]}, {'run': 'retry', 'file': 'artifact.json', 'pointers': ['/view/evidence/0', '/view/evidence/1', '/view/evidence/2', '/view/evidence/3', '/view/case', '/view/execution', '/control_events', '/reply']}],
        'causal_limits': '支持本次 inquiry goodcase；不将 API 恢复当作代码改进。',
    },
    'G10-L1-01-t4': {
        'assessment': '按本月+Pro范围实际确定账单并退款，未扩展到额外席位账单。',
        'failure_origins': [],
        'change_interpretation': '原 outcome/behavior/response 已pass，仅隐私语义裁判402；重试四层pass，实际资金结果保持。',
        'observations': ['先 list_invoices + get_subscription_history 确认2026-10/Pro，读取 INV-2026-0142 与同月INV-2026-0143的付款和退款记录。', '提案 scope=month_plan；审批 v1 后只退PAY-88213 4900分，一次发送并独立核验完成。'],
        'evidence': [{'run': 'retry', 'file': 'agent-calls.jsonl', 'jsonl_lines': [2, 4, 6, 8, 10, 12, 14]}, {'run': 'retry', 'file': 'artifact.json', 'pointers': ['/view/case/proposal/target_resolution', '/events/13', '/events/18', '/events/20', '/final_world/refunds']}],
        'causal_limits': '同版本重复，非版本增益。',
    },
    'G11-W1-01-t5': {
        'assessment': '原失败是可观察的抽取/协议恢复失败，重试确有完整执行成功；不能把变化归因于新代码修复。',
        'failure_origins': ['original_agent_extraction_truncation', 'original_agent_repair_budget_exhaustion', 'original_judge_provider_402'],
        'change_interpretation': '全试验重跑而非只补 judge；原失败与成功重复都必须保留。相同代码、任务、初始world、模型配置下轨迹不同，不能据此证明稳定性提升。',
        'observations': ['原extract第2行 stop_reason=length，无submit_fields，fallback request_action=unknown、scope_month/plan=null。调查虽找到了Pro重复付款，target检查两次报 customer_action_unresolved。', '原两次客户问题是范围/授权确认，已有脚本只覆盖“询问编号”，matcher走condition_not_matched；这里可视为模拟客户覆盖局限，未见如G07般把既有when明显误用。', '原第16/24/28行 length无tool，产生3次no_tool_call，events/24 budget_exhausted、events/26 escalated。原账务无退款，judge402发生在结果评分阶段，不是该业务失败的起因。', '重试第2行正常submit_fields给出refund、2026-10、Pro；实际查账单、订阅、两张当月账单付款、完整退款；无须额外客户澄清即可形成限定Pro范围的提案。', '重试操作员批准v1，PAY-88213 4900分 send201→matched_success，最终账务有真实completed退款。', '重试调查中部分模型facts/summary把4900分记为“4900 CNY”；代码计算及公开最终执行回复正确49元。该中间单位不严谨不应包装成模型金额推理完全正确。'],
        'evidence': [{'run': 'original', 'file': 'agent-calls.jsonl', 'jsonl_lines': [2, 14, 16, 22, 24, 28]}, {'run': 'original', 'file': 'artifact.json', 'pointers': ['/snapshot/extraction_fallback', '/events/8', '/events/16', '/events/24', '/events/26', '/control_events/0', '/control_events/2']}, {'run': 'retry', 'file': 'agent-calls.jsonl', 'jsonl_lines': [2, 4, 8, 10, 12, 14, 16, 18]}, {'run': 'retry', 'file': 'artifact.json', 'pointers': ['/view/case/proposal/target_resolution', '/events/15', '/events/20', '/events/22', '/view/execution', '/final_world/refunds', '/reply']}],
        'causal_limits': '原fail+402被纳入用户指定恢复名单，新pass不是模型修复的对照证据；原Agent缺陷继续保留到badcase记录，不能被替换后的汇总遗忘。',
        'suggested_followup': '后续版本改进抽取截断的显式重试/恢复、输出预算与无最终回复升级路径；模拟客户应补充合理范围确认的契约覆盖，再单独登记验证。',
    },
    'G13-t4': {
        'assessment': '两笔全额退款具有真实客户确认、独立操作员审批和逐笔执行证据。',
        'failure_origins': [],
        'change_interpretation': '原、新均实际完成98元整批退款；原unknown来自judge402，重试恢复完整判定。',
        'observations': ['代码生成包含INV-2026-0142、PAY-88213/14各49元、总额98元的范围问句；脚本合法匹配，客户明确全退。', 'customer_scope_confirmed携带真实command_id、scope_digest和9800分；随后操作员审批proposal v1完整两项。', '两项分别send201并matched_success，各sends_recorded=1；final_world恰有两笔新completed退款。最终回复列明每笔金额及真实退款ID。'],
        'evidence': [{'run': 'retry', 'file': 'artifact.json', 'pointers': ['/events/10', '/events/13', '/events/21', '/events/26', '/events/28', '/events/33', '/events/35', '/control_events/3/proposal/items', '/view/execution/items', '/reply']}],
        'causal_limits': '中间另一次模型length在预算内恢复；此单次成功不说明截断问题已消失。',
    },
    'G14-t4': {
        'assessment': '重试正确解释编号冲突、真实澄清后无退款；原控制器unknown与Agent业务表现应分开。',
        'failure_origins': ['original_harness_identifier_conflict_guard_false_negative', 'original_customer_matcher_truncation', 'original_judge_provider_402'],
        'change_interpretation': '原问句已明确“对不上…实际记录在”，但词表guard不接受→matcher二次length；重试换成“不一致”后合法选脚本。没有修改guard，不是缺陷已修复。',
        'observations': ['原customer-calls第2行选择脚本0、指出冲突原文；control_events/0 attempts/0被identifier_conflict_not_explained拒绝，attempt1 length，最后发unknown。Agent仍closed/insufficient_information无退款。', '重试读取INV0101付款与PAY88213实际归属INV0142，公开解释两者不一致并调用ask_customer；script0一次有效匹配。', '客户回复“我现在核对不了，先不要退款。”后，Agent closed/insufficient_information，无proposal、execution或资金写入，最终明确两张账单不同。'],
        'evidence': [{'run': 'original', 'file': 'customer-calls.jsonl', 'jsonl_lines': [2, 4]}, {'run': 'original', 'file': 'artifact.json', 'pointers': ['/control_events/0', '/events/14']}, {'run': 'retry', 'file': 'agent-calls.jsonl', 'jsonl_lines': [10, 12, 14, 22, 24, 26]}, {'run': 'retry', 'file': 'artifact.json', 'pointers': ['/control_events/0', '/control_events/1', '/events/17', '/view/case', '/reply']}],
        'causal_limits': '原guard漏识别是harness问题；重试更换自然语言避开问题，不能称guard已修。',
    },
    'G29-t4': {
        'assessment': '等待审批时更正目标实际取消旧范围，重新生成v2并只批准、执行新范围。',
        'failure_origins': [],
        'change_interpretation': '原、新都正确退款PAY-88101，原judge402导致unknown；不是新增状态机修复。',
        'observations': ['原先INV0142的提案v1进入awaiting_approval后，客户command明确改为INV0101且原账单不退。', 'followup_classified=new_info → 重查INV0101 → 新proposal v2；实际审批记录仅v2，两个scope/fingerprint不同。', '只向PAY-88101发送一次4900分退款并matched_success；INV0142的两笔付款未新增退款。'],
        'evidence': [{'run': 'retry', 'file': 'artifact.json', 'pointers': ['/events/9', '/events/12', '/events/14', '/events/19', '/events/24', '/events/29', '/events/31', '/control_events/2', '/view/execution', '/final_world/refunds']}],
        'causal_limits': '这是实际更正目标路径，不是单纯改变评分标签。',
    },
}


def validate_refs(findings):
    total = 0
    for finding in findings:
        for ref in finding['assistant_review']['evidence']:
            run = RUN if ref['run'] == 'retry' else BASE
            path = run / 'trials' / finding['trial_id'] / ref['file']
            if 'pointers' in ref:
                obj = read(path)
                for pointer in ref['pointers']:
                    value = obj
                    for token in pointer.strip('/').split('/'):
                        token = token.replace('~1', '/').replace('~0', '~')
                        value = value[int(token)] if isinstance(value, list) else value[token]
                    total += 1
            for line in ref.get('jsonl_lines', []):
                json.loads(path.read_text().splitlines()[line - 1])
                total += 1
    return total


def main():
    registration = read(RUN / 'retry-registration.json')
    completion = read(RUN / 'retry-completion.json')
    assert registration['selected_trial_ids'] == EXPECTED
    assert completion['completed_trial_ids'] == EXPECTED
    assert completion['complete'] and completion['no_terminal_provider_error']
    assert not completion['source_drift'] and not completion['base_archive_changed']
    source_comparison = []
    for relative, expected in registration['source_digests'].items():
        old, new = BASE / 'source' / relative, RUN / 'source' / relative
        old_hash, new_hash = digest(old), digest(new)
        source_comparison.append({'path': relative, 'registered_sha256': expected,
                                  'original_sha256': old_hash, 'retry_sha256': new_hash,
                                  'match': old_hash == new_hash == expected})
    assert all(row['match'] for row in source_comparison)
    findings = []
    for trial in EXPECTED:
        original, retry = extract(BASE, trial), extract(RUN, trial)
        assert original['input_identity'] == retry['input_identity']
        assert not retry['provider_errors']
        note = NOTES[trial]
        note.update({'reviewer': 'assistant:/root/controlled_mutation_builder',
                     'review_status': 'assistant_evidence_review_complete',
                     'human_review': 'pending', 'automatic_labels_modified': False})
        findings.append({'trial_id': trial, 'case_id': trial.rsplit('-t', 1)[0],
                         'trial_index': int(trial.rsplit('-t', 1)[1]),
                         'same_frozen_task_and_initial_world': True,
                         'original': original, 'retry': retry, 'assistant_review': note})
    reference_count = validate_refs(findings)
    all_sources = [s for f in findings for side in ('original', 'retry')
                   for s in f[side]['sources'].values()]
    assert all(digest(Path(s['path'])) == s['sha256'] for s in all_sources)
    output = {
        'schema_version': 'B1-infra-retry-targeted-audit-1',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'status': 'nine_registered_slots_evidence_reviewed_human_pending',
        'reviewer': 'assistant:/root/controlled_mutation_builder', 'human_review': 'pending',
        'additional_model_calls': 0, 'source_or_frozen_results_modified': False,
        'sources': {name: source(RUN / name) for name in ['retry-registration.json', 'retry-completion.json', 'manifest.json', 'fixtures.json', 'results.jsonl', 'retry-wrapper-source.py']},
        'original_sources': {name: source(BASE / name) for name in ['manifest.json', 'fixtures.json', 'results.jsonl']},
        'audit_script': source(Path(__file__)), 'extraction_helper': source(ROOT / 'analysis/B1/extract_targeted_audit.py'),
        'selection': {'trial_ids': EXPECTED, 'count': 9, 'complete_count': 9, 'missing': [],
                      'rule': registration['selection_rule'], 'replacement_rule': registration['replacement_rule'],
                      'amendment': registration['protocol_amendment'], 'no_best_of': True},
        'frozen_identity': {'model': read(RUN / 'manifest.json')['model'],
                            'all_130_frozen_sources_equal': len(source_comparison) == 130 and all(r['match'] for r in source_comparison),
                            'source_comparison': source_comparison,
                            'task_and_initial_world_equal_pairs': 9},
        'selection_only_automatic_counts': {'original': dict(Counter(f['original']['automatic']['overall'] for f in findings)),
                                             'retry': dict(Counter(f['retry']['automatic']['overall'] for f in findings))},
        'interpretation_limits': [
            '九条是事后用户授权的HTTP402恢复集合，不是独立随机测试样本；不计算总体提升率或以8/9估计发布质量。',
            '每个槽位完整重跑一次，包含Agent/客户/裁判；不是原轨迹只补裁判。原Agent失败可能因同版本重复而变化，尤其G11。',
            '原、新证据并排保留；用户授权替换逻辑槽位不等于删除原badcase。G07自动fail不改分、不再重试。',
            '自动标签保持冻结；审阅者为助手，所有人工审核字段pending；模型语义裁判pass不等于人工确认。',
            'G07存在模拟器语义错配，G14原有guard误拒；因果污染单列，未反事实重跑证明净影响。',
        ],
        'validation': {'json_pointer_and_jsonl_references_checked': reference_count,
                       'all_evidence_hashes_still_match_after_read': True,
                       'all_registered_sources_match_both_snapshots': True},
        'findings': findings,
    }
    OUT.with_suffix('.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    lines = [
        '# B1 九条基础设施重试：定向证据复核', '',
        '审阅者：assistant:/root/controlled_mutation_builder；人工审核：**pending**。仅离线读取归档，未调用模型、未改源码/题库/原始结果。', '',
        '用户授权重试原 HTTP402 的九个逻辑槽位，每槽完整重跑一次并全部保留。九条均有完整记录、无终端供应商错误；自动标签为8 pass、1 fail（G07-t4）。这是该恢复集合的计数，不是整体成功率或版本提升率。', '',
        '原、新130个冻结源文件逐个hash一致，九条任务及初始world一致；模型配置同为DeepSeek deepseek-flash、temperature=0、max_tokens=4096。全试验重跑仍可能产生不同轨迹，不能把变化当作新代码收益。', '',
        f'原运行：`{BASE}`。重试：`{RUN}`。完整来源SHA256、JSON指针、模型调用文件与行号见 [JSON报告]({OUT.with_suffix(".json")})。', '',
        '| 固定槽位 | 原自动→重试自动 | 本次主要证据结论 |',
        '|---|---|---|',
    ]
    for f in findings:
        lines.append(f'| {f["trial_id"]} | {f["original"]["automatic"]["overall"]} → {f["retry"]["automatic"]["overall"]} | {f["assistant_review"]["assessment"]} |')
    lines += ['', '下列判断独立于自动标签，不改变合并规则或原评分。G07需保留Agent缺陷及harness错配双重归因；G11原真实失败继续作为badcase留存。', '']
    for f in findings:
        note = f['assistant_review']
        lines += [f'## {f["trial_id"]}', '', note['change_interpretation'], '']
        lines += ['- ' + item for item in note['observations']]
        lines += ['', '归因边界：' + note['causal_limits'], '']
        if note.get('suggested_followup'):
            lines += ['后续建议（未实施）：' + note['suggested_followup'], '']
        for ref in note['evidence']:
            run = RUN if ref['run'] == 'retry' else BASE
            path = run / 'trials' / f['trial_id'] / ref['file']
            positions = ', '.join(ref.get('pointers', [])) or 'JSONL行 ' + ', '.join(map(str, ref.get('jsonl_lines', [])))
            lines += [f'- 证据 `{ref["run"]}`：[ {ref["file"]} ]({path})；{positions}']
        lines += ['']
    frozen_controls = RUN / 'source/src/billingops/evaluation/v2_controls.py'
    lines += ['## 测量局限与后续处理', '',
              f'- G07 客户matcher在普通目标确认中引用“全退/合计金额”条件。冻结[匹配器提示]({frozen_controls}:25)包含专门的两笔多扣规则；脚本1本身没有该条件，审阅判断为语义错配。是否修复模拟器后可避免最终升级未知。',
              f'- G14原问句“对不上”被[冲突词表]({frozen_controls}:123)漏识别。重试使用“不一致”后通过，只是自然语言路径变化，不能说控制器已修复。',
              '- G07最终回复缺失导致response unknown；其澄清金额错误属于当前final-only rubric的覆盖缺口，单列为证据发现，不擅自重打分。',
              '- 不再重试G07，不按成功结果另选样本；八条pass也不抵销G07失败或原G11失败。',
              f'- 校验：{reference_count}处JSON指针/JSONL引用可解析；读取前后证据hash一致；九条固定槽位无缺失。所有案例人工审核仍为pending。', '']
    OUT.with_suffix('.md').write_text('\n'.join(lines))
    print(json.dumps({'json': str(OUT.with_suffix('.json')), 'markdown': str(OUT.with_suffix('.md')),
                      'cases': len(findings), 'references_checked': reference_count,
                      'frozen_sources_equal': len(source_comparison),
                      'automatic_counts': output['selection_only_automatic_counts']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
