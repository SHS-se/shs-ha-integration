"""Pure tests for quarter-hour aggregation and forecast normalization."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from optimisation import (  # noqa: E402
    OptimisationInputError,
    aggregate_category_changes,
    build_base_load_profile,
    calibration_summary,
    daily_service_window,
    discrete_current_control,
    extract_timestamped_forecast,
    optimisation_plan_due,
    require_fresh_source,
    utc_slots,
    validate_service_windows,
    validate_plan_contract,
)


class QuarterAggregationTests(unittest.TestCase):
    def test_three_five_minute_changes_become_one_quarter(self) -> None:
        start = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
        rows = [(start + timedelta(minutes=5 * index), 0.1) for index in range(3)]
        result = aggregate_category_changes({
            "total_consumption": rows,
            "solar_production": [
                (start, 0.2),
                (start + timedelta(minutes=5), 0.1),
                (start + timedelta(minutes=10), 0.2),
            ],
            "battery_charge": [
                (start + timedelta(minutes=5 * index), 0.05)
                for index in range(3)
            ],
        })

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["total_load_kwh"], 0.3)
        self.assertEqual(result[0]["solar_production_kwh"], 0.5)
        self.assertEqual(result[0]["battery_charge_kwh"], 0.15)
        self.assertEqual(result[0]["quality"]["duration_seconds"], 900)

    def test_incomplete_five_minute_bucket_is_not_published(self) -> None:
        start = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
        result = aggregate_category_changes({
            "total_consumption": [(start, 0.1), (start + timedelta(minutes=5), 0.1)],
        })

        self.assertEqual(result, [])

    def test_base_profile_uses_median_and_subtracts_modelled_loads(self) -> None:
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        rows = []
        for day in range(4):
            for quarter in range(96):
                # Three ordinary days have 1 kW baseload; one 20 kW outlier
                # must not become the forecast shape for every future day.
                base_kwh = 5.0 if day == 3 else 0.25
                rows.append({
                    "start": (start + timedelta(days=day, minutes=quarter * 15)).isoformat(),
                    "total_load_kwh": base_kwh + 0.5,
                    "pool_heating_kwh": 0.5,
                })
        profile = build_base_load_profile(
            rows, "UTC", modelled_categories=("pool_heating",)
        )

        self.assertEqual(len(profile), 96)
        self.assertEqual(profile[0]["median_w"], 1000)
        self.assertGreater(profile[0]["p90_w"], profile[0]["median_w"])
        self.assertEqual(profile[0]["sample_count"], 4)

    def test_base_profile_selects_weekday_and_weekend_separately(self) -> None:
        monday = datetime(2026, 8, 3, tzinfo=timezone.utc)
        rows = []
        for day in range(7):
            for quarter in range(96):
                when = monday + timedelta(days=day, minutes=quarter * 15)
                rows.append({
                    "start": when.isoformat(),
                    "total_load_kwh": 0.5 if when.weekday() >= 5 else 0.25,
                })

        weekday = build_base_load_profile(
            rows, "UTC", minimum_samples=2, modelled_categories=(),
            day_type="weekday",
        )
        weekend = build_base_load_profile(
            rows, "UTC", minimum_samples=2, modelled_categories=(),
            day_type="weekend",
        )

        self.assertEqual(weekday[0]["median_w"], 1000)
        self.assertEqual(weekend[0]["median_w"], 2000)


class ForecastTests(unittest.TestCase):
    def test_daily_service_windows_fit_a_rolling_72_hour_horizon(self) -> None:
        captured = datetime(2026, 8, 10, 17, 47, tzinfo=timezone.utc)
        horizon = utc_slots(captured, 72)

        self.assertIsNone(daily_service_window(
            horizon,
            date(2026, 8, 10),
            "Europe/Stockholm",
            "20:00",
            "09:30",
            label="pool",
        ))
        first_full_day = daily_service_window(
            horizon,
            date(2026, 8, 11),
            "Europe/Stockholm",
            "20:00",
            "09:30",
            label="pool",
        )
        self.assertEqual(first_full_day, (
            datetime(2026, 8, 10, 22, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 11, 18, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 11, 7, 30, tzinfo=timezone.utc),
        ))
        final_day = daily_service_window(
            horizon,
            date(2026, 8, 13),
            "Europe/Stockholm",
            "20:00",
            "09:30",
            label="pool",
        )
        self.assertEqual(final_day[1], horizon[-1] + timedelta(minutes=15))

    def test_end_of_day_deadline_and_local_window_validation(self) -> None:
        captured = datetime(2026, 8, 10, 17, 47, tzinfo=timezone.utc)
        horizon = utc_slots(captured, 72)
        earliest, deadline, preferred = daily_service_window(
            horizon,
            date(2026, 8, 10),
            "Europe/Stockholm",
            "24:00",
            "00:00",
            label="boiler",
        )
        self.assertEqual(earliest, horizon[0])
        self.assertEqual(deadline, datetime(2026, 8, 10, 22, 0, tzinfo=timezone.utc))
        self.assertEqual(preferred, horizon[0])
        validate_service_windows([{
            "id": "boiler:2026-08-10",
            "earliest_start": earliest.isoformat(),
            "deadline": deadline.isoformat(),
        }], horizon)
        with self.assertRaisesRegex(OptimisationInputError, "outside horizon"):
            validate_service_windows([{
                "id": "pool:bad",
                "earliest_start": (horizon[0] - timedelta(minutes=15)).isoformat(),
                "deadline": horizon[4].isoformat(),
            }], horizon)

    def test_plan_refresh_retries_before_expiry_and_after_failure(self) -> None:
        issued = datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc)
        plan = {
            "status": "ready",
            "valid_until": (issued + timedelta(minutes=75)).isoformat(),
        }
        self.assertFalse(optimisation_plan_due(
            plan, issued + timedelta(minutes=44)
        ))
        self.assertTrue(optimisation_plan_due(
            plan, issued + timedelta(minutes=45)
        ))
        self.assertTrue(optimisation_plan_due(
            plan, issued + timedelta(minutes=15), retry_after_error=True
        ))

    def test_extracts_timestamped_watts_and_reports_provenance(self) -> None:
        entity = {
            "entity_id": "sensor.pv_forecast",
            "last_updated": "2026-08-10T08:01:00+00:00",
            "attributes": {
                "watts": {
                    "2026-08-10T08:00:00+00:00": 100,
                    "2026-08-10T08:15:00+00:00": 200,
                }
            },
        }
        values, used, issued = extract_timestamped_forecast(
            [entity], attribute_names=("watts",), value_keys=("watts", "value")
        )

        self.assertEqual(list(values.values()), [100, 200])
        self.assertEqual(used, ["sensor.pv_forecast"])
        self.assertEqual(issued, datetime(2026, 8, 10, 8, 1, tzinfo=timezone.utc))

    def test_rejects_positional_forecast_without_timestamps(self) -> None:
        with self.assertRaisesRegex(OptimisationInputError, "timestamped"):
            extract_timestamped_forecast(
                [{
                    "entity_id": "sensor.price",
                    "last_updated": "2026-08-10T08:01:00+00:00",
                    "attributes": {"today": [0.1, 0.2]},
                }],
                attribute_names=("today",),
                value_keys=("price", "value"),
            )

    def test_multiple_pv_arrays_are_summed_without_double_counting_attributes(self) -> None:
        entities = [
            {
                "entity_id": f"sensor.roof_{index}",
                "last_updated": "2026-08-10T08:01:00+00:00",
                "attributes": {
                    "watts": {"2026-08-10T08:00:00+00:00": watts},
                    "forecast": [
                        {"start": "2026-08-10T08:00:00+00:00", "watts": watts}
                    ],
                },
            }
            for index, watts in enumerate((100, 250))
        ]

        values, used, _ = extract_timestamped_forecast(
            entities,
            attribute_names=("watts", "forecast"),
            value_keys=("watts", "value"),
            combine="sum",
        )

        self.assertEqual(list(values.values()), [350])
        self.assertEqual(len(used), 2)

    def test_calibration_is_compact_and_separate_by_lead_day(self) -> None:
        result = calibration_summary([
            {"lead_day": 0, "predicted_kwh": 1.0, "actual_kwh": 0.8},
            {"lead_day": 0, "predicted_kwh": 1.0, "actual_kwh": 0.8},
            {"lead_day": 1, "predicted_kwh": 1.0, "actual_kwh": 0.5},
        ], minimum_samples=2)

        self.assertEqual(result["correction_factor_by_lead_day"][:2], [0.8, 1.0])
        self.assertEqual(result["sample_count_by_lead_day"][:2], [2, 1])

    def test_stale_forecast_is_rejected(self) -> None:
        captured = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        with self.assertRaisesRegex(OptimisationInputError, "stale"):
            require_fresh_source(
                captured - timedelta(hours=13),
                captured,
                max_age=timedelta(hours=12),
                label="PV forecast",
            )

    def test_discrete_current_capability_uses_number_entity_bounds(self) -> None:
        control = discrete_current_control(5, 16, 1, 3, 230)

        self.assertEqual(control, {
            "type": "discrete_current",
            "min_current_a": 5,
            "max_current_a": 16,
            "current_step_a": 1,
            "phase_count": 3,
            "voltage_v": 230,
        })
        with self.assertRaisesRegex(OptimisationInputError, "not aligned"):
            discrete_current_control(5, 16.5, 1, 3, 230)

    def test_cached_plan_accepts_and_verifies_ev_current_steps(self) -> None:
        now = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
        currents = [5] * 7 + [6, 6]
        slots = [
            {
                "start": (now + timedelta(minutes=15 * index)).isoformat(),
                "binding": True,
                "pool_w": 0,
                "boiler_w": 0,
                "ev_w": current * 3 * 230,
                "ev_target_current_a": current,
                "ev_min_current_a": 0,
                "ev_max_current_a": 16,
                "battery_soc": 0.5,
            }
            for index, current in enumerate(currents)
        ]
        service = {
            "id": "ev:departure",
            "device": "ev",
            "earliest_start": now.isoformat(),
            "deadline": (now + timedelta(hours=2, minutes=15)).isoformat(),
            "required_kwh": 8,
            "control": {
                "type": "discrete_current",
                "min_current_a": 5,
                "max_current_a": 16,
                "current_step_a": 1,
                "phase_count": 3,
                "voltage_v": 230,
            },
            "min_run_slots": 2,
        }
        plan = {
            "schema_version": 3,
            "mode": "live",
            "capabilities": {
                "pv": True,
                "battery": True,
                "pool": False,
                "boiler": False,
                "ev": True,
            },
            "slot_minutes": 15,
            "model_version": "quarter-hour-heuristic-v3",
            "status": "ready",
            "issued_at": now.isoformat(),
            "valid_until": (now + timedelta(hours=2, minutes=30)).isoformat(),
            "binding_until": (now + timedelta(hours=2, minutes=15)).isoformat(),
            "services": [service],
            "plans": {
                key: {
                    "status": "ready",
                    "slots": [dict(slot) for slot in slots],
                    "service_slots": {"ev:departure": list(range(9))},
                    "service_currents_a": {"ev:departure": list(currents)},
                }
                for key in ("baseline", "priority", "cost")
            },
        }
        validate_plan_contract(plan, now)

        plan["mode"] = "demo"
        with self.assertRaisesRegex(OptimisationInputError, "mode"):
            validate_plan_contract(plan, now)
        plan["mode"] = "live"

        plan["plans"]["priority"]["slots"][0]["ev_max_current_a"] = 17
        with self.assertRaisesRegex(OptimisationInputError, "charger capability"):
            validate_plan_contract(plan, now)
        plan["plans"]["priority"]["slots"][0]["ev_max_current_a"] = 16

        plan["plans"]["priority"]["service_currents_a"]["ev:departure"][0] = 5.5
        with self.assertRaisesRegex(OptimisationInputError, "current step"):
            validate_plan_contract(plan, now)

    def test_cached_plan_requires_matching_contiguous_scenarios(self) -> None:
        now = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
        slots = [
            {
                "start": (now + timedelta(minutes=15 * index)).isoformat(),
                "binding": True,
                "pool_w": 0,
                "boiler_w": 0,
                "ev_w": 0,
                "ev_target_current_a": 0,
                "ev_min_current_a": 0,
                "ev_max_current_a": 0,
                "battery_soc": 0.5,
            }
            for index in range(4)
        ]
        plan = {
            "schema_version": 3,
            "mode": "live",
            "capabilities": {
                "pv": True,
                "battery": True,
                "pool": False,
                "boiler": False,
                "ev": False,
            },
            "slot_minutes": 15,
            "model_version": "quarter-hour-heuristic-v3",
            "status": "ready",
            "issued_at": now.isoformat(),
            "valid_until": (now + timedelta(minutes=75)).isoformat(),
            "binding_until": (now + timedelta(hours=1)).isoformat(),
            "services": [],
            "plans": {
                key: {
                    "status": "ready",
                    "slots": [dict(slot) for slot in slots],
                    "service_slots": {},
                    "service_currents_a": {},
                }
                for key in ("baseline", "priority", "cost")
            },
        }
        validate_plan_contract(plan, now)

        plan["plans"]["cost"]["slots"][2]["start"] = (
            now + timedelta(minutes=60)
        ).isoformat()
        with self.assertRaisesRegex(OptimisationInputError, "not contiguous"):
            validate_plan_contract(plan, now)

    def test_cached_plan_binding_flags_must_match_the_boundary(self) -> None:
        now = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
        slots = [
            {
                "start": (now + timedelta(minutes=15 * index)).isoformat(),
                "binding": index < 2,
                "pool_w": 0,
                "boiler_w": 0,
                "ev_w": 0,
                "ev_target_current_a": 0,
                "ev_min_current_a": 0,
                "ev_max_current_a": 0,
                "battery_soc": 0.5,
            }
            for index in range(4)
        ]
        plan = {
            "schema_version": 3,
            "mode": "live",
            "capabilities": {
                "pv": True,
                "battery": True,
                "pool": False,
                "boiler": False,
                "ev": False,
            },
            "slot_minutes": 15,
            "model_version": "quarter-hour-heuristic-v3",
            "status": "ready",
            "issued_at": now.isoformat(),
            "valid_until": (now + timedelta(minutes=75)).isoformat(),
            "binding_until": (now + timedelta(minutes=30)).isoformat(),
            "services": [],
            "plans": {
                key: {
                    "status": "ready",
                    "slots": [dict(slot) for slot in slots],
                    "service_slots": {},
                    "service_currents_a": {},
                }
                for key in ("baseline", "priority", "cost")
            },
        }
        validate_plan_contract(plan, now)

        plan["plans"]["priority"]["slots"][2]["binding"] = True
        with self.assertRaisesRegex(OptimisationInputError, "different binding"):
            validate_plan_contract(plan, now)

    def test_cached_plan_rejects_fractional_device_power(self) -> None:
        now = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
        slots = [
            {
                "start": (now + timedelta(minutes=15 * index)).isoformat(),
                "binding": True,
                "pool_w": 0,
                "boiler_w": 3000 if index < 2 else 0,
                "ev_w": 0,
                "ev_target_current_a": 0,
                "ev_min_current_a": 0,
                "ev_max_current_a": 0,
                "battery_soc": 0.5,
            }
            for index in range(4)
        ]
        plan = {
            "schema_version": 3,
            "mode": "live",
            "capabilities": {
                "pv": True,
                "battery": True,
                "pool": False,
                "boiler": True,
                "ev": False,
            },
            "slot_minutes": 15,
            "model_version": "quarter-hour-heuristic-v3",
            "status": "ready",
            "issued_at": now.isoformat(),
            "valid_until": (now + timedelta(minutes=75)).isoformat(),
            "binding_until": (now + timedelta(hours=1)).isoformat(),
            "services": [{
                "id": "boiler:today",
                "device": "boiler",
                "required_kwh": 1.5,
                "earliest_start": now.isoformat(),
                "deadline": (now + timedelta(hours=1)).isoformat(),
                "control": {"type": "fixed_power", "power_w": 3000},
                "min_run_slots": 2,
            }],
            "plans": {
                key: {
                    "status": "ready",
                    "slots": [dict(slot) for slot in slots],
                    "service_slots": {"boiler:today": [0, 1]},
                    "service_currents_a": {},
                }
                for key in ("baseline", "priority", "cost")
            },
        }
        validate_plan_contract(plan, now)

        plan["plans"]["priority"]["slots"][0]["boiler_w"] = 1500
        with self.assertRaisesRegex(OptimisationInputError, "discrete schedule"):
            validate_plan_contract(plan, now)


if __name__ == "__main__":
    unittest.main()
