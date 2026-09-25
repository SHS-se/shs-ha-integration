"""Reproduce CPU and retained-memory costs locally from a controller export.

No Home Assistant imports or network access. Outputs counts and timings only.
Run: python3 scripts/benchmark-account.py diagnostics.json.gz --memory
"""
import argparse
from dataclasses import fields
import gc
import gzip
import json
from pathlib import Path
import statistics
import sys
from time import perf_counter, thread_time

sys.path.append(str(Path(__file__).resolve().parents[1] / 'custom_components/shs_energy'))
from execution_migration import read_account
import plan_execution as execution


def reachable_bytes(value):
    """Deduplicate shared objects; an object-graph estimate, never process RSS."""
    seen = set()
    def visit(item):
        if id(item) in seen:
            return 0
        seen.add(id(item))
        size = sys.getsizeof(item)
        if isinstance(item, dict):
            return size + sum(visit(k) + visit(v) for k, v in item.items())
        if isinstance(item, (tuple, list, set, frozenset)):
            return size + sum(map(visit, item))
        if hasattr(item, '__dict__'):
            return size + visit(vars(item))
        if hasattr(item, '__slots__'):
            return size + sum(visit(getattr(item, name)) for name in item.__slots__ if hasattr(item, name))
        return size
    return visit(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('export', type=Path)
    parser.add_argument('--memory', action='store_true')
    args = parser.parse_args()
    opener = gzip.open if args.export.suffix == '.gz' else open
    with opener(args.export) as handle:
        report = json.load(handle)
    raw = report['battery_execution']['accounting_journal']
    started = perf_counter()
    account = read_account(raw)
    hydration = perf_counter() - started
    del raw, report
    gc.collect()
    if not account.meters or account.observed is None:
        parser.error('Export must contain meter receipts and a state observation')
    at = account.observed.at_ms
    last = account.meters[-1]
    receipt = {field.name: getattr(last, field.name) for field in fields(last) if field.name != 'receipt'}
    observation = execution.StateObservation(at + 1, account.observed.stored_mwh, 'benchmark')
    operations = {
        'live_feedback': lambda: execution.live_feedback(account, at),
        'observation_append': lambda: execution.observe_state(account, observation),
        'duplicate_meter': lambda: execution.record_meter(account, **receipt),
        'meter_append': lambda: execution.record_meter(account, **{**receipt,
            'event_id': 'benchmark-new-receipt', 'source_at_ms': last.source_at_ms + 1}),
    }
    results = {}
    for name, operation in operations.items():
        start = thread_time()
        operation()
        cold = (thread_time() - start) * 1000
        times = []
        for _ in range(7):
            start = thread_time()
            operation()
            times.append((thread_time() - start) * 1000)
        results[name] = {'first_call_cpu_ms': cold, 'warm_median_cpu_ms': statistics.median(times)}
    result = {'hydration_wall_seconds': hydration,
        'records': {name: len(getattr(account, name)) for name in ('meters', 'observations', 'admissions')},
        'operations': results,
        'basis': 'Local synchronous thread CPU; first call can build indexes, warm is median of seven. '
                 'Does not measure host latency, disk I/O or whole-system memory.'}
    if args.memory:
        result['account_reachable_bytes'] = reachable_bytes(account)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
