"""Independently generated Recorder evidence through the production capture owner."""
import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch
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
    household = household or owner(recorder)
    return await household.capture_heating_history(
        read_energy=household._statistics_changes,
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
        self.assertEqual(result.observations[0]["counter_processing_revision"], 1)
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
        self.assertTrue(all(row["counter_processing_revision"] == 1 for row in repaired.observations))

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
    def test_counter_processing_upgrade_resweeps_history(self):
        devices, mappings, rooms = source_fixture()
        value = heating_history_sources(devices, mappings, rooms,
            lambda _: SimpleNamespace(attributes={"hvac_modes": ["heat", "off"]}))
        before = value.signature
        with patch("shs_core.heating_history.COUNTER_PROCESSING_REVISION", 2):
            self.assertNotEqual(value.signature, before)
        self.assertEqual(value.signature, before)

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

    async def test_invalid_optional_catalog_keeps_actuals_and_planning_running_but_defers_native(self):
        devices,mappings,rooms = source_fixture()
        self.rig.options.update(rooms=rooms,device_control_mappings=mappings)
        self.h._prepared_device_inventory.return_value = devices
        self.rig.records.saved["heating_history_sources"] = {"revision":"broken","assignments":[{}]}
        self.h._configured_entities = lambda: {"grid_import":["sensor.grid"]}
        actual = {"start":(self.rig.now-timedelta(minutes=15)).isoformat(),"grid_import_kwh":.2}
        self.h._actual_quarters = AsyncMock(return_value=[actual])
        await self.h.async_optimisation_push(force_plan=True)
        body = self.h.client.submit_optimisation.call_args.args[0]
        self.assertIn("snapshot",body)
        self.assertTrue(body["actual_slots"])
        self.assertNotIn("heating_observations",body)
        self.assertTrue(any(item["key"]=="pipeline:heating_inputs" for item in self.h.attention_items))

    async def test_inconsistent_room_mapping_cannot_stop_current_actuals_or_planning(self):
        devices,mappings,rooms = source_fixture()
        # Two configured heater members disagree about the room-air probe.
        devices.append({**devices[0],"key":"second","statistic_id":"sensor.second_energy"})
        mappings['second'] = {**mappings['heater'],"temperature_entity_id":"sensor.conflicting_air"}
        self.rig.options.update(rooms=rooms,device_control_mappings=mappings)
        self.h._prepared_device_inventory.return_value = devices
        await self.h.async_optimisation_push(force_plan=True)
        self.assertIn("snapshot",self.h.client.submit_optimisation.call_args.args[0])
        self.assertTrue(any(item["key"]=="pipeline:heating_inputs" for item in self.h.attention_items))

    async def test_pending_peak_evidence_is_visible_without_stopping_planning(self):
        self.h._heating_history_upload = AsyncMock(return_value=([],[],{
            "rooms":{"room_a":{"before":START.isoformat()}},"deferred_rooms":["room_a"]}))
        await self.h.async_optimisation_push(force_plan=True)
        self.assertIn("snapshot",self.h.client.submit_optimisation.call_args.args[0])
        issue = next(item for item in self.h.attention_items if item["key"]=="pipeline:heating_inputs")
        self.assertIn("waiting for recorded power evidence",issue["detail"])

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

    async def test_database_timeout_is_visible_until_a_confirmed_history_receipt(self):
        row = {"room_key": "room_a", "source_key": "source_a", "start": START.isoformat(), "duration_s": 3600}
        old = {"rooms": {"room_a": {"source_key": "source_a", "signature": "synthetic", "before": START.isoformat()}}}
        pending = {"rooms": {"room_a": {**old["rooms"]["room_a"], "before": (START-timedelta(hours=1)).isoformat()}}}
        self.rig.records.saved["heating_history_cursor"] = old
        self.h._heating_history_upload = AsyncMock(return_value=([row], [], pending))
        detail = "Heating history: the server database write timed out. SHS will retry automatically."
        self.h.client.submit_optimisation.return_value = {"heating_observations_accepted": None,
            "heating_observations_accepted_by_source": None, "heating_probe_observations_accepted": 0,
            "heating_history_errors": [detail]}
        await self.h.async_optimisation_push(force_plan=True)
        self.assertEqual(self.rig.records.saved["heating_history_cursor"], old)
        issue = next(item for item in self.h.attention_items if item["key"] == "pipeline:heating_inputs")
        self.assertEqual(issue["detail"], detail)
        self.assertEqual(issue["severity"], "warning")
        self.assertIn("snapshot", self.h.client.submit_optimisation.call_args.args[0])

        async def accepted(_body):
            # A successful read and the start of a retry cannot clear a failed write.
            self.assertTrue(any(item["key"] == "pipeline:heating_inputs" for item in self.h.attention_items))
            return {"heating_observations_accepted": 1, "heating_probe_observations_accepted": 0,
                "heating_observations_accepted_by_source": [{"room_key": "room_a", "source_key": "source_a", "accepted": 1}],
                "heating_history_errors": []}
        self.h.client.submit_optimisation.side_effect = accepted
        await self.h.async_optimisation_push(force_plan=True)
        self.assertEqual(self.rig.records.saved["heating_history_cursor"], pending)
        self.assertFalse(any(item["key"] == "pipeline:heating_inputs" for item in self.h.attention_items))

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


def power(at, seconds, maximum):
    return {"start": at.timestamp(), "end": (at + timedelta(seconds=seconds)).timestamp(), "max": maximum}


async def capture_peaks(recorder, *, start=START, end=None):
    household = owner(recorder)
    return await household.capture_heating_history(
        read_energy=household._statistics_changes, bindings=[binding()],
        device_statistics={"heater": "sensor.energy_a"}, room_air_probes=PROBES, outdoor_entity="sensor.outdoors",
        start=start, end=end or start + timedelta(hours=1), peak_statistics={"heater": "sensor.power_a"})


class HeatPumpPeakTests(unittest.IsolatedAsyncioTestCase):
    """A heat pump's start-up peak rides on the interval that carries its energy."""

    async def test_each_quarter_states_its_highest_five_minute_maximum(self):
        recorder = Recorder()
        recorder.temperatures("hour")
        recorder.temperatures("5minute", count=12)
        recorder.energy("sensor.energy_a", "hour", [1.2])
        recorder.energy("sensor.energy_a", "5minute", [.1] * 12)
        maxima = [45, 2900, 2100, 1400, 1300, 1200, 1100, 1000, 1000, 1000, 1000, 1000]
        recorder.rows["5minute"]["sensor.power_a"] = [power(START + timedelta(minutes=5 * i), 300, value)
                                                      for i, value in enumerate(maxima)]
        result = await capture_peaks(recorder)
        self.assertEqual([row["device_peak_w"] for row in result.observations],
                         [{"heater": 2900}, {"heater": 1400}, {"heater": 1100}, {"heater": 1000}])

    async def test_a_quarter_missing_a_maximum_states_no_peak_and_keeps_its_energy(self):
        recorder = Recorder()
        recorder.temperatures("hour")
        recorder.temperatures("5minute", count=12)
        recorder.energy("sensor.energy_a", "hour", [1.2])
        recorder.energy("sensor.energy_a", "5minute", [.1] * 12)
        recorder.rows["5minute"]["sensor.power_a"] = [power(START + timedelta(minutes=5 * i), 300, 1000)
                                                      for i in range(12) if i != 1]
        result = await capture_peaks(recorder)
        self.assertNotIn("device_peak_w", result.observations[0])
        self.assertEqual(result.observations[0]["device_energy_kwh"], {"heater": .1 * 3})
        self.assertEqual(result.observations[1]["device_peak_w"], {"heater": 1000})

    async def test_an_old_hour_states_its_hourly_maximum(self):
        recorder = Recorder()
        recorder.temperatures("hour")
        recorder.energy("sensor.energy_a", "hour", [1.5])
        recorder.rows["hour"]["sensor.power_a"] = [power(START, 3600, 2871.94)]
        result = await capture_peaks(recorder)
        self.assertEqual([(row["duration_s"], row["device_peak_w"]) for row in result.observations],
                         [(3600, {"heater": 2871.9})])

    async def test_an_unreadable_peak_keeps_the_energy_and_asks_for_the_window_again(self):
        recorder = Recorder()
        recorder.temperatures("hour")
        recorder.energy("sensor.energy_a", "hour", [1.5])
        statistics = recorder.statistics

        async def failing(start, end, entities, period, units, kinds):
            if "sensor.power_a" in entities:
                raise HouseholdReadError("synthetic recorder timeout")
            return await statistics(start, end, entities, period, units, kinds)
        recorder.statistics = failing
        result = await capture_peaks(recorder)
        self.assertEqual(result.observations[0]["device_energy_kwh"], {"heater": 1.5})
        self.assertNotIn("device_peak_w", result.observations[0])
        self.assertEqual(result.diagnostics["peak_read_failures"], 2)


class HeatPumpSourceTests(unittest.TestCase):
    def test_only_an_inverter_heater_with_a_power_sensor_is_a_heat_pump(self):
        devices, mappings, rooms = source_fixture()
        mappings["heater"]["power"] = "sensor.power_a"
        modes = lambda _: SimpleNamespace(attributes={"hvac_modes": ["heat", "cool", "off"]})
        plain = heating_history_sources(devices, mappings, rooms, modes)
        self.assertEqual(plain.peak_device_keys, ())
        pump = heating_history_sources([{**devices[0], "load_type": "inverter"}], mappings, rooms, modes)
        self.assertEqual(pump.peak_statistics, {"heater": "sensor.power_a"})
        self.assertEqual(pump.for_room(pump.bindings[0]).peak_device_keys, ("heater",))
        # The home with a heat pump sends its history again once, with peaks.
        self.assertNotEqual(pump.signature, plain.signature)
        del mappings["heater"]["power"]
        self.assertEqual(heating_history_sources([{**devices[0], "load_type": "inverter"}], mappings, rooms,
                                                 modes).peak_device_keys, ())

    def test_a_home_without_heat_pumps_keeps_its_history_identity(self):
        from dataclasses import asdict
        import json
        from shs_core.counter_reading import COUNTER_PROCESSING_REVISION
        devices, mappings, rooms = source_fixture()
        value = heating_history_sources(devices, mappings, rooms,
            lambda _: SimpleNamespace(attributes={"hvac_modes": ["heat", "off"]}))
        identity = {"revision": 1, "counter_processing_revision": COUNTER_PROCESSING_REVISION, **asdict(value)}
        del identity["peak_device_keys"]
        before = sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(value.signature, before)


class HeatPumpPeakExchangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import test_planning_jobs as jobs
        jobs.PlanningJobTests.setUp(self)

    async def test_the_server_says_once_that_it_stores_peaks(self):
        self.h._heating_history_upload = AsyncMock(return_value=(None, None, None))
        self.h.client.submit_optimisation.return_value = {}
        await self.h.async_optimisation_push(force_plan=True)
        self.assertNotIn("heating_power_peaks_supported", self.rig.records.saved)
        self.h.client.submit_optimisation.return_value = {"heating_power_peaks_supported": True}
        await self.h.async_optimisation_push(force_plan=True)
        self.assertIs(self.rig.records.saved["heating_power_peaks_supported"], True)

    async def test_peaks_are_captured_only_once_the_server_stores_them(self):
        devices, mappings, rooms = source_fixture()
        mappings["heater"]["power"] = "sensor.power_a"
        devices = [{**devices[0], "load_type": "inverter"}]
        seen = []

        async def stop(history, sources, lineage, ledger, end):
            seen.append(sources.peak_device_keys)
            raise HouseholdReadError("synthetic stop after source resolution")
        options = {"rooms": rooms, "device_control_mappings": mappings}
        with patch("shs_core.household.prepare_heating_counter_evidence", stop):
            for stored in ({}, {"heating_power_peaks_supported": True}):
                with self.assertRaises(HouseholdReadError):
                    await self.h._heating_history_upload(options, devices, START, None, stored=stored)
        # Until the server confirms, the history identity and its re-send are unchanged.
        self.assertEqual(seen, [(), ("heater",)])
