"""Read-only safety evidence for 735 original slots plus nine fixed full retries.

Never imports Agent/grader or calls a model. Original archives remain intact.
The source run remains provider-interrupted; the derived population is a
user-authorized infrastructure-recovery amendment, not a fresh unified run.
"""
from collections import Counter
from datetime import datetime, timezone
import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = Path(__file__).with_name('B1-unified-2-safety-audit-source.py')
spec = importlib.util.spec_from_file_location('frozen_independent_safety_audit', HELPER)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
HELPER_SHA256 = '3133f9ca30d835c5d3ca7fe1cdd8c4d021fb3dfb0426a8df555c0926c0bfccb2'


def load_rows(run):
    return [json.loads(s) for s in (run / 'results.jsonl').read_text().splitlines() if s.strip()]


def key(row):
    return f"{row['candidate_id']}-t{row['trial_index']}"


def errors(row, statuses=(402,)):
    return [{'role': role, **e} for role in ('agent', 'customer', 'judge') for e in row.get(role + '_metrics', {}).get('errors', []) if e.get('status') in statuses]


def costs(rows):
    out = {}
    for role in ('agent', 'customer', 'judge'):
        values = [r.get(role + '_metrics', {}).get('cost_usd_estimated') for r in rows]
        known = [v for v in values if type(v) in (int, float)]
        out[role] = {'known_estimated_usd': sum(known), 'unknown_cost_rows': len(values) - len(known)}
    return out


def validate_row(run, row, tasks, manifest):
    folder = run / 'trials' / key(row)
    assert audit.load(folder / 'result.json') == row
    assert audit.sha(folder / 'artifact.json') == row['artifact_sha256']
    artifact = audit.load(folder / 'artifact.json')
    task = tasks[row['candidate_id']]
    assert artifact['task'] == task
    assert audit.digest(task) == manifest['case_profiles'][row['candidate_id']]['task_sha256']
    assert artifact.get('fault_exposure') == row.get('fault_exposure') and artifact.get('attack_exposure') == row.get('attack_exposure')
    ref = row['response_grade_artifact']
    grade_path = folder / 'response-grade.json'
    assert audit.sha(grade_path) == ref['sha256']
    grade = audit.load(grade_path)
    assert grade['raw_artifact_sha256'] == row['artifact_sha256'] and grade['response_grade'] == row['response']
    assert grade['model_metrics'] == row['judge_metrics']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, default=ROOT / 'runs/dynamic-v2-B1-unified-2')
    parser.add_argument('--retry', type=Path, default=ROOT / 'runs/dynamic-v2-B1-unified-2-retry-1')
    parser.add_argument('--out-prefix', type=Path, default=Path(__file__).with_name('B1-unified-2-retry-1-merged-safety-audit'))
    args = parser.parse_args()
    base, retry = args.base.resolve(), args.retry.resolve()
    assert audit.sha(HELPER) == HELPER_SHA256, 'independent audit helper changed'
    manifest, fixtures = audit.load(base / 'manifest.json'), audit.load(base / 'fixtures.json')
    registration, completion = audit.load(retry / 'retry-registration.json'), audit.load(retry / 'retry-completion.json')
    original, fresh = load_rows(base), load_rows(retry)
    expected = {f'{cid}-t{i}' for cid, n in manifest['planned_trials'].items() for i in range(n)}
    selected = sorted(key(r) for r in original if errors(r))
    assert len(expected) == len(original) == 744 and len(manifest['planned_trials']) == 128
    assert len({key(r) for r in original}) == 744 and {key(r) for r in original} == expected
    assert len(selected) == len(set(selected)) == 9
    assert registration['schema_version'] == completion['schema_version'] == 'controlled-v2-infra-retry-1'
    assert Path(registration['base_run']).resolve() == base
    assert registration['selected_trial_ids'] == selected and set(registration['dispatch_order']) == set(selected) and len(registration['dispatch_order']) == 9
    assert registration['retry_policy'] == 'one_full_run_trial_per_selected_slot_no_best_of'
    assert registration['execution_kind'] == 'full_run_trial'
    assert completion['complete'] is True and completion['exact_selected_slots'] is True and completion['no_terminal_provider_error'] is True
    assert completion['status'] == 'complete_without_terminal_provider_errors'
    assert completion['recorded_trials'] == len(fresh) == 9 and len({key(r) for r in fresh}) == 9 and {key(r) for r in fresh} == set(selected)
    assert completion['selected_trial_ids'] == selected and set(completion['completed_trial_ids']) == set(selected)
    assert not completion['terminal_provider_errors'] and not completion['source_drift'] and not completion['base_archive_changed']
    assert completion['post_run_working_source_digests'] == registration['working_source_digests']
    assert completion['registration_sha256'] == audit.sha(retry / 'retry-registration.json')
    assert completion['results_sha256'] == audit.sha(retry / 'results.jsonl')
    assert all(not errors(r, (401, 402)) for r in fresh)
    assert (base / 'provider-stop.json').exists() and (base / 'incomplete-run.json').exists() and not (base / 'summary.json').exists()
    for name, field in [('manifest.json', 'base_manifest_sha256'), ('results.jsonl', 'base_results_sha256'), ('fixtures.json', 'base_fixture_sha256'), ('provider-stop.json', 'base_provider_stop_sha256')]:
        assert audit.sha(base / name) == registration[field]
    assert audit.sha(retry / 'manifest.json') == registration['retry_manifest_sha256'] == audit.sha(base / 'manifest.json')
    assert audit.sha(retry / 'fixtures.json') == audit.sha(base / 'fixtures.json')
    assert registration['source_digests'] == manifest['source']['source_digests']
    for rel, digest in registration['source_digests'].items():
        assert audit.sha(base / 'source' / rel) == audit.sha(retry / 'source' / rel) == digest
    assert audit.sha(retry / registration['wrapper_source']) == registration['wrapper_sha256']
    tasks = {t.get('case_id', t['candidate_id']): t for t in fixtures['tasks']}
    assert audit.digest(fixtures) == manifest['fixture_sha256']
    raw_402 = set()
    for folder in (base / 'trials').iterdir():
        for path in folder.glob('*-calls.jsonl'):
            if any((r := json.loads(line)).get('kind') == 'error' and r.get('status') == 402 for line in path.read_text().splitlines() if line.strip()):
                raw_402.add(folder.name)
    assert raw_402 == set(selected), 'selected errors differ from original raw model-call evidence'
    assert {p.parent.name for p in (retry / 'trials').glob('*/result.json')} == set(selected)
    base_by_id, fresh_by_id = {key(r): r for r in original}, {key(r): r for r in fresh}
    merged = [fresh_by_id.get(key(r), r) for r in original]
    assert len(merged) == 744 and {key(r) for r in merged} == expected
    trials, provenance, retired = [], [], []
    for row in original:
        validate_row(base, row, tasks, manifest)
        trial = key(row)
        source, chosen_row = (retry, fresh_by_id[trial]) if trial in selected else (base, row)
        if source == retry:
            validate_row(retry, chosen_row, tasks, manifest)
            audit.RUN = base
            retired.append(audit.audit_trial(tasks[row['candidate_id']], row))
        audit.RUN = source
        value = audit.audit_trial(tasks[chosen_row['candidate_id']], chosen_row)
        value['provenance'] = {'logical_trial': trial, 'source': 'retry' if source == retry else 'base', 'run': str(source),
            'artifact_path': str(source / 'trials' / trial / 'artifact.json'), 'artifact_sha256': chosen_row['artifact_sha256'],
            'original_artifact_sha256': row['artifact_sha256'], 'original_status': row['status'], 'selected_status': chosen_row['status']}
        trials.append(value)
        provenance.append(value['provenance'])
    summary = audit.aggregate(trials, manifest)
    retry_trials = [t for t in trials if t['provenance']['source'] == 'retry']
    report = {'schema_version': 'B1-retry-merged-independent-safety-audit-v1', 'created_at': datetime.now(timezone.utc).isoformat(),
        'run_validity': 'user_authorized_infrastructure_recovery_merged_evidence_not_fresh_unified_run',
        'base_run_validity': 'provider_terminal_error_interrupted_preserved', 'base_run': str(base), 'retry_run': str(retry),
        'scope': {'registered_slots': 744, 'original_retained_slots': 735, 'replacement_slots': 9, 'selected_original_trial_ids': selected,
            'original_files_modified': False, 'scores_modified': False, 'new_model_calls_by_audit': 0, 'environment': 'isolated simulated Billing per full trial, not production funds'},
        'human_review': 'pending', 'reviewer': 'assistant_independent_session_not_human',
        'audit_program_sha256': audit.sha(Path(__file__)), 'audit_helper_sha256': audit.sha(HELPER),
        'base_manifest_sha256': audit.sha(base / 'manifest.json'), 'base_results_sha256': audit.sha(base / 'results.jsonl'),
        'retry_registration_sha256': audit.sha(retry / 'retry-registration.json'), 'retry_completion_sha256': audit.sha(retry / 'retry-completion.json'),
        'retry_results_sha256': audit.sha(retry / 'results.jsonl'), 'frozen_source_files_verified_each_run': len(registration['source_digests']),
        'costs': {'original_744_attempts': costs(original), 'new_nine_attempts_additional': costs(fresh), 'retired_original_nine': costs([base_by_id[t] for t in selected]),
            'derived_744_selected_attempts': costs(merged), 'actual_all_753_attempts': costs(original + fresh),
            'interpretation': 'Original failed-provider attempts remain incurred cost; original+retry cost must not be replaced by derived-population cost.'},
        **summary, 'provenance': provenance, 'trials': trials,
        'retired_original_attempts': {'count': 9, 'included_in_merged_denominator': False, 'trials': retired,
            'logged_send_attempts': sum(t['refund_sending_count'] or 0 for t in retired), 'added_mock_refund_rows': sum(t['added_refund_count'] or 0 for t in retired)},
        'retry_attempts_only': {'count': 9, 'trials': [t['trial'] for t in retry_trials], 'logged_send_attempts': sum(t['refund_sending_count'] or 0 for t in retry_trials), 'added_mock_refund_rows': sum(t['added_refund_count'] or 0 for t in retry_trials)},
        'limitations': ['The 735+9 population follows a later explicit user amendment, not the original no-selective-resume preregistration.',
            'Each selected 402 slot was replaced once regardless of pass, fail, or unknown; this is not best-of selection.',
            'The base run remains provider-interrupted and no original score was repaired.',
            'Replay/retry simulations reset Billing per attempt; retired and replacement mock refund rows are not duplicate charges in one business world.',
            'Observed zero incidents is not 100% safety; privacy unknown remains unknown and semantic grades need human review.',
            'This audit presents evidence counts, not release approval or a confirmatory B0-to-B1 effectiveness claim.']}
    out = args.out_prefix.resolve()
    assert not out.is_relative_to(base) and not out.is_relative_to(retry)
    with out.with_suffix('.json').open('x') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write('\n')
    lines = ['# B1 基础设施重试合并：独立安全证据审计', '',
        '**后续授权的 735 条原记录 + 9 条固定重试；不是新跑的一轮 744 次，也不改变原运行 provider-interrupted 身份。**', '',
        '九条由原始所有 HTTP 402 自动推导，逐条完整重试一次，不按结果选择。业务失败与 unknown 均保留；每条来源和新旧证据 SHA-256 见 JSON。', '',
        '## 合并后的实际执行机会', '', '```json', json.dumps(report['financial_opportunities'], ensure_ascii=False, indent=2), '```', '',
        f"独立安全重建与原评分分歧 {len(report['independent_mismatches'])} 条；审批/回执/幂等证据问题 {len(report['strict_execution_issues'])} 条。", '',
        '| 安全项 | 观察事件 | 观察未发生 | unknown |', '| --- | ---: | ---: | ---: |']
    for name, count in report['archived_guardrails'].items():
        lines.append(f"| {name} | {count['observed_incident']} | {count['observed_absent']} | {count['unknown']} |")
    lines += ['', '## 故障实际消费', '', '```json', json.dumps(report['fault_reconstruction'], ensure_ascii=False, indent=2), '```', '',
        '## 攻击实际触达', '', '```json', json.dumps(report['attack_exposure'], ensure_ascii=False, indent=2), '```', '',
        '## 成本', '', '```json', json.dumps(report['costs'], ensure_ascii=False, indent=2), '```', '',
        '旧九次已消耗的调用费用仍在实际成本内；被替换的模拟退款行单列，不能与新九次混算为同一逻辑试验的重复动作。', '',
        '## 限制', ''] + ['- ' + text for text in report['limitations']]
    with out.with_suffix('.md').open('x') as f:
        f.write('\n'.join(lines) + '\n')
    print(json.dumps({'report': str(out.with_suffix('.md')), 'financial_opportunities': report['financial_opportunities'],
        'independent_mismatches': report['independent_mismatches'], 'strict_execution_issues': report['strict_execution_issues'],
        'privacy': report['archived_guardrails']['privacy_disclosure'], 'additional_retry_cost': report['costs']['new_nine_attempts_additional']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
