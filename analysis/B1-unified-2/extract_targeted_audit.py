"""Read-only B1 unified-2 evidence extraction; run after the parent confirms completion.

Reuses the exact B0 fixed selection and the audited, model-free extractor.
No source code, fixture, original result, or prior audit is modified.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEST = Path(__file__).resolve().parent
RUN = ROOT / 'runs/dynamic-v2-B1-unified-2'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--diagnostic-g14', action='store_true',
                        help='Also extract G14 trial 6 as an explicitly nonselection appendix.')
    parser.add_argument('--allow-provider-stopped-diagnostic', action='store_true',
                        help='Read interrupted-run evidence without claiming a complete evaluation.')
    args = parser.parse_args()
    if not (RUN / 'summary.json').is_file():
        if not args.allow_provider_stopped_diagnostic or not (RUN / 'incomplete-run.json').is_file():
            raise SystemExit('Full-run summary unavailable; requires explicit provider-stopped diagnostic mode.')
        stopped = json.loads((RUN / 'incomplete-run.json').read_text())
        if stopped.get('status') != 'stopped_provider_terminal_error':
            raise SystemExit('Unexpected interrupted-run status; inspect instead of assuming completion.')
    helper_path = ROOT / 'analysis/B1/extract_targeted_audit.py'
    spec = importlib.util.spec_from_file_location('fixed_audit_helpers', helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper.RUN, helper.DEST = RUN, DEST
    helper.main()
    output = DEST / 'fixed-selection-extract.json'
    extracted = json.loads(output.read_text())
    extracted['evaluation_completion'] = ('provider_stopped_diagnostic_not_complete_evaluation'
                                         if args.allow_provider_stopped_diagnostic else 'summary_present')
    extracted['provider_stopped_diagnostic_mode'] = args.allow_provider_stopped_diagnostic
    output.write_text(json.dumps(extracted, ensure_ascii=False, indent=2) + '\n')
    if args.diagnostic_g14:
        record = helper.extract('G14', 6, {})
        record['selection_role'] = 'separate_diagnostic_not_a_fixed_sample_replacement'
        (DEST / 'G14-t6-nonselection-diagnostic.json').write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
