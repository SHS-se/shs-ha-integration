"""Compare archive serialization CPU with a local controller diagnostic export.

Run with Python 3.13. No network, HA commands, or persistent writes are performed.
The store discards pages, isolating serialization/hashing from disk latency.
"""
import argparse
import asyncio
from dataclasses import replace
import gzip
import json
from pathlib import Path
import statistics
import sys
from time import process_time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'custom_components' / 'shs_energy'))
from execution_archive import ExecutionArchive, read_account
from home_runtime import ExecutionSession
from runtime_json import encode_value


class DiscardStore:
    async def async_save(self, _value):
        pass


async def benchmark(report, repeats):
    account = read_account(report['battery_execution']['accounting_journal'])
    original = ExecutionSession(account=account)
    counter = replace(original, account=replace(account, receipt=account.receipt + 1))
    receipt = replace(account.meters[-1], event_id='benchmark', receipt=account.receipt + 1)
    appended = replace(counter, account=replace(counter.account, meters=(*account.meters, receipt)))
    for name, updated in [('counter change', counter), ('meter append', appended)]:
        timings = {'whole session': [], 'incremental': []}
        for _ in range(repeats):
            for mode in timings:
                archive = ExecutionArchive(lambda _key: DiscardStore())
                if mode == 'whole session':
                    await archive.put(encode_value(original))
                else:
                    await archive.save_session(original)
                start = process_time()
                if mode == 'whole session':
                    await archive.put(encode_value(updated))
                else:
                    await archive.save_session(updated)
                timings[mode].append((process_time() - start) * 1000)
        print(name + ': ' + ', '.join(f'{mode} {statistics.median(times):.3f} ms CPU'
                                     for mode, times in timings.items()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('diagnostics', type=Path)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('--repeats must be positive')
    opener = gzip.open if args.diagnostics.suffix == '.gz' else open
    with opener(args.diagnostics, 'rt') as stream:
        asyncio.run(benchmark(json.load(stream), args.repeats))
