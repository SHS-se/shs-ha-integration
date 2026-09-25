"""Measure SQLite import, restart and single-record append on a local diagnostic export.

Python 3.13; temporary local database only. No HA access or commands. JSON bytes
in storage metrics exclude typed SQL columns and are not physical write bytes.
"""
import argparse
import asyncio
from dataclasses import replace
import gzip
import json
from pathlib import Path
import statistics
import sys
import tempfile
from time import perf_counter, process_time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'custom_components' / 'shs_energy'))
from execution_migration import read_account
from execution_storage import ExecutionStorage
from home_runtime import ExecutionSession


async def benchmark(report, repeats):
    account = read_account(report['battery_execution']['accounting_journal'])
    metadata = dict(schema='battery-runtime-v4', checkpoint=None, options=None,
                    devices=[], model_sources=None, ratings=None)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'execution.sqlite'
        storage = ExecutionStorage(path, asyncio.to_thread)
        await storage.load()
        initial = ExecutionSession(account=account)
        cpu, wall = process_time(), perf_counter()
        await storage.save(metadata, initial)
        output = {'import': {'cpu_ms':(process_time()-cpu)*1000,'wall_ms':(perf_counter()-wall)*1000},
                  'database_bytes':path.stat().st_size, 'files':len(list(path.parent.iterdir())),
                  'rows':{name:len(getattr(account,name)) for name in ('meters','observations','admissions','reconciliations')}}
        cpu, wall = process_time(), perf_counter()
        reopened = ExecutionStorage(path, asyncio.to_thread)
        restored = (await reopened.load())[1]
        output['restart'] = {'cpu_ms':(process_time()-cpu)*1000,'wall_ms':(perf_counter()-wall)*1000}
        if restored != initial:
            raise AssertionError('Restart changed execution evidence')
        times, rows = [], []
        for index in range(repeats):
            current = storage._session
            account = current.account
            receipt = replace(account.meters[-1], event_id=f'benchmark:{index}', receipt=account.receipt+1)
            updated = replace(current, account=replace(account, receipt=receipt.receipt, meters=(*account.meters,receipt)))
            before = storage.metrics['rows_appended']
            cpu, wall = process_time(), perf_counter()
            await storage.save(metadata, updated)
            times.append(((process_time()-cpu)*1000,(perf_counter()-wall)*1000))
            rows.append(storage.metrics['rows_appended'] - before)
        output['meter_append'] = {'median_cpu_ms':statistics.median(t[0] for t in times),
                                 'median_wall_ms':statistics.median(t[1] for t in times), 'rows_written_per_append':rows}
        print(json.dumps(output, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('diagnostics', type=Path)
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('--repeats must be positive')
    opener = gzip.open if args.diagnostics.suffix == '.gz' else open
    with opener(args.diagnostics, 'rt') as stream:
        asyncio.run(benchmark(json.load(stream), args.repeats))
