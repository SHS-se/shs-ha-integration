"""Independently generated Recorder evidence through the production capture owner."""
import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).parents[1] / "custom_components/shs_energy"))
from shs_core.heating_history import (HeatingRoomBinding, heating_history_sources, heating_history_window,
                                      accepted_heating_cursor, heating_recent_window)
from shs_core.api import ShsApiClient
from shs_core.household_ports import HouseholdReadError
from shs_core.household import Household

START = datetime(2025, 10, 1, tzinfo=timezone.utc)
PROBES = {"room_a": "sensor.air_a", "room_b": "sensor.air_b", "room_c": "sensor.air_c"}


def temperature(at, seconds, value):
    return {"start": at.timestamp(), "end": (at + timedelta(seconds=seconds)).timestamp(), "mean": value}


def counter(at, seconds, state, change):
    return {"start": at.timestamp(), "end": (at + timedelta(seconds=seconds)).timestamp(),
            "state": state, "change": change}


class Recorder:
    def __init__(self):
        self.rows = {"hour": {}, "5minute": {}}
        self.events = {}
        self.reads = []

    async def statistics(self, start, end, entities, period, units, kinds):
        self.reads.append((set(entities), period, kinds))
        return {entity: [row for row in self.rows[period].get(entity, ())
                         if start.timestamp() <= row["start"] <= end.timestamp()]
                for entity in entities}

    async def states(self, start, end, entities, *, with_attributes):
        return {entity: self.events.get(entity, []) for entity in entities}

    def temperatures(self, period, start=START, count=1):
        seconds = 3600 if period == "hour" else 300
        for entity, value in {"sensor.air_a": 20., "sensor.air_b": 19.,
                              "sensor.air_c": 21., "sensor.outdoors": -6.}.items():
            self.rows[period][entity] = [temperature(start + timedelta(seconds=i * seconds), seconds, value)
                                        for i in range(count)]

    def energy(self, entity, period, changes, start=START):
        seconds = 3600 if period == "hour" else 300
        state = 100.
        rows = [counter(start - timedelta(seconds=seconds), seconds, state, 0.)]
        for i, change in enumerate(changes):
            state += change
            rows.append(counter(start + timedelta(seconds=i * seconds), seconds, state, change))
        self.rows[period][entity] = rows


def owner(recorder):
    household = Household.__new__(Household)
    household.ports = SimpleNamespace(history=recorder)
    household._counter_bounds_w = {}
    household._open_catch_up = None
    household._store = SimpleNamespace(async_read=AsyncMock(return_value={}))
    return household


def binding(*, devices=("heater",), resistance=True, actuators=()):
    return HeatingRoomBinding("room_a", sha256(b"sensor.air_a").hexdigest(),
                              "sensor.air_a", devices, resistance, actuators)


async def capture(recorder, *, room=None, start=START, end=None, household=None):
    return await (household or owner(recorder)).capture_heating_history(
        bindings=[room or binding()], device_statistics={"heater": "sensor.energy_a", "other": "sensor.energy_b"},
        room_air_probes=PROBES, outdoor_entity="sensor.outdoors", start=start,
        end=end or start + timedelta(hours=1),
    )


class NativeHeatingHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_hour_stays_one_hour_with_source_and_meter_identity(self):
        recorder = Recorder()
        recorder.temperatures("hour")
        recorder.energy("sensor.energy_a", "hour", [.5])
        result = await capture(recorder)
        self.assertEqual(len(result.observations), 1)
        row = result.observations[0]
        self.assertEqual(row["duration_s"], 3600)
        self.assertEqual(row["source"], "recorder_hour_mean")
        self.assertEqual(row["device_energy_kwh"], {"heater": .5})
        self.assertEqual(row["device_statistics"], {"heater": "sensor.energy_a"})
        self.assertEqual(row["outdoor_temperature_c"], -6)
        self.assertEqual(row["house_temperature_c"], 20)
        self.assertIs(row["cooling"], False)
        self.assertEqual(row["source_key"], binding().source_key)

    async def test_four_complete_fine_intervals_supersede_native_hour(self):
        recorder = Recorder()
        recorder.temperatures("hour")
        recorder.temperatures("5minute", count=12)
        recorder.energy("sensor.energy_a", "hour", [1.2])
        recorder.energy("sensor.energy_a", "5minute", [.1] * 12)
        result = await capture(recorder)
        self.assertEqual([row["duration_s"] for row in result.observations], [900] * 4)
        self.assertAlmostEqual(sum(row["device_energy_kwh"]["heater"] for row in result.observations), 1.2)
        self.assertEqual(result.diagnostics, {"quarter_intervals": 4})

    async def test_incomplete_fine_coverage_uses_native_hour_without_overlap(self):
        recorder = Recorder()
        recorder.temperatures("hour")
        recorder.temperatures("5minute", count=12)
        recorder.rows["5minute"]["sensor.air_a"] = [r for i, r in enumerate(recorder.rows["5minute"]["sensor.air_a"]) if i not in (3, 4, 5)]
        recorder.energy("sensor.energy_a", "hour", [.6])
        recorder.energy("sensor.energy_a", "5minute", [.05] * 12)
        result = await capture(recorder)
        self.assertEqual([row["duration_s"] for row in result.observations], [3600])
        self.assertEqual(result.observations[0]["device_energy_kwh"]["heater"], .6)

    async def test_two_of_three_temperature_samples_are_not_a_native_quarter_mean(self):
        for hourly in (False, True):
            with self.subTest(hourly=hourly):
                recorder = Recorder()
                recorder.temperatures("5minute", count=12)
                recorder.rows["5minute"]["sensor.air_a"].pop(1)
                recorder.energy("sensor.energy_a", "5minute", [.05] * 12)
                if hourly:
                    recorder.temperatures("hour")
                    recorder.energy("sensor.energy_a", "hour", [.6])
                result = await capture(recorder)
                if hourly:
                    self.assertEqual([row["duration_s"] for row in result.observations], [3600])
                    self.assertEqual(result.observations[0]["room_temperature_c"], 20.)
                else:
                    self.assertEqual([row["duration_s"] for row in result.observations], [900] * 4)
                    self.assertIsNone(result.observations[0]["room_temperature_c"])
                    self.assertAlmostEqual(result.observations[0]["device_energy_kwh"]["heater"], .15)
                    self.assertEqual(result.observations[1]["room_temperature_c"], 20.)

    async def test_missing_member_is_unknown_and_keeps_available_fine_evidence(self):
        recorder = Recorder()
        recorder.temperatures("5minute", count=6)
        recorder.energy("sensor.energy_a", "5minute", [.05] * 6)
        result = await capture(recorder, room=binding(devices=("heater", "other")))
        self.assertEqual(len(result.observations), 2)
        self.assertTrue(all(row["device_energy_kwh"]["other"] is None for row in result.observations))
        self.assertEqual(result.diagnostics["incomplete_meter_intervals"], 2)

    async def test_actual_zero_and_unavailable_counter_remain_distinct(self):
        recorder = Recorder()
        recorder.temperatures("hour", count=2)
        recorder.energy("sensor.energy_a", "hour", [0.])
        result = await capture(recorder, end=START + timedelta(hours=2))
        self.assertEqual(result.observations[0]["device_energy_kwh"]["heater"], 0)
        self.assertIsNone(result.observations[1]["device_energy_kwh"]["heater"])

    async def test_counter_gap_does_not_assign_accumulated_energy_to_next_hour(self):
        recorder = Recorder()
        recorder.temperatures("hour", count=4)
        recorder.energy("sensor.energy_a", "hour", [.2, .2, .4, .2])
        rows = recorder.rows["hour"]["sensor.energy_a"]
        del rows[2]  # missing second hour; next Recorder change spans the gap
        rows[2]["change"] = .6
        result = await capture(recorder, end=START + timedelta(hours=4))
        values = [row["device_energy_kwh"]["heater"] for row in result.observations]
        self.assertEqual(values, [.2, None, None, .2])

    async def test_counter_reset_without_raw_readings_stays_unknown(self):
        recorder = Recorder()
        recorder.temperatures("hour", count=2)
        recorder.rows["hour"]["sensor.energy_a"] = [counter(START - timedelta(hours=1), 3600, 100., 0.),
            counter(START, 3600, 0., 0.), counter(START + timedelta(hours=1), 3600, 100.2, 100.2)]
        result = await capture(recorder, end=START + timedelta(hours=2))
        self.assertTrue(all(row["device_energy_kwh"]["heater"] is None for row in result.observations))

    async def test_standard_counter_catchup_cleaning_runs_before_join(self):
        recorder = Recorder()
        recorder.temperatures("hour", count=3)
        recorder.energy("sensor.energy_a", "hour", [0., 0., 2.])
        household = owner(recorder)
        household._counter_bounds_w = {"sensor.energy_a": 500.}
        result = await capture(recorder, household=household, end=START + timedelta(hours=3))
        self.assertTrue(all(row["device_energy_kwh"]["heater"] is None for row in result.observations))

    async def test_later_catchup_explicitly_retracts_previously_accepted_false_zeros(self):
        recorder = Recorder()
        recorder.temperatures("hour", count=3)
        recorder.energy("sensor.energy_a", "hour", [0., 0.])
        household = owner(recorder)
        household._counter_bounds_w = {"sensor.energy_a": 500.}
        initial = await capture(recorder, household=household, end=START + timedelta(hours=2))
        self.assertTrue(all(row["device_energy_kwh"]["heater"] == 0 for row in initial.observations))
        self.assertTrue(all(row["invalidated_device_keys"] == [] for row in initial.observations))
        recorder.energy("sensor.energy_a", "hour", [0., 0., 2.])
        repaired = await capture(recorder, household=household, end=START + timedelta(hours=3))
        self.assertEqual([row["duration_s"] for row in repaired.observations], [3600] * 3)
        self.assertTrue(all(row["invalidated_device_keys"] == ["heater"] for row in repaired.observations))

    async def test_absent_counter_and_missing_predecessor_do_not_withdraw_previous_measurements(self):
        recorder = Recorder()
        recorder.temperatures("hour", count=2)
        recorder.energy("sensor.energy_a", "hour", [.2])
        del recorder.rows["hour"]["sensor.energy_a"][0]
        result = await capture(recorder, end=START + timedelta(hours=2))
        self.assertTrue(all(row["device_energy_kwh"]["heater"] is None for row in result.observations))
        self.assertTrue(all(row["invalidated_device_keys"] == [] for row in result.observations))

    async def test_fine_counter_withdrawal_survives_selection_of_valid_native_hour(self):
        recorder = Recorder()
        recorder.temperatures("hour")
        recorder.temperatures("5minute", count=12)
        recorder.energy("sensor.energy_a", "hour", [.3])
        recorder.energy("sensor.energy_a", "5minute", [0., 0., .3] + [0.] * 9)
        household = owner(recorder)
        household._counter_bounds_w = {"sensor.energy_a": 500.}
        result = await capture(recorder, household=household)
        self.assertEqual(len(result.observations), 1)
        self.assertEqual(result.observations[0]["duration_s"], 3600)
        self.assertEqual(result.observations[0]["device_energy_kwh"]["heater"], .3)
        self.assertEqual(result.observations[0]["invalidated_device_keys"], ["heater"])

    async def test_coarse_counter_withdrawal_survives_selection_of_complete_quarters(self):
        recorder = Recorder()
        recorder.temperatures("hour")
        recorder.temperatures("5minute", count=12)
        recorder.energy("sensor.energy_a", "hour", [2.])
        recorder.energy("sensor.energy_a", "5minute", [.025] * 12)
        household = owner(recorder)
        household._counter_bounds_w = {"sensor.energy_a": 500.}
        result = await capture(recorder, household=household)
        self.assertEqual([row["duration_s"] for row in result.observations], [900] * 4)
        self.assertTrue(all(row["invalidated_device_keys"] == ["heater"] for row in result.observations))
        self.assertTrue(all(row["device_energy_kwh"]["heater"] is not None for row in result.observations))

    async def test_explicit_shared_meter_heater_limit_preserves_native_raw_hour(self):
        recorder = Recorder()
        recorder.temperatures("hour")
        recorder.energy("sensor.energy_a", "hour", [1.2])
        room = replace(binding(), heater_limits_w={"heater": 800.})
        result = await capture(recorder, room=room)
        row = result.observations[0]
        self.assertEqual(row["duration_s"], 3600)
        self.assertEqual(row["device_energy_kwh"], {"heater": 1.2})
        self.assertEqual(row["heater_energy_limits_w"], {"heater": 800.})
        self.assertEqual(row["invalidated_device_keys"], [])

    async def test_explicit_heater_limit_preserves_each_measured_quarter(self):
        recorder = Recorder()
        recorder.temperatures("5minute", count=12)
        recorder.energy("sensor.energy_a", "5minute", [.1] * 12)
        room = replace(binding(), heater_limits_w={"heater": 800.})
        result = await capture(recorder, room=room)
        self.assertEqual(len(result.observations), 4)
        for row in result.observations:
            self.assertEqual(row["duration_s"], 900)
            self.assertAlmostEqual(row["device_energy_kwh"]["heater"], .3)
            self.assertEqual(row["heater_energy_limits_w"], {"heater": 800.})

    async def test_heater_limit_must_be_explicit_finite_positive_and_for_bound_member(self):
        recorder = Recorder()
        for limits in ({"other": 800}, {"heater": 0}, {"heater": -1}, {"heater": float("nan")},
                       {"heater": float("inf")}, {"heater": True}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                await capture(recorder, room=replace(binding(), heater_limits_w=limits))
        self.assertEqual(recorder.reads, [])

    async def test_unknown_reversible_mode_is_never_assumed_heating(self):
        recorder = Recorder()
        recorder.temperatures("hour")
        recorder.energy("sensor.energy_a", "hour", [.5])
        result = await capture(recorder, room=binding(resistance=False, actuators=("climate.room",)))
        self.assertIsNone(result.observations[0]["cooling"])
        self.assertEqual(result.diagnostics["cooling_unobserved_intervals"], 1)
        recorder.events["climate.room"] = [(START, "heat", {"hvac_action": "heating"})]
        result = await capture(recorder, room=binding(resistance=False, actuators=("climate.room",)))
        self.assertIs(result.observations[0]["cooling"], False)
        recorder.events["climate.room"] = [(START, "heat_cool", {"hvac_action": "cooling"})]
        result = await capture(recorder, room=binding(resistance=False, actuators=("climate.room",)))
        self.assertIs(result.observations[0]["cooling"], True)

    async def test_configured_heating_relay_does_not_make_reversible_room_mode_unknown(self):
        recorder = Recorder()
        recorder.temperatures("hour")
        recorder.energy("sensor.energy_a", "hour", [.5])
        recorder.events["climate.reversible"] = [(START, "heat", {"hvac_action": "heating"})]
        room = HeatingRoomBinding("room_a", binding().source_key, "sensor.air_a", ("heater",), False,
                                  ("switch.heater", "climate.reversible"), ("switch.heater",))
        result = await capture(recorder, room=room)
        self.assertIs(result.observations[0]["cooling"], False)
        recorder.events["climate.reversible"] = [(START, "cool", {"hvac_action": "cooling"})]
        result = await capture(recorder, room=room)
        self.assertIs(result.observations[0]["cooling"], True)

    async def test_house_uses_selected_room_air_probes_not_equipment_temperatures(self):
        recorder = Recorder()
        recorder.temperatures("hour")
        recorder.rows["hour"]["sensor.equipment_chip"] = [temperature(START, 3600, 80.)]
        recorder.energy("sensor.energy_a", "hour", [.1])
        result = await capture(recorder)
        self.assertEqual(result.observations[0]["house_temperature_c"], 20)
        self.assertFalse(any("sensor.equipment_chip" in entities for entities, _, _ in recorder.reads))

    async def test_native_and_planning_house_boundary_use_same_configured_room_air(self):
        recorder = Recorder()
        recorder.temperatures("5minute", count=12)
        recorder.energy("sensor.energy_a", "5minute", [.01] * 12)
        for entity, value in {"sensor.equipment_chip": 80., "sensor.derived_temperature": 60.,
                              "sensor.pool_water": 30.}.items():
            recorder.rows["5minute"][entity] = [temperature(START + timedelta(minutes=i * 5), 300, value) for i in range(12)]
        household = owner(recorder)
        rooms = {room: {"temperature_entity_id": entity} for room, entity in PROBES.items()}
        rooms.update(outside={"temperature_entity_id": "sensor.outdoors"},
                     water={"temperature_entity_id": "sensor.pool_water"})
        planned = await household._house_temperature_quarters({"rooms": rooms,
            "outdoor_temperature_entity": "sensor.outdoors", "pool_water_temperature_entity": "sensor.pool_water"},
            START, START + timedelta(hours=1))
        native = await capture(recorder, household=household)
        self.assertEqual(list(planned.values()), [20.] * 4)
        self.assertEqual([row["house_temperature_c"] for row in native.observations], list(planned.values()))
        self.assertFalse(any("sensor.equipment_chip" in entities or "sensor.derived_temperature" in entities
                             or "sensor.pool_water" in entities for entities, _, _ in recorder.reads))

    async def test_half_open_window_and_dst_preserve_actual_elapsed_hours(self):
        start = datetime(2025, 10, 26, 1, tzinfo=ZoneInfo("Europe/Stockholm"))
        end = datetime(2025, 10, 26, 4, tzinfo=ZoneInfo("Europe/Stockholm"))
        utc = start.astimezone(timezone.utc)
        recorder = Recorder()
        recorder.temperatures("hour", start=utc, count=5)  # Recorder includes end bucket
        recorder.energy("sensor.energy_a", "hour", [.2] * 5, start=utc)
        result = await capture(recorder, start=start, end=end)
        self.assertEqual(len(result.observations), 4)
        stamps = [datetime.fromisoformat(row["start"]) for row in result.observations]
        self.assertEqual(stamps, [utc + timedelta(hours=i) for i in range(4)])
        self.assertTrue(all(row["duration_s"] == 3600 for row in result.observations))


def source_fixture():
    devices = [{"key": "heater", "statistic_id": "sensor.energy_a", "category": "heating",
                "planning_role": "controllable", "control_type": "switch_schedule",
                "mapping_summary": {"room_key": "room_a"}}]
    mappings = {"heater": {"control_type": "switch_schedule", "temperature_entity_id": "sensor.air_a",
                           "actuator_entity_ids": ["climate.heater"]}}
    rooms = {room: {"temperature_entity_id": entity} for room, entity in PROBES.items()}
    return devices, mappings, rooms


class HeatingSourceTests(unittest.TestCase):
    def test_verified_heating_scope_and_explicit_climate_capabilities(self):
        devices, mappings, rooms = source_fixture()
        devices += [{**devices[0], "key": "pool", "category": "pool_heating", "mapping_summary": {"room_key": "pool_room"}},
                    {**devices[0], "key": "cooler", "category": "cooling", "mapping_summary": {"room_key": "cooling_room"}}]
        mappings.update(pool=mappings["heater"], cooler=mappings["heater"])
        rooms.update(water={"temperature_entity_id": "sensor.water"},
                     outdoors={"temperature_entity_id": "sensor.outdoors"})
        def sources(modes):
            return heating_history_sources(devices, mappings, rooms,
                lambda _: SimpleNamespace(attributes={"hvac_modes": modes}),
                outdoor_entity="sensor.outdoors", pool_water_entity="sensor.water")
        value = sources(["heat", "off"])
        self.assertEqual(value.bindings[0].device_keys, ("heater",))
        self.assertTrue(value.bindings[0].heating_only)
        self.assertEqual(value.room_air_probes, PROBES)
        self.assertFalse(sources(["heat", "cool", "off"]).bindings[0].heating_only)
        mappings["heater"]["actuator_entity_ids"] = ["switch.heater"]
        self.assertTrue(sources(["heat", "off"]).bindings[0].heating_only)
        mappings["heater"]["actuator_entity_ids"] = ["switch.heater", "climate.reversible"]
        self.assertFalse(sources(["heat", "cool", "off"]).bindings[0].heating_only)

    def test_mixed_category_room_matches_server_eligibility_before_upload(self):
        devices, mappings, rooms = source_fixture()
        devices.append({**devices[0], "key": "cooler", "category": "cooling"})
        mappings["cooler"] = {**mappings["heater"], "actuator_entity_ids": ["climate.cooler"]}
        sources = heating_history_sources(devices, mappings, rooms, lambda _: None)
        self.assertEqual(sources.bindings, [])
        devices[-1]["planning_role"] = "base_load"
        sources = heating_history_sources(devices, mappings, rooms, lambda _: None)
        self.assertEqual(sources.bindings[0].device_keys, ("heater",))

    def test_mapping_probe_disagreement_never_changes_source_identity(self):
        devices, mappings, rooms = source_fixture()
        rooms["room_a"]["temperature_entity_id"] = "sensor.replacement"
        value = heating_history_sources(devices, mappings, rooms, lambda _: None)
        self.assertEqual(value.bindings, [])

    def test_source_changes_restart_bounded_400_day_cursor(self):
        devices, mappings, rooms = source_fixture()
        def sources():
            return heating_history_sources(devices, mappings, rooms, lambda _: None)
        value = sources()
        end = START + timedelta(days=420, minutes=15)
        first = heating_history_window(value, None, end)
        self.assertEqual(first[1], end.replace(minute=0) - timedelta(hours=2))
        self.assertLessEqual(first[1] - first[0], timedelta(days=1))
        cursor = {"signature": value.signature, "before": first[0].isoformat()}
        second = heating_history_window(value, cursor, end)
        self.assertEqual(second[1], first[0])
        devices[0]["statistic_id"] = "sensor.new_meter"
        self.assertEqual(heating_history_window(sources(), cursor, end), first)
        floor = end.replace(minute=0) - timedelta(days=400)
        cursor = {"signature": value.signature, "before": floor.isoformat()}
        self.assertIsNone(heating_history_window(value, cursor, end))

    def test_long_upload_outage_is_replayed_forward_without_leaving_a_gap(self):
        devices, mappings, rooms = source_fixture()
        value = heating_history_sources(devices, mappings, rooms, lambda _: None)
        end = START + timedelta(days=10)
        cursor = {"signature": value.signature, "before": START.isoformat(),
                  "accepted_until": (START + timedelta(hours=2)).isoformat()}
        start, finish = heating_recent_window(value, cursor, end)
        self.assertEqual(start, START)
        self.assertEqual(finish, START + timedelta(days=1))
        cursor["accepted_until"] = finish.isoformat()
        next_start, next_end = heating_recent_window(value, cursor, end)
        self.assertEqual(next_start, finish - timedelta(hours=2))
        self.assertGreater(next_end, finish)

    def test_cursor_advances_only_exact_source_ack_including_empty_reads(self):
        pending = {"rooms": {"room_a": {"source_key": "source_a", "signature": "synthetic", "before": START.isoformat()}}}
        row = {"room_key": "room_a", "source_key": "source_a"}
        for result in ({}, {"heating_observations_accepted": True}, {"heating_observations_accepted": 0},
                       {"heating_observations_accepted": 1}):
            self.assertIsNone(accepted_heating_cursor(result, [row], pending))
        reply = {"heating_observations_accepted": 1, "heating_observations_accepted_by_source": [
            {"room_key": "room_a", "source_key": "source_a", "accepted": 1}]}
        self.assertEqual(accepted_heating_cursor(reply, [row], pending), pending)
        empty = {"heating_observations_accepted": 0, "heating_observations_accepted_by_source": []}
        self.assertEqual(accepted_heating_cursor(empty, [], pending), pending)
        self.assertIsNone(accepted_heating_cursor(empty, None, pending))
        self.assertIsNone(accepted_heating_cursor(empty, [row], pending))

    def test_rejected_room_preserves_own_cursor_while_other_sources_advance(self):
        old = {"rooms": {room: {"source_key": room + "_source", "signature": room,
            "before": START.isoformat()} for room in ("room_a", "room_b")}}
        pending = {"rooms": {room: {**cursor, "before": (START - timedelta(hours=1)).isoformat()}
            for room, cursor in old["rooms"].items()}}
        rows = [{"room_key": room, "source_key": room + "_source"} for room in old["rooms"]]
        reply = {"heating_observations_accepted": 1, "heating_observations_accepted_by_source": [
            {"room_key": "room_a", "source_key": "room_a_source", "accepted": 1}]}
        result = accepted_heating_cursor(reply, rows, pending, old)
        self.assertEqual(result["rooms"]["room_a"], pending["rooms"]["room_a"])
        self.assertEqual(result["rooms"]["room_b"], old["rooms"]["room_b"])
        reply["heating_observations_accepted_by_source"][0]["source_key"] = "stale_probe"
        self.assertIsNone(accepted_heating_cursor(reply, rows, pending, old))

    def test_empty_capture_remains_explicit_in_upload(self):
        self.assertEqual(ShsApiClient.prepare_optimisation_submission([], heating_observations=[])["heating_observations"], [])
        self.assertNotIn("heating_observations", ShsApiClient.prepare_optimisation_submission([]))


class HeatingExchangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import test_planning_jobs as jobs
        jobs.PlanningJobTests.setUp(self)

    async def test_native_read_failure_cannot_stop_planning_or_advance_cursor(self):
        self.h._heating_history_upload = AsyncMock(side_effect=HouseholdReadError("recorder unavailable"))
        before = {"signature": "synthetic", "before": START.isoformat()}
        self.rig.records.saved["heating_history_cursor"] = before
        await self.h.async_optimisation_push(force_plan=True)
        body = self.h.client.submit_optimisation.call_args.args[0]
        self.assertIn("snapshot", body)
        self.assertNotIn("heating_observations", body)
        self.assertEqual(self.rig.records.saved["heating_history_cursor"], before)

    async def test_upload_passes_native_rows_and_records_exact_acceptance(self):
        row = {"room_key": "room_a", "source_key": "source_a", "start": START.isoformat(), "duration_s": 3600}
        pending = {"rooms": {"room_a": {"source_key": "source_a", "signature": "synthetic", "before": START.isoformat()}}}
        self.h._heating_history_upload = AsyncMock(return_value=([row], [], pending))
        self.h.client.submit_optimisation.return_value = {"heating_observations_accepted": 0}
        await self.h.async_optimisation_push(force_plan=True)
        self.assertNotIn("heating_history_cursor", self.rig.records.saved)
        self.h.client.submit_optimisation.return_value.update(heating_observations_accepted=1, heating_probe_observations_accepted=0,
            heating_observations_accepted_by_source=[{"room_key": "room_a", "source_key": "source_a", "accepted": 1}])
        await self.h.async_optimisation_push(force_plan=True)
        body = self.h.client.submit_optimisation.call_args.args[0]
        self.assertEqual(body["heating_observations"], [row])
        self.assertEqual(self.rig.records.saved["heating_history_cursor"], pending)

    async def test_normal_owner_captures_recent_and_older_windows_without_overlap(self):
        devices, mappings, rooms = source_fixture()
        self.rig.options.update(rooms=rooms, device_control_mappings=mappings,
                                outdoor_temperature_entity="sensor.outdoors")
        self.h.capture_heating_history = AsyncMock(return_value=SimpleNamespace(observations=[], probe_observations=[], diagnostics={}))
        end = START + timedelta(days=1, minutes=15)
        rows, probes, cursor = await self.h._heating_history_upload(self.rig.options, devices, end, None, lineage={"revision":"test", "assignments":[]})
        recent, older = [call.kwargs for call in self.h.capture_heating_history.call_args_list]
        self.assertEqual(older["end"], recent["start"])
        self.assertEqual(recent["end"], end)
        self.assertEqual(cursor["rooms"]["room_a"]["before"], older["start"].isoformat())
        self.assertEqual(rows, [])
        self.h.capture_heating_history.reset_mock()
        await self.h._heating_history_upload(self.rig.options, devices, end, cursor, include_history=False, lineage={"revision":"test", "assignments":[]})
        self.assertEqual(self.h.capture_heating_history.await_count, 1)

    async def test_source_rejection_does_not_restart_accepted_room_or_duplicate_shared_reads(self):
        devices, mappings, rooms = source_fixture()
        devices.append({**devices[0], "key": "heater_b", "statistic_id": "sensor.energy_b",
                        "mapping_summary": {"room_key": "room_b"}})
        mappings["heater_b"] = {**mappings["heater"], "temperature_entity_id": "sensor.air_b",
                                "actuator_entity_ids": ["climate.heater_b"]}
        self.rig.options.update(rooms=rooms, device_control_mappings=mappings)
        self.h.capture_heating_history = AsyncMock(return_value=SimpleNamespace(observations=[], probe_observations=[], diagnostics={}))
        end = START + timedelta(days=2)
        _, _, pending = await self.h._heating_history_upload(self.rig.options, devices, end, None, lineage={"revision":"test", "assignments":[]})
        self.assertEqual(self.h.capture_heating_history.await_count, 2)
        rows = [{"room_key": room, "source_key": entry["source_key"]} for room, entry in pending["rooms"].items()]
        reply = {"heating_observations_accepted": 1, "heating_probe_observations_accepted": 0, "heating_observations_accepted_by_source": [
            {**rows[0], "accepted": 1}]}
        cursor = accepted_heating_cursor(reply, rows, pending, probe_observations=[])
        self.h.capture_heating_history.reset_mock()
        _, _, next_pending = await self.h._heating_history_upload(self.rig.options, devices, end, cursor, lineage={"revision":"test", "assignments":[]})
        calls = [call.kwargs for call in self.h.capture_heating_history.call_args_list]
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(calls[0]["bindings"]), 2)  # Shared recent read.
        self.assertLess(next_pending["rooms"]["room_a"]["before"], pending["rooms"]["room_a"]["before"])
        self.assertEqual(next_pending["rooms"]["room_b"]["before"], pending["rooms"]["room_b"]["before"])
        devices[1]["statistic_id"] = "sensor.replacement_b"
        _, _, changed = await self.h._heating_history_upload(self.rig.options, devices, end, cursor, lineage={"revision":"test", "assignments":[]})
        self.assertEqual(changed["rooms"]["room_a"], next_pending["rooms"]["room_a"])
        self.assertNotEqual(changed["rooms"]["room_b"]["signature"], next_pending["rooms"]["room_b"]["signature"])


if __name__ == "__main__":
    unittest.main()
