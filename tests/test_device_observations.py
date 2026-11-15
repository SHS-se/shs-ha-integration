"""Variable-power and pool devices send the setting that held through each quarter and the heat metered in it."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / "custom_components/shs_energy"))
from types import SimpleNamespace
from unittest.mock import AsyncMock

from shs_core.device_observations import (
    current_power_setting, device_observation_entities, history_resend_window, observation_signature,
    quarter_settings, with_device_observations,
)
from household_fixture import Rig

T0 = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def iso(minutes):
    return at(minutes).isoformat()


class QuarterSettingTests(unittest.TestCase):
    def test_a_setting_held_through_the_quarter_is_reported(self):
        states = [(at(-30), "100.0", None)]
        self.assertEqual(quarter_settings(states, at(0), at(30)), {iso(0): 100.0, iso(15): 100.0})

    def test_the_quarter_a_setting_changed_in_has_no_setting(self):
        """A pump's capacity turned down by hand, part way through a quarter."""
        states = [(at(-30), "100", None), (at(19), "80", None)]
        self.assertEqual(quarter_settings(states, at(0), at(45)), {iso(0): 100.0, iso(30): 80.0})

    def test_a_change_on_the_boundary_belongs_to_the_next_quarter(self):
        states = [(at(-30), "12", None), (at(15), "8", None)]
        self.assertEqual(quarter_settings(states, at(0), at(30)), {iso(0): 12.0, iso(15): 8.0})

    def test_an_unavailable_blip_is_not_a_change(self):
        """A heat pump's setting can drop to unavailable for a few seconds at a time."""
        states = [(at(-30), "12.0", None), (at(3), "unavailable", None), (at(3.1), "12.0", None)]
        self.assertEqual(quarter_settings(states, at(0), at(15)), {iso(0): 12.0})

    def test_unknown_throughout_and_before_the_first_value_are_left_out(self):
        self.assertEqual(quarter_settings([(at(-30), "unavailable", None)], at(0), at(30)), {})
        self.assertEqual(quarter_settings([(at(20), "12", None)], at(0), at(45)), {iso(30): 12.0})
        self.assertEqual(quarter_settings([], at(0), at(30)), {})

    def test_a_change_during_an_outage_is_unknown_for_every_quarter_it_touches(self):
        """Turned down while the entity was unavailable: when, nobody saw."""
        states = [(at(-30), "100", None), (at(20), "unavailable", None), (at(50), "80", None)]
        self.assertEqual(quarter_settings(states, at(0), at(75)), {iso(0): 100.0, iso(60): 80.0})
        # The same value on both sides of an outage held through it.
        states = [(at(-30), "100", None), (at(20), "unavailable", None), (at(50), "100", None)]
        self.assertEqual(quarter_settings(states, at(0), at(60)),
                         {iso(0): 100.0, iso(15): 100.0, iso(30): 100.0, iso(45): 100.0})

    def test_a_repeated_value_does_not_split_the_span(self):
        states = [(at(-30), "80", None), (at(5), "80", None)]
        self.assertEqual(quarter_settings(states, at(0), at(15)), {iso(0): 80.0})


class EntityTests(unittest.TestCase):
    devices = [
        {"key": "heater", "category": "pool_heating", "control_type": "switch_schedule"},
        {"key": "pump", "category": "pool_heating", "control_type": "switch_schedule"},
        {"key": "floor", "category": "heating", "control_type": "switch_schedule"},
    ]

    def test_only_pool_devices_that_name_an_observation_are_observed(self):
        mappings = {
            "heater": {"power_setting_entity_id": "number.nibe", "heat_energy_entity_id": "sensor.heat"},
            "pump": {"power_setting_entity_id": "number.pump"},
            "floor": {"power_setting_entity_id": "number.floor"},
        }
        self.assertEqual(device_observation_entities(self.devices, mappings, None), {
            "heater": {"setting": "number.nibe", "heat": "sensor.heat"},
            "pump": {"setting": "number.pump"},
        })
        self.assertEqual(device_observation_entities(self.devices, {"pump": {"power_setting_entity_id": ""}}, None), {})

    def test_every_variable_power_device_is_observed_at_its_own_control(self):
        """A charger's current, or a heat pump's output dial, is its setting."""
        devices = [
            {"key": "charger", "category": "ev_charging", "control_type": "variable_power"},
            {"key": "heat_pump", "category": "heating_other", "control_type": "variable_power"},
            {"key": "pool_pump", "category": "pool_heating", "control_type": "variable_power"},
            {"key": "relay", "category": "heating_other", "control_type": "switch_schedule"},
        ]
        mappings = {
            "charger": {"control_type": "variable_power", "control_entity_id": "number.charger_current"},
            "heat_pump": {"control_type": "variable_power", "control_entity_id": "number.output",
                          "heat_energy_entity_id": "sensor.produced_heat"},
            # A pool device is driven as a relay: only a named setting is observed.
            "pool_pump": {"control_type": "variable_power", "control_entity_id": "number.ignored"},
            "relay": {"control_type": "switch_schedule", "heat_energy_entity_id": "sensor.not_variable"},
        }
        self.assertEqual(device_observation_entities(devices, mappings, None), {
            "charger": {"setting": "number.charger_current"},
            "heat_pump": {"setting": "number.output", "heat": "sensor.produced_heat"},
        })
        mappings["heat_pump"]["power_setting_entity_id"] = "number.own_dial"
        self.assertEqual(device_observation_entities(devices, mappings, None)["heat_pump"]["setting"], "number.own_dial")

    def test_signature_follows_what_is_observed(self):
        one = {"heater": {"setting": "number.nibe"}}
        self.assertEqual(observation_signature(one), observation_signature({"heater": {"setting": "number.nibe"}}))
        self.assertNotEqual(observation_signature(one), observation_signature({}))

    def test_quarters_sent_before_the_merge_was_fixed_are_sent_again_once(self):
        """The identity an earlier release stored no longer matches, so history is re-sent."""
        one = {"heater": {"setting": "number.nibe"}}
        self.assertNotEqual(observation_signature(one), '{"heater":{"setting":"number.nibe"}}')
        # A home that observes nothing has nothing to send again.
        self.assertEqual(observation_signature({}), "{}")

    def test_the_resend_walks_back_one_stretch_at_a_time_to_what_the_website_accepts(self):
        signature, end = observation_signature({"heater": {"setting": "number.nibe"}}), at(0)
        window = lambda before, sig=signature: history_resend_window(
            {"signature": sig, "before": before.isoformat()} if before else None, signature, end,
            chunk=timedelta(hours=72), reach=timedelta(hours=191))
        self.assertEqual(window(end - timedelta(hours=72)), (end - timedelta(hours=144), end - timedelta(hours=72)))
        self.assertEqual(window(end - timedelta(hours=144)), (end - timedelta(hours=191), end - timedelta(hours=144)))
        self.assertIsNone(window(end - timedelta(hours=191)))
        self.assertIsNone(window(None))
        # A cursor left by a re-send that was superseded is not continued.
        self.assertIsNone(window(end - timedelta(hours=72), "other"))

    def test_the_snapshot_says_what_each_setting_holds_now(self):
        states = {"number.nibe": SimpleNamespace(state="12.0"), "number.pump": SimpleNamespace(state="unavailable")}
        self.assertEqual(current_power_setting("number.nibe", states.get), {"value": 12.0})
        # Unreadable now is not the same as a device with no setting of its own.
        self.assertEqual(current_power_setting("number.pump", states.get), {"value": None})
        self.assertEqual(current_power_setting("number.gone", states.get), {"value": None})
        self.assertIsNone(current_power_setting(None, states.get))


class AttachTests(unittest.TestCase):
    def test_setting_and_heat_ride_only_on_quarters_with_that_devices_energy(self):
        quarters = [
            {"start": iso(0), "device_energy_kwh": {"heater": .75, "pump": .1}},
            {"start": iso(15), "device_energy_kwh": {"pump": .1}},
        ]
        result = with_device_observations(quarters,
            {"heater": {iso(0): 12.0, iso(15): 12.0}, "pump": {iso(15): 80.0}},
            [{"start": iso(0), "device_energy_kwh": {"heater": 3.0}},
             {"start": iso(15), "device_energy_kwh": {"heater": 3.1}}])
        # A setting that was read says what it found, a value or none.
        self.assertEqual(result[0]["device_power_setting"], {"heater": 12.0, "pump": None})
        self.assertEqual(result[0]["device_heat_kwh"], {"heater": 3.0})
        self.assertEqual(result[1]["device_power_setting"], {"pump": 80.0})
        self.assertNotIn("device_heat_kwh", result[1])

    def test_a_setting_that_was_not_read_says_nothing(self):
        quarters = [{"start": iso(0), "device_energy_kwh": {"heater": .75, "pump": .1}}]
        result = with_device_observations(quarters, {"pump": {iso(0): 80.0}}, [])
        self.assertEqual(result[0]["device_power_setting"], {"pump": 80.0})
        self.assertNotIn("device_power_setting", with_device_observations(
            [{"start": iso(0), "device_energy_kwh": {"heater": .75}}], {}, [])[0])


def five_minute(entity_values, start, quarters):
    return {entity: [{"start": (start + timedelta(minutes=5 * index)).timestamp(), "change": value}
                     for index in range(quarters * 3)] for entity, value in entity_values.items()}


class HouseholdUploadTests(unittest.IsolatedAsyncioTestCase):
    def rig(self):
        options = {"planning_mode": "live", "device_control_mappings": {
            "heater": {"control_type": "switch_schedule", "actuator_entity_ids": ["switch.pool"],
                       "power_setting_entity_id": "number.nibe", "heat_energy_entity_id": "sensor.heat"},
            "pump": {"control_type": "switch_schedule", "power_setting_entity_id": "number.pump"},
        }}
        rig = Rig(now=at(30), options=options)
        rig.household.resolved_options = lambda: options
        return rig

    devices = [
        {"key": "heater", "statistic_id": "sensor.heater_energy", "category": "pool_heating", "control_type": "switch_schedule"},
        {"key": "pump", "statistic_id": "sensor.pump_energy", "category": "pool_heating", "control_type": "switch_schedule"},
    ]

    async def test_upload_quarters_carry_setting_and_heat(self):
        rig = self.rig()
        series = five_minute({"sensor.heater_energy": .25, "sensor.pump_energy": .035, "sensor.heat": 1.0}, at(0), 2)
        series["sensor.heat"].pop(4)  # the heat meter's second quarter is late
        async def statistics(start, end, entities, period, units, kinds):
            return {entity: series[entity] for entity in entities if entity in series}
        rig.history.statistics.side_effect = statistics
        rig.history.states.return_value = {
            "number.nibe": [(at(-60), "12.0", None)],
            "number.pump": [(at(-60), "100.0", None), (at(19), "80.0", None)],
        }
        rows = await rig.household._device_actual_quarters(self.devices, at(0), at(30))
        self.assertEqual(rig.history.statistics.await_args.args[2],
                         {"sensor.heater_energy", "sensor.pump_energy", "sensor.heat"})
        # The pump's capacity changed in the second quarter: read, and none held.
        self.assertEqual([row["device_power_setting"] for row in rows],
                         [{"heater": 12.0, "pump": 100.0}, {"heater": 12.0, "pump": None}])
        self.assertEqual(rows[0]["device_heat_kwh"], {"heater": 3.0})
        self.assertNotIn("device_heat_kwh", rows[1])
        self.assertEqual(rows[1]["device_energy_kwh"], {"heater": .75, "pump": .105})

    async def test_unreadable_settings_keep_the_energy(self):
        from shs_core.household_ports import HouseholdReadError
        rig = self.rig()
        series = five_minute({"sensor.heater_energy": .25, "sensor.pump_energy": .035}, at(0), 1)
        async def statistics(start, end, entities, period, units, kinds):
            return {entity: series[entity] for entity in entities if entity in series}
        rig.history.statistics.side_effect = statistics
        rig.history.states.side_effect = HouseholdReadError("recorder busy")
        rows = await rig.household._device_actual_quarters(self.devices, at(0), at(15))
        self.assertEqual(rows[0]["device_energy_kwh"], {"heater": .75, "pump": .105})
        self.assertNotIn("device_power_setting", rows[0])


    async def test_heating_activity_is_stated_when_read_and_left_out_when_not(self):
        """A reversible unit: heating identifies its draw, and unread is not idle."""
        from shs_core.household_ports import HouseholdReadError
        rig = self.rig()
        series = five_minute({"sensor.heater_energy": .25}, at(0), 2)
        async def statistics(start, end, entities, period, units, kinds):
            return {entity: series[entity] for entity in entities if entity in series}
        rig.history.statistics.side_effect = statistics
        rig.history.states.return_value = {"number.nibe": [(at(-60), "12.0", None)]}
        rig.household._weather_heating_bindings = lambda devices: {"heater": ("climate.unit", "climate.unit|p|e")}
        rig.household._state_history = AsyncMock(return_value={"climate.unit": [
            (at(-60), "heat", {"hvac_action": "heating"}), (at(15), "heat", {"hvac_action": "idle"})]})
        rows = await rig.household._device_actual_quarters(self.devices[:1], at(0), at(30))
        self.assertEqual([row["device_weather_heating"] for row in rows],
                         [{"heater": "climate.unit|p|e"}, {"heater": None}])
        # Recorder did not answer for the unit: nothing is said about its heating.
        rig.household._state_history = AsyncMock(return_value={})
        rows = await rig.household._device_actual_quarters(self.devices[:1], at(0), at(30))
        self.assertTrue(all("device_weather_heating" not in row for row in rows))
        rig.household._state_history = AsyncMock(side_effect=HouseholdReadError("recorder busy"))
        rows = await rig.household._device_actual_quarters(self.devices[:1], at(0), at(30))
        self.assertEqual([row["device_energy_kwh"] for row in rows], [{"heater": .75}, {"heater": .75}])
        self.assertTrue(all("device_weather_heating" not in row for row in rows))


if __name__ == "__main__":
    unittest.main()
