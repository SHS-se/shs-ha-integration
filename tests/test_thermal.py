"""Pure tests for quarter-hour thermal aggregation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from thermal import (  # noqa: E402
    actuator_value,
    cooling_value,
    build_thermal_slots,
    interpolate_hourly_forecast,
    numeric_value,
    quarter_means,
    thermal_zone_inputs,
    time_weighted_quarters,
)

START = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return START + timedelta(minutes=minutes)


class QuarterMeansTests(unittest.TestCase):
    def test_averages_three_five_minute_means(self) -> None:
        rows = [(at(0), 21.0), (at(5), 22.0), (at(10), 23.0)]
        self.assertEqual(quarter_means(rows), {START: 22.0})

    def test_accepts_two_of_three_samples(self) -> None:
        rows = [(at(0), 20.0), (at(10), 22.0)]
        self.assertEqual(quarter_means(rows), {START: 21.0})

    def test_drops_a_quarter_with_one_surviving_sample(self) -> None:
        self.assertEqual(quarter_means([(at(0), 20.0)]), {})

    def test_ignores_samples_off_the_five_minute_grid(self) -> None:
        rows = [(at(0), 20.0), (at(3), 30.0), (at(5), 22.0)]
        self.assertEqual(quarter_means(rows), {START: 21.0})


class TimeWeightedTests(unittest.TestCase):
    def test_weights_by_duration_not_sample_count(self) -> None:
        # Three rapid "on" rows must not outvote the "off" that held the
        # remaining twelve minutes of the quarter.
        changes = [
            (at(0), "off", None),
            (at(1), "on", None),
            (at(1.5), "on", None),
            (at(2), "on", None),
            (at(3), "off", None),
        ]
        result = time_weighted_quarters(
            changes, START, at(15), actuator_value
        )
        self.assertAlmostEqual(result[START], 2 / 15, places=3)

    def test_holds_the_last_value_across_the_window(self) -> None:
        result = time_weighted_quarters(
            [(at(-600), "21.5", None)], START, at(15), numeric_value
        )
        self.assertEqual(result[START], 21.5)

    def test_unknown_intervals_do_not_count_as_zero(self) -> None:
        changes = [(at(0), "unavailable", None), (at(10), "on", None)]
        result = time_weighted_quarters(
            changes, START, at(15), actuator_value
        )
        # Only the final five minutes were knowable, which is below the
        # coverage floor, so nothing is reported for the quarter.
        self.assertEqual(result, {})

    def test_climate_action_beats_mode(self) -> None:
        changes = [(at(0), "heat", {"hvac_action": "idle"})]
        result = time_weighted_quarters(
            changes, START, at(15), actuator_value
        )
        self.assertEqual(result[START], 0.0)

    def test_cooling_is_not_counted_as_heating(self) -> None:
        # A summer aircon puts energy through the meter while the room gets
        # colder. Counting it as heating would ask the fit to explain an
        # impossibility, so it reads as no heat and is reported separately.
        changes = [(at(0), "cool", {"hvac_action": "cooling"})]
        self.assertEqual(
            time_weighted_quarters(changes, START, at(15), actuator_value)[START],
            0.0,
        )
        self.assertEqual(
            time_weighted_quarters(changes, START, at(15), cooling_value)[START],
            1.0,
        )

    def test_heating_is_not_counted_as_cooling(self) -> None:
        changes = [(at(0), "heat", {"hvac_action": "heating"})]
        self.assertEqual(
            time_weighted_quarters(changes, START, at(15), actuator_value)[START],
            1.0,
        )
        self.assertEqual(
            time_weighted_quarters(changes, START, at(15), cooling_value)[START],
            0.0,
        )

    def test_a_switch_without_an_action_never_reports_cooling(self) -> None:
        changes = [(at(0), "on", None)]
        self.assertEqual(
            time_weighted_quarters(changes, START, at(15), cooling_value)[START],
            0.0,
        )

    def test_cool_mode_without_an_action_still_excludes_heat(self) -> None:
        changes = [(at(0), "cool", None)]
        self.assertEqual(
            time_weighted_quarters(changes, START, at(15), actuator_value)[START],
            0.0,
        )

    def test_spans_multiple_quarters(self) -> None:
        result = time_weighted_quarters(
            [(at(0), "on", None)], START, at(30), actuator_value
        )
        self.assertEqual(result, {START: 1.0, at(15): 1.0})


class ForecastTests(unittest.TestCase):
    def test_interpolates_between_hours(self) -> None:
        records = [(START, 10.0), (at(60), 14.0)]
        starts = [START, at(15), at(30), at(45), at(60)]
        result = interpolate_hourly_forecast(records, starts)
        self.assertEqual(
            [result[start] for start in starts], [10.0, 11.0, 12.0, 13.0, 14.0]
        )

    def test_does_not_extrapolate_past_the_provider_horizon(self) -> None:
        result = interpolate_hourly_forecast([(START, 10.0)], [at(15)])
        self.assertEqual(result, {})


class BuildSlotsTests(unittest.TestCase):
    def test_requires_both_temperature_and_duty(self) -> None:
        zones = {
            "kitchen": {
                "room_temperature_c": {START: 21.0, at(15): 21.4},
                "actuator_duty": {START: 0.5},
                "comfort_max_c": {START: 22.0},
                "cooling_duty": {},
            }
        }
        slots = build_thermal_slots(zones, {START: 4.0})
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["start"], START.isoformat())
        self.assertEqual(slots[0]["outdoor_temperature_c"], 4.0)
        self.assertEqual(
            slots[0]["zone_observations"]["kitchen"],
            {
                "room_temperature_c": 21.0,
                "actuator_duty": 0.5,
                "comfort_max_c": 22.0,
            },
        )

    def test_cooling_is_reported_only_when_it_happened(self) -> None:
        zones = {
            "tv_room": {
                "room_temperature_c": {START: 24.0, at(15): 23.0},
                "actuator_duty": {START: 0.0, at(15): 0.0},
                "cooling_duty": {at(15): 0.8},
            }
        }
        slots = build_thermal_slots(zones, {})
        self.assertNotIn(
            "cooling_duty", slots[0]["zone_observations"]["tv_room"]
        )
        self.assertEqual(
            slots[1]["zone_observations"]["tv_room"]["cooling_duty"], 0.8
        )

    def test_outdoor_temperature_is_optional(self) -> None:
        zones = {
            "kitchen": {
                "room_temperature_c": {START: 21.0},
                "actuator_duty": {START: 0.0},
            }
        }
        slots = build_thermal_slots(zones, {})
        self.assertNotIn("outdoor_temperature_c", slots[0])


class ZoneInputTests(unittest.TestCase):
    def _device(self, **overrides: object) -> dict[str, object]:
        device = {
            "key": "kitchen",
            "statistic_id": "sensor.kitchen_heater_energy",
            "planning_role": "controllable",
            "control_type": "setpoint",
        }
        device.update(overrides)
        return device

    def test_reads_a_matching_setpoint_mapping(self) -> None:
        mappings = {
            "sensor.kitchen_heater_energy": {
                "control_type": "setpoint",
                "temperature_entity_id": "sensor.kitchen_temperature",
                "actuator_entity_ids": ["switch.kitchen_heaters"],
                "comfort_high_entity_id": "input_number.kitchen_high",
            }
        }
        zones = thermal_zone_inputs([self._device()], mappings)
        self.assertEqual(
            zones["kitchen"]["temperature_entity_id"],
            "sensor.kitchen_temperature",
        )

    def test_skips_a_mapping_of_a_different_control_type(self) -> None:
        mappings = {
            "sensor.kitchen_heater_energy": {
                "control_type": "switch_schedule",
                "temperature_entity_id": "sensor.kitchen_temperature",
                "actuator_entity_ids": ["switch.kitchen_heaters"],
            }
        }
        self.assertEqual(thermal_zone_inputs([self._device()], mappings), {})

    def test_skips_a_device_the_website_did_not_select(self) -> None:
        mappings = {
            "sensor.kitchen_heater_energy": {
                "control_type": "setpoint",
                "temperature_entity_id": "sensor.kitchen_temperature",
                "actuator_entity_ids": ["switch.kitchen_heaters"],
            }
        }
        device = self._device(planning_role="base_load")
        self.assertEqual(thermal_zone_inputs([device], mappings), {})


if __name__ == "__main__":
    unittest.main()
