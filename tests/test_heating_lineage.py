"""Synthetic physical source assignments and shared neutral Recorder facts."""
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from test_heating_history import START, PROBES, Recorder, binding, owner, source_fixture, temperature
from household_fixture import Rig
from shs_core.heating_history import (HeatingHistorySources, accepted_heating_cursor, queue_probe_repair,
    settle_probe_repair, probe_repair_windows, next_probe_repair)
from shs_core.heating_lineage import heating_source_segments, validate_heating_lineage, heating_lineage_revision
from shs_core.probe_evidence import native_probe_bounds, probe_interval_facts, probe_source_topology
from shs_core.household_ports import HouseholdReadError


def key(value):
    return sha256(value.encode()).hexdigest()


def lineage():
    return {"revision":"synthetic-v1", "assignments":[{
        "room_key":"room_a", "target_source_key":key("sensor.air_a"),
        "target_device_statistics":{"heater":"sensor.energy_a"},
        "observed_source_key":key("sensor.previous_air"),
        "observed_temperature_entity_id":"sensor.previous_air",
        "observed_device_statistics":{"heater":"sensor.previous_energy"},
        "valid_from":START.isoformat(), "valid_to":(START+timedelta(hours=1)).isoformat(),
        "heater_energy_limits_w":{"heater":800}}]}


class ProbeRepairQueueTests(unittest.TestCase):
    @staticmethod
    def at(hours):
        return (START+timedelta(hours=hours)).isoformat()

    def test_legacy_pending_span_migrates_once_without_completed_gap(self):
        evidence = {"repair_from":self.at(-100), "repair_before":self.at(-40)}
        expected = [[self.at(-100),self.at(-40)]]
        self.assertEqual(probe_repair_windows(evidence),expected)
        self.assertEqual(probe_repair_windows(evidence),expected)
        self.assertNotIn("repair_from",evidence)
        queue_probe_repair(evidence,self.at(-2),self.at(0))
        self.assertEqual(evidence["repair_windows"],expected+[[self.at(-2),self.at(0)]])

    def test_acknowledged_middle_splits_queue_and_recent_failure_preserves_gap(self):
        evidence = {}
        queue_probe_repair(evidence,self.at(-100),self.at(-40))
        settle_probe_repair(evidence,self.at(-80),self.at(-60))
        queue_probe_repair(evidence,self.at(-2),self.at(0))
        self.assertEqual(evidence["repair_windows"],[
            [self.at(-100),self.at(-80)],[self.at(-60),self.at(-40)],
            [self.at(-2),self.at(0)]])

    def test_repeated_recent_failures_cannot_starve_old_repairs_or_thermal_progress(self):
        previous = {"rooms":{},"probe_evidence":{}}
        queue_probe_repair(previous["probe_evidence"],self.at(-100),self.at(-40))
        rows = [{"room_key":"room_a","source_key":"source_a"}]
        reply = {"heating_observations_accepted":1,"heating_probe_observations_accepted":1,
            "heating_observations_accepted_by_source":[{**rows[0],"accepted":1}]}
        for cycle in range(6):
            evidence = dict(previous["probe_evidence"])
            repair = next_probe_repair(evidence,START+timedelta(hours=cycle),10)
            self.assertEqual([t.isoformat() for t in repair],
                [self.at(-100+10*cycle),self.at(-90+10*cycle)])
            recent = [self.at(cycle-2),self.at(cycle)]
            pending = {"rooms":{"room_a":{"before":self.at(-10-cycle)}},
                "probe_evidence":evidence,"probe_capture":{
                    "windows":[[t.isoformat() for t in repair],recent],
                    "read_failed_windows":[recent],
                    "repair_window":[t.isoformat() for t in repair]}}
            previous = accepted_heating_cursor(reply,rows,pending,previous,probe_observations=[{}])
            self.assertEqual(previous["rooms"],pending["rooms"])
        self.assertEqual(previous["probe_evidence"]["repair_windows"],
            [[self.at(-2),self.at(5)]])

    def test_retention_clips_expired_work_without_losing_future_pending_tail(self):
        evidence = {}
        queue_probe_repair(evidence,self.at(-401*24),self.at(-399*24))
        queue_probe_repair(evidence,self.at(1),self.at(2))
        repair = next_probe_repair(evidence,START,6)
        self.assertEqual(repair,(START-timedelta(days=400),START-timedelta(days=400)+timedelta(hours=6)))
        self.assertEqual(evidence["repair_windows"][-1],[self.at(1),self.at(2)])


class SourceLineageTests(unittest.TestCase):
    def sources(self):
        return HeatingHistorySources([binding()], {"heater":"sensor.energy_a"}, PROBES, "sensor.outdoor")

    def test_dated_sources_update_own_room_and_shared_house_contributors(self):
        found = heating_source_segments(self.sources(), validate_heating_lineage(lineage()),
            START, START+timedelta(hours=2))
        self.assertEqual(len(found), 2)
        old, current = [segment.sources for segment in found]
        self.assertEqual(old.bindings[0].temperature_entity_id, "sensor.previous_air")
        self.assertEqual(old.device_statistics, {"heater":"sensor.previous_energy"})
        self.assertEqual(old.room_air_probes["room_a"], "sensor.previous_air")
        self.assertEqual(old.bindings[0].heater_limits_w, {"heater":800})
        self.assertEqual(current, replace(self.sources(), counter_epochs={"heater": ((START+timedelta(hours=1)).isoformat(), None)}))
        self.assertEqual(found[0].end, found[1].start)

    def test_other_physical_binding_cannot_inherit_previous_heater(self):
        source = replace(self.sources(), device_statistics={"heater":"sensor.replacement"})
        found = heating_source_segments(source, validate_heating_lineage(lineage()), START, START+timedelta(hours=2))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].sources, source)

    def test_conflicting_overlap_and_invalid_identity_rejected(self):
        value = lineage()
        value["assignments"][0]["observed_source_key"] = key("sensor.wrong")
        with self.assertRaises(ValueError):
            validate_heating_lineage(value)

    def test_invalid_server_assignment_contracts_are_rejected_before_capture(self):
        for patch in ({"valid_to":"infinity"}, {"observed_device_statistics":{"different":"sensor.old"}},
                      {"heater_energy_limits_w":{"different":800}}):
            value = lineage()
            value["assignments"][0].update(patch)
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_heating_lineage(value)
        value = lineage()
        value["assignments"].append({**value["assignments"][0], "heater_energy_limits_w":{}})
        with self.assertRaises(ValueError):
            validate_heating_lineage(value)

    def test_peer_revision_changes_every_room_signature(self):
        source = self.sources()
        self.assertNotEqual(source.for_room(binding()).signature,
            replace(source, lineage_revision="new-peer-source").for_room(binding()).signature)

    def test_changed_peer_target_invalidates_historical_house_source_signature(self):
        source = self.sources()
        changed = replace(source, device_statistics={"heater":"sensor.replacement"})
        self.assertNotEqual(heating_lineage_revision(source,lineage()), heating_lineage_revision(changed,lineage()))

    def test_ack_requires_actual_sources_but_probe_storage_does_not_stop_measurements(self):
        pending = {"rooms":{"room_a":{"source_key":"current", "before":START.isoformat()}}}
        rows = [{"room_key":"room_a", "source_key":source} for source in ("old", "current")]
        response = {"heating_observations_accepted":1, "heating_probe_observations_accepted":1,
            "heating_observations_accepted_by_source":[{**rows[1], "accepted":1}]}
        self.assertIsNone(accepted_heating_cursor(response, rows, pending, probe_observations=[{}]))
        response["heating_observations_accepted"] = 2
        response["heating_observations_accepted_by_source"].append({**rows[0], "accepted":1})
        self.assertEqual(accepted_heating_cursor(response, rows, pending, probe_observations=[{}]), pending)
        response["heating_probe_observations_accepted"] = None
        pending.update(probe_evidence={},probe_capture={"windows":[[START.isoformat(),(START+timedelta(hours=1)).isoformat()]]})
        captured = accepted_heating_cursor(response, rows, pending, probe_observations=[{}])
        self.assertEqual(captured["rooms"],pending["rooms"])
        self.assertEqual(captured["probe_evidence"]["repair_windows"][0][0],START.isoformat())


class ProbeEvidenceTests(unittest.TestCase):
    def test_registry_companions_require_same_physical_device_and_sensed_class(self):
        classes = {"sensor.air":"temperature", "sensor.humidity":"humidity", "sensor.co2":"carbon_dioxide",
            "sensor.noise":"sound_pressure", "sensor.battery":"battery", "sensor.signal":"signal_strength",
            "sensor.other_humidity":"humidity"}
        entities = {entity:SimpleNamespace(entity_id=entity, device_id="other" if "other" in entity else "probe",
            disabled_by=None) for entity in classes}
        states = {entity:SimpleNamespace(attributes={"device_class":kind,"state_class":"measurement"})
            for entity,kind in classes.items()}
        result = probe_source_topology(["sensor.air"], entities,
            {"probe":SimpleNamespace(via_device_id="coordinator")}, states.get)
        self.assertEqual(result["sensor.air"], {"companions":["sensor.co2","sensor.humidity","sensor.noise"],
            "cohort_key":key("coordinator")})

    def test_joint_unchanged_requires_observed_companion_and_previous_interval(self):
        prior = START-timedelta(hours=1)
        bounds = {"sensor.air":{prior:(20,20,20), START:(20,20,20)},
                  "sensor.rh":{prior:(50,50,50), START:(51,50,52)}}
        topology = {"sensor.air":{"companions":["sensor.rh"],"cohort_key":key("bridge")}}
        def fact():
            return probe_interval_facts(START, 3600, {"room":"sensor.air"}, topology, bounds,
                {"sensor.air":{START:20}})["probes"][0]
        self.assertTrue(fact()["unchanged"])
        self.assertFalse(fact()["joint_unchanged"])
        bounds["sensor.rh"][START] = (50,50,50)
        self.assertTrue(fact()["joint_unchanged"])
        del bounds["sensor.rh"][prior]
        self.assertIsNone(fact()["joint_unchanged"])
        self.assertIsNone(fact()["reported"])
        self.assertIsNone(fact()["unavailable"])

    def test_missing_fine_sample_is_not_a_complete_interval(self):
        rows = [{**temperature(START+timedelta(minutes=5*i), 300,20), "min":20,"max":20} for i in range(3)]
        self.assertEqual(native_probe_bounds(rows[:2],START,START+timedelta(minutes=15),300), {})
        self.assertEqual(native_probe_bounds(rows,START,START+timedelta(minutes=15),300), {START:(20,20,20)})
        rows[0]["min"] = 21
        self.assertEqual(native_probe_bounds(rows,START,START+timedelta(minutes=15),300), {})

    def test_weighted_mean_roundoff_does_not_erase_constant_extrema(self):
        prior = START-timedelta(hours=1)
        # Independently generated numerical boundary, not a household reading.
        from math import nextafter, inf
        reading = 22.5
        weighted = nextafter(reading, inf)
        rows = [{**temperature(at,3600,weighted), "min":reading,"max":reading}
                for at in (prior,START)]
        bounds = native_probe_bounds(rows,prior,START+timedelta(hours=1),3600)
        self.assertEqual(len(bounds),2)
        facts = probe_interval_facts(START,3600,{"room":"sensor.air"},
            {"sensor.air":{"companions":["sensor.humidity"]}},
            {"sensor.air":bounds,"sensor.humidity":bounds},
            {"sensor.air":{START:weighted}})
        self.assertTrue(facts["probes"][0]["unchanged"])
        self.assertTrue(facts["probes"][0]["joint_unchanged"])
        self.assertEqual(facts["probes"][0]["mean"],weighted)


class SourceCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_out_of_range_room_probe_is_unknown_without_losing_heater_energy(self):
        recorder = Recorder()
        recorder.temperatures("hour")
        recorder.rows["hour"]["sensor.air_a"][0]["mean"] = 85
        recorder.energy("sensor.energy_a","hour",[.2])
        captured = await owner(recorder).capture_heating_history(read_energy=owner(recorder)._statistics_changes, bindings=[binding()],
            device_statistics={"heater":"sensor.energy_a"},room_air_probes=PROBES,outdoor_entity="sensor.outdoors",
            start=START,end=START+timedelta(hours=1))
        self.assertEqual(captured.observations[0]["device_energy_kwh"],{"heater":.2})
        self.assertIsNone(captured.observations[0]["room_temperature_c"])
        self.assertIsNone(captured.observations[0]["house_temperature_c"])
        self.assertIsNone(next(probe for probe in captured.probe_observations[0]["probes"] if probe["area_key"]=="room_a")["mean"])

    async def test_source_swap_inside_quarter_never_attributes_a_mixed_interval(self):
        recorder = Recorder()
        recorder.temperatures("5minute",count=24)
        recorder.temperatures("hour",count=2)
        recorder.energy("sensor.energy_a","5minute",[.01]*24)
        recorder.energy("sensor.energy_a","hour",[.12,.12])
        for period in ("5minute","hour"):
            recorder.rows[period]["sensor.previous_air"] = recorder.rows[period]["sensor.air_a"]
        sources = HeatingHistorySources([binding()],{"heater":"sensor.energy_a"},PROBES,"sensor.outdoors")
        catalog = lineage()
        catalog["assignments"][0].update(valid_to=(START+timedelta(minutes=37)).isoformat(),
            observed_device_statistics={"heater":"sensor.energy_a"},heater_energy_limits_w={})
        rows = []
        for segment in heating_source_segments(sources,validate_heating_lineage(catalog),START,START+timedelta(hours=2)):
            q = segment.sources
            result = await owner(recorder).capture_heating_history(read_energy=owner(recorder)._statistics_changes, bindings=q.bindings,device_statistics=q.device_statistics,
                room_air_probes=q.room_air_probes,outdoor_entity=q.outdoor_entity,start=segment.start,end=segment.end)
            rows.extend(result.observations)
        swap = START+timedelta(minutes=37)
        from datetime import datetime
        self.assertTrue(rows)
        for row in rows:
            at = datetime.fromisoformat(row["start"])
            self.assertFalse(at < swap < at+timedelta(seconds=row["duration_s"]))

    async def test_companion_query_failure_preserves_energy_and_exposes_repair_window(self):
        recorder = Recorder()
        recorder.temperatures("hour")
        recorder.energy("sensor.energy_a","hour",[.2])
        original = recorder.statistics
        async def read(start,end,entities,*args):
            if "sensor.humidity" in entities:
                raise HouseholdReadError("companion statistics unavailable")
            return await original(start,end,entities,*args)
        recorder.statistics = read
        result = await owner(recorder).capture_heating_history(read_energy=owner(recorder)._statistics_changes, bindings=[binding()],
            device_statistics={"heater":"sensor.energy_a"},room_air_probes=PROBES,outdoor_entity="sensor.outdoors",
            start=START,end=START+timedelta(hours=1),probe_topology={"sensor.air_a":{"companions":["sensor.humidity"]}})
        self.assertEqual(result.observations[0]["device_energy_kwh"],{"heater":.2})
        self.assertEqual(result.diagnostics["probe_evidence_read_failures"],2)
        self.assertIsNone(result.probe_observations[0]["probes"][0]["joint_unchanged"])
    async def test_server_catalog_is_persisted_and_rpc_omission_preserves_it(self):
        rig = Rig()
        record = {}
        reply = {"device_configuration":[],"home_configuration":{"battery":{"included":False}},
            "heating_history_sources":lineage()}
        await rig.household._record_device_exchange(record,[],reply)
        self.assertEqual(record["heating_history_sources"],validate_heating_lineage(lineage()))
        del reply["heating_history_sources"]
        await rig.household._record_device_exchange(record,[],reply)
        self.assertEqual(record["heating_history_sources"],validate_heating_lineage(lineage()))

    async def test_historical_house_quarters_use_same_source_assignment(self):
        devices,mappings,rooms = source_fixture()
        rig = Rig(options={"rooms":rooms,"device_control_mappings":mappings},
            stored={"heating_history_sources":validate_heating_lineage(lineage())})
        async def means(entities,start,end):
            return {entity:[(start+timedelta(minutes=5*i),value) for i in range(3)]
                for entity,value in {"sensor.previous_air":20,"sensor.air_b":21,"sensor.air_c":22,
                    "sensor.air_a":-10}.items() if entity in entities}
        rig.household._statistics_means = AsyncMock(side_effect=means)
        result = await rig.household._house_temperature_quarters(rig.options,START,START+timedelta(minutes=15),devices=devices)
        self.assertEqual(result,{START:21})
        self.assertNotIn("sensor.air_a",rig.household._statistics_means.call_args.args[0])
    async def test_shared_facts_once_per_bucket_and_do_not_filter_measured_energy(self):
        recorder = Recorder()
        recorder.temperatures("hour", start=START-timedelta(hours=1), count=2)
        for values in recorder.rows["hour"].values():
            for row in values:
                row.update(min=row["mean"],max=row["mean"])
        recorder.energy("sensor.energy_a", "hour", [.2])
        recorder.rows["hour"]["sensor.rh"] = [{**temperature(START+timedelta(hours=i),3600,50),"min":50,"max":50} for i in (-1,0)]
        result = await owner(recorder).capture_heating_history(read_energy=owner(recorder)._statistics_changes, bindings=[binding(),replace(binding(),room_key="room_b")],
            device_statistics={"heater":"sensor.energy_a"},room_air_probes=PROBES,outdoor_entity="sensor.outdoors",
            start=START,end=START+timedelta(hours=1),probe_topology={"sensor.air_a":{"companions":["sensor.rh"]}})
        self.assertEqual(len(result.observations), 2)
        self.assertEqual(len(result.probe_observations), 1)
        self.assertEqual(result.observations[0]["device_energy_kwh"], {"heater":.2})
        fact = next(f for f in result.probe_observations[0]["probes"] if f["area_key"]=="room_a")
        self.assertTrue(fact["joint_unchanged"])
        self.assertEqual(result.observations[0]["heater_energy_limits_w"], {})

    async def test_automatic_backfill_waits_for_source_catalog_and_resolves_all_peers(self):
        devices, mappings, rooms = source_fixture()
        rig = Rig(options={"rooms":rooms,"device_control_mappings":mappings})
        rig.household.capture_heating_history = AsyncMock(return_value=SimpleNamespace(
            observations=[],probe_observations=[],diagnostics={}))
        end = START+timedelta(hours=2)
        await rig.household._heating_history_upload(rig.options,devices,end,None)
        self.assertEqual(rig.household.capture_heating_history.await_count,1)
        rig.household.capture_heating_history.reset_mock()
        await rig.household._heating_history_upload(rig.options,devices,end,None,lineage=lineage())
        calls = [call.kwargs for call in rig.household.capture_heating_history.call_args_list]
        old = next(call for call in calls if call["start"]==START)
        self.assertEqual(old["bindings"][0].temperature_entity_id,"sensor.previous_air")
        self.assertEqual(old["room_air_probes"]["room_a"],"sensor.previous_air")
        self.assertEqual(old["device_statistics"],{"heater":"sensor.previous_energy"})

    async def test_metadata_outage_keeps_energy_progress_and_queues_auxiliary_repair(self):
        devices,mappings,rooms = source_fixture()
        rig = Rig(options={"rooms":rooms,"device_control_mappings":mappings})
        rig.history.probe_sources.side_effect = HouseholdReadError("registry unavailable")
        row = {"room_key":"room_a","source_key":key("sensor.air_a"),"start":START.isoformat(),"duration_s":3600}
        rig.household.capture_heating_history = AsyncMock(return_value=SimpleNamespace(
            observations=[row],probe_observations=[],diagnostics={}))
        rows,_,pending = await rig.household._heating_history_upload(rig.options,devices,START,None)
        self.assertEqual(rows,[row])
        reply = {"heating_observations_accepted":1,"heating_probe_observations_accepted":0,
            "heating_observations_accepted_by_source":[{"room_key":"room_a","source_key":row["source_key"],"accepted":1}]}
        cursor = accepted_heating_cursor(reply,rows,pending,probe_observations=[])
        self.assertEqual(cursor["rooms"],pending["rooms"])
        self.assertTrue(cursor["probe_evidence"]["repair_windows"])
        # Recovery discovers the topology, queues a complete evidence resweep,
        # and leaves the normal thermal cursor advancing independently.
        rig.history.probe_sources.side_effect = None
        rig.history.probe_sources.return_value = {"sensor.air_a":{"companions":["sensor.humidity"],"cohort_key":None}}
        _,_,next_pending = await rig.household._heating_history_upload(rig.options,devices,START,cursor)
        self.assertEqual(next_pending["probe_evidence"]["repair_windows"][0][0],(START-timedelta(days=400)).isoformat())
        self.assertEqual(next_pending["rooms"],cursor["rooms"])
        repaired = accepted_heating_cursor(reply,rows,next_pending,cursor,probe_observations=[])
        self.assertGreater(repaired["probe_evidence"]["repair_windows"][0][0],next_pending["probe_evidence"]["repair_windows"][0][0])
