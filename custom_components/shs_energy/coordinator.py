"""Subscription-status coordinator and daily push scheduler."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
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

_LOGGER = logging.getLogger(__name__)


class ShsStatusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls integration-status and owns the daily push."""

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

        self._sync_subscription_issue(bool(status.get("subscription_active")))
        return status

    def _sync_subscription_issue(self, active: bool) -> None:
        """Raise or clear the 'subscription inactive' repair issue."""
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

    # ------------------------------------------------------------------
    # Daily push
    # ------------------------------------------------------------------

    def _configured_entities(self) -> dict[str, list[str]]:
        """Category → entity_ids from the options flow."""
        return {
            category: list(
                self.entry.options.get(f"{OPT_PREFIX_ENTITIES}{category}", [])
            )
            for category in CATEGORIES
        }

    async def _daily_changes(
        self, entity_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, dict[str, float]]:
        """Per-day kWh change for each entity via recorder statistics.

        Returns {date_iso: {entity_id: change_kwh}}.
        """
        if not entity_ids:
            return {}

        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            set(entity_ids),
            "day",
            None,
            {"change"},
        )

        per_day: dict[str, dict[str, float]] = {}
        for entity_id, rows in stats.items():
            for row in rows:
                start_ts = row.get("start")
                change = row.get("change")
                if start_ts is None or change is None or change < 0:
                    continue
                day = dt_util.as_local(
                    dt_util.utc_from_timestamp(start_ts)
                ).date().isoformat()
                per_day.setdefault(day, {})[entity_id] = float(change)
        return per_day

    async def async_push_days(self, days_back: int) -> None:
        """Compute and push readings for the last `days_back` full days."""
        entities_by_category = self._configured_entities()
        all_entities = sorted(
            {e for ids in entities_by_category.values() for e in ids}
        )
        if not all_entities:
            _LOGGER.debug("No sensors configured yet; skipping push")
            return

        today_start = dt_util.start_of_local_day()
        start = today_start - timedelta(days=days_back)
        per_day = await self._daily_changes(all_entities, start, today_start)
        if not per_day:
            _LOGGER.debug("No statistics found for configured sensors")
            return

        readings: list[dict[str, Any]] = []
        for day, entity_changes in sorted(per_day.items()):
            for category, entity_ids in entities_by_category.items():
                values = [
                    entity_changes[e] for e in entity_ids if e in entity_changes
                ]
                if not values:
                    continue
                readings.append(
                    {
                        "date": day,
                        "category": category,
                        "kwh": round(sum(values), 3),
                    }
                )

        if not readings:
            return

        try:
            result = await self.client.push_readings(readings)
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
        self.last_push_date = max(per_day)
        await self._store.async_save({"last_push_date": self.last_push_date})
        self.async_update_listeners()
        _LOGGER.debug(
            "Pushed %s readings (accepted=%s)", len(readings), result.get("accepted")
        )

    async def async_scheduled_push(self, _now: datetime | None = None) -> None:
        """Nightly job: push yesterday, catching up any missed days."""
        stored = await self._store.async_load() or {}
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
