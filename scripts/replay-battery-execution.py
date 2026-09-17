#!/usr/bin/env python3
"""Recalculate battery accounting and recorded assessments from a diagnostic dump.

Reads JSON or gzip JSON locally. No Home Assistant, network or device access.
This checks the executor and accounting, not planner economics or hardware delivery.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import gzip
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'custom_components' / 'shs_energy'))
from battery_conversion import Conversion
from execution_archive import read_account
from home_runtime import ExecutionTrace
from plan_execution import LiveState, assess_execution, feedback
from runtime_json import decode_value


def replay(value):
    dump = value.get('battery_execution', value)
    if not isinstance(dump, dict) or 'accounting_journal' not in dump:
        raise ValueError('dump has no replacement battery execution journal')
    account = read_account(dump['accounting_journal'])
    accounting = feedback(account, dump['accounting_at_ms'])
    mismatches = []
    if accounting != dump['accounting']:
        mismatches.append({'part': 'accounting'})
    assessment = None
    if dump.get('assessment') is not None:
        live = LiveState(**dump['execution_input'])
        assessment = asdict(assess_execution(account, live, Conversion.read(dump['conversion_basis'])))
        if assessment != dump['assessment']:
            mismatches.append({'part': 'current_assessment', 'recalculated': assessment})
    checked = 0
    for index, raw in enumerate(dump['execution_traces']):
        trace = decode_value(raw, ExecutionTrace)
        if trace.assessment is None:
            continue
        admissions = tuple(a for a in account.admissions if a.receipt <= trace.account_receipt)
        prefix = replace(account, receipt=trace.account_receipt,
            admissions=admissions,
            meters=tuple(m for m in account.meters if m.receipt <= trace.account_receipt),
            observations=account.observations[:trace.observation_count],
            reconciliations=account.reconciliations[:trace.reconciliation_count],
            requests=(), opening=account.opening if admissions else None)
        if not prefix.contract or prefix.contract.id != trace.reference_id:
            raise ValueError(f'trace {index} has no matching reference prefix')
        recalculated = assess_execution(prefix, trace.live, Conversion.read(json.loads(trace.conversion_json)))
        checked += 1
        if recalculated != trace.assessment:
            mismatches.append({'part': 'trace', 'index': index, 'at_ms': trace.at_ms,
                               'recalculated': asdict(recalculated)})
    return {'matches_recorded_results': not mismatches, 'mismatches': mismatches,
            'receipt': account.receipt, 'reference_id': account.contract.id if account.contract else None,
            'recorded_events': len(dump['execution_traces']), 'assessments_checked': checked,
            'accounting': accounting, 'assessment': assessment}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dump', type=Path)
    args = parser.parse_args()
    try:
        opener = gzip.open if args.dump.suffix == '.gz' else open
        with opener(args.dump, 'rt', encoding='utf-8') as stream:
            result = replay(json.load(stream))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(2, f'Cannot replay battery dump: {error}\n')
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result['matches_recorded_results'] else 1


if __name__ == '__main__':
    sys.exit(main())
