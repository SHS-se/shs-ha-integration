"""Replay a synthetic 9-report/second battery workload in temporary SQLite.

No HA connection or physical device writes. Compare --mode legacy against a
previous checkout's --core-root with --mode paced against the current checkout.
Process CPU includes storage workers; commits are not physical disk-write bytes.
"""
import argparse
import asyncio
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
from time import process_time, perf_counter

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--mode',choices=('legacy','paced'),required=True)
parser.add_argument('--seconds',type=int,default=60)
parser.add_argument('--core-root',type=Path,default=ROOT/'custom_components/shs_energy')
args=parser.parse_args()
if args.seconds < 1:parser.error('--seconds must be positive')
sys.path[:0]=[str(ROOT/'app'),str(ROOT/'tests'),str(args.core_root)]

from test_battery_runtime import Rig
from gateway_fixture import IDENTITY
from shs_core.battery_runtime import iso
from shs_core.battery_writer import BatteryWriterFence
from shs_app.runtime import AppBatteryRuntime
from shs_app.indexed_storage import IndexedStorage
from shs_app.sources import ObservationMirror


async def replay():
    with tempfile.TemporaryDirectory() as directory:
        rig=Rig()
        mirror=ObservationMirror()
        mirror.install_snapshot(dict(configuration={'home':'benchmark'},through=0,
            observations={key:{'value':dict(row,entity_id=key,kind='state_report')}
                          for key,row in rig.rows.items()}))
        store=IndexedStorage(Path(directory)/'execution.sqlite',asyncio.to_thread,mirror)
        rig.store=store
        runtime=rig.runtime=AppBatteryRuntime(rig.coordinator,rig.controller,store,lambda:rig.now)
        rig.fence=BatteryWriterFence(rig.fence_store,rig.controller.lock,rig.controller.options,
            lambda:rig.now,runtime.identity)
        rig.coordinator.battery_writer=rig.fence
        try:
            await rig.start();await rig.advance(1)
            runtime.receipt_driven=True
            before=runtime.resource_counts()
            cpu,wall=process_time(),perf_counter()
            initial=rig.now
            ordinal=0
            entities=('sensor.battery','sensor.house','sensor.pv','sensor.grid','sensor.soc',
                'sensor.grid_import','sensor.grid_export','sensor.battery_charge','sensor.battery_discharge')
            for second in range(1,args.seconds+1):
                for offset,entity in enumerate(entities):
                    rig.now=initial+second*1000+offset
                    row=rig.rows[entity]
                    row['last_reported']=iso(rig.now)
                    if entity in entities[5:]:
                        # Equal-value later knots and older corrections remain facts.
                        if second%3:row['state']=str(float(row['state'])+.0001)
                        if second%17==0:row['last_reported']=iso(rig.now-1000)
                    ordinal+=1
                    receipt=dict(ordinal=ordinal,kind='observation',payload=dict(row,entity_id=entity,kind='state_report'))
                    mirror.apply(receipt,notify=False)
                    if args.mode=='legacy':await runtime.consume_receipt(IDENTITY,receipt)
                    else:await runtime.ingest_receipt(IDENTITY,receipt)
                if args.mode=='paced' and second%5==0:
                    await runtime.commit_evidence()
                    await runtime.refresh();await runtime.host.idle()
            if args.mode=='paced':
                rig.now+=5000
                await runtime.commit_evidence()
            after=runtime.resource_counts()
            account=runtime.host.state.execution.account
            facts={key:[asdict(row) for row in getattr(account,key)] for key in ('meters','observations')}
            digest=sha256(json.dumps(facts,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            output=dict(mode=args.mode,reports=ordinal,simulated_seconds=args.seconds,
                cpu_ms=(process_time()-cpu)*1000,wall_ms=(perf_counter()-wall)*1000,
                checkpoint_saves=after['storage_commits']-before['storage_commits'],
                evidence_query_groups=after['evidence_queries']-before['evidence_queries'],
                facts_sha256=digest,facts={key:len(value) for key,value in facts.items()},
                completed_receipt=runtime._processing['receipt'],native_commands=len(rig.calls),
                basis='Synthetic replay, not a live CPU or physical disk-I/O acceptance measurement')
            print(json.dumps(output,indent=2))
        finally:
            await runtime.close(release=False)


asyncio.run(replay())
