"""An impossible or unavailable reading leaves out only its own device.

User requirement, 23 September 2026. Realistic state — a car above its charge
limit, an empty car, a pack below a raised cut-off, a warm pool — is never an
issue. A missing entity is configuration, reported by the snapshot builder.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.append(str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from measurements import device_measurement_issues, fraction  # noqa: E402

NOW = datetime(2026, 9, 23, 4, 55, tzinfo=timezone.utc)
OPTIONS = {
    "battery_soc_entity": "sensor.battery_soc",
    "battery_min_soc": "sensor.cut_off",
    "battery_capacity_kwh": 18.08,
    "battery_charge_max_w": 8_800,
    "battery_discharge_max_w": 9_600,
    "pool_water_temperature_entity": "sensor.pool_water",
    "ev_connected_entity": "binary_sensor.cable",
    "ev_soc_entity": "sensor.car_soc",
    "ev_target_soc_entity": "number.charge_limit",
    "ev_energy_remaining_entity": "sensor.energy_remaining",
    "device_control_mappings": {
        "sensor.car_charging": {"control_type": "variable_power", "control_entity_id": "number.charge_current"},
    },
}
CHARGER = {"key": "sensor.car_charging", "category": "ev_charging", "control_type": "variable_power",
           "planning_role": "controllable"}


def state(value, unit=None, *, age=timedelta(seconds=30)):
    return SimpleNamespace(state=value, attributes={"unit_of_measurement": unit} if unit else {},
                           last_reported=NOW - age)


class MeasurementIssueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.states = {
            "sensor.battery_soc": state("22.8", "%"),
            "sensor.cut_off": state("10", "%"),
            "sensor.pool_water": state("28.82", "°C"),
            "binary_sensor.cable": state("on"),
            "sensor.car_soc": state("83", "%"),
            "number.charge_limit": state("80", "%"),
            "sensor.energy_remaining": state("62.9", "kWh"),
            "number.charge_current": state("16", "A"),
        }

    def issues(self, **reads):
        flags = {"battery": True, "pool": True, "ev": True, **reads}
        return device_measurement_issues(OPTIONS, self.states.get, NOW, devices=[CHARGER],
                                         known_ev_capacity_kwh=None, **flags)

    def fields(self, **reads):
        return [(issue["device"], issue["field"]) for issue in self.issues(**reads)]

    def test_realistic_state_beyond_a_target_is_not_an_issue(self) -> None:
        # The replayed 04:45 home: a car at 83 % against an 80 % limit.
        self.assertEqual(self.issues(), [])
        self.states["sensor.battery_soc"] = state("8", "%")  # below a 10 % cut-off
        self.states["sensor.pool_water"] = state("34")  # past its stop temperature
        self.assertEqual(self.issues(), [])

    def test_impossible_readings_name_only_their_own_device(self) -> None:
        self.states["sensor.car_soc"] = state("105", "%")
        self.assertEqual(self.issues(), [{
            "device": "ev", "field": "soc", "entity_id": "sensor.car_soc", "value": 1.05,
            "reason": "The car reported a state of charge of 105%, outside 0–100%.",
            "detected_by": "home_assistant",
        }])
        self.setUp()
        self.states["sensor.pool_water"] = state("500")
        self.assertEqual(self.fields(), [("pool", "water_temperature_c")])
        self.setUp()
        self.states["sensor.energy_remaining"] = state("-10", "kWh")
        self.assertEqual(self.fields(), [("ev", "energy_remaining")])
        self.setUp()
        self.states["sensor.cut_off"] = state("150", "%")
        self.assertEqual(self.fields(), [("battery", "battery_min_soc")])

    def test_unavailable_or_non_numeric_readings_are_named(self) -> None:
        self.states["sensor.battery_soc"] = state("unavailable")
        self.states["binary_sensor.cable"] = state("unknown")
        self.states["number.charge_current"] = state("unavailable")
        self.states["sensor.pool_water"] = state("warm")
        self.assertEqual(sorted(self.fields()), [
            ("battery", "soc"), ("ev", "charge_current"), ("ev", "connected"), ("pool", "water_temperature_c"),
        ])
        values = {issue["field"]: issue["value"] for issue in self.issues()}
        self.assertEqual(values["soc"], "unavailable")
        self.assertEqual(values["water_temperature_c"], "warm")

    def test_a_stale_battery_reading_leaves_out_the_battery(self) -> None:
        self.states["sensor.battery_soc"] = state("40", "%", age=timedelta(minutes=20))
        [issue] = self.issues()
        self.assertEqual((issue["device"], issue["field"], issue["value"]), ("battery", "soc", 0.4))
        self.assertIn("20 minutes", issue["reason"])

    def test_an_empty_car_needs_a_known_battery_size(self) -> None:
        self.states["sensor.car_soc"] = state("0", "%")
        self.states["sensor.energy_remaining"] = state("0", "kWh")
        self.assertEqual(self.fields(), [("ev", "energy_remaining")])
        known = device_measurement_issues(OPTIONS, self.states.get, NOW, battery=True, pool=True, ev=True,
                                          devices=[CHARGER], known_ev_capacity_kwh=75.8)
        self.assertEqual(known, [])

    def test_devices_the_snapshot_does_not_read_are_never_inspected(self) -> None:
        self.states = {}
        self.assertEqual(self.issues(battery=False, pool=False, ev=False), [])
        # A missing entity is configuration: the builder names it with its field.
        self.assertEqual(self.issues(), [])

    def test_a_typed_cut_off_is_configuration_not_a_reading(self) -> None:
        options = {**OPTIONS, "battery_min_soc": 1.5}
        issues = device_measurement_issues(options, self.states.get, NOW, battery=True, pool=False, ev=False)
        self.assertEqual(issues, [])

    def test_issues_match_the_planner_contract(self) -> None:
        self.states["sensor.car_soc"] = state("unavailable")
        self.states["sensor.pool_water"] = state("500")
        for issue in self.issues():
            with self.subTest(issue=issue):
                self.assertEqual(set(issue), {"device", "field", "entity_id", "value", "reason", "detected_by"})
                self.assertIn(issue["device"], {"battery", "ev", "pool"})
                self.assertTrue(issue["field"] and issue["reason"])
                self.assertIsInstance(issue["value"], (int, float, str, type(None)))
                self.assertEqual(issue["detected_by"], "home_assistant")

    def test_a_percentage_unit_decides_the_scale(self) -> None:
        self.assertEqual(fraction("1", "%"), 0.01)
        self.assertEqual(fraction("1", None), 1)
        self.assertEqual(fraction("55", None), 0.55)
        self.assertIsNone(fraction("unavailable", "%"))


class SnapshotWiringTests(unittest.TestCase):
    """The coordinator cannot be imported here; guard the wiring it owns."""

    SOURCE = (Path(__file__).parents[1] / "custom_components" / "shs_energy" / "coordinator.py").read_text(
        encoding="utf-8")

    def test_the_builder_isolates_devices_before_reading_them(self) -> None:
        body = self.SOURCE[self.SOURCE.index("async def _build_optimisation_snapshot("):]
        body = body[:body.index("async def _retry_pending_plan_ack(")]
        checked = body.index("device_measurement_issues(")
        # Switched off before any device reading, through the customer's own switches.
        self.assertLess(checked, body.index("battery_entity = ("))
        self.assertLess(checked, body.index("self._build_services("))
        self.assertIn('options[{"battery": OPT_BATTERY_ENABLED, "pool": OPT_POOL_ENABLED, "ev": OPT_EV_ENABLED}[device]] = False',
                      body)
        self.assertIn('snapshot["measurement_issues"] = measurement_issues', body)
        # Setup warnings keep describing the configuration, not this snapshot.
        self.assertIn("self._sync_battery_control_issue(configured,", body)
        self.assertIn("self._sync_pool_control_issue(\n            configured,", body)


if __name__ == "__main__":
    unittest.main()
