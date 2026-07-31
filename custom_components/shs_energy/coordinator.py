"""Subscription coordinator, daily reading push, and tariff calculation."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    DOMAIN,
    ISSUE_SUBSCRIPTION_INACTIVE,
    OPT_PREFIX_ENTITIES,
    STATUS_POLL_INTERVAL_HOURS,
    STORAGE_KEY_TEMPLATE,
    STORAGE_VERSION,
)
from .tariff import (
    HourlyGridReading,
    TariffError,
    calculate_month,
    earliest_tariff_date,
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
        self.tariff_catalog: dict[str, Any] | None = None
        self.tariff_status = "not_configured"
        self.last_tariff_error: str | None = None
        self.last_calculation_error: str | None = None
        self.latest_calculation: dict[str, Any] | None = None
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
            elif not self._configured_entities().get("grid_import"):
                self.tariff_status = "missing_grid_import"
            else:
                self.tariff_status = "configured"
        return status

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

    def _configured_entities(self) -> dict[str, list[str]]:
        """Return category → entity ids from the options flow."""
        return {
            category: list(
                self.entry.options.get(f"{OPT_PREFIX_ENTITIES}{category}", [])
            )
            for category in CATEGORIES
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
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            dt_util.as_utc(start),
            dt_util.as_utc(end),
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
            self.last_calculation_error = ", ".join(catalog.get("missing_inputs", []))
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

        calculations: list[dict[str, Any]] = []
        errors: list[str] = []
        for month in months:
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
        stored = await self._store.async_load() or {}
        entities_by_category = self._configured_entities()
        all_entities = sorted(
            {entity for values in entities_by_category.values() for entity in values}
        )

        today_start = dt_util.start_of_local_day()
        start = today_start - timedelta(days=days_back)
        per_day = await self._daily_changes(all_entities, start, today_start)
        readings: list[dict[str, Any]] = []
        for day, entity_changes in sorted(per_day.items()):
            for category, entity_ids in entities_by_category.items():
                values = [
                    entity_changes[entity]
                    for entity in entity_ids
                    if entity in entity_changes
                ]
                if values:
                    readings.append(
                        {
                            "date": day,
                            "category": category,
                            "kwh": round(sum(values), 3),
                        }
                    )

        calculations, catalog_hash, tariff_attempted = await self._tariff_calculations(
            entities_by_category, days_back, stored
        )
        if not readings and not calculations:
            if tariff_attempted and catalog_hash:
                stored["tariff_catalog_hash"] = catalog_hash
                await self._store.async_save(stored)
            _LOGGER.debug("No daily readings or tariff calculations to push")
            self.async_update_listeners()
            return

        try:
            result = await self.client.push_readings(readings, calculations)
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
        if self.latest_calculation:
            stored["latest_calculation"] = self.latest_calculation
        await self._store.async_save(stored)
        self.async_update_listeners()
        _LOGGER.debug(
            "Pushed %s readings and %s calculations "
            "(accepted=%s, calculations_accepted=%s)",
            len(readings),
            len(calculations),
            result.get("accepted"),
            result.get("calculations_accepted"),
        )

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
