#!/usr/bin/env python3
"""Compare frozen/patched scope computation, not model or end-to-end success.

Ground truth comes from the manually specified development asset. The replay
supplies identical complete read evidence and extraction labels to both paths.
It performs no network requests and creates no refunds.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path
import re

from billingops.investigation.b1 import assess_refund
from billingops.investigation.evidence import build_index
from billingops.money import evaluate_invoice_refund

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evidence_for(task):
    world = task['world']
    rows = []
    for invoice in world['invoices']:
        if invoice['customer_id'] != task['customer_id']:
            continue
        rows += [
            {'tool': 'get_invoice', 'args': {'invoice_id': invoice['id']}, 'ok': True, 'data': invoice},
            {'tool': 'list_payments', 'args': {'invoice_id': invoice['id']}, 'ok': True,
             'data': [p for p in world['payments'] if p['invoice_id'] == invoice['id']]},
        ]
    rows.append({'tool': 'list_customer_refunds', 'args': {}, 'ok': True,
                 'data': [r for r in world['refunds'] if r['customer_id'] == task['customer_id']]})
    return rows


def observe(resolution, verdict):
    permitted = resolution['ok'] and resolution.get('request_action') == 'refund'
    return {
        'resolution_ok': resolution['ok'], 'reason': resolution['reason'],
        'request_action': resolution.get('request_action'),
        'requested_payment_ids': resolution.get('requested_payment_ids', []),
        'authorization_scope': resolution.get('authorization_scope'),
        'verdict': verdict['reason'] if isinstance(verdict, dict) else verdict.reason,
        'items': [dict(item) if isinstance(item, dict) else asdict(item) for item in (verdict.get('items', []) if isinstance(verdict, dict) else verdict.items)] if permitted else [],
    }


def matches(observed, expected, task, payment_ids):
    mode = expected['mode']
    actual = {i['payment_id'] for i in observed['items']}
    if mode in {'unresolved', 'permission_refused'}:
        return not observed['resolution_ok'] and not actual
    if mode == 'read_only':
        return observed['request_action'] == 'inquire' and not actual
    if mode == 'explicit_payment':
        if not observed['resolution_ok'] or set(observed['requested_payment_ids']) != set(payment_ids):
            return False
        # A named payment may already be fully refunded: scope must remain
        # selected, while no alternate transaction may be substituted.
        available = {r['payment_id'] for r in task['expected_refunds']}
        desired = set(payment_ids) if task.get('interaction_probe') else available
        return actual == desired
    return observed['resolution_ok'] and actual == {r['payment_id'] for r in task['expected_refunds']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=ROOT/'data/tasks/authorization-scope-v1.json')
    parser.add_argument('--baseline', type=Path, default=ROOT/'analysis/authorization-scope-v1/baseline-source/target.py')
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('billingops.investigation._frozen_authorization_target', args.baseline)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    bundle = json.loads(args.bundle.read_text())
    records = []
    for task in bundle['tasks']:
        expected = task['authorization_expectations']
        role = 'locator' if expected['mode'] == 'invoice_all' else 'exclusive'
        action = 'inquire' if expected['mode'] == 'read_only' else 'refund'
        roles = ['exclusive', 'locator', 'unspecified'] if expected['mode'] in {'explicit_payment', 'unresolved'} else [role]
        turns = task.get('interaction_probe', {}).get('turns') or [
            {'customer_text': task['user_input'], 'authorized_payment_ids': expected['payment_ids']}]
        utterances = []
        for turn_index, turn in enumerate(turns):
            utterances.append({'text': turn['customer_text'], 'source': 'opening' if turn_index == 0 else 'customer_followup', 'command_id': None if turn_index == 0 else f'probe-{turn_index}'})
            for extracted_role in roles:
                evidence = evidence_for(task)
                state = {'customer_id': task['customer_id'], 'text': utterances[0]['text'],
                         'as_of': task['as_of'], 'evidence': evidence, 'clarifications': [],
                         'extra_info': [row['text'] for row in utterances[1:]],
                         'authorization_utterances': deepcopy(utterances),
                         'extracted': {'request_action': action, 'payment_reference_role': extracted_role}}
                idx = build_index(evidence)
                invoice_id = next(iter(task['author_scope']['invoice_ids']), 'INV-2026-0142')
                old_resolution = baseline.resolve_invoice_target(state, idx, invoice_id, as_of=task['as_of'])
                old_verdict = evaluate_invoice_refund(customer_id=task['customer_id'], invoice=idx.invoices.get(invoice_id),
                    payments=idx.payments_by_invoice.get(invoice_id, []), refunds_by_payment=idx.refunds_by_payment,
                    requested_payment_ids=old_resolution.get('requested_payment_ids') if old_resolution.get('payment_reference_role') == 'exclusive' else None)
                new_resolution, new_verdict = assess_refund(state, idx, {'target_invoice_id': invoice_id})
                old, new = observe(old_resolution, old_verdict), observe(new_resolution, new_verdict)
                ids = turn.get('authorized_payment_ids', expected['payment_ids'])
                records.append({'case_id': task.get('case_id', task['candidate_id']), 'turn': turn_index,
                    'extracted_role': extracted_role, 'input': turn['customer_text'],
                    'expected': {**expected, 'payment_ids': ids},
                    'before': old, 'after': new,
                    'before_matches': matches(old, expected, task, ids),
                    'after_matches': matches(new, expected, task, ids)})
    mining = []
    for path in [ROOT/'data/tasks/dynamic-v2/gold-fixtures-v0.2.json', ROOT/'data/tasks/dynamic-v2/controlled-coverage-v1.json']:
        old_tasks = json.loads(path.read_text())['tasks']
        hits = [t.get('case_id', t['candidate_id']) for t in old_tasks
                if len(set(re.findall(r'PAY-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*', t['user_input']))) > 1
                and re.search(r'只|仅|only|do not|不要|不退', t['user_input'], re.I)]
        mining.append({'path': str(path.relative_to(ROOT)), 'sha256': sha(path), 'tasks': len(old_tasks),
                       'multi_payment_mention_plus_literal_restriction': hits,
                       'method': 'lexical coverage inventory, not proof of complete semantic coverage'})
    report = {'kind': 'deterministic_scope_replay_not_agent_benchmark', 'suite_sha256': sha(args.bundle),
              'baseline_target_sha256': sha(args.baseline),
              'current_source_hashes': {str(p.relative_to(ROOT)): sha(p) for p in [
                  ROOT/'src/billingops/investigation/authorization.py', ROOT/'src/billingops/investigation/target.py',
                  ROOT/'src/billingops/investigation/b1.py', ROOT/'src/billingops/money.py']},
              'conditions': 'Same world, utterances, extraction labels and existing money rules; only authorization resolution differs. No model, approval or payment execution.',
              'counts': {'cases': len(bundle['tasks']), 'scope_checks': len(records),
                         'before_matches': sum(r['before_matches'] for r in records), 'after_matches': sum(r['after_matches'] for r in records)},
              'old_dataset_inventory': mining, 'records': records,
              'limitations': ['Development-exposed synthetic labels; not independently human reviewed.',
                              'Role and turn probes share cases; their count is not independent scenario count.',
                              'Only front-end scope computation. Graph integration and real Billing tests are reported separately.']}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write('\n')
    print(json.dumps(report['counts']))
    for row in records:
        if not row['after_matches']:
            print(json.dumps({k: row[k] for k in ['case_id', 'turn', 'extracted_role', 'after']}, ensure_ascii=False))
    raise SystemExit(0 if all(r['after_matches'] for r in records) else 1)


if __name__ == '__main__':
    main()
