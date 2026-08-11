"""Subscription coordinator, daily reading push, and tariff calculation."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import logging
from math import sqrt
from typing import Any
from uuid import uuid4

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    ShsApiClient,
    ShsApiError,
    ShsAuthError,
    ShsSubscriptionInactiveError,
)
from .const import (
    BACKFILL_MAX_DAYS,
    CATEGORIES,
    CONFIGURABLE_CATEGORIES,
    DEFAULT_FORECAST_RESOLUTION_MINUTES,
    DOMAIN,
    ISSUE_MISSING_CUSTOMER_INPUT,
    ISSUE_OPTIMISATION_CONFIGURATION,
    ISSUE_SUBSCRIPTION_INACTIVE,
    MAX_KWH_PER_READING,
    OPT_FORECAST_RESOLUTION_MINUTES,
    OPT_PREFIX_ENTITIES,
    OPT_SUPPLIER_EXPORT_PRICE,
    OPT_SUPPLIER_IMPORT_PRICE,
    OPT_PV_FORECAST_ENTITIES,
    OPT_SUPPLIER_IMPORT_FORECAST_ENTITY,
    OPT_SUPPLIER_EXPORT_FORECAST_ENTITY,
    OPT_ELECTRICITY_PRICE_AREA,
    OPT_PV_FORECAST_LATITUDE,
    OPT_PV_FORECAST_LONGITUDE,
    OPT_BATTERY_SOC_ENTITY,
    OPT_GRID_EXPORT_POWER_ENTITY,
    OPT_BATTERY_CAPACITY_KWH,
    OPT_BATTERY_CHARGE_MAX_W,
    OPT_BATTERY_DISCHARGE_MAX_W,
    OPT_BATTERY_MIN_SOC,
    OPT_BATTERY_MAX_SOC,
    OPT_BATTERY_TARGET_SOC,
    OPT_BATTERY_TARGET_IS_HARD,
    OPT_BATTERY_CHARGE_EFFICIENCY,
    OPT_BATTERY_DISCHARGE_EFFICIENCY,
    OPT_GRID_IMPORT_LIMIT_W,
    OPT_GRID_EXPORT_LIMIT_W,
    OPT_TERMINAL_SOC_MIN,
    OPT_TERMINAL_ENERGY_VALUE,
    OPT_POOL_POWER_W,
    OPT_POOL_ENABLED_ENTITY,
    OPT_POOL_MIN_RUN_SLOTS,
    OPT_POOL_DEADLINE,
    OPT_POOL_DEFERRABLE_CONFIRMED,
    OPT_POOL_BASELINE_START,
    OPT_BOILER_POWER_W,
    OPT_BOILER_MAX_INHIBIT_SLOTS,
    OPT_BOILER_DEFERRABLE_CONFIRMED,
    OPT_EV_CONNECTED_ENTITY,
    OPT_EV_SOC_ENTITY,
    OPT_EV_TARGET_SOC_ENTITY,
    OPT_EV_DEPARTURE_ENTITY,
    OPT_EV_POWER_W,
    OPT_EV_BATTERY_KWH,
    OPT_EV_CHARGE_EFFICIENCY,
    OPT_EV_MIN_RUN_SLOTS,
    OPT_EV_CHARGE_CURRENT_ENTITY,
    OPT_EV_MIN_CURRENT_A,
    OPT_EV_MAX_CURRENT_A,
    OPT_EV_CURRENT_STEP_A,
    OPT_EV_ENERGY_REMAINING_ENTITY,
    OPT_EV_PHASE_COUNT,
    OPT_EV_VOLTAGE,
    OPT_EV_DEFAULT_DEPARTURE,
    OPT_EV_DEFERRABLE_CONFIRMED,
    OPT_EV_ELECTRICAL_CONFIRMED,
    OPT_POOL_PLANNING_ENABLED,
    OPT_BOILER_PLANNING_ENABLED,
    OPT_EV_PLANNING_ENABLED,
    OPT_PLANNING_MODE,
    PLANNING_MODE_DISABLED,
    PLANNING_MODE_LIVE,
    OPTIMISATION_ACTUAL_BACKFILL_HOURS,
    OPTIMISATION_HORIZON_HOURS,
    OPTIMISATION_PROFILE_DAYS,
    STATUS_POLL_INTERVAL_HOURS,
    STORAGE_KEY_TEMPLATE,
    SUPPLIER_BACKFILL_MAX_DAYS,
    STORAGE_VERSION,
)
from .configuration import async_energy_dashboard_inventory, resolved_options
from .optimisation import (
    ACTUAL_FIELD_BY_CATEGORY,
    OptimisationInputError,
    aggregate_category_changes,
    aggregate_device_changes,
    build_base_load_profile,
    build_empirical_device_profile,
    calibration_summary,
    daily_service_window,
    daily_requirement,
    discrete_current_control,
    extract_timestamped_forecast,
    normalized_fraction,
    optimisation_plan_due,
    parse_number,
    quarter_start,
    require_fresh_source,
    state_is_on,
    utc_slots,
    validate_plan_contract,
    validate_service_windows,
)
from .tariff import (
    HourlyGridReading,
    TariffError,
    calculate_month,
    current_demand_charge,
    current_grid_prices,
    grid_operator,
    grid_price_forecast,
    display_components,
    earliest_tariff_date,
    missing_input_labels,
    tariff_component_definitions,
    tariff_timezone,
    validate_tariff_catalog,
)

_LOGGER = logging.getLogger(__name__)


class ShsStatusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll status/catalogue and own recorder aggregation plus nightly upload."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ShsApiClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_status",
            update_interval=timedelta(hours=STATUS_POLL_INTERVAL_HOURS),
        )
        self.entry = entry
        self.client = client
        self.last_push_date: str | None = None
        self.last_push_error: str | None = None
        self.skipped_readings: list[str] = []
        self.supplier_cost_days = 0
        self.tariff_catalog: dict[str, Any] | None = None
        self.tariff_status = "not_configured"
        self.missing_questions: list[str] = []
        self.last_tariff_error: str | None = None
        self.last_calculation_error: str | None = None
        self.latest_calculation: dict[str, Any] | None = None
        self.optimisation_plan: dict[str, Any] | None = None
        self.last_optimisation_push: str | None = None
        self.last_optimisation_error: str | None = None
        self.last_actual_slots_accepted = 0
        self.actuals_accepted_until: str | None = None
        self.optimisation_missing_inputs: list[str] = []
        self.tariff_components: dict[str, dict[str, str]] = {}
        self._push_lock = asyncio.Lock()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY_TEMPLATE.format(entry_id=entry.entry_id)
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            status = await self.client.status()
        except ShsAuthError as err:
            raise UpdateFailed(f"device token rejected: {err}") from err
        except ShsApiError as err:
            raise UpdateFailed(str(err)) from err

        active = bool(status.get("subscription_active"))
        self._sync_subscription_issue(active)
        if not active:
            self.tariff_catalog = None
            self.tariff_components = {}
            self.tariff_status = "subscription_inactive"
            self.last_tariff_error = None
            self.missing_questions = []
            self._sync_missing_input_issue()
            return status

        try:
            catalog = await self.client.tariff()
            validate_tariff_catalog(catalog)
        except ShsSubscriptionInactiveError:
            self.tariff_catalog = None
            self.tariff_components = {}
            self.tariff_status = "subscription_inactive"
            self.last_tariff_error = "subscription_inactive"
            self._sync_subscription_issue(False)
        except (ShsApiError, TariffError) as err:
            self.tariff_catalog = None
            self.tariff_components = {}
            self.tariff_status = "error"
            self.last_tariff_error = str(err)
            _LOGGER.warning("Tariff catalogue refresh failed: %s", err)
        else:
            self.tariff_catalog = catalog
            self.tariff_components = tariff_component_definitions(catalog)
            self.last_tariff_error = None
            if catalog.get("configuration") is None:
                self.tariff_status = "missing_customer_input"
                self.missing_questions = missing_input_labels(
                    catalog, self.hass.config.language
                )
            elif not self._configured_entities().get("grid_import"):
                self.tariff_status = "missing_grid_import"
                self.missing_questions = []
            else:
                self.tariff_status = "configured"
                self.missing_questions = []
            self._sync_missing_input_issue()
        return status

    def _sync_missing_input_issue(self) -> None:
        """Raise or clear the repair issue naming the unanswered questions."""
        if not self.missing_questions:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_MISSING_CUSTOMER_INPUT)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_MISSING_CUSTOMER_INPUT,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_MISSING_CUSTOMER_INPUT,
            translation_placeholders={
                "questions": "\n".join(f"- {q}" for q in self.missing_questions)
            },
        )

    def _sync_subscription_issue(self, active: bool) -> None:
        """Raise or clear the subscription repair issue."""
        if active:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_SUBSCRIPTION_INACTIVE)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_SUBSCRIPTION_INACTIVE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SUBSCRIPTION_INACTIVE,
        )

    def _sync_optimisation_issue(self) -> None:
        """Expose short capability-level gaps only for requested live planning."""
        mode = resolved_options(self.hass, dict(self.entry.options))[OPT_PLANNING_MODE]
        if mode == PLANNING_MODE_DISABLED or not self.optimisation_missing_inputs:
            ir.async_delete_issue(
                self.hass, DOMAIN, ISSUE_OPTIMISATION_CONFIGURATION
            )
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_OPTIMISATION_CONFIGURATION,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_OPTIMISATION_CONFIGURATION,
            translation_placeholders={
                "inputs": "\n".join(
                    f"- {value}" for value in self.optimisation_missing_inputs
                )
            },
        )

    @property
    def latest_display_components(self) -> list[dict[str, Any]]:
        """Invoice-style (VAT-inclusive) view of the latest calculation.

        Only the presentation changes: what gets pushed to the portal stays
        ex-VAT with its own VAT component.
        """
        calculation = self.latest_calculation
        if not calculation:
            return []
        configuration = (self.tariff_catalog or {}).get("configuration") or {}
        return display_components(
            calculation, bool(configuration.get("export_vat_registered"))
        )

    @property
    def grid_prices(self) -> dict[str, Any] | None:
        """Per-kWh grid prices for right now, or None when unavailable."""
        catalog = self.tariff_catalog
        if not catalog:
            return None
        try:
            return current_grid_prices(catalog, dt_util.utcnow())
        except TariffError as err:
            _LOGGER.debug("Grid price unavailable: %s", err)
            return None

    @property
    def grid_price_forecast(self) -> list[dict[str, Any]]:
        """Exact grid prices from this slot to the end of tomorrow."""
        catalog = self.tariff_catalog
        if not catalog:
            return []
        resolution = self.entry.options.get(
            OPT_FORECAST_RESOLUTION_MINUTES, DEFAULT_FORECAST_RESOLUTION_MINUTES
        )
        now = dt_util.now()
        start = now.replace(minute=0, second=0, microsecond=0) + timedelta(
            minutes=resolution * (now.minute // resolution)
        )
        end = dt_util.start_of_local_day() + timedelta(days=2)
        try:
            return grid_price_forecast(catalog, start, end, resolution)
        except TariffError as err:
            _LOGGER.debug("Grid price forecast unavailable: %s", err)
            return []

    @property
    def demand_charge(self) -> dict[str, Any] | None:
        """The effektavgift rule in force right now, if the revision has one."""
        catalog = self.tariff_catalog
        if not catalog:
            return None
        try:
            return current_demand_charge(catalog, dt_util.utcnow())
        except TariffError as err:
            _LOGGER.debug("Demand charge unavailable: %s", err)
            return None

    @property
    def grid_operator(self) -> dict[str, Any] | None:
        """The network operator whose tariff this home is on."""
        catalog = self.tariff_catalog
        if not catalog:
            return None
        try:
            return grid_operator(catalog)
        except TariffError as err:
            _LOGGER.debug("Grid operator unavailable: %s", err)
            return None

    def _configured_entities(self) -> dict[str, list[str]]:
        """Return category → entity ids from the options flow."""
        return {
            category: list(
                self.entry.options.get(f"{OPT_PREFIX_ENTITIES}{category}", [])
            )
            for category in CONFIGURABLE_CATEGORIES
        }

    async def _statistics_changes(
        self,
        entity_ids: list[str],
        start: datetime,
        end: datetime,
        period: str,
    ) -> dict[str, list[tuple[datetime, float]]]:
        """Read non-negative recorder changes with UTC-aware timestamps."""
        if not entity_ids:
            return {}
        end_utc = dt_util.as_utc(end)
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            dt_util.as_utc(start),
            end_utc,
            set(entity_ids),
            period,
            {"energy": "kWh"},
            {"change"},
        )

        result: dict[str, list[tuple[datetime, float]]] = {}
        for entity_id, rows in stats.items():
            values: list[tuple[datetime, float]] = []
            for row in rows:
                start_value = row.get("start")
                change = row.get("change")
                if start_value is None or change is None or change < 0:
                    continue
                if isinstance(start_value, datetime):
                    start_utc = start_value.astimezone(timezone.utc)
                else:
                    start_utc = dt_util.utc_from_timestamp(float(start_value))
                # The recorder returns the bucket that starts exactly on end,
                # so the still-running current day/hour would otherwise be
                # pushed as if it were complete. Keep the window half-open.
                if start_utc >= end_utc:
                    continue
                values.append((start_utc, float(change)))
            result[entity_id] = values
        return result

    async def _daily_changes(
        self, entity_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, dict[str, float]]:
        """Return ``{local_date: {entity_id: change_kwh}}``."""
        stats = await self._statistics_changes(entity_ids, start, end, "day")
        per_day: dict[str, dict[str, float]] = {}
        for entity_id, rows in stats.items():
            for start_utc, change in rows:
                day = dt_util.as_local(start_utc).date().isoformat()
                per_day.setdefault(day, {})[entity_id] = change
        return per_day

    async def _hourly_grid_readings(
        self,
        entities_by_category: dict[str, list[str]],
        start: datetime,
        end: datetime,
    ) -> list[HourlyGridReading]:
        """Sum grid import/export sensor changes into tariff-hour readings."""
        import_entities = entities_by_category.get("grid_import", [])
        export_entities = entities_by_category.get("grid_export", [])
        if not import_entities:
            return []
        all_entities = sorted(set(import_entities) | set(export_entities))
        stats = await self._statistics_changes(all_entities, start, end, "hour")
        import_by_hour: dict[datetime, float] = {}
        export_by_hour: dict[datetime, float] = {}
        for entity_id in import_entities:
            for timestamp, change in stats.get(entity_id, []):
                import_by_hour[timestamp] = import_by_hour.get(timestamp, 0.0) + change
        for entity_id in export_entities:
            for timestamp, change in stats.get(entity_id, []):
                export_by_hour[timestamp] = export_by_hour.get(timestamp, 0.0) + change
        return [
            HourlyGridReading(
                start=timestamp,
                import_kwh=change,
                export_kwh=export_by_hour.get(timestamp, 0.0),
            )
            for timestamp, change in sorted(import_by_hour.items())
        ]

    async def _hourly_price_means(
        self, entity_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, dict[datetime, float]]:
        """Read hourly mean prices, keyed by entity then hour."""
        if not entity_ids:
            return {}
        end_utc = dt_util.as_utc(end)
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            dt_util.as_utc(start),
            end_utc,
            set(entity_ids),
            "hour",
            None,
            {"mean"},
        )
        result: dict[str, dict[datetime, float]] = {}
        for entity_id, rows in stats.items():
            means: dict[datetime, float] = {}
            for row in rows:
                start_value = row.get("start")
                mean = row.get("mean")
                if start_value is None or mean is None:
                    continue
                if isinstance(start_value, datetime):
                    start_utc = start_value.astimezone(timezone.utc)
                else:
                    start_utc = dt_util.utc_from_timestamp(float(start_value))
                if start_utc >= end_utc:
                    continue
                means[start_utc] = float(mean)
            result[entity_id] = means
        return result

    @staticmethod
    def _supplier_sweep_start(
        stored: dict[str, Any], catalog_hash: str | None, today_start: datetime
    ) -> tuple[datetime, bool]:
        """How far back to re-price supplier cost, and whether that is a deep pass.

        Normally the current month and the one before it: that is the window an
        arriving invoice gets reconciled against, and recomputing it every night
        repairs any day a failed push left behind. Once — and again whenever the
        tariff catalogue changes, so both halves of the bill move together — it
        sweeps the whole retained history instead, which is what recovers months
        that predate the feature.
        """
        deep_sweep = (
            not stored.get("supplier_costs_backfilled")
            or stored.get("supplier_costs_catalog_hash") != catalog_hash
        )
        if deep_sweep:
            return today_start - timedelta(days=SUPPLIER_BACKFILL_MAX_DAYS), True
        first_of_month = today_start.replace(day=1)
        return (first_of_month - timedelta(days=1)).replace(day=1), False

    async def _supplier_daily_costs(
        self,
        entities_by_category: dict[str, list[str]],
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """Value each hour's grid energy at that hour's supplier price.

        The grid tariff never covers the energy itself, so this is the missing
        half of what a day actually cost. Pricing hour by hour rather than on a
        daily average is the whole point: consumption correlates with price.
        """
        import_entity = self.entry.options.get(OPT_SUPPLIER_IMPORT_PRICE) or None
        export_entity = self.entry.options.get(OPT_SUPPLIER_EXPORT_PRICE) or None
        if not import_entity and not export_entity:
            return []
        readings = await self._hourly_grid_readings(entities_by_category, start, end)
        if not readings:
            return []
        prices = await self._hourly_price_means(
            sorted({entity for entity in (import_entity, export_entity) if entity}),
            start,
            end,
        )
        import_prices = prices.get(import_entity or "", {})
        export_prices = prices.get(export_entity or "", {})

        per_day: dict[str, dict[str, float]] = {}
        for reading in readings:
            import_price = import_prices.get(reading.start)
            export_price = export_prices.get(reading.start)
            if import_price is None and export_price is None:
                # No price for this hour: leave the day short rather than
                # valuing energy at a rate that was never quoted.
                continue
            day = dt_util.as_local(reading.start).date().isoformat()
            totals = per_day.setdefault(
                day,
                {
                    "import_kwh": 0.0,
                    "import_cost_sek": 0.0,
                    "export_kwh": 0.0,
                    "export_credit_sek": 0.0,
                    "priced_hours": 0.0,
                },
            )
            if import_price is not None:
                totals["import_kwh"] += reading.import_kwh
                totals["import_cost_sek"] += reading.import_kwh * import_price
            if export_price is not None:
                totals["export_kwh"] += reading.export_kwh
                totals["export_credit_sek"] += reading.export_kwh * export_price
            totals["priced_hours"] += 1

        return [
            {
                "date": day,
                "import_kwh": round(totals["import_kwh"], 3),
                "import_cost_sek": round(totals["import_cost_sek"], 2),
                "export_kwh": round(totals["export_kwh"], 3),
                "export_credit_sek": round(totals["export_credit_sek"], 2),
                "priced_hours": int(totals["priced_hours"]),
            }
            for day, totals in sorted(per_day.items())
        ]

    @staticmethod
    def _catalog_hash(catalog: dict[str, Any]) -> str:
        encoded = json.dumps(
            catalog, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    async def _tariff_calculations(
        self,
        entities_by_category: dict[str, list[str]],
        days_back: int,
        stored: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """Calculate affected months, expanding to history after catalogue changes."""
        catalog = self.tariff_catalog
        if catalog is None:
            return [], None, False
        catalog_hash = self._catalog_hash(catalog)
        if catalog.get("configuration") is None:
            self.tariff_status = "missing_customer_input"
            self.last_calculation_error = "; ".join(
                missing_input_labels(catalog, self.hass.config.language)
            )
            return [], catalog_hash, True
        if not entities_by_category.get("grid_import"):
            self.tariff_status = "missing_grid_import"
            self.last_calculation_error = "grid_import_sensor_not_configured"
            return [], catalog_hash, True

        try:
            tariff_tz = tariff_timezone(catalog)
            yesterday = datetime.now(tariff_tz).date() - timedelta(days=1)
            first_published = earliest_tariff_date(catalog)
            if first_published is None:
                self.tariff_status = "not_configured"
                self.last_calculation_error = "no_published_tariff_versions"
                return [], catalog_hash, True
        except (KeyError, TypeError, ValueError, TariffError) as err:
            self.tariff_status = "calculation_error"
            self.last_calculation_error = str(err)
            return [], catalog_hash, True
        if first_published > yesterday:
            self.last_calculation_error = None
            self.tariff_status = "configured"
            return [], catalog_hash, True

        catalog_changed = stored.get("tariff_catalog_hash") != catalog_hash
        if catalog_changed:
            first_affected = first_published
        else:
            first_affected = yesterday - timedelta(days=max(1, days_back) - 1)
        query_start_date = date(first_affected.year, first_affected.month, 1)
        query_start = datetime.combine(query_start_date, time.min, tariff_tz)
        query_end = datetime.combine(yesterday + timedelta(days=1), time.min, tariff_tz)
        readings = await self._hourly_grid_readings(
            entities_by_category, query_start, query_end
        )
        if not readings:
            self.tariff_status = "calculation_error"
            self.last_calculation_error = "hourly_grid_statistics_not_available"
            return [], catalog_hash, True

        months: list[date] = []
        cursor = query_start_date
        last_month = date(yesterday.year, yesterday.month, 1)
        while cursor <= last_month:
            months.append(cursor)
            cursor = date(
                cursor.year + (cursor.month == 12),
                (cursor.month % 12) + 1,
                1,
            )

        # The catalogue reaches further back than any home's recorder history,
        # so months from before this home started logging are simply out of
        # range. Skipping them keeps the status clean instead of reporting a
        # calculation error for every month the customer was never metered.
        months_with_data = {
            (local.year, local.month)
            for local in (
                reading.start.astimezone(tariff_tz) for reading in readings
            )
        }

        calculations: list[dict[str, Any]] = []
        errors: list[str] = []
        for month in months:
            if (month.year, month.month) not in months_with_data:
                continue
            try:
                calculations.append(calculate_month(catalog, readings, month))
            except TariffError as err:
                errors.append(f"{month:%Y-%m}: {err}")
        self.last_calculation_error = "; ".join(errors[-3:]) or None
        self.tariff_status = "calculation_error" if errors else "configured"
        if calculations:
            self.latest_calculation = max(
                calculations, key=lambda value: value["billing_month"]
            )
        return calculations, catalog_hash, True

    async def async_push_days(self, days_back: int) -> None:
        """Compute and upload daily readings plus affected monthly grid costs."""
        async with self._push_lock:
            await self._async_push_days_unlocked(days_back)

    async def _async_push_days_unlocked(self, days_back: int) -> None:
        """Run one serialized reading/calculation upload."""
        try:
            await self._collect_and_push(days_back)
        finally:
            # Every exit path has to reach the sensors, refusals and crashes
            # included. Returning without this leaves them showing whatever
            # they were created with — typically "unknown" after a restart —
            # and hides the very error that explains why.
            self.async_update_listeners()

    async def _collect_and_push(self, days_back: int) -> None:
        """Aggregate the recorder, calculate, and upload in one pass."""
        stored = await self._store.async_load() or {}
        entities_by_category = self._configured_entities()
        daily_entities_by_category = {
            category: entities_by_category[category] for category in CATEGORIES
        }
        all_entities = sorted(
            {
                entity
                for values in daily_entities_by_category.values()
                for entity in values
            }
        )

        today_start = dt_util.start_of_local_day()
        start = today_start - timedelta(days=days_back)
        per_day = await self._daily_changes(all_entities, start, today_start)
        readings: list[dict[str, Any]] = []
        skipped: list[str] = []
        for day, entity_changes in sorted(per_day.items()):
            for category, entity_ids in daily_entities_by_category.items():
                if not entity_ids or any(
                    entity not in entity_changes for entity in entity_ids
                ):
                    # A category made from several meters is only meaningful
                    # when every mapped meter covers the day. Missing is
                    # unknown, never a smaller-looking partial total.
                    continue
                values = [entity_changes[entity] for entity in entity_ids]
                kwh = round(sum(values), 3)
                if kwh > MAX_KWH_PER_READING:
                    skipped.append(f"{category} {day} ({kwh} kWh)")
                    continue
                readings.append({"date": day, "category": category, "kwh": kwh})
        self.skipped_readings = skipped
        if skipped:
            _LOGGER.warning(
                "Skipped implausible daily readings, most likely a reset "
                "counter behind one of the mapped sensors: %s",
                "; ".join(skipped),
            )

        calculations, catalog_hash, tariff_attempted = await self._tariff_calculations(
            daily_entities_by_category, days_back, stored
        )
        supplier_sweep_start, deep_sweep = self._supplier_sweep_start(
            stored, catalog_hash, today_start
        )
        supplier_costs = await self._supplier_daily_costs(
            daily_entities_by_category, supplier_sweep_start, today_start
        )
        self.supplier_cost_days = len(supplier_costs)
        if not readings and not calculations and not supplier_costs:
            if tariff_attempted and catalog_hash:
                stored["tariff_catalog_hash"] = catalog_hash
                await self._store.async_save(stored)
            _LOGGER.debug("No daily readings or tariff calculations to push")
            return

        try:
            result = await self.client.push_readings(
                readings, calculations, supplier_costs
            )
        except ShsSubscriptionInactiveError:
            self.last_push_error = "subscription_inactive"
            self._sync_subscription_issue(False)
            _LOGGER.warning("Push refused: subscription inactive")
            return
        except ShsApiError as err:
            self.last_push_error = str(err)
            _LOGGER.warning("Push failed: %s", err)
            return

        self.last_push_error = None
        if per_day:
            self.last_push_date = max(per_day)
            stored["last_push_date"] = self.last_push_date
        if tariff_attempted and catalog_hash:
            stored["tariff_catalog_hash"] = catalog_hash
        if deep_sweep:
            # Only once the portal has accepted it, so a failed push repeats the
            # sweep rather than marking history done that never landed.
            stored["supplier_costs_backfilled"] = True
            stored["supplier_costs_catalog_hash"] = catalog_hash
        if self.latest_calculation:
            stored["latest_calculation"] = self.latest_calculation
        await self._store.async_save(stored)
        _LOGGER.debug(
            "Pushed %s readings, %s calculations and %s supplier-cost days "
            "(accepted=%s, calculations_accepted=%s, supplier_costs_accepted=%s)",
            len(readings),
            len(calculations),
            len(supplier_costs),
            result.get("accepted"),
            result.get("calculations_accepted"),
            result.get("supplier_costs_accepted"),
        )

    def _optimisation_options(self) -> dict[str, Any]:
        """Resolve defaults and require only capabilities enabled for this home."""
        options = resolved_options(self.hass, dict(self.entry.options))
        configuration = (self.tariff_catalog or {}).get("configuration") or {}
        fuse_a = configuration.get("fuse_a")
        if fuse_a and not options.get(OPT_GRID_IMPORT_LIMIT_W):
            phases = 1 if configuration.get("connection_type") == "single_phase" else 3
            options[OPT_GRID_IMPORT_LIMIT_W] = round(
                float(fuse_a) * (230 if phases == 1 else sqrt(3) * 400), 1
            )
        if not options.get(OPT_GRID_EXPORT_LIMIT_W) and options.get(
            OPT_GRID_IMPORT_LIMIT_W
        ):
            options[OPT_GRID_EXPORT_LIMIT_W] = options[OPT_GRID_IMPORT_LIMIT_W]
        required = [
            OPT_FORECAST_RESOLUTION_MINUTES,
            OPT_ELECTRICITY_PRICE_AREA,
            OPT_GRID_IMPORT_LIMIT_W,
            OPT_GRID_EXPORT_LIMIT_W,
        ]
        if not options.get(
            OPT_SUPPLIER_IMPORT_FORECAST_ENTITY
        ) and not self.hass.services.has_service("tibber", "get_prices"):
            required.append(OPT_SUPPLIER_IMPORT_FORECAST_ENTITY)
        if not options.get(
            OPT_SUPPLIER_EXPORT_FORECAST_ENTITY
        ) and not self.hass.services.has_service(
            "nordpool", "get_prices_for_date"
        ):
            required.append(OPT_SUPPLIER_EXPORT_FORECAST_ENTITY)
        entities = self._configured_entities()
        can_derive_total = bool(entities.get("grid_import"))
        if not entities.get("total_consumption") and not can_derive_total:
            required.append("a whole-home meter or Energy Dashboard grid meter")
        if options.get(OPT_PV_FORECAST_ENTITIES):
            required.extend([OPT_PV_FORECAST_LATITUDE, OPT_PV_FORECAST_LONGITUDE])
        if options.get(OPT_BATTERY_SOC_ENTITY):
            required.extend([
                OPT_BATTERY_CAPACITY_KWH,
                OPT_BATTERY_CHARGE_MAX_W,
                OPT_BATTERY_DISCHARGE_MAX_W,
                OPT_BATTERY_MIN_SOC,
                OPT_BATTERY_MAX_SOC,
                OPT_BATTERY_TARGET_SOC,
                OPT_BATTERY_TARGET_IS_HARD,
                OPT_BATTERY_CHARGE_EFFICIENCY,
                OPT_BATTERY_DISCHARGE_EFFICIENCY,
                OPT_TERMINAL_SOC_MIN,
                OPT_TERMINAL_ENERGY_VALUE,
            ])
        if options.get(OPT_POOL_PLANNING_ENABLED):
            required.extend(
                [f"{OPT_PREFIX_ENTITIES}pool_heating",
                 OPT_POOL_DEFERRABLE_CONFIRMED,
                 OPT_POOL_ENABLED_ENTITY, OPT_POOL_POWER_W,
                 OPT_POOL_MIN_RUN_SLOTS, OPT_POOL_DEADLINE,
                 OPT_POOL_BASELINE_START]
            )
        if options.get(OPT_BOILER_PLANNING_ENABLED):
            required.extend(
                [f"{OPT_PREFIX_ENTITIES}hot_water",
                 OPT_BOILER_DEFERRABLE_CONFIRMED,
                 OPT_BOILER_POWER_W, OPT_BOILER_MAX_INHIBIT_SLOTS]
            )
        if options.get(OPT_EV_PLANNING_ENABLED):
            required.extend(
                [f"{OPT_PREFIX_ENTITIES}ev_charging",
                 OPT_EV_DEFERRABLE_CONFIRMED, OPT_EV_ELECTRICAL_CONFIRMED,
                 OPT_EV_CONNECTED_ENTITY, OPT_EV_SOC_ENTITY, OPT_EV_TARGET_SOC_ENTITY,
                 OPT_EV_BATTERY_KWH, OPT_EV_CHARGE_EFFICIENCY,
                 OPT_EV_MIN_RUN_SLOTS, OPT_EV_DEFAULT_DEPARTURE]
            )
            if options.get(OPT_EV_CHARGE_CURRENT_ENTITY):
                required.extend((
                    OPT_EV_MIN_CURRENT_A,
                    OPT_EV_MAX_CURRENT_A,
                    OPT_EV_CURRENT_STEP_A,
                ))
            elif not options.get(OPT_EV_POWER_W):
                required.append("EV charging power or configured current entity")
        missing = sorted(
            key for key in required
            if options.get(key) is None or options.get(key) == "" or options.get(key) == []
        )
        for enabled_key, confirmation_keys in (
            (
                OPT_POOL_PLANNING_ENABLED,
                (OPT_POOL_DEFERRABLE_CONFIRMED,),
            ),
            (
                OPT_BOILER_PLANNING_ENABLED,
                (OPT_BOILER_DEFERRABLE_CONFIRMED,),
            ),
            (
                OPT_EV_PLANNING_ENABLED,
                (
                    OPT_EV_DEFERRABLE_CONFIRMED,
                    OPT_EV_ELECTRICAL_CONFIRMED,
                ),
            ),
        ):
            if not options.get(enabled_key):
                continue
            missing.extend(
                key
                for key in confirmation_keys
                if options.get(key) is not True and key not in missing
            )
        if options.get(OPT_SUPPLIER_IMPORT_FORECAST_ENTITY) == options.get(
            OPT_SUPPLIER_EXPORT_FORECAST_ENTITY
        ) and options.get(OPT_SUPPLIER_IMPORT_FORECAST_ENTITY):
            missing.append(
                "supplier import and export forecasts must be different entities"
            )
        if options.get(OPT_FORECAST_RESOLUTION_MINUTES) not in (None, 15):
            missing.append("forecast resolution must be 15 minutes")
        groups: list[str] = []
        if any("forecast" in value or value == OPT_ELECTRICITY_PRICE_AREA for value in missing):
            groups.append("Prices: separate import and export forecasts plus the Swedish price area")
        if any("battery" in value or "terminal" in value for value in missing):
            groups.append("Battery: state of charge and equipment ratings")
        if any("pool" in value for value in missing):
            groups.append("Pool: aggregate meter, enabled state and rated power")
        if any("boiler" in value or "hot_water" in value for value in missing):
            groups.append("Water heater: aggregate meter and rated power")
        if any("ev" in value.lower() for value in missing):
            groups.append("EV: connection, SOC, target, capacity and charging power")
        if any("whole-home" in value or "grid meter" in value for value in missing):
            groups.append("Consumption: configure the Home Assistant Energy Dashboard grid meter")
        if any("grid_" in value and "forecast" not in value for value in missing):
            groups.append("Electrical limits: grid import and export limits")
        self.optimisation_missing_inputs = groups or missing
        self._sync_optimisation_issue()
        if missing:
            raise OptimisationInputError(
                "energy planning setup incomplete: " + "; ".join(self.optimisation_missing_inputs)
            )
        return options

    def _entity_payload(self, entity_id: str) -> dict[str, Any]:
        state = self.hass.states.get(entity_id)
        if state is None:
            raise OptimisationInputError(f"{entity_id} does not exist")
        if state.state in ("unknown", "unavailable"):
            raise OptimisationInputError(f"{entity_id} is {state.state}")
        return {
            "entity_id": entity_id,
            "state": state.state,
            "attributes": dict(state.attributes),
            "last_updated": state.last_updated,
            "last_reported": state.last_reported,
        }

    async def _tibber_import_forecast(
        self, start: datetime, end: datetime, captured: datetime
    ) -> tuple[dict[datetime, float], list[str], datetime]:
        """Read native quarter-hour supplier prices from Tibber's action."""
        if not self.hass.services.has_service("tibber", "get_prices"):
            raise OptimisationInputError(
                "import price forecast needs a canonical entity or tibber.get_prices"
            )
        try:
            response = await self.hass.services.async_call(
                "tibber",
                "get_prices",
                {"start": start.isoformat(), "end": end.isoformat()},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            raise OptimisationInputError(
                "Tibber did not return an import price forecast"
            ) from err
        groups = (response or {}).get("prices")
        if not isinstance(groups, dict) or len(groups) != 1:
            raise OptimisationInputError(
                "tibber.get_prices must return exactly one configured home"
            )
        records = next(iter(groups.values()))
        return extract_timestamped_forecast(
            [{
                "entity_id": "service.tibber.get_prices",
                "last_updated": captured,
                "attributes": {"forecast": records},
            }],
            attribute_names=("forecast",),
            value_keys=("price", "total", "value"),
        )

    async def _nordpool_export_forecast(
        self,
        horizon: list[datetime],
        price_area: str,
        captured: datetime,
    ) -> tuple[dict[datetime, float], list[str], datetime]:
        """Read official Nord Pool spot prices and convert SEK/MWh to SEK/kWh."""
        if not self.hass.services.has_service("nordpool", "get_prices_for_date"):
            raise OptimisationInputError(
                "export price forecast needs a canonical entity or "
                "nordpool.get_prices_for_date"
            )
        entries = self.hass.config_entries.async_entries("nordpool")
        if len(entries) != 1:
            raise OptimisationInputError(
                "Nord Pool export forecast needs exactly one configured entry"
            )
        records: list[Any] = []
        days = sorted({
            slot.astimezone(dt_util.DEFAULT_TIME_ZONE).date() for slot in horizon
        })
        for day in days:
            try:
                response = await self.hass.services.async_call(
                    "nordpool",
                    "get_prices_for_date",
                    {
                        "config_entry": entries[0].entry_id,
                        "date": day.isoformat(),
                        "areas": [price_area],
                        "currency": "SEK",
                    },
                    blocking=True,
                    return_response=True,
                )
            except HomeAssistantError as err:
                if not records:
                    raise OptimisationInputError(
                        "Nord Pool did not publish the first required price day"
                    ) from err
                break
            records.append(response)
        values, _used, issued = extract_timestamped_forecast(
            [{
                "entity_id": "service.nordpool.get_prices_for_date",
                "last_updated": captured,
                "attributes": {"forecast": records},
            }],
            attribute_names=("forecast",),
            value_keys=("price", "value"),
        )
        return (
            {timestamp: value / 1_000 for timestamp, value in values.items()},
            ["service.nordpool.get_prices_for_date"],
            issued,
        )

    async def _category_quarter_changes(
        self,
        entities_by_category: dict[str, list[str]],
        start: datetime,
        end: datetime,
    ) -> dict[str, list[tuple[datetime, float]]]:
        all_entities = sorted(
            {entity for values in entities_by_category.values() for entity in values}
        )
        statistics = await self._statistics_changes(
            all_entities, start, end, "5minute"
        )
        result: dict[str, list[tuple[datetime, float]]] = {}
        for category, entity_ids in entities_by_category.items():
            per_entity = [
                dict(statistics.get(entity_id, [])) for entity_id in entity_ids
            ]
            if not per_entity or any(not values for values in per_entity):
                result[category] = []
                continue
            complete_starts = set(per_entity[0]).intersection(
                *(set(values) for values in per_entity[1:])
            )
            result[category] = [
                (timestamp, sum(values[timestamp] for values in per_entity))
                for timestamp in sorted(complete_starts)
            ]
        return result

    async def _actual_quarters(
        self,
        entities_by_category: dict[str, list[str]],
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        changes = await self._category_quarter_changes(
            entities_by_category, start, end
        )
        rows = aggregate_category_changes(changes)
        configured = {
            category for category, values in entities_by_category.items() if values
        }
        balance = {
            "grid_import": ("grid_import_kwh", 1),
            "solar_production": ("solar_production_kwh", 1),
            "battery_discharge": ("battery_discharge_kwh", 1),
            "grid_export": ("grid_export_kwh", -1),
            "battery_charge": ("battery_charge_kwh", -1),
        }
        for row in rows:
            if row.get("total_load_kwh") is not None:
                continue
            required = [
                field for category, (field, _sign) in balance.items()
                if category in configured
            ]
            if "grid_import_kwh" not in required or any(
                field not in row for field in required
            ):
                continue
            total = sum(
                float(row[field]) * sign
                for category, (field, sign) in balance.items()
                if category in configured
            )
            row["total_load_kwh"] = round(max(0.0, total), 6)
        return rows

    async def _device_actual_quarters(
        self,
        devices: list[dict[str, Any]],
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """Return complete per-device Energy Dashboard quarters."""
        statistic_by_key = {
            str(device["key"]): str(device["statistic_id"])
            for device in devices
        }
        statistics = await self._statistics_changes(
            sorted(set(statistic_by_key.values())), start, end, "5minute"
        )
        return aggregate_device_changes({
            key: statistics.get(statistic_id, [])
            for key, statistic_id in statistic_by_key.items()
        })

    async def _daily_category_totals(
        self,
        entities_by_category: dict[str, list[str]],
        start: datetime,
        end: datetime,
    ) -> dict[str, dict[str, float]]:
        all_entities = sorted(
            {entity for values in entities_by_category.values() for entity in values}
        )
        per_day = await self._daily_changes(all_entities, start, end)
        result: dict[str, dict[str, float]] = {}
        for day, entity_values in per_day.items():
            for category, entity_ids in entities_by_category.items():
                values = [entity_values[value] for value in entity_ids if value in entity_values]
                if values and len(values) == len(entity_ids):
                    result.setdefault(category, {})[day] = sum(values)
        return result

    @staticmethod
    def _time_option(value: Any, label: str) -> time:
        try:
            parsed = time.fromisoformat(str(value))
        except ValueError as err:
            raise OptimisationInputError(f"{label} must be HH:MM") from err
        return parsed.replace(second=0, microsecond=0)

    def _build_services(
        self,
        options: dict[str, Any],
        entities_by_category: dict[str, list[str]],
        daily_totals: dict[str, dict[str, float]],
        actuals: list[dict[str, Any]],
        horizon: list[datetime],
        device_models: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        local_tz = dt_util.DEFAULT_TIME_ZONE
        first = horizon[0]
        end = horizon[-1] + timedelta(minutes=15)
        today = dt_util.now().date()
        done_today: dict[str, float] = {}
        for category, field in ACTUAL_FIELD_BY_CATEGORY.items():
            done_today[category] = sum(
                float(row.get(field) or 0)
                for row in actuals
                if datetime.fromisoformat(row["start"]).astimezone(local_tz).date() == today
            )

        services: list[dict[str, Any]] = []
        samples: dict[str, int] = {}
        for category, device, enabled_key, power_key, run_key, deadline_key, baseline_key, priority in (
            ("pool_heating", "pool", OPT_POOL_PLANNING_ENABLED,
             OPT_POOL_POWER_W, OPT_POOL_MIN_RUN_SLOTS,
             OPT_POOL_DEADLINE, OPT_POOL_BASELINE_START, 2),
        ):
            if not options.get(enabled_key) or not entities_by_category.get(category):
                continue
            if category == "pool_heating" and not state_is_on(
                self._entity_payload(options[OPT_POOL_ENABLED_ENTITY])["state"]
            ):
                continue
            requirement, count = daily_requirement(
                daily_totals.get(category, {}), category
            )
            samples[category] = count
            minimum_run = parse_number(options[run_key], run_key)
            if not minimum_run.is_integer() or minimum_run < 1:
                raise OptimisationInputError(
                    f"{run_key} must be a positive whole number"
                )
            local_days = sorted({slot.astimezone(local_tz).date() for slot in horizon})
            for day in local_days:
                window = daily_service_window(
                    horizon,
                    day,
                    str(local_tz),
                    options[deadline_key],
                    options[baseline_key],
                    label=device,
                )
                if window is None:
                    continue
                earliest, deadline, baseline = window
                required = requirement
                if day == today:
                    required = max(0.0, requirement - done_today.get(category, 0.0))
                if required <= 0:
                    continue
                services.append({
                    "id": f"{device}:{day.isoformat()}",
                    "device": device,
                    "earliest_start": earliest.isoformat(),
                    "deadline": deadline.isoformat(),
                    "required_kwh": round(required, 3),
                    "control": {
                        "type": "fixed_power",
                        "power_w": parse_number(options[power_key], power_key),
                    },
                    "min_run_slots": int(minimum_run),
                    "priority": priority,
                    "baseline_preferred_start": baseline.isoformat(),
                })

        if options.get(OPT_BOILER_PLANNING_ENABLED):
            boiler_models = [
                model for model in device_models
                if model["category"] == "hot_water"
            ]
            if not boiler_models:
                raise OptimisationInputError(
                    "water-heater planning needs a complete empirical device profile"
                )
            expected_w = [
                round(sum(model["forecast_w_by_slot"][index]
                          for model in boiler_models), 2)
                for index in range(len(horizon))
            ]
            rated_power_w = parse_number(
                options[OPT_BOILER_POWER_W], OPT_BOILER_POWER_W
            )
            if max(expected_w, default=0) > rated_power_w + 1e-6:
                raise OptimisationInputError(
                    "empirical water-heater expected power exceeds its reviewed rating"
                )
            maximum_inhibit = parse_number(
                options[OPT_BOILER_MAX_INHIBIT_SLOTS],
                OPT_BOILER_MAX_INHIBIT_SLOTS,
            )
            if not maximum_inhibit.is_integer() or maximum_inhibit < 1:
                raise OptimisationInputError(
                    f"{OPT_BOILER_MAX_INHIBIT_SLOTS} must be a positive whole number"
                )
            samples["hot_water"] = sum(
                int(model["profile_sample_count"]) for model in boiler_models
            )
            by_day: dict[date, list[int]] = {}
            for index, slot in enumerate(horizon):
                by_day.setdefault(slot.astimezone(local_tz).date(), []).append(index)
            for day, indices in sorted(by_day.items()):
                required_kwh = sum(expected_w[index] for index in indices) / 4_000
                services.append({
                    "id": f"boiler:{day.isoformat()}",
                    "device": "boiler",
                    "earliest_start": horizon[indices[0]].isoformat(),
                    "deadline": (
                        horizon[indices[-1]] + timedelta(minutes=15)
                    ).isoformat(),
                    "required_kwh": round(required_kwh, 5),
                    "control": {
                        "type": "duty_cycle",
                        "rated_power_w": rated_power_w,
                        "expected_power_w_by_slot": expected_w,
                        "max_consecutive_inhibit_slots": int(maximum_inhibit),
                    },
                    "priority": 1,
                })

        connected_id = (
            options.get(OPT_EV_CONNECTED_ENTITY)
            if options.get(OPT_EV_PLANNING_ENABLED)
            else None
        )
        if connected_id and state_is_on(self._entity_payload(connected_id)["state"]):
            soc = normalized_fraction(
                self._entity_payload(options[OPT_EV_SOC_ENTITY])["state"],
                OPT_EV_SOC_ENTITY,
            )
            target = normalized_fraction(
                self._entity_payload(options[OPT_EV_TARGET_SOC_ENTITY])["state"],
                OPT_EV_TARGET_SOC_ENTITY,
            )
            departure_entity = options.get(OPT_EV_DEPARTURE_ENTITY)
            if departure_entity:
                departure_raw = self._entity_payload(departure_entity)["state"]
                try:
                    departure = datetime.fromisoformat(
                        str(departure_raw).replace("Z", "+00:00")
                    )
                except ValueError as err:
                    raise OptimisationInputError(
                        f"{OPT_EV_DEPARTURE_ENTITY} must contain an ISO timestamp"
                    ) from err
                if departure.tzinfo is None:
                    raise OptimisationInputError(
                        f"{OPT_EV_DEPARTURE_ENTITY} timestamp must include a timezone"
                    )
                departure = departure.astimezone(timezone.utc)
            else:
                departure_time = self._time_option(
                    options[OPT_EV_DEFAULT_DEPARTURE], OPT_EV_DEFAULT_DEPARTURE
                )
                local_first = first.astimezone(local_tz)
                departure = datetime.combine(
                    local_first.date(), departure_time, local_tz
                ).astimezone(timezone.utc)
                if departure <= first:
                    departure += timedelta(days=1)
            if departure <= first or departure > end:
                raise OptimisationInputError(
                    "connected EV departure must fall inside the 72-hour horizon"
                )
            efficiency = parse_number(
                options[OPT_EV_CHARGE_EFFICIENCY], OPT_EV_CHARGE_EFFICIENCY
            )
            capacity = parse_number(options[OPT_EV_BATTERY_KWH], OPT_EV_BATTERY_KWH)
            remaining_entity = options.get(OPT_EV_ENERGY_REMAINING_ENTITY)
            if remaining_entity and soc > 0:
                remaining = parse_number(
                    self._entity_payload(remaining_entity)["state"], remaining_entity
                )
                capacity = remaining / soc
            required = max(0.0, target - soc) * capacity / efficiency
            if required > 0:
                minimum_run = parse_number(
                    options[OPT_EV_MIN_RUN_SLOTS], OPT_EV_MIN_RUN_SLOTS
                )
                if not minimum_run.is_integer() or minimum_run < 1:
                    raise OptimisationInputError(
                        f"{OPT_EV_MIN_RUN_SLOTS} must be a positive whole number"
                    )
                current_entity = options.get(OPT_EV_CHARGE_CURRENT_ENTITY)
                if current_entity:
                    current_payload = self._entity_payload(current_entity)
                    if current_payload["attributes"].get("unit_of_measurement") != "A":
                        raise OptimisationInputError(
                            f"{current_entity} must declare unit A"
                        )
                    configured_min = parse_number(
                        options[OPT_EV_MIN_CURRENT_A], OPT_EV_MIN_CURRENT_A
                    )
                    configured_max = parse_number(
                        options[OPT_EV_MAX_CURRENT_A], OPT_EV_MAX_CURRENT_A
                    )
                    configured_step = parse_number(
                        options[OPT_EV_CURRENT_STEP_A], OPT_EV_CURRENT_STEP_A
                    )
                    entity_min = parse_number(
                        current_payload["attributes"].get("min"),
                        f"{current_entity} min",
                    )
                    entity_max = parse_number(
                        current_payload["attributes"].get("max"),
                        f"{current_entity} max",
                    )
                    entity_step = parse_number(
                        current_payload["attributes"].get("step"),
                        f"{current_entity} step",
                    )
                    if configured_min < entity_min or configured_max > entity_max:
                        raise OptimisationInputError(
                            "configured EV current range is outside the entity bounds"
                        )
                    if entity_step <= 0:
                        raise OptimisationInputError(
                            f"{current_entity} step must be positive"
                        )
                    step_ratio = configured_step / entity_step
                    if abs(step_ratio - round(step_ratio)) > 1e-6:
                        raise OptimisationInputError(
                            "configured EV current step is not supported by the entity"
                        )
                    control = discrete_current_control(
                        configured_min,
                        configured_max,
                        configured_step,
                        options[OPT_EV_PHASE_COUNT],
                        options[OPT_EV_VOLTAGE],
                        label=current_entity,
                    )
                else:
                    control = {
                        "type": "fixed_power",
                        "power_w": round(parse_number(
                            options.get(OPT_EV_POWER_W), "EV charging power"
                        ), 1),
                    }
                services.append({
                    "id": f"ev:{departure.isoformat()}",
                    "device": "ev",
                    "earliest_start": first.isoformat(),
                    "deadline": departure.isoformat(),
                    "required_kwh": round(required, 3),
                    "control": control,
                    "min_run_slots": int(minimum_run),
                    "priority": 3,
                    "baseline_preferred_start": first.isoformat(),
                })
        validate_service_windows(services, horizon)
        return services, samples

    @staticmethod
    def _observe_calibration(
        stored: dict[str, Any], actuals: list[dict[str, Any]]
    ) -> list[dict[str, float | int]]:
        ledger = dict(stored.get("pv_forecast_ledger") or {})
        observations = list(stored.get("pv_calibration_observations") or [])
        for actual in actuals:
            actual_kwh = actual.get("solar_production_kwh")
            if actual_kwh is None:
                continue
            key = datetime.fromisoformat(actual["start"]).astimezone(timezone.utc).isoformat()
            predictions = ledger.pop(key, {})
            for lead, predicted in predictions.items():
                observations.append({
                    "lead_day": int(lead),
                    "predicted_kwh": float(predicted),
                    "actual_kwh": float(actual_kwh),
                })
        stored["pv_forecast_ledger"] = ledger
        stored["pv_calibration_observations"] = observations[-1500:]
        return observations[-1500:]

    @staticmethod
    def _record_forecast_ledger(
        stored: dict[str, Any], pv: dict[datetime, float], captured: datetime
    ) -> None:
        ledger = dict(stored.get("pv_forecast_ledger") or {})
        for start, watts in pv.items():
            lead = max(0, min(3, int((start - captured).total_seconds() // 86400)))
            entries = dict(ledger.get(start.isoformat()) or {})
            entries[str(lead)] = round(watts / 1000 * 0.25, 6)
            ledger[start.isoformat()] = entries
        cutoff = captured - timedelta(hours=1)
        stored["pv_forecast_ledger"] = {
            key: value for key, value in ledger.items()
            if datetime.fromisoformat(key) >= cutoff
        }

    async def _build_optimisation_snapshot(
        self,
        options: dict[str, Any],
        entities_by_category: dict[str, list[str]],
        actuals: list[dict[str, Any]],
        stored: dict[str, Any],
        devices: list[dict[str, Any]],
    ) -> dict[str, Any]:
        captured = dt_util.utcnow()
        horizon = utc_slots(captured, OPTIMISATION_HORIZON_HOURS)
        horizon_end = horizon[-1] + timedelta(minutes=15)

        pv_entities = [
            self._entity_payload(entity_id)
            for entity_id in options.get(OPT_PV_FORECAST_ENTITIES, [])
        ]
        if pv_entities:
            pv, pv_used, pv_issued = extract_timestamped_forecast(
                pv_entities,
                # `watts` is the canonical adapter contract. Accepting a
                # generic positional array would reintroduce kW/kWh ambiguity.
                attribute_names=("watts",),
                value_keys=("watts", "power", "value", "estimate"),
                combine="sum",
            )
        else:
            pv = {start: 0.0 for start in horizon}
            pv_used = []
            pv_issued = captured
        import_entity = (
            self._entity_payload(options[OPT_SUPPLIER_IMPORT_FORECAST_ENTITY])
            if options.get(OPT_SUPPLIER_IMPORT_FORECAST_ENTITY)
            else None
        )
        export_entity = (
            self._entity_payload(options[OPT_SUPPLIER_EXPORT_FORECAST_ENTITY])
            if options.get(OPT_SUPPLIER_EXPORT_FORECAST_ENTITY)
            else None
        )
        battery_entity = (
            self._entity_payload(options[OPT_BATTERY_SOC_ENTITY])
            if options.get(OPT_BATTERY_SOC_ENTITY)
            else None
        )
        price_area = str(options[OPT_ELECTRICITY_PRICE_AREA]).strip().upper()
        if price_area not in {"SE1", "SE2", "SE3", "SE4"}:
            raise OptimisationInputError(
                f"{OPT_ELECTRICITY_PRICE_AREA} must be SE1, SE2, SE3 or SE4"
            )
        pv_latitude = parse_number(
            options[OPT_PV_FORECAST_LATITUDE], OPT_PV_FORECAST_LATITUDE
        )
        pv_longitude = parse_number(
            options[OPT_PV_FORECAST_LONGITUDE], OPT_PV_FORECAST_LONGITUDE
        )
        if abs(pv_latitude - self.hass.config.latitude) > 0.05 or abs(
            pv_longitude - self.hass.config.longitude
        ) > 0.05:
            raise OptimisationInputError(
                "PV forecast coordinates do not match the Home Assistant home location"
            )
        for payload in pv_entities:
            attributes = payload["attributes"]
            declared_latitude = attributes.get("latitude")
            declared_longitude = attributes.get("longitude")
            if declared_latitude is not None and declared_longitude is not None and (abs(
                parse_number(declared_latitude, f"{payload['entity_id']} latitude")
                - pv_latitude
            ) > 0.05 or abs(
                parse_number(declared_longitude, f"{payload['entity_id']} longitude")
                - pv_longitude
            ) > 0.05):
                raise OptimisationInputError(
                    f"{payload['entity_id']} forecast location does not match the configured home"
                )
        for payload, label in (
            (import_entity, OPT_SUPPLIER_IMPORT_FORECAST_ENTITY),
            (export_entity, OPT_SUPPLIER_EXPORT_FORECAST_ENTITY),
        ):
            if payload is None:
                continue
            if payload["attributes"].get("unit_of_measurement") != "SEK/kWh":
                raise OptimisationInputError(
                    f"{label} must declare unit SEK/kWh"
                )
            declared_area = next(
                (
                    str(payload["attributes"][key]).upper()
                    for key in ("price_area", "area", "region")
                    if payload["attributes"].get(key)
                ),
                None,
            )
            if declared_area is not None and declared_area != price_area:
                raise OptimisationInputError(
                    f"{label} declares {declared_area}, expected {price_area}"
                )
        if import_entity:
            import_prices, import_used, import_issued = extract_timestamped_forecast(
                [import_entity],
                attribute_names=(
                    "prices", "forecast", "today", "tomorrow",
                    "raw_today", "raw_tomorrow",
                ),
                value_keys=("price", "total", "value", "price_sek_per_kwh"),
            )
            import_provider = "home_assistant_entity"
        else:
            import_prices, import_used, import_issued = (
                await self._tibber_import_forecast(
                    horizon[0], horizon_end, captured
                )
            )
            import_provider = "tibber.get_prices"
        if export_entity:
            export_prices, export_used, export_issued = extract_timestamped_forecast(
                [export_entity],
                attribute_names=(
                    "prices", "forecast", "today", "tomorrow",
                    "raw_today", "raw_tomorrow",
                ),
                value_keys=("price", "spot", "value", "price_sek_per_kwh"),
            )
            export_provider = "home_assistant_entity"
        else:
            export_prices, export_used, export_issued = (
                await self._nordpool_export_forecast(
                    horizon, price_area, captured
                )
            )
            export_provider = "nordpool.get_prices_for_date"
        if pv_entities:
            require_fresh_source(
                pv_issued, captured, max_age=timedelta(hours=12), label="PV forecast"
            )
        require_fresh_source(
            import_issued,
            captured,
            max_age=timedelta(hours=48),
            label="import price forecast",
        )
        require_fresh_source(
            export_issued,
            captured,
            max_age=timedelta(hours=48),
            label="export price forecast",
        )
        if battery_entity:
            require_fresh_source(
                battery_entity["last_reported"],
                captured,
                max_age=timedelta(minutes=15),
                label="battery SOC",
            )

        # Short-term recorder statistics may still be settling immediately at
        # the boundary. Keeping a full-quarter safety lag avoids publishing a
        # low partial bucket as history or learning from it.
        profile_end = quarter_start(captured) - timedelta(minutes=15)
        profile_start = profile_end - timedelta(
            days=OPTIMISATION_PROFILE_DAYS
        )
        profile_actuals = await self._actual_quarters(
            entities_by_category, profile_start, profile_end
        )
        device_profile_actuals = await self._device_actual_quarters(
            devices, profile_start, profile_end
        )
        device_models: list[dict[str, Any]] = []
        for device in devices:
            planning_role = device["planning_role"]
            control_type = device["control_type"]
            if (
                planning_role == "base_load" and control_type is not None
            ) or (
                planning_role == "controllable"
                and control_type not in (
                    "switch_schedule", "variable_power", "permit_inhibit",
                    "setpoint", "current_limit",
                )
            ) or planning_role not in ("base_load", "controllable"):
                raise OptimisationInputError(
                    f"{device['name']} has an invalid planning role or control type"
                )
            try:
                empirical = {
                    day_type: build_empirical_device_profile(
                        device_profile_actuals,
                        device["key"],
                        str(dt_util.DEFAULT_TIME_ZONE),
                        minimum_samples=2,
                        day_type=day_type,
                    )
                    for day_type in ("weekday", "weekend")
                }
            except OptimisationInputError as err:
                if planning_role == "controllable":
                    raise OptimisationInputError(
                        f"{device['name']} needs a complete empirical profile "
                        "before it can be controllable"
                    ) from err
                continue
            active_values = [
                profile["active_power_w"] for profile in empirical.values()
                if profile["active_power_w"] is not None
            ]
            active_power_w = (
                round(sum(active_values) / len(active_values), 1)
                if active_values else None
            )
            profile_sample_count = sum(
                int(profile["sample_count"]) for profile in empirical.values()
            )
            device["active_power_w"] = active_power_w
            device["profile_sample_count"] = profile_sample_count
            device["inference"] = {
                **device["inference"],
                "history_days": OPTIMISATION_PROFILE_DAYS,
                "profile": "weekday_weekend_trimmed_mean_v1",
            }
            if planning_role == "base_load":
                continue
            forecast_w: list[float] = []
            for start in horizon:
                local = start.astimezone(dt_util.DEFAULT_TIME_ZONE)
                day_type = "weekend" if local.weekday() >= 5 else "weekday"
                forecast_w.append(empirical[day_type]["expected_w"][
                    local.hour * 4 + local.minute // 15
                ])
            device_models.append({
                **device,
                "load_type": device.get(
                    "load_type", device["suggested_load_type"]
                ),
                "planning_role": "controllable",
                "control_type": control_type,
                "forecast_w_by_slot": forecast_w,
            })
        modelled_device_keys = tuple(
            str(model["key"]) for model in device_models
        )
        complete_device_keys = set(modelled_device_keys)
        for category, enabled_key in (
                ("pool_heating", OPT_POOL_PLANNING_ENABLED),
                ("hot_water", OPT_BOILER_PLANNING_ENABLED),
                ("ev_charging", OPT_EV_PLANNING_ENABLED),
        ):
            if not options.get(enabled_key):
                continue
            category_devices = [
                device for device in devices
                if device["category"] == category
                and device["planning_role"] == "controllable"
            ]
            missing = [
                str(device["name"])
                for device in category_devices
                if str(device["key"]) not in complete_device_keys
            ]
            if not category_devices or missing:
                detail = (
                    ", ".join(missing)
                    if missing
                    else "no controllable Energy Dashboard device"
                )
                raise OptimisationInputError(
                    f"{category} planning needs complete empirical profiles; missing {detail}"
                )
        profiles = {
            day_type: build_base_load_profile(
                profile_actuals,
                str(dt_util.DEFAULT_TIME_ZONE),
                device_slots=device_profile_actuals,
                modelled_device_keys=modelled_device_keys,
                minimum_samples=2,
                day_type=day_type,
            )
            for day_type in ("weekday", "weekend")
        }
        sample_count = sum(
            int(value["sample_count"])
            for profile in profiles.values()
            for value in profile
        )

        daily_totals = await self._daily_category_totals(
            entities_by_category,
            dt_util.start_of_local_day() - timedelta(days=30),
            dt_util.start_of_local_day(),
        )
        services, service_samples = self._build_services(
            options, entities_by_category, daily_totals, profile_actuals, horizon,
            device_models,
        )

        if self.tariff_catalog is None:
            raise OptimisationInputError("grid tariff catalogue is unavailable")
        grid_slots = {
            datetime.fromisoformat(value["start"]): value
            for value in grid_price_forecast(
                self.tariff_catalog,
                horizon[0],
                horizon_end,
                15,
            )
        }
        observations = self._observe_calibration(stored, actuals)
        calibration = calibration_summary(observations) if pv_entities else {
            "correction_factor_by_lead_day": [1.0, 1.0, 1.0, 1.0],
            "sample_count_by_lead_day": [0, 0, 0, 0],
        }
        errors = [
            abs(float(row["predicted_kwh"]) - float(row["actual_kwh"])) /
            max(float(row["actual_kwh"]), 0.001) * 100
            for row in observations if float(row["actual_kwh"]) > 0.01
        ]
        biases = [
            (float(row["predicted_kwh"]) - float(row["actual_kwh"])) /
            max(float(row["actual_kwh"]), 0.001) * 100
            for row in observations if float(row["actual_kwh"]) > 0.01
        ]

        slots: list[dict[str, Any]] = []
        price_gap = False
        for start in horizon:
            if start not in pv:
                raise OptimisationInputError(
                    f"PV forecast missing {start.isoformat()}"
                )
            local = start.astimezone(dt_util.DEFAULT_TIME_ZONE)
            day_type = "weekend" if local.weekday() >= 5 else "weekday"
            bucket = profiles[day_type][local.hour * 4 + local.minute // 15]
            supplier_import = import_prices.get(start)
            supplier_export = export_prices.get(start)
            grid = grid_slots.get(start)
            if supplier_import is None or supplier_export is None or grid is None:
                price_gap = True
                all_in_import = None
                all_in_export = None
            else:
                if price_gap:
                    raise OptimisationInputError(
                        "supplier prices contain a gap before a later published slot"
                    )
                all_in_import = supplier_import + float(
                    grid["import_price_sek_per_kwh"]
                )
                all_in_export = supplier_export + float(
                    grid["export_price_sek_per_kwh"]
                )
            slots.append({
                "start": start.isoformat(),
                "pv_forecast_w": round(pv[start], 2),
                "base_load_forecast_w": bucket["median_w"],
                "base_load_p10_w": bucket["p10_w"],
                "base_load_p90_w": bucket["p90_w"],
                "import_price_sek_per_kwh": (
                    None if all_in_import is None else round(all_in_import, 5)
                ),
                "export_price_sek_per_kwh": (
                    None if all_in_export is None else round(all_in_export, 5)
                ),
            })

        battery_soc = (
            normalized_fraction(battery_entity["state"], OPT_BATTERY_SOC_ENTITY)
            if battery_entity else None
        )
        valid_pv = max(pv) + timedelta(minutes=15)
        valid_import = max(import_prices) + timedelta(minutes=15)
        valid_export = max(export_prices) + timedelta(minutes=15)
        calibrated = any(
            count >= 20 for count in calibration["sample_count_by_lead_day"]
        )
        battery = None if battery_entity is None else {
            "capacity_kwh": parse_number(
                options[OPT_BATTERY_CAPACITY_KWH], OPT_BATTERY_CAPACITY_KWH
            ),
            "soc": battery_soc,
            "min_soc": parse_number(options[OPT_BATTERY_MIN_SOC], OPT_BATTERY_MIN_SOC),
            "max_soc": parse_number(options[OPT_BATTERY_MAX_SOC], OPT_BATTERY_MAX_SOC),
            "charge_max_w": parse_number(
                options[OPT_BATTERY_CHARGE_MAX_W], OPT_BATTERY_CHARGE_MAX_W
            ),
            "discharge_max_w": parse_number(
                options[OPT_BATTERY_DISCHARGE_MAX_W], OPT_BATTERY_DISCHARGE_MAX_W
            ),
            "charge_efficiency": parse_number(
                options[OPT_BATTERY_CHARGE_EFFICIENCY], OPT_BATTERY_CHARGE_EFFICIENCY
            ),
            "discharge_efficiency": parse_number(
                options[OPT_BATTERY_DISCHARGE_EFFICIENCY],
                OPT_BATTERY_DISCHARGE_EFFICIENCY,
            ),
        }
        capabilities = {
            "pv": bool(pv_entities),
            "battery": battery is not None,
            "pool": bool(options.get(OPT_POOL_PLANNING_ENABLED)),
            "boiler": bool(options.get(OPT_BOILER_PLANNING_ENABLED)),
            "ev": bool(options.get(OPT_EV_PLANNING_ENABLED)),
        }
        base_source_categories = (
            "total_consumption", "grid_import", "grid_export",
            "solar_production", "battery_charge", "battery_discharge",
        )
        snapshot = {
            "schema_version": 5,
            "mode": "live",
            "capabilities": capabilities,
            "snapshot_id": str(uuid4()),
            "captured_at": captured.isoformat(),
            "timezone": str(dt_util.DEFAULT_TIME_ZONE),
            "slot_minutes": 15,
            "slots": slots,
            "sources": {
                "pv": ({
                    "provider": "home_assistant_entity",
                    "entity_ids": pv_used,
                    "issued_at": pv_issued.isoformat(),
                    "valid_until": valid_pv.isoformat(),
                    "quality": "calibrated" if calibrated else "provider_raw",
                    "sample_count": len(observations),
                    "mape_percent": round(sum(errors) / len(errors), 2) if errors else None,
                    "bias_percent": round(sum(biases) / len(biases), 2) if biases else None,
                    "location": {
                        "latitude": round(pv_latitude, 5),
                        "longitude": round(pv_longitude, 5),
                    },
                } if pv_entities else None),
                "base_load": {
                    "provider": "home_assistant_recorder",
                    "entity_ids": sorted({
                        entity_id
                        for category in base_source_categories
                        for entity_id in entities_by_category[category]
                    } | {
                        str(model["statistic_id"]) for model in device_models
                    }),
                    "issued_at": captured.isoformat(),
                    "valid_until": (captured + timedelta(hours=2)).isoformat(),
                    "quality": "measured",
                    "sample_count": sample_count,
                },
                "import_price": {
                    "provider": import_provider,
                    "entity_ids": import_used,
                    "issued_at": import_issued.isoformat(),
                    "valid_until": valid_import.isoformat(),
                    "quality": "provider_raw",
                    "location": {"market_area": price_area},
                },
                "export_price": {
                    "provider": export_provider,
                    "entity_ids": export_used,
                    "issued_at": export_issued.isoformat(),
                    "valid_until": valid_export.isoformat(),
                    "quality": "provider_raw",
                    "location": {"market_area": price_area},
                },
                "battery": ({
                    "provider": "home_assistant_state",
                    "entity_ids": [options[OPT_BATTERY_SOC_ENTITY]],
                    "issued_at": battery_entity["last_reported"].isoformat(),
                    "valid_until": (captured + timedelta(minutes=75)).isoformat(),
                    "quality": "measured",
                    "sample_count": 1,
                } if battery_entity else None),
            },
            "pv_calibration": calibration,
            "battery": battery,
            "grid": {
                "import_limit_w": parse_number(
                    options[OPT_GRID_IMPORT_LIMIT_W], OPT_GRID_IMPORT_LIMIT_W
                ),
                "export_limit_w": parse_number(
                    options[OPT_GRID_EXPORT_LIMIT_W], OPT_GRID_EXPORT_LIMIT_W
                ),
            },
            "policy": {
                "battery_end_of_solar_target_soc": (
                    parse_number(options[OPT_BATTERY_TARGET_SOC], OPT_BATTERY_TARGET_SOC)
                    if battery else 0
                ),
                "battery_target_is_hard": (
                    bool(options[OPT_BATTERY_TARGET_IS_HARD]) if battery else False
                ),
                "terminal_soc_min": (
                    parse_number(options[OPT_TERMINAL_SOC_MIN], OPT_TERMINAL_SOC_MIN)
                    if battery else 0
                ),
                "terminal_energy_value_sek_per_kwh": (
                    parse_number(options[OPT_TERMINAL_ENERGY_VALUE], OPT_TERMINAL_ENERGY_VALUE)
                    if battery else 0
                ),
            },
            "device_models": device_models,
            "services": services,
            "service_requirement_sample_days": service_samples,
        }
        if pv_entities:
            self._record_forecast_ledger(
                stored, {start: pv[start] for start in horizon}, captured
            )
        return snapshot

    async def async_optimisation_push(
        self, _now: datetime | None = None, *, force_plan: bool = False
    ) -> None:
        """Upload completed quarters and refresh or retry the rolling plan."""
        async with self._push_lock:
            stored = await self._store.async_load() or {}
            self.optimisation_plan = self.optimisation_plan or stored.get(
                "optimisation_plan"
            )
            snapshot_error: str | None = None
            try:
                entities = self._configured_entities()
                devices = await async_energy_dashboard_inventory(self.hass)
                for device in devices:
                    category_entities = entities.setdefault(device["category"], [])
                    if device["statistic_id"] not in category_entities:
                        category_entities.append(device["statistic_id"])
                stored_metadata = stored.get("optimisation_device_metadata", {})
                stored_configuration = stored.get(
                    "optimisation_device_configuration", {}
                )
                for device in devices:
                    metadata = stored_metadata.get(device["key"], {})
                    for field in (
                        "active_power_w", "profile_sample_count", "inference"
                    ):
                        if field in metadata:
                            device[field] = metadata[field]
                    configuration = stored_configuration.get(device["key"], {})
                    device["load_type"] = configuration.get(
                        "load_type", device["suggested_load_type"]
                    )
                    device["planning_role"] = configuration.get(
                        "planning_role", device["suggested_planning_role"]
                    )
                    device["control_type"] = configuration.get(
                        "control_type", device["suggested_control_type"]
                    )
                # Do not race the recorder's five-minute statistics job. Actual
                # history may trail real time by one quarter; live reactive
                # control continues to use the local power sensor directly.
                complete_end = quarter_start(dt_util.utcnow()) - timedelta(minutes=15)
                accepted = stored.get("optimisation_actuals_accepted_until")
                if accepted:
                    # Re-send the last accepted quarter once. The database
                    # upsert keeps the unique row count at 96/day, while a
                    # recorder category that settled late can fill a prior
                    # sparse row without sending raw samples.
                    start = datetime.fromisoformat(accepted)
                else:
                    start = complete_end - timedelta(
                        hours=OPTIMISATION_ACTUAL_BACKFILL_HOURS
                    )
                start = max(
                    start.astimezone(timezone.utc),
                    complete_end - timedelta(hours=OPTIMISATION_ACTUAL_BACKFILL_HOURS),
                )
                actuals = (
                    await self._actual_quarters(entities, start, complete_end)
                    if (entities.get("total_consumption") or entities.get("grid_import"))
                    and start < complete_end
                    else []
                )
                if actuals and devices:
                    device_actuals = await self._device_actual_quarters(
                        devices, start, complete_end
                    )
                    device_energy_by_start = {
                        row["start"]: row["device_energy_kwh"]
                        for row in device_actuals
                    }
                    for row in actuals:
                        if row["start"] in device_energy_by_start:
                            row["device_energy_kwh"] = device_energy_by_start[
                                row["start"]
                            ]
                # Consume forecast/actual pairs on every quarter-hour exchange,
                # not only during the hourly replan. Otherwise three out of
                # four observations would pass the upload watermark before the
                # calibration ledger saw them.
                self._observe_calibration(stored, actuals)
                last_plan = stored.get("optimisation_plan")
                plan_due = optimisation_plan_due(
                    last_plan,
                    dt_util.utcnow(),
                    force=force_plan,
                    retry_after_error=bool(self.last_optimisation_error),
                )
                snapshot = None
                if plan_due:
                    mode = resolved_options(
                        self.hass, dict(self.entry.options)
                    )[OPT_PLANNING_MODE]
                    if mode == PLANNING_MODE_DISABLED:
                        self.optimisation_missing_inputs = []
                        self._sync_optimisation_issue()
                    elif mode == PLANNING_MODE_LIVE:
                        try:
                            options = self._optimisation_options()
                            snapshot = await self._build_optimisation_snapshot(
                                options, entities, actuals, stored, devices
                            )
                        except (OptimisationInputError, KeyError, TypeError, ValueError) as err:
                            snapshot_error = str(err)
                            if not self.optimisation_missing_inputs:
                                self.optimisation_missing_inputs = [snapshot_error]
                            self._sync_optimisation_issue()
                            _LOGGER.warning("Optimisation plan skipped: %s", err)
                    else:
                        snapshot_error = (
                            f"unsupported planning mode {mode!r}; review the integration"
                        )
                        self.optimisation_missing_inputs = [snapshot_error]
                        self._sync_optimisation_issue()
                if not actuals and snapshot is None:
                    self.last_optimisation_error = snapshot_error
                    self.async_update_listeners()
                    return
                result = await self.client.push_optimisation(
                    actuals, snapshot, devices
                )
                self.last_actual_slots_accepted = int(
                    result.get("actual_slots_accepted") or 0
                )
                self.actuals_accepted_until = result.get(
                    "actuals_accepted_until"
                )
                if result.get("plan"):
                    validate_plan_contract(result["plan"], dt_util.utcnow())
            except ShsSubscriptionInactiveError:
                self.last_optimisation_error = "subscription_inactive"
                self._sync_subscription_issue(False)
                return
            except (ShsApiError, OptimisationInputError, KeyError, TypeError, ValueError) as err:
                self.last_optimisation_error = str(err)
                _LOGGER.warning("Optimisation push skipped: %s", err)
                self.async_update_listeners()
                return

            self.last_optimisation_error = snapshot_error
            self.last_optimisation_push = dt_util.utcnow().isoformat()
            if result.get("actuals_accepted_until"):
                stored["optimisation_actuals_accepted_until"] = result[
                    "actuals_accepted_until"
                ]
            if result.get("plan"):
                self.last_optimisation_error = None
                self.optimisation_plan = result["plan"]
                stored["optimisation_plan"] = self.optimisation_plan
            elif self.optimisation_plan is None:
                self.optimisation_plan = stored.get("optimisation_plan")
            stored["last_optimisation_push"] = self.last_optimisation_push
            stored["optimisation_device_metadata"] = {
                device["key"]: {
                    "active_power_w": device["active_power_w"],
                    "profile_sample_count": device["profile_sample_count"],
                    "inference": device["inference"],
                }
                for device in devices
            }
            stored["optimisation_device_configuration"] = {
                configuration["key"]: {
                    "load_type": configuration["load_type"],
                    "planning_role": configuration["planning_role"],
                    "control_type": configuration["control_type"],
                }
                for configuration in result.get("device_configuration", [])
            }
            await self._store.async_save(stored)
            self.async_update_listeners()

    @property
    def current_plan_slot(self) -> dict[str, Any] | None:
        """Current priority-plan slot, only while its full contract is valid."""
        if resolved_options(
            self.hass, dict(self.entry.options)
        )[OPT_PLANNING_MODE] != PLANNING_MODE_LIVE:
            return None
        plan = self.optimisation_plan
        if not plan or plan.get("status") != "ready":
            return None
        now = dt_util.utcnow()
        try:
            validate_plan_contract(plan, now, require_recent_issue=False)
            if now >= datetime.fromisoformat(plan["binding_until"]):
                return None
            slots = plan["plans"]["priority"]["slots"]
        except (OptimisationInputError, KeyError, TypeError, ValueError):
            return None
        for slot in slots:
            start = datetime.fromisoformat(slot["start"])
            if start <= now < start + timedelta(minutes=15):
                return slot if slot.get("binding") is True else None
        return None

    @property
    def reactive_surplus_w(self) -> float | None:
        """Measured export opportunity for one central local allocator."""
        entity_id = self.entry.options.get(OPT_GRID_EXPORT_POWER_ENTITY)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = parse_number(state.state, entity_id)
        except OptimisationInputError:
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit == "kW":
            value *= 1_000
        elif unit != "W":
            return None
        return max(0.0, value)

    async def async_scheduled_push(self, _now: datetime | None = None) -> None:
        """Nightly job: push yesterday and catch up missed raw-reading days."""
        stored = await self._store.async_load() or {}
        self.latest_calculation = self.latest_calculation or stored.get(
            "latest_calculation"
        )
        last_pushed = stored.get("last_push_date")
        self.last_push_date = self.last_push_date or last_pushed

        yesterday = (dt_util.start_of_local_day() - timedelta(days=1)).date()
        if last_pushed:
            try:
                gap = (yesterday - datetime.fromisoformat(last_pushed).date()).days
            except ValueError:
                gap = 1
            days_back = max(1, min(gap, BACKFILL_MAX_DAYS))
        else:
            days_back = BACKFILL_MAX_DAYS
        await self.async_push_days(days_back)
