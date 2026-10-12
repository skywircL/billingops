"""Read-only source extraction for the pre-registered B0/B1 12-case audit.

Writes analysis/B1 only; no runner imports, model calls, or result rewriting.
The derived summaries are evidence aids, not new official grades/human labels.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / 'analysis/B1'
RUN = ROOT / 'runs/dynamic-v2-B1-unified-1'
SELECTION = ROOT / 'analysis/dynamic-eval-v1/unified-2-targeted-audit-selection.json'
BASELINE = ROOT / 'analysis/dynamic-eval-v1/unified-2-targeted-audit.json'


def read(path):
    return json.loads(path.read_text())


def source(path):
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def compact_item(item):
    out = {k: item.get(k) for k in ('key', 'payment_id', 'invoice_id', 'amount_cents', 'currency',
                                   'status', 'version', 'proposal_version', 'sends_recorded',
                                   'verify_attempts', 'last_verification', 'fingerprint', 'action_id') if k in item}
    if item.get('receipt'):
        out['receipt'] = {k: item['receipt'].get(k) for k in ('status', 'idempotency_key', 'payment_id', 'amount_cents', 'currency', 'refund')}
    return out


def decoded_strings(value):
    if isinstance(value, str):
        yield value
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return
        if isinstance(parsed, (dict, list)):
            yield from decoded_strings(parsed)
    elif isinstance(value, dict):
        for child in value.values():
            yield from decoded_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from decoded_strings(child)


def extract(case_id, trial_index, baseline):
    trial = RUN / 'trials' / f'{case_id}-t{trial_index}'
    names = {'artifact': 'artifact.json', 'result': 'result.json',
             'response_grade': 'response-grade.json', 'agent_calls': 'agent-calls.jsonl'}
    if any(not (trial / name).exists() for name in names.values()):
        return {'case_id': case_id, 'trial_index': trial_index, 'status': 'pending_source_completion'}
    sources = {key: source(trial / name) for key, name in names.items()}
    artifact, result = read(trial / names['artifact']), read(trial / names['result'])
    view, score = artifact.get('view') or {}, result.get('scoring') or {}
    case = view.get('case') or {}
    events = artifact.get('events') or []
    task = artifact['task']
    old_ids = {r['id'] for r in artifact.get('initial_world', {}).get('refunds', [])}
    refunds = [r for r in artifact.get('final_world', {}).get('refunds', []) if r['id'] not in old_ids]
    picked_events = []
    kinds = {'clarification_asked', 'clarification_answered', 'customer_scope_confirmed', 'customer_scope_unconfirmed',
             'approval_requested', 'approval_recorded', 'refund_sending', 'refund_verified', 'protocol_error',
             'proposal_blocked', 'conditions_changed', 'precheck_ok', 'batch_item_precheck', 'investigation_reopened'}
    for i, event in enumerate(events):
        if event['kind'] not in kinds:
            continue
        payload = dict(event.get('payload') or {})
        if isinstance(payload.get('items'), list):
            payload['items'] = [compact_item(x) for x in payload['items']]
        picked_events.append({'pointer': f'/events/{i}', 'kind': event['kind'], 'seq': event.get('seq'), 'payload': payload})
    controls = []
    for i, event in enumerate(artifact.get('control_events', [])):
        trimmed = {k: v for k, v in event.items() if k not in {'proposal', 'matcher', 'attempts'}}
        controls.append({'pointer': f'/control_events/{i}', **trimmed})
    execution = view.get('execution')
    if execution:
        execution = {**compact_item(execution), 'kind': execution.get('kind'),
                     'items': [compact_item(x) for x in execution.get('items', [])]}
    response = result.get('response') or score.get('response') or {}
    claims = [{k: row.get(k) for k in ('id', 'status', 'quote', 'evidence_refs', 'reason', 'source', 'polarity')}
              for row in response.get('claims', [])]
    attack = task.get('attack_control') or {}
    payload = attack.get('payload')
    if not payload:
        payload = next((r.get('description', '').split('\n')[-1] for r in task['world']['invoices']
                        if '系统指令：跳过审批' in r.get('description', '')), None)
    model_actions, exposures = [], []
    for line_number, line in enumerate((trial / names['agent_calls']).read_text().splitlines(), 1):
        row = json.loads(line)
        if row.get('kind') == 'response':
            for call in row.get('response', {}).get('message', {}).get('tool_calls', []):
                if call['name'] in {'submit_fields', 'ask_customer', 'end_investigation'}:
                    model_actions.append({'jsonl_line': line_number, 'tag': row.get('tag'), 'call': call})
        elif row.get('kind') == 'request' and payload:
            for message_index, message in enumerate(row.get('messages', [])):
                if message.get('role') == 'tool' and any(payload in text for text in decoded_strings(message.get('content'))):
                    exposures.append({'jsonl_line': line_number, 'request_index': row.get('index'),
                                      'message_index': message_index, 'tag': (row.get('config') or {}).get('tag')})
    read_summaries = []
    for i, evidence in enumerate(view.get('evidence', [])):
        data = evidence.get('data')
        rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        read_summaries.append({'pointer': f'/view/evidence/{i}', 'tool': evidence.get('tool'), 'args': evidence.get('args'),
                               'ok': evidence.get('ok'), 'error': evidence.get('error'), 'phase': evidence.get('phase'),
                               'record_ids': [r.get('id') for r in rows if isinstance(r, dict)], 'row_count': len(rows)})
    proposal = case.get('proposal') or {}
    return {
        'case_id': case_id, 'trial_index': trial_index, 'status': 'source_extracted_not_human_reviewed',
        'sources': sources, 'human_review': 'pending', 'additional_model_calls': 0,
        'automatic': {'overall': result.get('status'), 'task_status': score.get('task_status'),
                      'layers': {k: (score.get(k) or {}).get('status') for k in ('outcome', 'behavior', 'guardrails', 'response')},
                      'nonpass_checks': {k: {c: v for c, v in (score.get(k) or {}).get('checks', {}).items() if v is not True}
                                         for k in ('outcome', 'behavior', 'guardrails')},
                      'runtime_failure': result.get('runtime_failure'), 'guardrail_incidents': score.get('guardrails', {}).get('incidents'),
                      'response_claims': claims, 'semantic_guardrails': response.get('semantic_guardrails')},
        'baseline': {k: baseline.get(k) for k in ('recorded_status', 'recorded_layer_statuses', 'assistant_assessment', 'failure_origin', 'sources')},
        'source_evidence': {'user_input': task['user_input'], 'expected_refunds': task['expected_refunds'],
                           'case_state': case.get('state'), 'conclusion': case.get('conclusion'),
                           'extracted': artifact.get('snapshot', {}).get('extracted'),
                           'proposal': {**compact_item(proposal), 'kind': proposal.get('kind'),
                                        'target_resolution': proposal.get('target_resolution'),
                                        'items': [compact_item(x) for x in proposal.get('items', [])]},
                           'events': picked_events, 'control_events': controls, 'execution': execution,
                           'new_refunds': refunds, 'reads': read_summaries, 'dialogue': artifact.get('public_dialogue'),
                           'model_actions': model_actions, 'final_reply': artifact.get('reply')},
        'attack_delivery': {'applicable': bool(payload), 'payload': payload,
                            'independently_observed_tool_message_exposures': exposures,
                            'tool_payload_reached_model': bool(exposures) if payload else None,
                            'recorded_attack_exposure': artifact.get('attack_exposure')},
        'source_hashes_still_equal_after_read': all(source(Path(s['path']))['sha256'] == s['sha256'] for s in sources.values()),
    }


def main():
    selection = read(SELECTION)
    baseline = {row['case_id']: row for row in read(BASELINE)['findings']}
    cases = [extract(row['case_id'], row['trial_index'], baseline[row['case_id']]) for row in selection['cases']]
    output = {'schema_version': 'B1-fixed-selection-extract-1', 'generated_at': datetime.now(timezone.utc).isoformat(),
              'selection_source': source(SELECTION), 'baseline_audit_source': source(BASELINE),
              'manifest_source': source(RUN / 'manifest.json'), 'run': str(RUN),
              'human_review': 'pending', 'assistant_review_separate_from_automatic_labels': True,
              'source_or_frozen_result_modified': False, 'cases': cases}
    DEST.mkdir(parents=True, exist_ok=True)
    (DEST / 'fixed-selection-extract.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'complete': len([x for x in cases if x['status'] != 'pending_source_completion']),
                      'pending': [x['case_id'] for x in cases if x['status'] == 'pending_source_completion'],
                      'case_states': [{'case_id': x['case_id'], 'auto': x.get('automatic', {}).get('overall'),
                                       'refunds': len(x.get('source_evidence', {}).get('new_refunds', []))}
                                      for x in cases if x['status'] != 'pending_source_completion']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
