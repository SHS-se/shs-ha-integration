"""Compare startup receipt batching against per-receipt replay in real SQLite.

Uses synthetic measurements and Verification mode; no HA calls or device writes.
The evidence digest includes every retained counter and SOC fact.
"""
import argparse
import asyncio
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
from time import perf_counter, process_time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'app'),str(ROOT/'tests'),str(ROOT/'custom_components/shs_energy')]
from test_battery_runtime import Rig
from gateway_fixture import IDENTITY
from shs_core.battery_runtime import iso
from shs_core.battery_writer import BatteryWriterFence
from shs_app.runtime import AppBatteryRuntime
from shs_app.indexed_storage import IndexedStorage
from shs_app.sources import ObservationMirror

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--seconds',type=int,default=60)
args = parser.parse_args()
if args.seconds < 1:
    parser.error('--seconds must be positive')


async def replay(batched):
    with tempfile.TemporaryDirectory() as directory:
        rig = Rig(mode='control_verification')
        mirror = ObservationMirror()
        mirror.install_snapshot(dict(configuration={'home':'benchmark'},through=0,
            observations={key:{'value':dict(row,entity_id=key,kind='state_report')}
                          for key,row in rig.rows.items()}))
        store = rig.store = IndexedStorage(Path(directory)/'execution.sqlite',asyncio.to_thread,mirror)
        runtime = rig.runtime = AppBatteryRuntime(rig.coordinator,rig.controller,store,lambda:rig.now)
        rig.fence = BatteryWriterFence(rig.fence_store,rig.controller.lock,rig.controller.options,
            lambda:rig.now,runtime.identity)
        rig.coordinator.battery_writer = rig.fence
        try:
            await rig.start()
            runtime.receipt_driven = True
            before = runtime.resource_counts()
            initial = rig.now
            cpu,wall = process_time(),perf_counter()
            ordinal = 0
            entities = ('sensor.battery','sensor.house','sensor.pv','sensor.grid','sensor.soc',
                        'sensor.grid_import','sensor.grid_export','sensor.battery_charge','sensor.battery_discharge')
            for second in range(1,args.seconds+1):
                for offset,entity in enumerate(entities):
                    rig.now = initial + second*1000 + offset
                    row = rig.rows[entity]
                    row['last_reported'] = iso(rig.now)
                    if entity in entities[5:]:
                        if second % 3:
                            row['state'] = str(float(row['state']) + .0001)
                        if second % 17 == 0:
                            row['last_reported'] = iso(rig.now-1000)
                    ordinal += 1
                    receipt = dict(ordinal=ordinal,kind='observation',
                        payload=dict(row,entity_id=entity,kind='state_report'))
                    mirror.apply(receipt,notify=False)
                    if batched:
                        await runtime.ingest_receipt(IDENTITY,receipt,replay=True)
                        if ordinal % 256 == 0:
                            await runtime.commit_evidence(force=True)
                    else:
                        await runtime.consume_receipt(IDENTITY,receipt)
            if batched:
                await runtime.commit_evidence(force=True)
            elapsed,cpu_ms = (perf_counter()-wall)*1000,(process_time()-cpu)*1000
            after = runtime.resource_counts()
            account = runtime.host.state.execution.account
            facts = {key:[asdict(row) for row in getattr(account,key)] for key in ('meters','observations')}
            fingerprint = sha256(json.dumps(facts,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            return dict(mode='batched' if batched else 'per_receipt',reports=ordinal,
                cpu_ms=round(cpu_ms,2),wall_ms=round(elapsed,2),
                checkpoint_saves=after['storage_commits']-before['storage_commits'],
                completed_receipt=runtime._processing['receipt'],source_receipt=store.source_checkpoint['receipt'],
                facts_sha256=fingerprint,facts={key:len(value) for key,value in facts.items()},
                native_commands=len(rig.calls))
        finally:
            await runtime.close(release=False)


async def main():
    previous = await replay(False)
    batched = await replay(True)
    if (previous['facts_sha256'] != batched['facts_sha256']
            or previous['completed_receipt'] != batched['completed_receipt']
            or batched['source_receipt'] != batched['completed_receipt']
            or previous['native_commands'] or batched['native_commands']):
        raise AssertionError('Startup batching changed accounting facts, completion, or physical commands')
    print(json.dumps(dict(per_receipt=previous,batched=batched,
        wall_speedup=round(previous['wall_ms']/batched['wall_ms'],2),
        save_reduction=round(previous['checkpoint_saves']/batched['checkpoint_saves'],2),
        basis='Synthetic SQLite replay; live startup timing is verified separately.'),indent=2))


asyncio.run(main())
