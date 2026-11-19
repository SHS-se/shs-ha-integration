"""Synthetic Recorder ports: source-qualified evidence and normal/replay parity."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.append(str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from shs_core.counter_evidence import CounterSource, envelope, qualification, refresh_counter_evidence, validate_counter_evidence
from shs_core.counter_reading import CounterReader
from shs_core.heating_history import HeatingHistorySources, accepted_heating_cursor, capture_heating_history, heating_history_sources
from shs_core.heating_lineage import (heating_counter_sources, heating_source_segments,
    prepare_heating_counter_evidence, qualified_heating_reader, validate_heating_lineage)
from shs_core.household_ports import HouseholdReadError
from test_heating_history import START, PROBES, Recorder, binding, owner, source_fixture
from test_heating_lineage import lineage
from household_fixture import Rig

UTC = timezone.utc
MONTH = datetime(2025, 9, 1, tzinfo=UTC)
NOW = datetime(2025, 11, 1, tzinfo=UTC)


def peak(start, end, maximum, minimum=0):
    return {'start': start.timestamp(), 'end': end.timestamp(), 'max': maximum, 'min': minimum}


class PowerRecorder(Recorder):
    def __init__(self):
        super().__init__()
        self.rows['month'] = {}
        self.unavailable = False

    async def statistics(self, start, end, entities, period, units, kinds):
        if self.unavailable and units == {'power':'W'}:
            raise HouseholdReadError('synthetic power outage')
        return await super().statistics(start, end, entities, period, units, kinds)


class PeakLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_exception_retention_and_restart_preserve_peak_but_explicit_correction_replaces_it(self):
        recorder, ledger = PowerRecorder(), {}
        source = CounterSource('sensor.energy', 'sensor.power')
        recorder.rows['month']['sensor.power'] = [peak(MONTH, START, 1200)]
        await refresh_counter_evidence(recorder, ledger, [source], NOW)
        signature = qualification(ledger, source)
        self.assertEqual(envelope(ledger, source), 1200)
        # A restart reads exactly the durable JSON, no owner-local numeric seed.
        ledger = json.loads(json.dumps(ledger))
        recorder.rows['month'] = {}
        await refresh_counter_evidence(recorder, ledger, [source], NOW+timedelta(days=500))
        self.assertEqual(qualification(ledger, source), signature)
        recorder.unavailable = True
        await refresh_counter_evidence(recorder, ledger, [source], NOW+timedelta(days=501))
        self.assertEqual(qualification(ledger, source), signature)
        recorder.unavailable = False
        recorder.rows['month']['sensor.power'] = [peak(MONTH, START, 800)]
        # Replay a returned correction within retained scope; a later read of a
        # smaller rolling maximum alone was not allowed to remove the old peak.
        ledger[source.key].pop('checked_at')
        await refresh_counter_evidence(recorder, ledger, [source], NOW)
        self.assertEqual(envelope(ledger, source), 800)
        recorder.rows['month']['sensor.power'] = [peak(MONTH, START, 0)]
        await refresh_counter_evidence(recorder, ledger, [source], NOW+timedelta(hours=1))
        self.assertIsNone(envelope(ledger, source))

    async def test_signed_import_export_and_complete_month_supersedes_corrected_hour(self):
        recorder, ledger = PowerRecorder(), {}
        imports = CounterSource('sensor.import', 'sensor.grid')
        exports = CounterSource('sensor.export', 'sensor.grid', 'min')
        recorder.rows['month']['sensor.grid'] = [peak(START, NOW, 9000, -4000)]
        recorder.rows['hour']['sensor.grid'] = [peak(START, START+timedelta(hours=1), 10000, -5000)]
        await refresh_counter_evidence(recorder, ledger, [imports, exports], START+timedelta(hours=1))
        self.assertEqual(envelope(ledger, imports), 10000)
        self.assertEqual(envelope(ledger, exports), 5000)
        await refresh_counter_evidence(recorder, ledger, [imports, exports], NOW)
        self.assertEqual(envelope(ledger, imports), 9000)
        self.assertEqual(envelope(ledger, exports), 4000)
        self.assertTrue(all(bucket['period']=='month' for bucket in ledger[imports.key]['buckets'].values()))

    async def test_epoch_boundary_uses_actual_hours_and_never_leaks_another_hardware_peak(self):
        recorder, ledger = PowerRecorder(), {}
        cutover = START+timedelta(hours=2)
        old = CounterSource('sensor.reused', 'sensor.reused_power', begin=START.isoformat(), end=cutover.isoformat())
        new = CounterSource('sensor.reused', 'sensor.reused_power', begin=cutover.isoformat())
        recorder.rows['month']['sensor.reused_power'] = [peak(START, NOW, 3000)]
        recorder.rows['hour']['sensor.reused_power'] = [peak(START+timedelta(hours=i),START+timedelta(hours=i+1),value)
            for i,value in enumerate([500, 600, 3000, 2500])]
        await refresh_counter_evidence(recorder, ledger, [old,new], NOW)
        self.assertNotEqual(old.key,new.key)
        self.assertEqual(envelope(ledger,old),600)
        self.assertEqual(envelope(ledger,new),3000)
        # Hour straddling a non-hour-aligned epoch cannot qualify either side.
        narrow = replace(old,begin=(START+timedelta(minutes=30)).isoformat(),end=(START+timedelta(minutes=90)).isoformat())
        await refresh_counter_evidence(recorder,ledger,[narrow],NOW)
        self.assertIsNone(envelope(ledger,narrow))

    async def test_identical_refresh_has_stable_qualification_and_pair_change_is_pending(self):
        recorder, ledger = PowerRecorder(), {}
        source = CounterSource('sensor.energy','sensor.power')
        recorder.rows['month']['sensor.power'] = [peak(MONTH,START,1000)]
        await refresh_counter_evidence(recorder,ledger,[source],NOW)
        saved = qualification(ledger,source)
        await refresh_counter_evidence(recorder,ledger,[source],NOW+timedelta(hours=2))
        self.assertEqual(qualification(ledger,source),saved)
        changed = replace(source,power='sensor.other_power')
        await refresh_counter_evidence(recorder,ledger,[changed],NOW+timedelta(hours=2))
        self.assertIsNone(envelope(ledger,changed))
        self.assertEqual(envelope(ledger,source),1000)

    async def test_partial_retention_month_cannot_replace_complete_peak_or_delete_hours(self):
        recorder,ledger = PowerRecorder(),{}
        source = CounterSource('sensor.energy','sensor.power')
        recorder.rows['month']['sensor.power'] = [peak(MONTH,START,1200)]
        await refresh_counter_evidence(recorder,ledger,[source],NOW)
        first = MONTH+timedelta(days=10)
        later = first+timedelta(days=400)
        recorder.rows['month']['sensor.power'] = [peak(MONTH,START,400)]
        recorder.rows['hour']['sensor.power'] = [peak(first,first+timedelta(hours=1),400)]
        # Native month labels are unchanged although its leading days were not
        # part of Recorder's rolling query reduction.
        original = recorder.statistics
        async def partial(start,end,entities,period,units,kinds):
            if period=='month':return recorder.rows['month']
            return await original(start,end,entities,period,units,kinds)
        recorder.statistics = partial
        await refresh_counter_evidence(recorder,ledger,[source],later)
        self.assertEqual(envelope(ledger,source),1200)
        buckets = ledger[source.key]['buckets']
        self.assertEqual(sorted(v['value_w'] for v in buckets.values()),[400,1200])
        await refresh_counter_evidence(recorder,ledger,[source],later+timedelta(hours=1))
        self.assertEqual(len(buckets),2)
        self.assertEqual(envelope(ledger,source),1200)

    async def test_malformed_optional_evidence_and_recorder_rows_do_not_block_other_sources(self):
        recorder, ledger = PowerRecorder(), {}
        source = CounterSource('sensor.energy','sensor.power')
        recorder.rows['month']['sensor.power'] = [peak(MONTH,START,1000)]
        await refresh_counter_evidence(recorder,ledger,[source],NOW)
        ledger[source.key]['checked_at'] = 'not a timestamp'
        ledger['broken'] = ['not an entry']
        ledger['future'] = {'version':99}
        ledger[source.key]['buckets']['bad'] = {'period':'month','start':'garbage','end':None,'value_w':100}
        recorder.rows['month']['sensor.power'] += [None, {'start':'invalid','end':1,'max':1}, {'start':float('inf')}]
        recorder.statistics = AsyncMock(return_value=recorder.rows['month'])
        await refresh_counter_evidence(recorder,ledger,[source],NOW+timedelta(hours=1))
        self.assertEqual(envelope(ledger,source),1000)
        self.assertEqual(len(ledger),1)
        self.assertEqual(len(ledger[source.key]['buckets']),1)
        self.assertEqual(validate_counter_evidence(['broken whole field']),{})

    async def test_current_planning_retains_durable_evidence_across_missing_reads_and_restart(self):
        options = {'solar_production_power_entity':'sensor.power'}
        rig = Rig(now=NOW,options=options,stored={'counter_peak_evidence':['damaged optional field']})
        rig.household.resolved_options = lambda: options
        recorder = PowerRecorder()
        recorder.rows['month']['sensor.power'] = [peak(MONTH,START,1200)]
        rig.history.statistics.side_effect = recorder.statistics
        await rig.household._refresh_counter_bounds({'solar_production':['sensor.energy']},[])
        self.assertEqual(rig.household._counter_bounds_w,{'sensor.energy':1200})
        saved = await rig.household._store.async_load()
        restarted = Rig(now=NOW+timedelta(hours=2),options=options,stored=saved)
        restarted.household.resolved_options = lambda: options
        restarted.history.statistics.side_effect = HouseholdReadError('synthetic unavailable archive')
        await restarted.household._refresh_counter_bounds({'solar_production':['sensor.energy']},[])
        self.assertEqual(restarted.household._counter_bounds_w,{'sensor.energy':1200})
    async def test_invalid_heating_catalog_does_not_remove_unrelated_grid_evidence_after_restart(self):
        options = {'grid_power_entity':'sensor.power'}
        recorder,ledger = PowerRecorder(),{}
        recorder.rows['month']['sensor.power'] = [peak(MONTH,START,12000)]
        source = CounterSource('sensor.grid','sensor.power')
        await refresh_counter_evidence(recorder,ledger,[source],NOW)
        rig = Rig(now=NOW+timedelta(hours=2),options=options,
            stored={'counter_peak_evidence':ledger,'heating_history_sources':{'invalid':True}})
        rig.household.resolved_options = lambda:options
        rig.history.statistics.side_effect = HouseholdReadError('synthetic unavailable power')
        await rig.household._refresh_counter_bounds({'grid_import':['sensor.grid']},[])
        self.assertEqual(rig.household._counter_bounds_w,{'sensor.grid':12000})


class SourceQualificationTests(unittest.IsolatedAsyncioTestCase):
    def sources(self):
        return HeatingHistorySources([binding()],{'heater':'sensor.energy_a'},PROBES,'sensor.outdoors',
            device_power_statistics={'heater':'sensor.current_power'})

    def assignment(self):
        value = lineage()
        value['assignments'][0]['observed_device_power_statistics'] = {'heater':'sensor.old_power'}
        return validate_heating_lineage(value)

    async def test_normal_upload_and_replay_share_old_source_cleaning_and_saved_evidence(self):
        recorder = PowerRecorder()
        recorder.temperatures('hour', count=2)
        recorder.rows['hour']['sensor.previous_air'] = deepcopy(recorder.rows['hour']['sensor.air_a'])
        recorder.energy('sensor.previous_energy','hour',[2.0])
        recorder.rows['month']['sensor.old_power'] = [peak(START,NOW,9000)]
        recorder.rows['hour']['sensor.old_power'] = [peak(START,START+timedelta(hours=1),500)]
        recorder.rows['month']['sensor.current_power'] = [peak(START,NOW,3000)]
        recorder.rows['hour']['sensor.current_power'] = [peak(START+timedelta(hours=1),START+timedelta(hours=2),3000)]
        devices,mappings,rooms = source_fixture()
        mappings['heater']['power'] = 'sensor.current_power'
        rig = Rig(options={'rooms':rooms,'device_control_mappings':mappings, 'outdoor_temperature_entity':'sensor.outdoors'})
        async def read(start,end,entities,period,units,kinds):
            if units == {'energy':'kWh'}:
                durable = await rig.household._store.async_read()
                self.assertTrue(durable.get('counter_peak_evidence'))
            return await recorder.statistics(start,end,entities,period,units,kinds)
        rig.history.statistics.side_effect = read
        rig.history.states.side_effect = recorder.states
        end = START+timedelta(hours=2)
        record = {}
        normal,_,_ = await rig.household._heating_history_upload(rig.options,devices,end,None,
            lineage=self.assignment(),stored=record)
        # The exact normal owner persisted the evidence before derived capture.
        persisted = await rig.household._store.async_read()
        self.assertEqual(persisted['counter_peak_evidence'],record['counter_peak_evidence'])
        ledger = json.loads(json.dumps(persisted['counter_peak_evidence']))
        sources = await prepare_heating_counter_evidence(recorder,
            heating_history_sources(devices,mappings,rooms,rig.household.ports.read_state,outdoor_entity='sensor.outdoors'),
            self.assignment(),ledger,end)
        segment = heating_source_segments(sources,self.assignment(),START,START+timedelta(hours=1))[0]
        reader,pending = qualified_heating_reader(recorder,segment.sources,ledger)
        self.assertFalse(pending)
        replay = await capture_heating_history(history=recorder,read_energy=reader.changes,
            bindings=segment.sources.bindings,device_statistics=segment.sources.device_statistics,
            room_air_probes=segment.sources.room_air_probes,outdoor_entity='sensor.outdoors',start=segment.start,end=segment.end)
        actual = next(row for row in normal if row['start']==START.isoformat())
        self.assertEqual(actual,replay.observations[0])
        self.assertIsNone(actual['device_energy_kwh']['heater'])
        self.assertEqual(actual['invalidated_device_keys'],['heater'])
        # Empty archive after restart cannot resurrect the same 2 kWh jump.
        recorder.rows['month'] = {}
        await prepare_heating_counter_evidence(recorder,self.sources(),self.assignment(),ledger,end+timedelta(hours=2))
        reader,pending = qualified_heating_reader(recorder,segment.sources,ledger)
        self.assertFalse(pending)
        changes = await reader.changes(['sensor.previous_energy'],START,START+timedelta(hours=1),'hour')
        self.assertEqual(changes['sensor.previous_energy'],[])

    def test_historical_missing_pair_clears_current_pair_and_current_epoch_starts_at_cutover(self):
        legacy = validate_heating_lineage(lineage())
        old,current = heating_source_segments(self.sources(),legacy,START,START+timedelta(hours=2))
        self.assertEqual(old.sources.device_power_statistics,{})
        source = heating_counter_sources(current.sources)['heater']
        self.assertEqual(source.begin,(START+timedelta(hours=1)).isoformat())
        self.assertEqual(source.power,'sensor.current_power')
        # A reused energy ID still has distinct physical epochs.
        value = self.assignment()
        value['assignments'][0]['observed_device_statistics'] = {'heater':'sensor.energy_a'}
        segments = heating_source_segments(self.sources(),value,START,START+timedelta(hours=2))
        self.assertNotEqual(*(heating_counter_sources(item.sources)['heater'].key for item in segments))

    def test_power_association_is_validated_and_included_in_overlap_conflicts(self):
        value = self.assignment()
        value['assignments'].append({**value['assignments'][0], 'observed_device_power_statistics':{'heater':'sensor.other'}})
        with self.assertRaises(ValueError):
            validate_heating_lineage(value)
        value = lineage()
        value['assignments'][0]['observed_device_power_statistics'] = {'unmapped':'sensor.power'}
        with self.assertRaises(ValueError):
            validate_heating_lineage(value)

    async def test_pending_pair_defers_its_cursor_without_blocking_unpaired_peer(self):
        recorder,ledger = PowerRecorder(),{}
        sources = await prepare_heating_counter_evidence(recorder,self.sources(),None,ledger,NOW)
        reader,pending = qualified_heating_reader(recorder,sources,ledger)
        self.assertEqual(pending,{'heater'})
        self.assertEqual(reader.bounds_w,{})
        previous = {'rooms':{'room_a':{'before':'previous'}}}
        pending_cursor = {'rooms':{'room_a':{'before':'new'},'room_b':{'before':'peer'}},'deferred_rooms':['room_a']}
        ack = {'heating_observations_accepted':0,'heating_observations_accepted_by_source':[]}
        result = accepted_heating_cursor(ack,[],pending_cursor,previous)
        self.assertEqual(result['rooms'],{'room_a':{'before':'previous'},'room_b':{'before':'peer'}})

    async def test_changed_envelope_restarts_only_its_room_signature_and_identical_reads_do_not(self):
        recorder,ledger = PowerRecorder(),{}
        sources = replace(self.sources(),bindings=[binding(),replace(binding(devices=('other',)),room_key='room_b')],
            device_statistics={'heater':'sensor.energy_a','other':'sensor.energy_b'},
            device_power_statistics={'heater':'sensor.current_power','other':'sensor.other_power'})
        recorder.rows['month'] = {'sensor.current_power':[peak(MONTH,START,1000)],
                                 'sensor.other_power':[peak(MONTH,START,2000)]}
        first = await prepare_heating_counter_evidence(recorder,sources,None,ledger,NOW)
        same = await prepare_heating_counter_evidence(recorder,sources,None,ledger,NOW+timedelta(hours=1))
        self.assertEqual(first.signature,same.signature)
        recorder.rows['month']['sensor.current_power'][0]['max'] = 800
        changed = await prepare_heating_counter_evidence(recorder,sources,None,ledger,NOW+timedelta(hours=2))
        self.assertNotEqual(first.for_room(first.bindings[0]).signature,changed.for_room(changed.bindings[0]).signature)
        self.assertEqual(first.for_room(first.bindings[1]).signature,changed.for_room(changed.bindings[1]).signature)
