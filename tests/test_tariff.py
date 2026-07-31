"""Pure tests for the versioned tariff engine (no Home Assistant runtime)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from tariff import (  # noqa: E402
    HourlyGridReading,
    MissingTariffError,
    UnsupportedTariffError,
    calculate_month,
    missing_input_labels,
    tariff_component_definitions,
)

TZ = ZoneInfo("Europe/Stockholm")


def definition(
    *,
    tax_ore: float,
    transfer_ore: float,
    fixed_three: float,
    fixed_single: float,
    fixed_apartment: float,
    demand: bool,
    single_transfer_ore: float | None = None,
) -> dict:
    three_phase = {
        "selector": "fuse_a",
        "fixed_monthly_sek_ex_vat": {"16": fixed_three, "20": fixed_three},
        "transfer": {"mode": "flat", "ore_per_kwh_ex_vat": transfer_ore},
    }
    single_phase = {
        "selector": "fuse_a",
        "fixed_monthly_sek_ex_vat": {"20": fixed_single},
        "transfer": (
            {
                "mode": "flat_by_selector",
                "ore_per_kwh_ex_vat": {
                    "20": single_transfer_ore or transfer_ore
                },
            }
            if not demand
            else {"mode": "flat", "ore_per_kwh_ex_vat": transfer_ore}
        ),
    }
    if demand:
        demand_rule = {
            "rate_sek_per_kw_ex_vat": 65,
            "top_n": 3,
            "distinct_local_days": True,
            "night_start_hour": 22,
            "night_end_hour": 6,
            "night_factor": 0.5,
        }
        three_phase["demand"] = demand_rule
        single_phase["demand"] = demand_rule
    return {
        "schema_version": 1,
        "vat_rate": 0.25,
        "energy_tax": {
            "ore_per_kwh_ex_vat": tax_ore,
            "reduction_ore_per_kwh": 9.6,
        },
        "plans": {
            "three_phase": three_phase,
            "single_phase": single_phase,
            "apartment": {
                "selector": "apartment_band",
                "fixed_monthly_sek_ex_vat": {"100_plus": fixed_apartment},
                "transfer": {
                    "mode": "flat",
                    "ore_per_kwh_ex_vat": 20.8,
                },
            },
        },
        "export_credit": {
            "schedule": "swedish_winter_weekday_06_22_v1",
            "ore_per_kwh_ex_vat_by_area": {
                "stockholm": {"high": 4.4, "low": 3.3},
            },
        },
    }


def catalog(configuration: dict) -> dict:
    return {
        "schema_version": 2,
        "calculation_version": 2,
        "timezone": "Europe/Stockholm",
        "configuration": {"profile_id": "profile", **configuration},
        "missing_inputs": [],
        "profiles": [
            {
                "id": "profile",
                "provider_key": "ellevio",
                "tariff_key": "small_connection",
                "provider_name": "Ellevio",
                "display_name": "Ellevio",
                "currency": "SEK",
                "versions": [
                    {
                        "id": "2025",
                        "profile_id": "profile",
                        "revision": "ellevio-2025-01-01",
                        "valid_from": "2025-01-01",
                        "valid_to": "2025-12-31",
                        "calculation_model": "se_grid_v1",
                        "definition": definition(
                            tax_ore=43.9,
                            transfer_ore=5,
                            fixed_three=292,
                            fixed_single=104,
                            fixed_apartment=88,
                            demand=True,
                        ),
                    },
                    {
                        "id": "2026-early",
                        "profile_id": "profile",
                        "revision": "ellevio-2026-01-01",
                        "valid_from": "2026-01-01",
                        "valid_to": "2026-05-31",
                        "calculation_model": "se_grid_v1",
                        "definition": definition(
                            tax_ore=36,
                            transfer_ore=5.6,
                            fixed_three=316,
                            fixed_single=116,
                            fixed_apartment=72,
                            demand=True,
                        ),
                    },
                    {
                        "id": "2026-late",
                        "profile_id": "profile",
                        "revision": "ellevio-2026-06-01",
                        "valid_from": "2026-06-01",
                        "valid_to": None,
                        "calculation_model": "se_grid_v1",
                        "definition": definition(
                            tax_ore=36,
                            transfer_ore=20.8,
                            fixed_three=360,
                            fixed_single=136,
                            fixed_apartment=72,
                            demand=False,
                            single_transfer_ore=40,
                        ),
                    },
                ],
            }
        ],
    }


def config(**overrides: object) -> dict:
    result = {
        "connection_type": "three_phase",
        "fuse_a": 16,
        "grid_area": "stockholm",
        "production_enabled": False,
        "energy_tax_reduced": False,
        "include_vat": False,
        "export_vat_registered": False,
    }
    result.update(overrides)
    return result


def hourly_readings(
    start_day: date,
    end_day: date,
    imports: dict[tuple[date, int], float] | None = None,
    exports: dict[tuple[date, int], float] | None = None,
) -> list[HourlyGridReading]:
    imports = imports or {}
    exports = exports or {}
    cursor = datetime.combine(start_day, time.min, TZ).astimezone(timezone.utc)
    end = datetime.combine(end_day + timedelta(days=1), time.min, TZ).astimezone(
        timezone.utc
    )
    result = []
    while cursor < end:
        local = cursor.astimezone(TZ)
        key = (local.date(), local.hour)
        result.append(
            HourlyGridReading(cursor, imports.get(key, 0), exports.get(key, 0))
        )
        cursor += timedelta(hours=1)
    return result


def components(result: dict, category: str) -> list[dict]:
    return [value for value in result["components"] if value["category"] == category]


class TariffCalculationTests(unittest.TestCase):
    def test_demand_uses_distinct_days_and_halves_night_peaks(self) -> None:
        start = date(2025, 1, 1)
        readings = hourly_readings(
            start,
            date(2025, 1, 4),
            {
                (start, 12): 10,
                (date(2025, 1, 2), 23): 8,
                (date(2025, 1, 3), 12): 6,
                (date(2025, 1, 4), 12): 5,
            },
        )
        result = calculate_month(catalog(config()), readings, date(2025, 1, 1))

        self.assertEqual(result["peak_demand_kw"], 7)
        demand_component = components(result, "peak_demand")[0]
        self.assertEqual(demand_component["component_key"], "peak_demand_fee")
        self.assertEqual(demand_component["amount_sek"], 455)
        self.assertEqual(result["tariff_revisions"], ["ellevio-2025-01-01"])

    def test_june_2026_single_phase_selector_rate_has_no_demand(self) -> None:
        day = date(2026, 6, 1)
        readings = hourly_readings(day, day, {(day, 12): 10})
        result = calculate_month(
            catalog(config(connection_type="single_phase", fuse_a=20)),
            readings,
            day,
        )

        self.assertEqual(components(result, "peak_demand"), [])
        self.assertEqual(components(result, "energy_transfer")[0]["amount_sek"], 4)
        self.assertEqual(result["peak_demand_kw"], None)

    def test_apartment_band_selects_its_fixed_fee(self) -> None:
        day = date(2026, 6, 1)
        readings = hourly_readings(day, day, {(day, 12): 10})
        result = calculate_month(
            catalog(
                config(
                    connection_type="apartment",
                    apartment_band="100_plus",
                    fuse_a=None,
                )
            ),
            readings,
            day,
        )

        self.assertEqual(components(result, "fixed_fee")[0]["amount_sek"], 2.4)
        self.assertEqual(components(result, "energy_transfer")[0]["amount_sek"], 2.08)

    def test_export_credit_observes_holiday_and_high_load_hours(self) -> None:
        first = date(2026, 1, 1)
        second = date(2026, 1, 2)
        readings = hourly_readings(
            first,
            second,
            exports={(first, 12): 1, (second, 12): 1, (second, 23): 1},
        )
        result = calculate_month(
            catalog(config(production_enabled=True)), readings, first
        )
        credits = components(result, "export_credit")

        self.assertEqual(len(credits), 2)
        self.assertAlmostEqual(
            sum(value["amount_sek"] for value in credits), -0.11
        )

    def test_energy_tax_and_vat_are_explicit_components(self) -> None:
        day = date(2026, 6, 1)
        readings = hourly_readings(day, day, {(day, 12): 10})
        result = calculate_month(
            catalog(config(include_vat=True)), readings, day
        )

        self.assertEqual(components(result, "energy_tax")[0]["amount_sek"], 3.6)
        self.assertEqual(components(result, "vat")[0]["amount_sek"], 4.42)
        self.assertEqual(result["total_amount_sek"], 22.1)

    def test_dst_day_with_23_real_hours_is_complete_input(self) -> None:
        day = date(2026, 3, 29)
        readings = hourly_readings(day, day)
        result = calculate_month(catalog(config()), readings, date(2026, 3, 1))

        self.assertEqual(result["coverage_start"], "2026-03-29")
        self.assertEqual(result["coverage_end"], "2026-03-29")

    def test_first_version_can_start_partway_through_a_month(self) -> None:
        payload = catalog(config())
        version = payload["profiles"][0]["versions"][-1]
        version["valid_from"] = "2026-06-15"
        payload["profiles"][0]["versions"] = [version]
        readings = hourly_readings(date(2026, 6, 1), date(2026, 6, 16))
        result = calculate_month(payload, readings, date(2026, 6, 1))

        self.assertEqual(result["coverage_start"], "2026-06-15")
        self.assertEqual(result["coverage_end"], "2026-06-16")
        self.assertEqual(components(result, "fixed_fee")[0]["amount_sek"], 24)

    def test_component_keys_include_historical_demand(self) -> None:
        definitions = tariff_component_definitions(catalog(config()))
        self.assertIn("peak_demand_fee", definitions)
        self.assertIn("grid_energy_transfer", definitions)

    def test_missing_questionnaire_input_fails_explicitly(self) -> None:
        payload = catalog(config())
        payload["configuration"] = None
        payload["missing_inputs"] = ["main_fuse_a"]
        day = date(2026, 6, 1)
        with self.assertRaises(MissingTariffError):
            calculate_month(payload, hourly_readings(day, day), day)

    def test_unknown_schema_fails_instead_of_guessing(self) -> None:
        payload = catalog(config())
        payload["schema_version"] = 3
        day = date(2026, 6, 1)
        with self.assertRaises(UnsupportedTariffError):
            calculate_month(payload, hourly_readings(day, day), day)


class MissingInputLabelTests(unittest.TestCase):
    def test_server_question_text_is_preferred_over_the_key(self) -> None:
        payload = {
            "missing_inputs": ["has_solar"],
            "missing_input_details": [
                {
                    "key": "has_solar",
                    "question_sv": "Finns det solceller på bostaden?",
                    "question_en": "Does the home have solar panels?",
                }
            ],
        }
        self.assertEqual(
            missing_input_labels(payload, "en"), ["Does the home have solar panels?"]
        )
        self.assertEqual(
            missing_input_labels(payload, "sv"), ["Finns det solceller på bostaden?"]
        )

    def test_older_server_without_details_still_reads_usefully(self) -> None:
        self.assertEqual(
            missing_input_labels({"missing_inputs": ["main_fuse_a"]}),
            ["Main fuse size"],
        )

    def test_unknown_key_falls_back_to_the_key_itself(self) -> None:
        self.assertEqual(
            missing_input_labels({"missing_inputs": ["something_new"]}),
            ["something_new"],
        )

    def test_nothing_missing_yields_no_labels(self) -> None:
        self.assertEqual(missing_input_labels({}), [])


if __name__ == "__main__":
    unittest.main()
