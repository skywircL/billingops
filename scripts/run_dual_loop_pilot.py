#!/usr/bin/env python3
"""Registered paired diagnostic, not a full benchmark or release result."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from billingops.evaluation.artifacts import digest, file_digest, source_manifest, write_json
from billingops.evaluation.v2_runner import make_plan, run_bundle, utc_now

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    'G01', 'G01-U1-48', 'G01-U1-50', 'G02', 'G03', 'G04-W1-01',
    'G06-L1-01', 'G07', 'G08', 'G08-F3-01', 'G09', 'G09-U1-50',
    'G11-W1-01', 'G12', 'G13-L1-01', 'G14', 'G15-W1-01', 'G16',
    'G17', 'G18-W1-01', 'G19-S1-01', 'G21', 'G23', 'G25-W1-01',
    'G26', 'G27-W1-01', 'G28', 'G29', 'G31', 'G32',
]


def run(out: Path, workers: int = 8) -> dict:
    bundle_path = ROOT / 'data/tasks/dynamic-v2/controlled-coverage-v1.json'
    bundle = json.loads(bundle_path.read_text())
    plans = {revision: make_plan(bundle, mode='pilot', selected=CASES, workers=workers, agent_revision=revision)
             for revision in ('B1', 'B2')}
    assert len(CASES) == len(set(CASES)) == 30
    assert plans['B1']['evaluation_versions'] == plans['B2']['evaluation_versions']
    out.mkdir(parents=True, exist_ok=False)
    source = source_manifest()['source_digests']
    registration = {'registered_at': utc_now(), 'type': 'exposed_development_paired_diagnostic',
        'sample_rationale': 'Known Agent/protocol/reply defects plus normal refunds, price review, permission, withdrawal, target correction and lost-receipt controls',
        'case_ids': CASES, 'trials_per_case_per_revision': 1, 'total_trials': 60,
        'order': ['B1', 'B2'], 'plans': plans, 'bundle_sha256': file_digest(bundle_path),
        'source_digests': source, 'script_sha256': file_digest(Path(__file__)),
        'retry_policy': 'No automatic retry or selective replacement; stop pair on provider terminal failure or source drift',
        'interpretation': 'New controls and new grader apply to BOTH revisions. Not comparable to archived B1 80.21%; one repeat does not establish stability or generalization.'}
    write_json(out / 'registration.json', registration)
    results = {}
    for revision in registration['order']:
        if source_manifest()['source_digests'] != source or file_digest(bundle_path) != registration['bundle_sha256']:
            stopped = {'status': 'stopped_source_drift', 'before_revision': revision}
            write_json(out / 'incomplete-pair.json', stopped)
            return stopped
        summary = run_bundle(bundle, out / revision, mode='pilot', selected=CASES, workers=workers, agent_revision=revision)
        if summary.get('status') == 'stopped_provider_terminal_error':
            stopped = {'status': 'stopped_provider_terminal_error', 'revision': revision, 'summary': summary}
            write_json(out / 'incomplete-pair.json', stopped)
            return stopped
        results[revision] = [json.loads(line) for line in (out / revision / 'results.jsonl').read_text().splitlines()]
    unchanged = source_manifest()['source_digests'] == source
    if not unchanged:
        stopped = {'status': 'stopped_source_drift', 'after_revision': 'B2'}
        write_json(out / 'incomplete-pair.json', stopped)
        return stopped
    indexed = {revision: {r['case_id']: r for r in rows} for revision, rows in results.items()}
    pairs = []
    for cid in CASES:
        pairs.append({'case_id': cid, **{revision: {'status': indexed[revision][cid]['status'],
            'artifact': indexed[revision][cid]['artifact'], 'artifact_sha256': indexed[revision][cid]['artifact_sha256'],
            'runtime_failure': indexed[revision][cid].get('runtime_failure'),
            'failed_or_unknown': [{'layer': layer, 'check': key, 'value': value}
                for layer in ('outcome', 'behavior', 'guardrails')
                for key, value in indexed[revision][cid]['scoring'].get(layer, {}).get('checks', {}).items() if value is not True]
                + [{'layer': 'response', 'check': c['id'], 'value': c['status'], 'reason': c.get('reason')}
                   for c in indexed[revision][cid]['response'].get('claims', []) if c['status'] != 'pass']}
            for revision in registration['order']}})
    counts = {revision: dict(Counter(row['status'] for row in rows)) for revision, rows in results.items()}
    report = {'status': 'completed', 'finished_at': utc_now(), 'source_unchanged': unchanged,
              'registration_sha256': digest(registration), 'counts': counts, 'pairs': pairs,
              'estimated_cost_usd': {v: sum(r[f'{role}_metrics']['cost_usd_estimated'] for r in rows for role in ('agent', 'customer', 'judge')) for v, rows in results.items()},
              'limits': ['30 exposed cases, one trial each; diagnosis only', 'New semantic judge remains human-calibration pending', 'No new full-benchmark, production or safety-rate claim']}
    write_json(out / 'report.json', report)
    lines = ['# B1/B2：双 Loop 修复后的成对诊断', '',
        '固定30题，两版本各1次；同一题库、模型、新评分器和新模拟客户。不是完整128题评测，不能与旧80.21%直接相减。', '',
        '|版本|pass|fail|unknown|', '|---|---:|---:|---:|']
    lines += [f"|{v}|{counts[v].get('pass', 0)}|{counts[v].get('fail', 0)}|{counts[v].get('unknown', 0)}|" for v in registration['order']]
    lines += ['', '|Case|B1|B2|', '|---|---|---|']
    lines += [f"|{r['case_id']}|{r['B1']['status']}|{r['B2']['status']}|" for r in pairs]
    lines += ['', '所有失败和未知均保留，未挑选成功重试。源码读取前后未漂移；逐条断言、证据路径、费用和登记哈希见 report.json。新裁判仍待人工校准。']
    (out / 'report.md').write_text('\n'.join(lines) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=8)
    args = parser.parse_args()
    report = run(args.out.resolve(), args.workers)
    print(json.dumps({k: v for k, v in report.items() if k != 'pairs'}, ensure_ascii=False))
    raise SystemExit(0 if report['status'] == 'completed' else 2)
