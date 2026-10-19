"""Input accuracy is a live share per input, never a projection of past error."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))
from shs_core.input_accuracy import (battery_check, input_accuracy, measurement_check, price_check, prune_quarters,
                                     quarter_completeness, room_model_check, sensor_check, solar_forecast_check)

NOW = datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc)  # 10:00 local


def state(value, age=1):
    return SimpleNamespace(state=value, attributes={}, last_updated=NOW - timedelta(seconds=age),
                           last_reported=NOW - timedelta(seconds=age))


class PriceCheckTests(unittest.TestCase):
    def test_prices_come_from_the_reports_estimate_accuracy(self):
        report = {"calculated_at": "2026-10-18T16:00:05Z", "prices": {"percentage": 83, "grade": "lime", "quarters": 96}}
        self.assertEqual(price_check(report), {"key": "prices", "percentage": 83, "grade": "lime", "quarters": 96,
                                               "calculated_at": "2026-10-18T16:00:05Z", "issues": []})

    def test_a_report_without_a_price_comparison_is_unscored(self):
        for report in (None, {"version": 1, "calculated_at": "2026-10-18T04:00:05Z"},
                       {"calculated_at": "x", "prices": {"percentage": None, "grade": None, "quarters": 0}}):
            check = price_check(report)
            self.assertIsNone(check["percentage"])
            self.assertEqual(check["quarters"], 0)


class RoomModelTests(unittest.TestCase):
    def test_planned_rooms_without_a_clean_model_are_named_with_the_fit_reason(self):
        rooms = [{"key": "bath", "name": "Master bathroom", "status": "limited", "issues": ["insufficient_heating"]},
                 {"key": "bed", "name": "Mark's bedroom", "status": "waiting", "issues": ["waiting"]},
                 {"key": "office", "name": "Office", "status": "fitted", "issues": []},
                 {"key": "spare", "name": "Spare room", "status": "unidentified", "issues": []}]
        check = room_model_check(rooms)
        self.assertEqual((check["key"], check["healthy"], check["expected"], check["percentage"]), ("room_models", 1, 4, 25))
        self.assertEqual(check["issues"], ["Mark's bedroom: waiting", "Master bathroom: insufficient_heating",
                                           "Spare room: unidentified"])

    def test_no_planned_room_or_an_older_website_has_no_check(self):
        self.assertIsNone(room_model_check(None))
        self.assertIsNone(room_model_check([]))
        accuracy = input_accuracy(options={}, read=lambda entity: None, accuracy_report=None,
                                  measured_quarters={}, now=NOW, room_models=[{"name": "Bath", "status": "fitted"}])
        self.assertEqual([check["key"] for check in accuracy["checks"]], ["prices", "room_models"])


class SensorAndSolarTests(unittest.TestCase):
    def test_live_plant_readings_must_be_fresh_but_meters_may_be_quiet(self):
        options = {"battery_soc_entity": "sensor.soc", "grid_power_entity": "sensor.grid",
                   "entities_heating": ["sensor.heater_energy"], "ev_connected_entity": "binary_sensor.cable",
                   "excluded_device_readings": ["sensor.ignored"], "entities_household": ["sensor.ignored"]}
        states = {"sensor.soc": state("55", age=900), "sensor.grid": state("120"),
                  "sensor.heater_energy": state("716.0", age=33_000), "binary_sensor.cable": state("on")}
        check = sensor_check(options, states.get, NOW)
        self.assertEqual((check["healthy"], check["expected"], check["issues"]), (3, 4, ["sensor.soc"]))
        states["binary_sensor.cable"] = state("unavailable")
        self.assertEqual(sensor_check(options, states.get, NOW)["issues"], ["binary_sensor.cable", "sensor.soc"])

    def test_solar_forecast_needs_usable_sensors_and_is_skipped_without_solar(self):
        self.assertIsNone(solar_forecast_check({}, {}.get))
        options = {"pv_forecast_entities": ["sensor.today", "sensor.tomorrow"], "entities_solar_production": ["sensor.pv"]}
        today = state("20.5")
        today.attributes = {"watts": {"2026-10-18T12:00:00+02:00": 4200}}
        check = solar_forecast_check(options, {"sensor.today": today, "sensor.tomorrow": state("unknown")}.get)
        self.assertEqual((check["percentage"], check["issues"]), (50, ["sensor.tomorrow"]))
        self.assertEqual(solar_forecast_check({"entities_solar_production": ["sensor.pv"]}, {}.get)["percentage"], 0)

    def test_a_numeric_forecast_without_a_readable_watts_series_is_unusable(self):
        # 18 October 2026: eight numeric forecast sensors scored 100 % while the
        # replan failed, because none carried the `watts` series it reads.
        options = {"pv_forecast_entities": ["sensor.bare", "sensor.positional", "sensor.today"]}
        bare, positional, today = state("20.5"), state("12.4"), state("20.5")
        positional.attributes = {"watts": [4200, 4350]}
        today.attributes = {"watts": {"2026-10-18T12:00:00+02:00": 4200}}
        states = {"sensor.bare": bare, "sensor.positional": positional, "sensor.today": today}
        check = solar_forecast_check(options, states.get)
        self.assertEqual((check["healthy"], check["issues"]), (1, ["sensor.bare", "sensor.positional"]))


class MeasurementTests(unittest.TestCase):
    def test_missing_and_partial_quarters_are_gaps(self):
        start = NOW - timedelta(hours=1)
        entities = {"grid_import": ["sensor.import"], "solar_production": ["sensor.pv"]}
        rows = [{"start": (start + timedelta(minutes=15 * n)).isoformat(), "grid_import_kwh": 1.0,
                 "solar_production_kwh": 0.0, "total_load_kwh": 1.0} for n in (0, 2)]
        rows.append({"start": (start + timedelta(minutes=15)).isoformat(), "grid_import_kwh": 1.0, "total_load_kwh": 1.0})
        quarters = quarter_completeness(rows, entities, start, NOW)
        check = measurement_check(quarters, NOW)
        self.assertEqual((check["healthy"], check["expected"], check["percentage"]), (2, 4, 50))

    def test_only_the_last_day_counts_and_nothing_evaluated_is_unscored(self):
        old = {(NOW - timedelta(days=2)).isoformat(): False}
        self.assertEqual(prune_quarters(old, NOW), {})
        self.assertIsNone(measurement_check(old, NOW))


class HeadlineTests(unittest.TestCase):
    def test_headline_averages_the_checks_that_have_a_score(self):
        options = {"grid_power_entity": "sensor.grid", "battery_soc_entity": "sensor.soc"}
        read = {"sensor.grid": state("1"), "sensor.soc": state("unavailable")}.get
        result = input_accuracy(options=options, read=read, accuracy_report=None, measured_quarters={}, now=NOW)
        self.assertEqual([check["key"] for check in result["checks"]], ["prices", "sensors"])
        self.assertEqual((result["percentage"], result["grade"]), (50, "red"))  # unscored prices are left out
        report = {"calculated_at": "2026-10-18T04:00:05Z", "prices": {"percentage": 90, "grade": "green", "quarters": 96}}
        result = input_accuracy(options=options, read=read, accuracy_report=report, measured_quarters={}, now=NOW)
        self.assertEqual(result["percentage"], 70)


class BatteryCheckTests(unittest.TestCase):
    def test_a_missed_charge_target_lowers_accuracy_instead_of_raising_a_banner(self):
        # 18 October 2026: the 00:00 target was missed by 0.16 kWh, the next one reached.
        missed, reached = 1792360800000, 1792365300000
        check = battery_check([(missed, 156144), (reached, 0)])
        self.assertEqual((check["key"], check["healthy"], check["expected"], check["percentage"]), ("battery", 1, 2, 50))
        self.assertEqual(check["issues"], ["2026-10-18T22:00:00+00:00"])
        result = input_accuracy(options={}, read={}.get, accuracy_report=None, measured_quarters={}, now=NOW,
                                charge_targets=[(missed, 156144), (reached, 0)])
        self.assertEqual([c["key"] for c in result["checks"]], ["prices", "battery"])

    def test_no_measured_charge_target_is_no_check(self):
        self.assertIsNone(battery_check([]))
        self.assertIsNone(battery_check(None))


if __name__ == "__main__":
    unittest.main()
