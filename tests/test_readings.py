"""Pure tests for daily per-category readings and negative recorder changes."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from const import MAX_KWH_PER_READING, MAX_NEGATIVE_CHANGE_KWH  # noqa: E402
from readings import daily_category_readings, usable_change  # noqa: E402


class UsableChangeTests(unittest.TestCase):
    def test_positive_change_is_the_energy_itself(self) -> None:
        self.assertEqual(usable_change(1.25), 1.25)
        self.assertEqual(usable_change(0), 0.0)

    def test_meter_resuming_a_hair_low_is_worth_zero(self) -> None:
        # sensor.pool_heater_energy came back from a power cut reporting
        # 7862.724 where the recorder had last seen 7862.724003.
        self.assertEqual(usable_change(-0.000003), 0.0)
        self.assertEqual(usable_change(-0.000669), 0.0)
        self.assertEqual(usable_change(-MAX_NEGATIVE_CHANGE_KWH), 0.0)

    def test_counter_reset_stays_unknown(self) -> None:
        self.assertIsNone(usable_change(-MAX_NEGATIVE_CHANGE_KWH - 0.001))
        self.assertIsNone(usable_change(-7862.724))


class DailyCategoryReadingTests(unittest.TestCase):
    def test_every_mapped_meter_reporting_sums_into_one_reading(self) -> None:
        readings, skipped, incomplete = daily_category_readings(
            {"2026-08-27": {"sensor.a": 1.5, "sensor.b": 2.25}},
            {"heating": ["sensor.a", "sensor.b"]},
        )

        self.assertEqual(
            readings, [{"date": "2026-08-27", "category": "heating", "kwh": 3.75}]
        )
        self.assertEqual(skipped, [])
        self.assertEqual(incomplete, [])

    def test_rounding_artefact_no_longer_costs_the_whole_category(self) -> None:
        """The Aug 28 regression: one meter at -0.000669 kWh erased household.

        The clamp happens upstream in ``usable_change``, so by the time the day
        is assembled the meter is present at zero and the category survives.
        """
        changes = {
            "sensor.dishwasher": usable_change(-0.000669),
            "sensor.freezer": 0.85,
        }
        self.assertIsNotNone(changes["sensor.dishwasher"])
        readings, _, incomplete = daily_category_readings(
            {"2026-08-28": changes},
            {"household": ["sensor.dishwasher", "sensor.freezer"]},
        )

        self.assertEqual(
            readings, [{"date": "2026-08-28", "category": "household", "kwh": 0.85}]
        )
        self.assertEqual(incomplete, [])

    def test_silent_meter_withholds_the_category_and_says_so(self) -> None:
        readings, _, incomplete = daily_category_readings(
            {
                "2026-08-29": {"sensor.pump": 0.1, "sensor.floor": 0.2},
                "2026-08-30": {"sensor.pump": 0.1, "sensor.floor": 0.2},
            },
            {"pool_heating": ["sensor.heater", "sensor.pump", "sensor.floor"]},
        )

        # Withholding the partial total is deliberate; going quiet about it
        # was the bug.
        self.assertEqual(readings, [])
        self.assertEqual(
            incomplete,
            ["pool_heating: 2 days without sensor.heater (latest 2026-08-30)"],
        )

    def test_one_missing_day_reads_as_a_day(self) -> None:
        _, _, incomplete = daily_category_readings(
            {"2026-08-31": {"sensor.pump": 0.1}},
            {"pool_heating": ["sensor.heater", "sensor.pump"]},
        )

        self.assertEqual(
            incomplete,
            ["pool_heating: 1 day without sensor.heater (latest 2026-08-31)"],
        )

    def test_longest_running_gap_is_reported_first(self) -> None:
        _, _, incomplete = daily_category_readings(
            {
                "2026-08-29": {"sensor.towel": 0.4},
                "2026-08-30": {"sensor.towel": 0.4},
                "2026-08-31": {},
            },
            {
                "pool_heating": ["sensor.heater"],
                "heating": ["sensor.towel"],
            },
        )

        self.assertEqual(len(incomplete), 2)
        self.assertTrue(incomplete[0].startswith("pool_heating: 3 days"))
        self.assertTrue(incomplete[1].startswith("heating: 1 day"))

    def test_unconfigured_category_is_not_reported_as_a_gap(self) -> None:
        readings, skipped, incomplete = daily_category_readings(
            {"2026-08-27": {"sensor.a": 1.0}},
            {"heating": ["sensor.a"], "ev_charging": []},
        )

        self.assertEqual(len(readings), 1)
        self.assertEqual(skipped, [])
        self.assertEqual(incomplete, [])

    def test_implausible_total_is_still_skipped_separately(self) -> None:
        readings, skipped, incomplete = daily_category_readings(
            {"2026-08-27": {"sensor.a": MAX_KWH_PER_READING + 1}},
            {"household": ["sensor.a"]},
        )

        self.assertEqual(readings, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("household 2026-08-27", skipped[0])
        self.assertEqual(incomplete, [])


if __name__ == "__main__":
    unittest.main()
