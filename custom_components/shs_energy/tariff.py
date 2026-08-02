"""Strict, versioned Swedish grid-tariff calculation engine.

The website owns one global tariff publication timeline. This module only
accepts the machine-readable contract delivered to a paired device; unknown
schema versions, models, or rules fail explicitly instead of being guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 2
CALCULATION_VERSION = 2
CALCULATION_MODEL = "se_grid_v1"
EXPORT_SCHEDULE = "swedish_winter_weekday_06_22_v1"


class TariffError(Exception):
    """Base class for tariff contract and calculation failures."""


class UnsupportedTariffError(TariffError):
    """The server sent a contract version or rule this build cannot execute."""


class MissingTariffError(TariffError):
    """No unambiguous published rate version covers the requested date."""


class MissingTariffInputError(TariffError):
    """Hourly recorder data is absent or incomplete for the calculation period."""


@dataclass(frozen=True)
class HourlyGridReading:
    """Grid energy measured during the hour beginning at ``start``."""

    start: datetime
    import_kwh: float
    export_kwh: float = 0.0


@dataclass(frozen=True)
class _ResolvedTariff:
    profile_id: str
    revision: str
    profile: dict[str, Any]
    version: dict[str, Any]
    configuration: dict[str, Any]
    definition: dict[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        return (self.profile_id, self.revision)


def _as_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UnsupportedTariffError(f"{label} must be an object")
    return value


def _as_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise UnsupportedTariffError(f"{label} must be an array")
    return value


def _parse_date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise UnsupportedTariffError(f"{label} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as err:
        raise UnsupportedTariffError(f"{label} must be an ISO date") from err


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise UnsupportedTariffError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as err:
        raise UnsupportedTariffError(f"{label} must be numeric") from err
    if not math.isfinite(result):
        raise UnsupportedTariffError(f"{label} must be finite")
    return result


def _money(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _quantity(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _unit_price(value: float) -> float:
    return float(
        Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    )


def _next_month(month: date) -> date:
    return date(month.year + (month.month == 12), (month.month % 12) + 1, 1)


def _month_end(month: date) -> date:
    return _next_month(month) - timedelta(days=1)


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _date_is_active(value: date, valid_from: date, valid_to: date | None) -> bool:
    return value >= valid_from and (valid_to is None or value <= valid_to)


class _Catalog:
    def __init__(self, payload: dict[str, Any]) -> None:
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise UnsupportedTariffError(
                f"unsupported tariff schema {payload.get('schema_version')!r}"
            )
        if payload.get("calculation_version") != CALCULATION_VERSION:
            version = payload.get("calculation_version")
            raise UnsupportedTariffError(f"unsupported calculation version {version!r}")

        timezone_name = payload.get("timezone")
        if not isinstance(timezone_name, str):
            raise UnsupportedTariffError("tariff timezone is missing")
        try:
            self.timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as err:
            raise UnsupportedTariffError(
                f"unsupported tariff timezone {timezone_name!r}"
            ) from err

        self.payload = payload
        profiles = _as_list(payload.get("profiles"), "profiles")
        missing_inputs = _as_list(payload.get("missing_inputs"), "missing_inputs")
        if any(not isinstance(value, str) or not value for value in missing_inputs):
            raise UnsupportedTariffError("missing_inputs must contain names")
        self.missing_inputs = missing_inputs
        configuration = payload.get("configuration")
        self.configuration = (
            None if configuration is None else _as_dict(configuration, "configuration")
        )
        self.profiles: dict[str, dict[str, Any]] = {}
        for raw_profile in profiles:
            profile = _as_dict(raw_profile, "profile")
            profile_id = profile.get("id")
            if not isinstance(profile_id, str) or not profile_id:
                raise UnsupportedTariffError("profile id is missing")
            if profile_id in self.profiles:
                raise UnsupportedTariffError(f"duplicate profile {profile_id}")
            if profile.get("currency") != "SEK":
                raise UnsupportedTariffError("only SEK tariff profiles are supported")
            _as_list(profile.get("versions"), "profile versions")
            self.profiles[profile_id] = profile

    def resolve_optional(self, value: date) -> _ResolvedTariff | None:
        """Resolve a date, returning ``None`` when it is outside the catalogue."""
        if self.configuration is None:
            raise MissingTariffError(
                "customer tariff inputs missing: " + ", ".join(self.missing_inputs)
            )
        profile_id = self.configuration.get("profile_id")
        if not isinstance(profile_id, str) or not profile_id:
            raise UnsupportedTariffError("configured tariff profile is missing")
        profile = self.profiles.get(profile_id)
        if profile is None:
            raise UnsupportedTariffError(
                f"configured tariff profile {profile_id} is missing"
            )

        versions: list[dict[str, Any]] = []
        for raw_version in _as_list(profile.get("versions"), "profile versions"):
            version = _as_dict(raw_version, "tariff version")
            valid_from = _parse_date(version.get("valid_from"), "version.valid_from")
            valid_to_raw = version.get("valid_to")
            valid_to = (
                None
                if valid_to_raw is None
                else _parse_date(valid_to_raw, "version.valid_to")
            )
            if _date_is_active(value, valid_from, valid_to):
                versions.append(version)

        if not versions:
            return None
        if len(versions) > 1:
            raise MissingTariffError(
                f"expected one tariff rate version on {value.isoformat()}, "
                f"found {len(versions)}"
            )
        version = versions[0]
        if version.get("calculation_model") != CALCULATION_MODEL:
            raise UnsupportedTariffError(
                f"unsupported calculation model {version.get('calculation_model')!r}"
            )
        revision = version.get("revision")
        if not isinstance(revision, str) or not revision:
            raise UnsupportedTariffError("tariff revision is missing")
        definition = _as_dict(version.get("definition"), "tariff definition")
        if definition.get("schema_version") != 1:
            raise UnsupportedTariffError(
                f"unsupported definition schema {definition.get('schema_version')!r}"
            )
        return _ResolvedTariff(
            profile_id=profile_id,
            revision=revision,
            profile=profile,
            version=version,
            configuration=self.configuration,
            definition=definition,
        )

    def resolve(self, value: date) -> _ResolvedTariff:
        """Resolve a date or fail when it is outside the published catalogue."""
        resolved = self.resolve_optional(value)
        if resolved is None:
            raise MissingTariffError(f"no tariff covers {value.isoformat()}")
        return resolved


def validate_tariff_catalog(payload: dict[str, Any]) -> None:
    """Validate the envelope and catalogue indexes before it is cached."""
    _Catalog(_as_dict(payload, "tariff payload"))


def tariff_timezone(payload: dict[str, Any]) -> ZoneInfo:
    """Return the contract timezone after validating the catalogue envelope."""
    return _Catalog(_as_dict(payload, "tariff payload")).timezone


# Only used when the server sends no question text (older backend, or an input
# that is not a home-profile question at all).
_MISSING_INPUT_FALLBACKS = {
    "main_fuse_a": "Main fuse size",
    "has_solar": "Solar panels",
    "central_tariff_settings": "Tariff catalogue not published yet",
}


def missing_input_labels(payload: dict[str, Any], language: str = "en") -> list[str]:
    """Return readable names for the unanswered home-profile questions."""
    details = payload.get("missing_input_details")
    if isinstance(details, list) and details:
        labels: list[str] = []
        for detail in details:
            if not isinstance(detail, dict):
                continue
            key = detail.get("key")
            preferred = "question_sv" if language.startswith("sv") else "question_en"
            text = detail.get(preferred) or detail.get("question_en") or detail.get(
                "question_sv"
            )
            if isinstance(text, str) and text:
                labels.append(text)
            elif isinstance(key, str) and key:
                labels.append(_MISSING_INPUT_FALLBACKS.get(key, key))
        if labels:
            return labels
    keys = payload.get("missing_inputs")
    if not isinstance(keys, list):
        return []
    return [
        _MISSING_INPUT_FALLBACKS.get(key, key)
        for key in keys
        if isinstance(key, str) and key
    ]


def earliest_tariff_date(payload: dict[str, Any]) -> date | None:
    """Return the first globally published effective date."""
    catalog = _Catalog(_as_dict(payload, "tariff payload"))
    dates = [
        _parse_date(version.get("valid_from"), "version.valid_from")
        for profile in catalog.profiles.values()
        for version in _as_list(profile.get("versions"), "profile versions")
    ]
    return min(dates) if dates else None


def tariff_component_definitions(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Return every stable component key present anywhere in the catalogue."""
    catalog = _Catalog(_as_dict(payload, "tariff payload"))
    result: dict[str, dict[str, str]] = {}
    for profile in catalog.profiles.values():
        for raw_version in _as_list(profile.get("versions"), "profile versions"):
            version = _as_dict(raw_version, "tariff version")
            definition = _as_dict(version.get("definition"), "tariff definition")
            result.update({
                "fixed_grid_fee": {"label": "Fixed grid fee", "category": "fixed_fee"},
                "grid_energy_transfer": {"label": "Grid energy transfer", "category": "energy_transfer"},
                "energy_tax": {"label": "Swedish energy tax", "category": "energy_tax"},
            })
            plans = _as_dict(definition.get("plans"), "plans")
            if any(
                _as_dict(plan, "plan").get("demand") is not None
                for plan in plans.values()
            ):
                result["peak_demand_fee"] = {
                    "label": "Peak-demand fee",
                    "category": "peak_demand",
                }
            if definition.get("export_credit") is not None:
                result["export_credit_high"] = {
                    "label": "Grid export credit (high load)",
                    "category": "export_credit",
                }
                result["export_credit_low"] = {
                    "label": "Grid export credit (low load)",
                    "category": "export_credit",
                }
    # No "vat" entry on purpose: VAT is folded into each displayed component
    # instead of standing on its own, so the sensors read like an invoice row.
    return result


def display_components(
    calculation: dict[str, Any],
    export_vat_registered: bool = False,
) -> list[dict[str, Any]]:
    """Return invoice-style components with VAT folded into each amount.

    The stored calculation keeps every rate ex-VAT plus a separate VAT
    component, which is what gets pushed to the portal. Ellevio's own invoice
    rows instead quote gross unit prices, so for display each VAT-able
    component absorbs its share and the standalone VAT row disappears. Each
    component keeps its ex-VAT figures so the derivation stays visible.
    """
    components = calculation.get("components")
    if not isinstance(components, list):
        return []

    # A month can span revisions, and each group carries its own VAT row.
    rate_by_group: dict[tuple[Any, Any, Any], float] = {}
    for component in components:
        if not isinstance(component, dict) or component.get("category") != "vat":
            continue
        rate = component.get("unit_price_sek")
        if isinstance(rate, (int, float)):
            rate_by_group[_display_group(component)] = float(rate)

    displayed: list[dict[str, Any]] = []
    for component in components:
        if not isinstance(component, dict) or component.get("category") == "vat":
            continue
        rate = rate_by_group.get(_display_group(component), 0.0)
        if component.get("category") == "export_credit" and not export_vat_registered:
            rate = 0.0
        amount_ex_vat = component.get("amount_sek")
        unit_price_ex_vat = component.get("unit_price_sek")
        gross = (
            round(amount_ex_vat * (1 + rate), 2)
            if isinstance(amount_ex_vat, (int, float))
            else amount_ex_vat
        )
        entry = dict(component)
        entry["amount_sek"] = gross
        entry["amount_sek_ex_vat"] = amount_ex_vat
        entry["vat_rate"] = rate
        entry["vat_amount_sek"] = (
            round(gross - amount_ex_vat, 2)
            if isinstance(amount_ex_vat, (int, float))
            and isinstance(gross, (int, float))
            else None
        )
        entry["unit_price_sek_ex_vat"] = unit_price_ex_vat
        if isinstance(unit_price_ex_vat, (int, float)):
            entry["unit_price_sek"] = round(unit_price_ex_vat * (1 + rate), 5)
        displayed.append(entry)
    return displayed


def current_grid_prices(
    payload: dict[str, Any], when: datetime
) -> dict[str, Any] | None:
    """Marginal grid cost of one more kWh, in or out, at ``when``.

    This is the network side only — transfer, energy tax and VAT. What the
    electricity supplier charges for the energy itself is not part of the grid
    tariff and is not included.

    Returns ``None`` while the tariff is unconfigured or the date falls outside
    the published catalogue.
    """
    catalog = _Catalog(_as_dict(payload, "tariff payload"))
    if catalog.configuration is None:
        return None
    local = when.astimezone(catalog.timezone)
    resolved = catalog.resolve_optional(local.date())
    if resolved is None:
        return None

    plan, selector_value = _plan(resolved)
    transfer_rate = _transfer_rate(plan, selector_value)
    tax_rate = _energy_tax_rate(resolved)

    include_vat = resolved.configuration.get("include_vat")
    if not isinstance(include_vat, bool):
        raise UnsupportedTariffError("include_vat must be boolean")
    vat_rate = (
        _number(resolved.definition.get("vat_rate"), "VAT rate") if include_vat else 0.0
    )

    import_ex_vat = transfer_rate + tax_rate
    import_price = import_ex_vat * (1 + vat_rate)

    export_price = 0.0
    export_ex_vat = 0.0
    band: str | None = None
    production_enabled = resolved.configuration.get("production_enabled")
    if production_enabled is True:
        export_rule = _as_dict(
            resolved.definition.get("export_credit"), "export credit"
        )
        area_rates = _as_dict(
            export_rule.get("ore_per_kwh_ex_vat_by_area"), "export area rates"
        )
        area = resolved.configuration.get("grid_area")
        rates = _as_dict(area_rates.get(area), f"export rates for {area}")
        band = (
            "high"
            if _is_high_load_hour(local, export_rule.get("schedule"))
            else "low"
        )
        export_ex_vat = _number(rates.get(band), f"export {band} rate") / 100
        # Micro-production is outside VAT unless the customer registered for it.
        export_vat_registered = resolved.configuration.get("export_vat_registered")
        if not isinstance(export_vat_registered, bool):
            raise UnsupportedTariffError("export_vat_registered must be boolean")
        export_price = export_ex_vat * (
            1 + vat_rate if export_vat_registered else 1.0
        )

    return {
        "import_price_sek_per_kwh": round(import_price, 5),
        "import_transfer_sek_per_kwh": round(transfer_rate * (1 + vat_rate), 5),
        "import_energy_tax_sek_per_kwh": round(tax_rate * (1 + vat_rate), 5),
        "import_price_sek_per_kwh_ex_vat": round(import_ex_vat, 5),
        "export_price_sek_per_kwh": round(export_price, 5),
        "export_price_sek_per_kwh_ex_vat": round(export_ex_vat, 5),
        "load_period": band,
        "vat_rate": vat_rate,
        "tariff_revision": resolved.revision,
    }


def _display_group(component: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (
        component.get("tariff_revision"),
        component.get("period_start"),
        component.get("period_end"),
    )


def _expected_utc_hours(local_day: date, tariff_timezone: ZoneInfo) -> set[datetime]:
    local_start = datetime.combine(local_day, time.min, tariff_timezone)
    local_end = datetime.combine(
        local_day + timedelta(days=1), time.min, tariff_timezone
    )
    cursor = local_start.astimezone(timezone.utc)
    end = local_end.astimezone(timezone.utc)
    result: set[datetime] = set()
    while cursor < end:
        result.add(cursor)
        cursor += timedelta(hours=1)
    return result


def _normalize_readings(
    readings: list[HourlyGridReading],
    month: date,
    tariff_timezone: ZoneInfo,
) -> tuple[list[HourlyGridReading], date, date, bool]:
    buckets: dict[datetime, list[float]] = {}
    for reading in readings:
        if reading.start.tzinfo is None or reading.start.utcoffset() is None:
            raise MissingTariffInputError(
                "hourly reading timestamps must be timezone-aware"
            )
        if reading.start.minute or reading.start.second or reading.start.microsecond:
            raise MissingTariffInputError(
                "hourly reading timestamps must begin on the hour"
            )
        import_kwh = _number(reading.import_kwh, "import_kwh")
        export_kwh = _number(reading.export_kwh, "export_kwh")
        if import_kwh < 0 or export_kwh < 0:
            raise MissingTariffInputError("hourly grid energy cannot be negative")
        utc_start = reading.start.astimezone(timezone.utc)
        local_start = utc_start.astimezone(tariff_timezone)
        if local_start.year != month.year or local_start.month != month.month:
            continue
        bucket = buckets.setdefault(utc_start, [0.0, 0.0])
        bucket[0] += import_kwh
        bucket[1] += export_kwh

    if not buckets:
        raise MissingTariffInputError(f"no hourly grid data for {month:%Y-%m}")

    raw_days = sorted({value.astimezone(tariff_timezone).date() for value in buckets})
    first_raw, last_raw = raw_days[0], raw_days[-1]
    day_complete: dict[date, bool] = {}
    for local_day in _date_range(first_raw, last_raw):
        actual = {
            timestamp
            for timestamp in buckets
            if timestamp.astimezone(tariff_timezone).date() == local_day
        }
        day_complete[local_day] = actual == _expected_utc_hours(
            local_day, tariff_timezone
        )

    complete_days = [value for value in raw_days if day_complete.get(value)]
    if not complete_days:
        raise MissingTariffInputError(f"no complete hourly grid days for {month:%Y-%m}")
    coverage_start, coverage_end = complete_days[0], complete_days[-1]
    incomplete_internal = [
        value
        for value in _date_range(coverage_start, coverage_end)
        if not day_complete.get(value, False)
    ]
    if incomplete_internal:
        raise MissingTariffInputError(
            f"hourly grid data has a gap on {incomplete_internal[0].isoformat()}"
        )

    normalized = [
        HourlyGridReading(timestamp, values[0], values[1])
        for timestamp, values in sorted(buckets.items())
        if coverage_start
        <= timestamp.astimezone(tariff_timezone).date()
        <= coverage_end
    ]
    is_complete = coverage_start == month and coverage_end == _month_end(month)
    return normalized, coverage_start, coverage_end, is_complete


def _plan(resolved: _ResolvedTariff) -> tuple[dict[str, Any], str]:
    connection_type = resolved.configuration.get("connection_type")
    if connection_type not in {"three_phase", "single_phase", "apartment"}:
        raise UnsupportedTariffError(
            f"unsupported connection type {connection_type!r}"
        )
    plans = _as_dict(resolved.definition.get("plans"), "plans")
    plan = _as_dict(plans.get(connection_type), f"plan {connection_type}")
    selector = plan.get("selector")
    if selector not in {"fuse_a", "apartment_band"}:
        raise UnsupportedTariffError(f"unsupported plan selector {selector!r}")
    selector_value = resolved.configuration.get(selector)
    if isinstance(selector_value, bool) or not isinstance(selector_value, (str, int)):
        raise UnsupportedTariffError(f"tariff selector {selector} is missing")
    return plan, str(selector_value)


def _transfer_rate(plan: dict[str, Any], selector_value: str) -> float:
    transfer = _as_dict(plan.get("transfer"), "transfer rule")
    mode = transfer.get("mode")
    raw_rate = transfer.get("ore_per_kwh_ex_vat")
    if mode == "flat":
        return _number(raw_rate, "transfer rate") / 100
    if mode == "flat_by_selector":
        rates = _as_dict(raw_rate, "selector transfer rates")
        if selector_value not in rates:
            raise UnsupportedTariffError(
                f"transfer rate missing for selector {selector_value}"
            )
        return _number(rates[selector_value], "selector transfer rate") / 100
    raise UnsupportedTariffError(f"unsupported transfer mode {mode!r}")


def _energy_tax_rate(resolved: _ResolvedTariff) -> float:
    """Swedish energy tax in SEK/kWh ex VAT, after any municipal reduction."""
    energy_tax = _as_dict(resolved.definition.get("energy_tax"), "energy tax")
    rate_ore = _number(energy_tax.get("ore_per_kwh_ex_vat"), "energy tax rate")
    reduced = resolved.configuration.get("energy_tax_reduced")
    if reduced is True:
        rate_ore -= _number(
            energy_tax.get("reduction_ore_per_kwh"), "energy tax reduction"
        )
    elif reduced is not False:
        raise UnsupportedTariffError("energy_tax_reduced must be boolean")
    return max(0.0, rate_ore / 100)


def _fixed_rate(plan: dict[str, Any], selector_value: str) -> float:
    rates = _as_dict(plan.get("fixed_monthly_sek_ex_vat"), "fixed rates")
    if selector_value not in rates:
        raise UnsupportedTariffError(
            f"fixed monthly rate missing for selector {selector_value}"
        )
    return _number(rates[selector_value], "fixed monthly rate")


def _is_night_hour(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _demand_quantity(
    rule: dict[str, Any],
    readings: list[HourlyGridReading],
    tariff_timezone: ZoneInfo,
) -> float:
    top_n = int(_number(rule.get("top_n"), "demand top_n"))
    if top_n <= 0:
        raise UnsupportedTariffError("demand top_n must be positive")
    night_start = int(_number(rule.get("night_start_hour"), "night start hour"))
    night_end = int(_number(rule.get("night_end_hour"), "night end hour"))
    night_factor = _number(rule.get("night_factor"), "night factor")
    if not (0 <= night_start <= 23 and 0 <= night_end <= 23 and 0 <= night_factor <= 1):
        raise UnsupportedTariffError("invalid demand night rule")

    adjusted: list[tuple[date, float]] = []
    for reading in readings:
        local_start = reading.start.astimezone(tariff_timezone)
        factor = (
            night_factor
            if _is_night_hour(local_start.hour, night_start, night_end)
            else 1.0
        )
        adjusted.append((local_start.date(), reading.import_kwh * factor))

    if rule.get("distinct_local_days") is True:
        daily_maxima: dict[date, float] = {}
        for local_day, value in adjusted:
            daily_maxima[local_day] = max(daily_maxima.get(local_day, 0.0), value)
        candidates = sorted(daily_maxima.values(), reverse=True)[:top_n]
    elif rule.get("distinct_local_days") is False:
        candidates = sorted((value for _, value in adjusted), reverse=True)[:top_n]
    else:
        raise UnsupportedTariffError("demand distinct_local_days must be boolean")
    return sum(candidates) / len(candidates) if candidates else 0.0


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _excluded_high_load_dates(year: int) -> set[date]:
    easter = _easter_sunday(year)
    return {
        date(year, 1, 1),
        date(year, 1, 6),
        easter - timedelta(days=3),  # Maundy Thursday
        easter - timedelta(days=2),  # Good Friday
        easter + timedelta(days=1),  # Easter Monday
        date(year, 12, 24),
        date(year, 12, 25),
        date(year, 12, 26),
        date(year, 12, 31),
    }


def _is_high_load_hour(local_start: datetime, schedule: Any) -> bool:
    if schedule != EXPORT_SCHEDULE:
        raise UnsupportedTariffError(f"unsupported export schedule {schedule!r}")
    return (
        local_start.month in {11, 12, 1, 2, 3}
        and local_start.weekday() < 5
        and 6 <= local_start.hour < 22
        and local_start.date() not in _excluded_high_load_dates(local_start.year)
    )


def _component(
    component_key: str,
    category: str,
    label: str,
    amount: float,
    quantity: float | None,
    unit: str | None,
    unit_price: float | None,
    period_start: date,
    period_end: date,
    revision: str,
) -> dict[str, Any]:
    return {
        "component_key": component_key,
        "category": category,
        "label": label,
        "amount_sek": _money(amount),
        "quantity": None if quantity is None else _quantity(quantity),
        "unit": unit,
        "unit_price_sek": None if unit_price is None else _unit_price(unit_price),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "tariff_revision": revision,
    }


def _input_hash(
    payload: dict[str, Any],
    readings: list[HourlyGridReading],
    month: date,
    coverage_start: date,
    coverage_end: date,
) -> str:
    canonical = {
        "catalog": payload,
        "billing_month": month.isoformat(),
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "readings": [
            {
                "start": reading.start.astimezone(timezone.utc).isoformat(),
                "import_kwh": round(reading.import_kwh, 6),
                "export_kwh": round(reading.export_kwh, 6),
            }
            for reading in readings
        ],
    }
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def calculate_month(
    payload: dict[str, Any],
    readings: list[HourlyGridReading],
    billing_month: date | str,
) -> dict[str, Any]:
    """Calculate one monthly, component-level grid-cost snapshot."""
    catalog = _Catalog(_as_dict(payload, "tariff payload"))
    month = (
        _parse_date(billing_month, "billing_month")
        if isinstance(billing_month, str)
        else billing_month
    )
    if not isinstance(month, date) or month.day != 1:
        raise UnsupportedTariffError("billing_month must be the first of a month")

    normalized, coverage_start, coverage_end, is_complete = _normalize_readings(
        readings, month, catalog.timezone
    )
    initial_days = _date_range(coverage_start, coverage_end)
    optional_by_day = {
        value: catalog.resolve_optional(value) for value in initial_days
    }
    covered_days = [value for value, resolved in optional_by_day.items() if resolved]
    if not covered_days:
        raise MissingTariffError(f"no tariff covers {month:%Y-%m}")
    coverage_start, coverage_end = covered_days[0], covered_days[-1]
    missing_internal = [
        value
        for value in _date_range(coverage_start, coverage_end)
        if optional_by_day.get(value) is None
    ]
    if missing_internal:
        raise MissingTariffError(
            f"tariff catalogue has a gap on {missing_internal[0].isoformat()}"
        )
    normalized = [
        reading
        for reading in normalized
        if coverage_start
        <= reading.start.astimezone(catalog.timezone).date()
        <= coverage_end
    ]
    is_complete = coverage_start == month and coverage_end == _month_end(month)
    coverage_days = _date_range(coverage_start, coverage_end)
    resolved_by_day: dict[date, _ResolvedTariff] = {}
    for value in coverage_days:
        resolved = optional_by_day[value]
        if resolved is None:
            raise MissingTariffError(f"no tariff covers {value.isoformat()}")
        resolved_by_day[value] = resolved
    grouped_days: dict[tuple[str, str], list[date]] = {}
    resolved_by_key: dict[tuple[str, str], _ResolvedTariff] = {}
    for local_day, resolved in resolved_by_day.items():
        grouped_days.setdefault(resolved.key, []).append(local_day)
        resolved_by_key[resolved.key] = resolved

    readings_by_key: dict[tuple[str, str], list[HourlyGridReading]] = {
        key: [] for key in grouped_days
    }
    for reading in normalized:
        local_day = reading.start.astimezone(catalog.timezone).date()
        readings_by_key[resolved_by_day[local_day].key].append(reading)

    components: list[dict[str, Any]] = []
    peak_demand_values: list[float] = []
    revisions: list[str] = []
    seen_revisions: set[str] = set()
    # The fixed fee follows how much of the month the tariff itself covers, not
    # how much of it has been metered: a month in progress must not show a
    # growing "fixed" fee, while a revision that genuinely starts mid-month
    # still only charges its own share.
    month_days = _date_range(month, _month_end(month))
    days_in_month = len(month_days)
    tariff_days: dict[tuple[str, str], int] = {}
    for value in month_days:
        resolved_day = catalog.resolve_optional(value)
        if resolved_day is not None:
            tariff_days[resolved_day.key] = tariff_days.get(resolved_day.key, 0) + 1

    for key, days in grouped_days.items():
        resolved = resolved_by_key[key]
        if resolved.revision not in seen_revisions:
            seen_revisions.add(resolved.revision)
            revisions.append(resolved.revision)
        plan, selector_value = _plan(resolved)
        period_start, period_end = min(days), max(days)
        group_readings = readings_by_key[key]
        import_kwh = sum(value.import_kwh for value in group_readings)
        export_kwh = sum(value.export_kwh for value in group_readings)

        fixed_rate = _fixed_rate(plan, selector_value)
        fixed_fraction = tariff_days.get(key, len(days)) / days_in_month
        group_components = [
            _component(
                "fixed_grid_fee",
                "fixed_fee",
                "Fixed grid fee",
                fixed_rate * fixed_fraction,
                fixed_fraction,
                "month",
                fixed_rate,
                period_start,
                period_end,
                resolved.revision,
            )
        ]

        transfer_rate = _transfer_rate(plan, selector_value)
        group_components.append(
            _component(
                "grid_energy_transfer",
                "energy_transfer",
                "Grid energy transfer",
                import_kwh * transfer_rate,
                import_kwh,
                "kWh",
                transfer_rate,
                period_start,
                period_end,
                resolved.revision,
            )
        )

        demand_raw = plan.get("demand")
        if demand_raw is not None:
            demand = _as_dict(demand_raw, "demand rule")
            demand_rate = _number(
                demand.get("rate_sek_per_kw_ex_vat"), "demand rate"
            )
            demand_kw = _demand_quantity(demand, group_readings, catalog.timezone)
            peak_demand_values.append(demand_kw)
            group_components.append(
                _component(
                    "peak_demand_fee",
                    "peak_demand",
                    "Peak-demand fee",
                    demand_kw * demand_rate,
                    demand_kw,
                    "kW",
                    demand_rate,
                    period_start,
                    period_end,
                    resolved.revision,
                )
            )

        tax_rate = _energy_tax_rate(resolved)
        group_components.append(
            _component(
                "energy_tax",
                "energy_tax",
                "Swedish energy tax",
                import_kwh * tax_rate,
                import_kwh,
                "kWh",
                tax_rate,
                period_start,
                period_end,
                resolved.revision,
            )
        )

        production_enabled = resolved.configuration.get("production_enabled")
        if not isinstance(production_enabled, bool):
            raise UnsupportedTariffError("production_enabled must be boolean")
        if production_enabled and export_kwh > 0:
            export_rule = _as_dict(
                resolved.definition.get("export_credit"), "export credit"
            )
            area = resolved.configuration.get("grid_area")
            area_rates = _as_dict(
                export_rule.get("ore_per_kwh_ex_vat_by_area"),
                "export area rates",
            )
            rates = _as_dict(area_rates.get(area), f"export rates for {area}")
            export_by_band = {"high": 0.0, "low": 0.0}
            for reading in group_readings:
                local_start = reading.start.astimezone(catalog.timezone)
                band = (
                    "high"
                    if _is_high_load_hour(local_start, export_rule.get("schedule"))
                    else "low"
                )
                export_by_band[band] += reading.export_kwh
            for band in ("high", "low"):
                quantity = export_by_band[band]
                if quantity <= 0:
                    continue
                credit_rate = _number(rates.get(band), f"export {band} rate") / 100
                group_components.append(
                    _component(
                        f"export_credit_{band}",
                        "export_credit",
                        f"Grid export credit ({band} load)",
                        -quantity * credit_rate,
                        quantity,
                        "kWh",
                        -credit_rate,
                        period_start,
                        period_end,
                        resolved.revision,
                    )
                )

        include_vat = resolved.configuration.get("include_vat")
        if not isinstance(include_vat, bool):
            raise UnsupportedTariffError("include_vat must be boolean")
        if include_vat:
            vat_rate = _number(resolved.definition.get("vat_rate"), "VAT rate")
            taxable = sum(
                component["amount_sek"]
                for component in group_components
                if component["category"] != "export_credit"
            )
            export_vat_registered = resolved.configuration.get(
                "export_vat_registered"
            )
            if not isinstance(export_vat_registered, bool):
                raise UnsupportedTariffError("export_vat_registered must be boolean")
            if export_vat_registered:
                taxable += sum(
                    component["amount_sek"]
                    for component in group_components
                    if component["category"] == "export_credit"
                )
            group_components.append(
                _component(
                    "vat",
                    "vat",
                    "VAT",
                    taxable * vat_rate,
                    None,
                    None,
                    vat_rate,
                    period_start,
                    period_end,
                    resolved.revision,
                )
            )

        components.extend(group_components)

    grid_import_kwh = sum(value.import_kwh for value in normalized)
    grid_export_kwh = sum(value.export_kwh for value in normalized)
    total = _money(sum(component["amount_sek"] for component in components))
    return {
        "billing_month": month.isoformat(),
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "is_complete": is_complete,
        "currency": "SEK",
        "calculation_model": CALCULATION_MODEL,
        "calculation_version": CALCULATION_VERSION,
        "tariff_revisions": revisions,
        "input_hash": _input_hash(
            payload, normalized, month, coverage_start, coverage_end
        ),
        "grid_import_kwh": _quantity(grid_import_kwh),
        "grid_export_kwh": _quantity(grid_export_kwh),
        "peak_demand_kw": (
            None if not peak_demand_values else _quantity(max(peak_demand_values))
        ),
        "components": components,
        "total_amount_sek": total,
    }
