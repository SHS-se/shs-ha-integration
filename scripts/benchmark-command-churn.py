"""Replay changing demand with overlapping command work on fresh or frozen storage.

All device/HA ports are simulated. A supplied SQLite archive is copied, never
modified. CPU covers this process and storage workers, not the whole container.
Use --checkout for each implementation; startup and steady replay are separate.
"""
import argparse
import asyncio
from copy import deepcopy
from contextvars import ContextVar
from dataclasses import asdict, replace
from hashlib import sha256, file_digest
import json
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
from time import perf_counter, process_time

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--checkout', type=Path, default=ROOT)
parser.add_argument('--archive', type=Path)
parser.add_argument('--seconds', type=int, default=60)
parser.add_argument('--service-delay-ms', type=int, default=200)
parser.add_argument('--readback-delay-ms', type=int, default=3000)
args = parser.parse_args()
if args.seconds < 5 or min(args.service_delay_ms, args.readback_delay_ms) < 0:
    parser.error('Use at least five seconds and nonnegative delays')
sys.path[:0] = [str(args.checkout/'app'), str(args.checkout/'tests')]
# Append the component path: its select.py must not shadow stdlib select.
sys.path.append(str(args.checkout/'custom_components/shs_energy'))
from test_battery_runtime import Rig
from gateway_fixture import IDENTITY
from shs_core.battery_runtime import iso
from shs_core.battery_writer import BatteryWriterFence
from shs_core import home_runtime as rt, plan_execution as ex
from shs_app.runtime import AppBatteryRuntime
from shs_app.indexed_storage import IndexedStorage
from shs_app.sources import ObservationMirror


def charged_writes():
    path = Path('/proc/self/io')
    if not path.exists():
        return None
    return int(dict(line.split(': ', 1) for line in path.read_text().splitlines())['write_bytes'])


async def replay():
    with tempfile.TemporaryDirectory(prefix='shs-churn-') as directory:
        archive_hash=None
        if args.archive:
            with args.archive.open('rb') as source:
                archive_hash=file_digest(source,'sha256').hexdigest()
        revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=args.checkout,text=True).strip() if (args.checkout/'.git').exists() else None
        rig, mirror = Rig(), ObservationMirror()
        path = Path(directory)/'execution.sqlite'
        if args.archive:
            shutil.copyfile(args.archive, path)
        store = IndexedStorage(path, asyncio.to_thread, mirror)
        runtime = rig.runtime = AppBatteryRuntime(rig.coordinator, rig.controller, store, lambda: rig.now)
        rig.store = store
        startup_cpu, startup_wall = process_time(), perf_counter()
        identity, ordinal = IDENTITY, 0
        if args.archive:
            await runtime.load()
            metadata = store.metadata
            rig.options = deepcopy(metadata['options'])
            source = store.source_checkpoint
            mirror.install_snapshot(dict(configuration=source['context'], through=source['receipt'],
                observations={key:{'value':row} for key,row in source['rows'].items()}))
            rig.rows = mirror.rows
            state = runtime._loaded[0]
            rig.now = state.last_time_ms + 1
            # Initial simulated source publication after restart; excluded from
            # the measured replay. Subsequent publication follows explicit delays.
            for row in rig.rows.values():
                row["last_reported"] = iso(rig.now)
            identity = metadata['gateway_processing']['gateway_identity']
            ordinal = source['receipt']
            contract = store._session.account.contract
            if contract.interval(rig.now + args.seconds*1000) is None:
                raise ValueError('Replay extends beyond the frozen current plan')
            rig.plan = dict(plan_id=contract.plan_id, snapshot_id='frozen',
                valid_until=iso(contract.valid_until_ms), binding_until=iso(contract.valid_until_ms),
                battery_supply_scope=asdict(state.authority.supply_scope) if state.authority.supply_scope.kind == "selected" else {"kind":state.authority.supply_scope.kind},
                battery_execution=ex.contract_wire(contract),
                grid=dict(import_limit_w=state.authority.plant.import_limit_w,
                          export_limit_w=state.authority.plant.export_limit_w),
                plans={'priority':{'slots':[{'start':iso(row.start_ms), 'duration_hours':(row.end_ms-row.start_ms)/3600000}
                                           for row in contract.intervals]}})
            async def devices(): return deepcopy(metadata['devices'])
            rig.coordinator.async_battery_planned_devices = devices
            runtime._model_at = rig.now
            rig.fence_store.saved = dict(schema='battery-writer-v1', owner='runtime',
                epoch=state.groups[0].grant_epoch, surfaces=sorted(runtime._control_entities()))
        else:
            # A demand-following contract actually changes its limit with load.
            contract = ex.read_contract(rig.plan['battery_execution'])
            row = replace(contract.intervals[0], operation='supply_house', target_kind='demand_following',
                charge_ac_mwh=0, import_mwh=0, discharge_ac_mwh=contract.intervals[0].load_mwh,
                charge_ac_limit_w=0, discharge_ac_limit_w=4000, follows_demand=True,
                stored_end_mwh=4800000, rounding_mwh=0)
            rig.plan['battery_execution'] = ex.contract_wire(replace(contract, intervals=(row,), objectives=()))
            mirror.install_snapshot(dict(configuration={'home':'benchmark'}, through=0,
                observations={key:{'value':dict(row, entity_id=key, kind='state_report')}
                              for key,row in rig.rows.items()}))
            rig.rows = mirror.rows
        rig.fence = BatteryWriterFence(rig.fence_store, rig.controller.lock, rig.controller.options,
                                      lambda:rig.now, runtime.identity)
        rig.coordinator.battery_writer = rig.fence
        try:
            await rig.fence.open()
            await runtime.open()
            # Real HA reporting times are retained on restart. No helper refreshes
            # every sensor timestamp or makes accepted commands instant readbacks.
            runtime.receipt_driven = True
            await runtime.refresh()
            await runtime.host.idle()
            if runtime._last_error:
                raise RuntimeError(runtime._last_error)
            before = runtime.resource_counts()
            fact_start = {name:len(getattr(runtime.host.state.execution.account,name)) for name in ('meters','observations')}
            if args.archive:
                # Keep the full frozen archive and original authority, but offer
                # a clearly labelled simulated demand-following plan. The captured
                # live plan was holding; replaying it would omit command churn.
                state=runtime.host.state
                account=ex.request_replan(state.execution.account)
                old=account.contract
                rows=tuple(replace(row, operation='supply_house', target_kind='demand_following',
                    charge_ac_mwh=0, discharge_ac_mwh=row.load_mwh, pv_mwh=0,
                    import_mwh=0, export_mwh=0, curtailed_mwh=0, unserved_mwh=0, rounding_mwh=0,
                    charge_ac_limit_w=0, discharge_ac_limit_w=state.authority.plant.discharge_max_w,
                    follows_demand=True) for row in old.intervals)
                contract=replace(old,id='benchmark-demand',generation=account.requested_generation,
                    previous_contract_id=old.id,source_receipt=account.receipt,intervals=rows)
                runtime.host.state=replace(state,execution=replace(state.execution,account=account))
                rig.plan['battery_execution']=ex.contract_wire(contract)
                await runtime.host.accept(rt.ExecutionPlanOffered(contract))
                await runtime.refresh()
                await runtime.host.idle()
                if runtime.host.state.execution.account.contract.id != contract.id:
                    raise AssertionError(('Simulated archive plan was not admitted', runtime.host.state.execution.plan_rejection, runtime._last_error))
                before=runtime.resource_counts()
                fact_start={name:len(getattr(runtime.host.state.execution.account,name)) for name in fact_start}
            startup = dict(cpu_ms=(process_time()-startup_cpu)*1000, wall_ms=(perf_counter()-startup_wall)*1000)
            calls_before = len(rig.calls)
            initial, first_ordinal = rig.now, ordinal
            pending, publications = [], []
            dispatch_id=ContextVar('benchmark_dispatch')
            original_dispatch=runtime._dispatch
            async def timed_dispatch(effect):
                token=dispatch_id.set(effect.attempt_id)
                try: return await original_dispatch(effect)
                finally: dispatch_id.reset(token)
            async def delayed_service(domain, name, data, blocking):
                # The native call completes first; its state publication comes later.
                future = asyncio.get_running_loop().create_future()
                pending.append((rig.now+args.service_delay_ms, future, domain, name, deepcopy(data), ('send',dispatch_id.get())))
                await future
            rig.controller.hass.services.async_call = delayed_service
            async def readback(entities):
                rig.readbacks += 1  # Publication is governed by the replay timeline.
            rig.coordinator.async_battery_native_readback = readback
            original_transition = runtime._transition
            async def delayed_transition(effect):
                future = asyncio.get_running_loop().create_future()
                pending.append((rig.now+200, future, None, None, None, ('transition',effect.group_id,effect.token)))
                await future
                return await original_transition(effect)
            runtime.host.ports = replace(runtime.host.ports, transition=delayed_transition, dispatch=timed_dispatch)
            power = [rig.options[key] for key in ('battery_power_measurement_entity', 'house_consumption_power_entity',
                      'solar_production_power_entity','grid_power_entity','battery_soc_entity')]
            meters = [rig.options['entities_'+name][0] for name in ('grid_import','grid_export','battery_charge','battery_discharge')]
            async def receipt(entity, kind='state_report'):
                nonlocal ordinal
                ordinal += 1
                row = dict(rig.rows[entity], entity_id=entity, kind=kind)
                packet = dict(ordinal=ordinal, kind='observation', payload=row)
                mirror.apply(packet, notify=False)
                await runtime.ingest_receipt(identity, packet)
            async def progress():
                for item in tuple(pending):
                    at, future, domain, name, data, work_key = item
                    if at <= rig.now:
                        pending.remove(item)
                        if data:
                            rig.calls.append((domain,name,data))
                            assert store._revision > 0, 'command before durability'
                            publications.append((rig.now+args.readback_delay_ms, data))
                        future.set_result(None)
                for at, data in tuple(publications):
                    if at <= rig.now:
                        publications.remove((at,data))
                        entity=data['entity_id']
                        rig.rows[entity].update(state=str(data.get('value',data.get('option'))), last_reported=iso(rig.now))
                        await receipt(entity, 'state_change')
                host=runtime.host
                if host._wake_at is not None and host._wake_at <= rig.now:
                    host._wake.cancel()
                    host._wake_at=host._wake=None
                    # Baseline's deadline event was Tick; the candidate separates it.
                    await host.accept(rt.ExecutionWake() if hasattr(rt,'ExecutionWake') else rt.Tick())
                # Finish ready callbacks and SQLite transactions before advancing
                # virtual time. Workers explicitly waiting for a future timed
                # service/route result stay in flight while receipts arrive.
                while True:
                    await asyncio.sleep(0)
                    await host._queue.join()
                    blocked={item[5] for item in pending}
                    ready=[task for key,task in host._work.items() if not task.done() and key not in blocked]
                    if not ready and host._queue.empty(): break
            operation_start=deepcopy(runtime.profiler.operations)
            status_time_ms={}
            cpu, wall, writes = process_time(), perf_counter(), charged_writes()
            for millisecond in range(100, args.seconds*1000+1, 100):
                rig.now=initial+millisecond
                await progress()
                if millisecond%1000==0:
                    second=millisecond//1000
                    # Small, frequent fluctuations match the observed live demand.
                    load=800+(second*37%127)
                    values=(0,load,0,load,50)
                    for entity,value in zip(power,values):
                        row=rig.rows[entity]
                        scale=1000 if row['attributes'].get('unit_of_measurement')=='kW' else 1
                        row.update(state=str(value/scale), last_reported=iso(rig.now))
                        await receipt(entity)
                    for entity in meters:
                        row=rig.rows[entity]
                        row.update(state=str(float(row['state'])+.0001),last_reported=iso(rig.now))
                        await receipt(entity)
                if millisecond%5000==0:
                    await runtime.commit_evidence()
                    await runtime.refresh()
                    runtime.snapshot()  # Routine UI/controller projection, with archive queries.
                await progress()
                status=runtime.host.state.groups[0].status
                status_time_ms[status]=status_time_ms.get(status,0)+100
            await runtime.host._queue.join()
            await runtime.host.commit_evidence()
            if runtime._last_error or runtime._fault_history:
                raise AssertionError(('Replay runtime faults', runtime._last_error, runtime._fault_history))
            after = runtime.resource_counts()
            cpu_ms, wall_ms=(process_time()-cpu)*1000,(perf_counter()-wall)*1000
            ending_writes=charged_writes()
            account=runtime.host.state.execution.account
            facts={name:[asdict(row) for row in getattr(account,name)[fact_start[name]:]] for name in fact_start}
            measured={name:[{key:value for key,value in row.items() if key not in ('receipt','event_id')}
                            for row in rows] for name,rows in facts.items()}
            measurement_fingerprint=sha256(json.dumps(measured,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            fingerprint=sha256(json.dumps(facts,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            operations=runtime.profiler.operations
            result=dict(checkout=str(args.checkout),git_revision=revision,python=sys.version,
                archive_sha256=archive_hash,archive_bytes=args.archive.stat().st_size if args.archive else 0,
                startup=startup,simulated_seconds=args.seconds,reports=ordinal-first_ordinal,
                cpu_ms=cpu_ms,wall_ms=wall_ms,checkpoint_saves=after['storage_commits']-before['storage_commits'],
                evidence_query_groups=after['evidence_queries']-before['evidence_queries'],
                charged_write_bytes=None if writes is None else ending_writes-writes,
                commands=len(rig.calls)-calls_before,status_time_ms=status_time_ms,unresolved_attempts=len(runtime.host.state.groups[0].attempts),
                inflight_workers=len(pending),unpublished_readbacks=len(publications),
                new_facts={name:len(rows) for name,rows in facts.items()},facts_sha256=fingerprint,
                measurements_sha256=measurement_fingerprint,
                evidence_comparison="Measurement fields exact; gateway receipt/event IDs differ with the commands produced",
                plan_workload="Simulated demand-following replacement on original frozen archive" if args.archive else "Simulated demand-following plan",
                completed_receipt=runtime._processing['receipt'],last_receipt=ordinal,
                operations={name:{key:row[key]-operation_start[name][key]
                    for key in ("calls","failures","wall_ms","cpu_ms")} for name,row in operations.items()},
                final_status=runtime.snapshot()['state'],final_reason=runtime.snapshot()['reason'],
                final_target=dict(runtime.host.state.groups[0].desired.target) if runtime.host.state.groups[0].desired else None,
                final_operation=runtime.host.state.execution.assessment.operation if runtime.host.state.execution.assessment else None,
                basis='Simulated ports and clock; process CPU is not deployed container CPU; commits are not disk bytes')
            print(json.dumps(result,indent=2))
        finally:
            await runtime.close(release=False)


asyncio.run(replay())
