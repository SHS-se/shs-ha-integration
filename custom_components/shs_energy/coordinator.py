"""Subscription coordinator, daily reading push, and tariff calculation."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import logging
from math import isfinite, sqrt
from typing import Any
from uuid import uuid4

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
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
    ISSUE_DEVICE_CONTROL_MAPPING,
    ISSUE_UNPLANNED_SERVICE,
    ISSUE_OPTIMISATION_CONFIGURATION,
    ISSUE_OPTIMISATION_PLAN_REFUSED,
    ISSUE_SUBSCRIPTION_INACTIVE,
    MAX_KWH_PER_READING,
    MAX_THERMAL_SLOTS_PER_PUSH,
    THERMAL_BACKFILL_HOURS,
    OPT_OUTDOOR_TEMPERATURE_ENTITY,
    OPT_WEATHER_FORECAST_ENTITY,
    OPT_FORECAST_RESOLUTION_MINUTES,
    OPT_CONFIGURATION_REVIEWED_AT,
    OPT_DEVICE_CONTROL_MAPPINGS,
    OPT_PREFIX_ENTITIES,
    OPT_PV_FORECAST_ENTITIES,
    OPT_PV_FORECAST_LATITUDE,
    OPT_PV_FORECAST_LONGITUDE,
    OPT_BATTERY_SOC_ENTITY,
    OPT_EV_SOC_ENTITY,
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
    OPT_BATTERY_EXPORT_ENABLED,
    OPT_BATTERY_EXPORT_MIN_PRICE,
    OPT_BATTERY_EXPORT_RESERVE_SOC,
    OPT_GRID_IMPORT_LIMIT_W,
    OPT_GRID_EXPORT_LIMIT_W,
    OPT_TERMINAL_SOC_MIN,
    OPT_POOL_VOLUME_M3,
    OPT_POOL_WATER_TEMPERATURE_ENTITY,
    OPT_TERMINAL_ENERGY_VALUE,
    OPT_PLANNING_MODE,
    PLANNING_MODE_DISABLED,
    PLANNING_MODE_LIVE,
    OPTIMISATION_ACTUAL_BACKFILL_HOURS,
    OPTIMISATION_HORIZON_HOURS,
    OPTIMISATION_PROFILE_DAYS,
    OPTIMISATION_STARTUP_ISSUE_GRACE_SECONDS,
    PRICE_BACKFILL_CHUNK_DAYS,
    PRICE_BACKFILL_MAX_DAYS,
    STATUS_POLL_INTERVAL_HOURS,
    STORAGE_KEY_TEMPLATE,
    SUPPLIER_BACKFILL_MAX_DAYS,
    STORAGE_VERSION,
)
from .configuration import (
    area_name_by_id,
    async_energy_dashboard_inventory,
    entity_area_id_by_id,
    entity_display_name_by_id,
    resolved_options,
)
from .device_controls import (
    apply_requested_configuration,
    is_room_thermal_control,
    mapping_report,
    planning_path,
    requested_controllable_devices,
)
from .optimisation import (
    OptimisationInputError,
    aggregate_category_changes,
    aggregate_device_changes,
    build_base_load_model,
    calibration_summary,
    extract_timestamped_forecast,
    normalized_fraction,
    optimisation_plan_due,
    parse_number,
    quarter_start,
    require_fresh_source,
    SNAPSHOT_SCHEMA_VERSION,
    utc_slots,
    validate_plan_contract,
)
from .planning import build_device_models, build_services, unplanned_services
from .thermal import (
    actuator_value,
    cooling_value,
    build_thermal_slots,
    interpolate_hourly_forecast,
    numeric_value,
    quarter_means,
    thermal_zone_inputs,
    time_weighted_quarters,
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
from .supplier import (
    SupplierPriceError,
    all_in_price_slots,
    hourly_supplier_price_means,
    supplier_price_forecast,
    validate_supplier_prices,
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
        self.supplier_prices: dict[str, Any] | None = None
        self.tariff_status = "not_configured"
        self.missing_questions: list[str] = []
        self.last_tariff_error: str | None = None
        self.last_price_error: str | None = None
        self.last_calculation_error: str | None = None
        self.latest_calculation: dict[str, Any] | None = None
        self.optimisation_plan: dict[str, Any] | None = None
        self.last_optimisation_push: str | None = None
        self.last_optimisation_error: str | None = None
        self.last_actual_slots_accepted = 0
        self.actuals_accepted_until: str | None = None
        self.last_thermal_slots_accepted = 0
        self.optimisation_missing_inputs: list[str] = []
        self.optimisation_unplanned_services: list[str] = []
        self._attention: dict[str, dict[str, Any]] = {}
        # The website's own view of every meter, so a warning can name the
        # meter a customer has to change rather than an internal option key.
        self.device_configuration: dict[str, dict[str, Any]] = {}
        self.device_control_mapping_gaps: list[str] = []
        self._loaded_options = dict(entry.options)
        self._optimisation_issue_grace_until = dt_util.utcnow() + timedelta(
            seconds=OPTIMISATION_STARTUP_ISSUE_GRACE_SECONDS
        )
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
            self.supplier_prices = None
            self.tariff_components = {}
            self.tariff_status = "subscription_inactive"
            self.last_tariff_error = None
            self.last_price_error = None
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
        try:
            prices = await self.client.prices()
            validate_supplier_prices(prices)
        except ShsSubscriptionInactiveError:
            self.supplier_prices = None
            self.last_price_error = "subscription_inactive"
            self._sync_subscription_issue(False)
        except (ShsApiError, SupplierPriceError) as err:
            self.supplier_prices = None
            self.last_price_error = str(err)
            _LOGGER.warning("Supplier price refresh failed: %s", err)
        else:
            self.supplier_prices = prices
            self.last_price_error = None
            price_questions = missing_input_labels(
                prices, self.hass.config.language
            )
            self.missing_questions = list(dict.fromkeys([
                *self.missing_questions,
                *price_questions,
            ]))
        self._sync_missing_input_issue()
        return status

    # ------------------------------------------------------------------
    # Anything needing a person, recorded once and shown everywhere
    # ------------------------------------------------------------------

    @property
    def attention_items(self) -> list[dict[str, Any]]:
        """Everything currently asking for a decision, worst first.

        The panel used to derive its readiness cards from a handful of fields
        chosen by hand, so it could show four green "Ready" badges while Home
        Assistant was displaying a repair warning about the same installation.
        Both now come from `_set_attention`, which raises the repair and
        records the item in one call — they cannot disagree, because there is
        no second place to update.

        Each item also carries where the fix lives. A warning that names a
        problem without naming the field is only marginally better than
        silence, which is what "set the EV charging meter to variable-power
        control on the website" turned out to be.
        """
        order = {"error": 0, "warning": 1}
        return sorted(
            self._attention.values(),
            key=lambda item: (order.get(item["severity"], 2), item["title"]),
        )

    def _set_attention(
        self,
        key: str,
        *,
        severity: str,
        title: str,
        detail: str,
        fix: dict[str, Any],
        items: list[str] | None = None,
        placeholders: dict[str, str] | None = None,
    ) -> None:
        """Record one thing needing a person, and raise its repair."""
        self._attention[key] = {
            "key": key,
            "severity": severity,
            "title": title,
            "detail": detail,
            "items": list(items or []),
            "fix": fix,
        }
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            key,
            is_fixable=False,
            severity=(
                ir.IssueSeverity.ERROR
                if severity == "error"
                else ir.IssueSeverity.WARNING
            ),
            translation_key=key,
            translation_placeholders=placeholders or {},
        )

    def _clear_attention(self, key: str) -> None:
        """Drop one item and its repair together."""
        self._attention.pop(key, None)
        ir.async_delete_issue(self.hass, DOMAIN, key)

    def _sync_missing_input_issue(self) -> None:
        """Raise or clear the repair issue naming the unanswered questions."""
        if not self.missing_questions:
            self._clear_attention(ISSUE_MISSING_CUSTOMER_INPUT)
            return
        self._set_attention(
            ISSUE_MISSING_CUSTOMER_INPUT,
            severity="warning",
            title="The website is waiting for answers",
            detail=(
                "Billing and tariff questions have to be answered on the "
                "Smart Home Solutions website before they take effect here."
            ),
            items=list(self.missing_questions),
            fix={"kind": "website", "path": "/portal/settings/energy-tariffs"},
            placeholders={
                "questions": "\n".join(f"- {q}" for q in self.missing_questions)
            },
        )

    async def async_price_refresh(self, _now: datetime | None = None) -> None:
        """Refresh server prices on native Swedish market-quarter boundaries."""
        await self.async_request_refresh()

    def _sync_subscription_issue(self, active: bool) -> None:
        """Raise or clear the subscription repair issue."""
        if active:
            self._clear_attention(ISSUE_SUBSCRIPTION_INACTIVE)
            return
        self._set_attention(
            ISSUE_SUBSCRIPTION_INACTIVE,
            severity="warning",
            title="The energy subscription is not active",
            detail=(
                "Planning stops when the subscription lapses. Monitoring "
                "continues and nothing local is changed."
            ),
            fix={"kind": "website", "path": "/portal/billing"},
        )

    def _sync_plan_refused_issue(self, reason: str | None) -> None:
        """Surface a refused plan where a customer can actually see it.

        A plan the integration will not execute is not a debug detail. Until
        now it produced a log warning and an attribute on a diagnostic sensor,
        so an installation could spend days publishing plans in the portal
        while Home Assistant quietly executed none of them.
        """
        if reason is None:
            self._clear_attention(ISSUE_OPTIMISATION_PLAN_REFUSED)
            return
        self._set_attention(
            ISSUE_OPTIMISATION_PLAN_REFUSED,
            severity="error",
            title="The latest plan was refused",
            detail=(
                "Home Assistant received a plan it will not execute, so no "
                "planned control is running."
            ),
            items=[reason],
            fix={"kind": "panel", "tab": "diagnostics", "section": None},
            placeholders={"reason": reason},
        )

    def _sync_optimisation_issue(self) -> None:
        """Expose short capability-level gaps only for requested live planning."""
        mode = resolved_options(self.hass, dict(self.entry.options))[OPT_PLANNING_MODE]
        transient_startup_gap = (
            dt_util.utcnow() < self._optimisation_issue_grace_until
            and self.optimisation_input_gap_is_transient()
        )
        if (
            mode == PLANNING_MODE_DISABLED
            or not self.optimisation_missing_inputs
            or transient_startup_gap
        ):
            self._clear_attention(ISSUE_OPTIMISATION_CONFIGURATION)
            return
        self._set_attention(
            ISSUE_OPTIMISATION_CONFIGURATION,
            severity="warning",
            title="Planning is missing an input it needs",
            detail=(
                "Every item below is a field on this panel. Planning stays off "
                "until each one is filled in."
            ),
            items=list(self.optimisation_missing_inputs),
            fix={"kind": "panel", "tab": "inputs", "section": None},
            placeholders={
                "inputs": "\n".join(
                    f"- {value}" for value in self.optimisation_missing_inputs
                )
            },
        )

    def _sync_unplanned_service_issue(self) -> None:
        """Surface a service the website left in base load, without blocking.

        Deliberately a warning rather than a missing input: refusing to plan
        the whole home because the car is unrouted would trade a silent partial
        plan for no plan at all, and the rest of the objective is still correct
        without it.
        """
        if not self.optimisation_unplanned_services:
            self._clear_attention(ISSUE_UNPLANNED_SERVICE)
            return
        self._set_attention(
            ISSUE_UNPLANNED_SERVICE,
            severity="warning",
            title="A service is configured but not being planned",
            detail=(
                "The equipment is set up here, but no meter on the website is "
                "set to control it, so the planner never sees it."
            ),
            items=list(self.optimisation_unplanned_services),
            fix={"kind": "website", "path": "/portal/energy-modeling"},
            placeholders={
                "services": "\n".join(
                    f"- {value}" for value in self.optimisation_unplanned_services
                )
            },
        )

    def _sync_device_control_issue(
        self,
        configuration: dict[str, dict[str, Any]],
        mappings: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Raise one actionable issue for website-requested local mappings."""
        active_mappings = (
            mappings
            if mappings is not None
            else self.entry.options.get(OPT_DEVICE_CONTROL_MAPPINGS, {})
        )
        if not isinstance(active_mappings, dict):
            active_mappings = {}
        known_entity_ids = {state.entity_id for state in self.hass.states.async_all()}
        entity_names = entity_display_name_by_id(self.hass)
        area_names = area_name_by_id(self.hass)
        entity_area_ids = entity_area_id_by_id(self.hass)
        self.device_control_mapping_gaps = []
        for device in requested_controllable_devices(configuration):
            report = mapping_report(
                device.get("control_type"), active_mappings.get(device["key"]),
                known_entity_ids,
                entity_names,
                area_names,
                entity_area_ids,
                room_control=is_room_thermal_control(
                    device.get("control_type"), device.get("category")
                ),
            )
            if report["mapping_status"] != "ready":
                self.device_control_mapping_gaps.append(
                    str(device.get("name") or device["key"])
                )
        if not self.device_control_mapping_gaps:
            self._clear_attention(ISSUE_DEVICE_CONTROL_MAPPING)
            return
        self._set_attention(
            ISSUE_DEVICE_CONTROL_MAPPING,
            severity="warning",
            title="A controllable device needs its local entities",
            detail=(
                "The website asked for these devices to be controllable. Each "
                "one has a card on the Devices tab; until it is complete the "
                "device stays in base load and nothing local is changed."
            ),
            items=list(self.device_control_mapping_gaps),
            fix={"kind": "panel", "tab": "devices", "section": None},
            placeholders={
                "devices": "\n".join(
                    f"- {name}" for name in self.device_control_mapping_gaps
                )
            },
        )

    def options_update_requires_reload(self) -> bool:
        """Apply mapping-only option changes live; reload for everything else."""
        current = dict(self.entry.options)
        keys = set(self._loaded_options) | set(current)
        changed = {
            key for key in keys if self._loaded_options.get(key) != current.get(key)
        }
        self._loaded_options = current
        live_keys = {
            OPT_CONFIGURATION_REVIEWED_AT,
            OPT_DEVICE_CONTROL_MAPPINGS,
        }
        return bool(changed - live_keys)

    def optimisation_input_gap_is_transient(self) -> bool:
        """Return whether entity providers may still be starting up."""
        transient_markers = (" does not exist", " is unavailable", " is unknown")
        return bool(self.optimisation_missing_inputs) and all(
            any(marker in value for marker in transient_markers)
            for value in self.optimisation_missing_inputs
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

    def _mapped_power_w(self, mapping: dict[str, Any]) -> float | None:
        """Resolve the card's reviewed watts or current W/kW sensor value."""
        value = mapping.get("power")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            watts = float(value)
            return watts if isfinite(watts) and watts > 0 else None
        if not isinstance(value, str) or "." not in value:
            return None
        state = self.hass.states.get(value)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            watts = float(state.state)
        except (TypeError, ValueError):
            return None
        if state.attributes.get("unit_of_measurement") == "kW":
            watts *= 1_000
        return watts if isfinite(watts) and watts > 0 else None

    async def _prepared_device_inventory(
        self,
        stored: dict[str, Any],
        mappings: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Hydrate Energy Dashboard devices with server requests and local status."""
        devices = await async_energy_dashboard_inventory(self.hass)
        stored_metadata = stored.get("optimisation_device_metadata", {})
        for device in devices:
            metadata = stored_metadata.get(device["key"], {})
            for field in ("active_power_w", "profile_sample_count", "inference"):
                if field in metadata:
                    device[field] = metadata[field]
        prepared = apply_requested_configuration(
            devices,
            stored.get("optimisation_device_configuration", {}),
            mappings
            if mappings is not None
            else self.entry.options.get(OPT_DEVICE_CONTROL_MAPPINGS, {}),
            {state.entity_id for state in self.hass.states.async_all()},
            entity_display_name_by_id(self.hass),
            area_name_by_id(self.hass),
            entity_area_id_by_id(self.hass),
        )
        active_mappings = mappings if mappings is not None else self.entry.options.get(
            OPT_DEVICE_CONTROL_MAPPINGS, {}
        )
        if not isinstance(active_mappings, dict):
            active_mappings = {}
        for device in prepared:
            mapping = active_mappings.get(device["key"], {})
            watts = self._mapped_power_w(mapping)
            if watts is not None:
                device["active_power_w"] = round(watts, 1)
        return prepared

    def _record_device_exchange(
        self,
        stored: dict[str, Any],
        devices: list[dict[str, Any]],
        result: dict[str, Any],
        mappings: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Persist one authoritative website configuration response."""
        configurations = result.get("device_configuration")
        if not isinstance(configurations, list) or len(configurations) != len(devices):
            raise ShsApiError("website returned an incomplete device configuration")
        configuration = {
            value["key"]: {
                "key": value["key"],
                "statistic_id": value["statistic_id"],
                "name": value["name"],
                "category": value["category"],
                "load_type": value["load_type"],
                "planning_role": value["planning_role"],
                "control_type": value["control_type"],
            }
            for value in configurations
        }
        if set(configuration) != {device["key"] for device in devices}:
            raise ShsApiError("website device configuration does not match inventory")
        stored["optimisation_device_metadata"] = {
            device["key"]: {
                "active_power_w": device["active_power_w"],
                "profile_sample_count": device["profile_sample_count"],
                "inference": device["inference"],
            }
            for device in devices
        }
        stored["optimisation_device_configuration"] = configuration
        self.device_configuration = configuration
        self._sync_device_control_issue(configuration, mappings)
        return configuration

    async def async_refresh_device_configuration(self) -> list[dict[str, Any]]:
        """Force-fetch website requests when the user opens Configure.

        Opening a configuration screen must not wait on the planning pipeline:
        that reads ten days of five-minute statistics for every device before
        a website round-trip, which made the panel take about ten seconds to
        appear. A changed request still deserves a fresh plan, so one is
        scheduled in the background instead of awaited, and an unchanged
        request needs no replan at all.
        """
        async with self._push_lock:
            stored = await self._store.async_load() or {}
            previous = dict(stored.get("optimisation_device_configuration", {}))
            devices = await self._prepared_device_inventory(stored)
            result = await self.client.push_optimisation(
                [], None, devices, device_inventory_complete=True
            )
            configuration = self._record_device_exchange(stored, devices, result)
            configuration_changed = configuration != previous
            if configuration_changed:
                self.optimisation_plan = None
                stored.pop("optimisation_plan", None)
                # The first exchange discovers the new website request. Report
                # its status immediately in a second device-only exchange so a
                # previously saved matching mapping becomes Ready in one click.
                apply_requested_configuration(
                    devices,
                    configuration,
                    self.entry.options.get(OPT_DEVICE_CONTROL_MAPPINGS, {}),
                    {state.entity_id for state in self.hass.states.async_all()},
                    entity_display_name_by_id(self.hass),
                    area_name_by_id(self.hass),
                    entity_area_id_by_id(self.hass),
                )
                result = await self.client.push_optimisation(
                    [], None, devices, device_inventory_complete=True
                )
                configuration = self._record_device_exchange(
                    stored, devices, result
                )
            await self._store.async_save(stored)
            self.async_update_listeners()
            requested = requested_controllable_devices(configuration)
        if configuration_changed and resolved_options(
            self.hass, dict(self.entry.options)
        )[OPT_PLANNING_MODE] == PLANNING_MODE_LIVE:
            self.entry.async_create_background_task(
                self.hass,
                self.async_optimisation_push(force_plan=True),
                name=f"{DOMAIN}_replan_after_role_change",
            )
        return requested

    async def async_report_device_mapping(
        self,
        device_key: str,
        mappings: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Publish one newly reviewed mapping and return server acknowledgement."""
        async with self._push_lock:
            stored = await self._store.async_load() or {}
            devices = await self._prepared_device_inventory(stored, mappings)
            result = await self.client.push_optimisation(
                [], None, devices, device_inventory_complete=True
            )
            configuration = self._record_device_exchange(
                stored, devices, result, mappings
            )
            await self._store.async_save(stored)
            self.async_update_listeners()
            reported = next(
                (device for device in devices if device["key"] == device_key),
                None,
            )
            if reported is None or device_key not in configuration:
                raise ShsApiError("website did not acknowledge the saved device mapping")
            return {
                "mapping_status": reported["mapping_status"],
                "mapping_error": reported["mapping_error"],
                "mapping_summary": reported["mapping_summary"],
            }

    async def async_cached_device_configuration(self) -> list[dict[str, Any]]:
        """Return the last website request without making a network request."""
        stored = await self._store.async_load() or {}
        configuration = stored.get("optimisation_device_configuration", {})
        if not isinstance(configuration, dict):
            return []
        return requested_controllable_devices(configuration)

    async def async_cached_exchange_status(self) -> dict[str, Any]:
        """Return durable exchange watermarks for the configuration panel."""
        stored = await self._store.async_load() or {}
        return {
            "thermal_slots_accepted_until": stored.get(
                "thermal_slots_accepted_until"
            ),
            "last_optimisation_push": stored.get("last_optimisation_push"),
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

    async def _statistics_means(
        self, entity_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, list[tuple[datetime, float]]]:
        """Read recorder five-minute ``mean`` statistics for temperatures.

        The energy path asks for ``change`` in kWh, which no temperature
        entity produces. Measurement sensors are summarised as ``mean``
        instead, and those statistics live as long as ``purge_keep_days``.
        """
        if not entity_ids:
            return {}
        end_utc = dt_util.as_utc(end)
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            dt_util.as_utc(start),
            end_utc,
            set(entity_ids),
            "5minute",
            None,
            {"mean"},
        )

        result: dict[str, list[tuple[datetime, float]]] = {}
        for entity_id, rows in stats.items():
            values: list[tuple[datetime, float]] = []
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
                values.append((start_utc, float(mean)))
            result[entity_id] = values
        return result

    async def _state_history(
        self,
        entity_ids: list[str],
        start: datetime,
        end: datetime,
        *,
        with_attributes: bool,
    ) -> dict[str, list[tuple[datetime, Any, dict[str, Any] | None]]]:
        """Read raw state changes for entities the statistics engine ignores.

        ``input_number`` helpers carry no ``state_class``, so no statistics
        exist for a comfort band, and an actuator's on/off history is not a
        number at all. Both have to come from state changes. Attributes are
        fetched only when a climate entity's ``hvac_action`` is needed, since
        they dominate the row size over a multi-day window.
        """
        if not entity_ids:
            return {}
        # One extra state before the window is what makes the first quarter
        # complete: a helper that last changed days ago still governed it.
        history = await get_instance(self.hass).async_add_executor_job(
            lambda: get_significant_states(
                self.hass,
                dt_util.as_utc(start),
                dt_util.as_utc(end),
                sorted(set(entity_ids)),
                include_start_time_state=True,
                significant_changes_only=False,
                minimal_response=False,
                no_attributes=not with_attributes,
            )
        )

        result: dict[str, list[tuple[datetime, Any, dict[str, Any] | None]]] = {}
        for entity_id, states in (history or {}).items():
            rows: list[tuple[datetime, Any, dict[str, Any] | None]] = []
            for state in states:
                changed = getattr(state, "last_updated", None) or getattr(
                    state, "last_changed", None
                )
                if changed is None:
                    continue
                rows.append((
                    changed,
                    getattr(state, "state", None),
                    dict(getattr(state, "attributes", None) or {})
                    if with_attributes
                    else None,
                ))
            result[entity_id] = rows
        return result

    async def _outdoor_temperature_quarters(
        self, options: dict[str, Any], start: datetime, end: datetime
    ) -> dict[datetime, float]:
        """Measured outdoor temperature on the quarter grid, when configured."""
        entity_id = options.get(OPT_OUTDOOR_TEMPERATURE_ENTITY)
        if not entity_id:
            return {}
        statistics = await self._statistics_means([entity_id], start, end)
        return quarter_means(statistics.get(entity_id, []))

    async def _outdoor_forecast(
        self, options: dict[str, Any], horizon: list[datetime]
    ) -> tuple[dict[datetime, float], str | None]:
        """Resample the weather provider's hourly forecast onto planning slots.

        Modern Home Assistant serves forecasts from ``weather.get_forecasts``
        rather than a ``forecast`` attribute, so this asks the service and
        never reads a stale attribute copy.
        """
        entity_id = options.get(OPT_WEATHER_FORECAST_ENTITY)
        if not entity_id or not horizon:
            return {}, None
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            _LOGGER.debug("Outdoor forecast unavailable from %s: %s", entity_id, err)
            return {}, None

        entries = ((response or {}).get(entity_id) or {}).get("forecast") or []
        records: list[tuple[datetime, float]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            when = entry.get("datetime")
            temperature = entry.get("temperature")
            if not isinstance(when, str) or temperature is None:
                continue
            try:
                moment = datetime.fromisoformat(when.replace("Z", "+00:00"))
            except ValueError:
                continue
            if moment.tzinfo is None:
                continue
            records.append((moment, float(temperature)))
        if not records:
            return {}, None
        return interpolate_hourly_forecast(records, horizon), entity_id

    async def _thermal_quarters(
        self,
        options: dict[str, Any],
        devices: list[dict[str, Any]],
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """Build complete quarter-hour thermal observations for every zone."""
        zones = thermal_zone_inputs(
            devices, options.get(OPT_DEVICE_CONTROL_MAPPINGS, {})
        )
        if not zones or start >= end:
            return []

        temperature_entities = sorted(
            {zone["temperature_entity_id"] for zone in zones.values()}
        )
        helper_entities = sorted({
            entity
            for zone in zones.values()
            for entity in (zone.get("setpoint_entity_id"),)
            if isinstance(entity, str) and entity.strip()
        })
        actuator_entities = sorted(
            {entity for zone in zones.values() for entity in zone["actuator_entity_ids"]}
        )

        temperatures = await self._statistics_means(
            temperature_entities, start, end
        )
        # A setpoint may be a climate entity, whose target lives in an
        # attribute rather than the state, so helpers are read with attributes.
        helper_history = await self._state_history(
            helper_entities, start, end, with_attributes=True
        )
        actuator_history = await self._state_history(
            actuator_entities, start, end, with_attributes=True
        )

        temperature_quarters = {
            entity_id: quarter_means(rows)
            for entity_id, rows in temperatures.items()
        }
        helper_quarters = {
            entity_id: time_weighted_quarters(
                rows, start, end, numeric_value
            )
            for entity_id, rows in helper_history.items()
        }
        actuator_quarters = {
            entity_id: time_weighted_quarters(
                rows, start, end, actuator_value
            )
            for entity_id, rows in actuator_history.items()
        }
        # Cooling is not modelled, only detected. A quarter spent cooling is
        # excluded from training rather than read as a heater that somehow
        # made the room colder.
        cooling_quarters = {
            entity_id: time_weighted_quarters(
                rows, start, end, cooling_value
            )
            for entity_id, rows in actuator_history.items()
        }

        series: dict[str, dict[str, dict[datetime, float]]] = {}
        for key, zone in zones.items():
            duties: dict[datetime, list[float]] = {}
            cooling: dict[datetime, list[float]] = {}
            for entity_id in zone["actuator_entity_ids"]:
                for slot, duty in actuator_quarters.get(entity_id, {}).items():
                    duties.setdefault(slot, []).append(duty)
                for slot, duty in cooling_quarters.get(entity_id, {}).items():
                    cooling.setdefault(slot, []).append(duty)
            # Several actuators serving one zone are one zone-level demand.
            # Averaging keeps the value a fraction of the quarter rather than
            # a count of running heaters.
            zone_series: dict[str, dict[datetime, float]] = {
                "room_temperature_c": temperature_quarters.get(
                    zone["temperature_entity_id"], {}
                ),
                "actuator_duty": {
                    slot: round(sum(values) / len(values), 4)
                    for slot, values in duties.items()
                    if len(values) == len(zone["actuator_entity_ids"])
                },
                # Any actuator cooling taints the whole zone-quarter, so this
                # is a maximum rather than a mean.
                "cooling_duty": {
                    slot: round(max(values), 4)
                    for slot, values in cooling.items()
                    if max(values) > 0
                },
            }
            for field, mapping_key in (("setpoint_c", "setpoint_entity_id"),):
                entity_id = zone.get(mapping_key)
                if isinstance(entity_id, str) and entity_id.strip():
                    zone_series[field] = helper_quarters.get(entity_id, {})
            series[key] = zone_series

        outdoor = await self._outdoor_temperature_quarters(options, start, end)
        slots = build_thermal_slots(series, outdoor)
        return slots[-MAX_THERMAL_SLOTS_PER_PUSH:]

    async def _pool_quarters(
        self, options: dict[str, Any], start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Quarter-hour pool water temperature, when the sensor is mapped.

        This is the training series for the pool's loss coefficient and its
        heat pump's COP against air temperature. Neither is asked for at
        commissioning, and neither can be fitted without it — the pool heater's
        energy and the outdoor forecast are already stored server-side, so the
        water temperature is the only missing term.
        """
        entity_id = options.get(OPT_POOL_WATER_TEMPERATURE_ENTITY)
        if not isinstance(entity_id, str) or not entity_id.strip():
            return []
        if start >= end:
            return []
        means = await self._statistics_means([entity_id], start, end)
        quarters = quarter_means(means.get(entity_id, []))
        return [
            {
                "start": slot.isoformat(),
                "water_temperature_c": round(value, 3),
                "quality": {
                    "aggregation": "mean_of_recorder_5minute_means",
                    "duration_seconds": 900,
                },
            }
            for slot, value in sorted(quarters.items())
        ][-MAX_THERMAL_SLOTS_PER_PUSH:]

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
        readings = await self._hourly_grid_readings(entities_by_category, start, end)
        if not readings:
            return []

        current_catalog = self.supplier_prices
        if not current_catalog or current_catalog.get("configuration") is None:
            return []
        terms_valid_from = current_catalog.get("terms_valid_from")
        if not isinstance(terms_valid_from, str):
            raise SupplierPriceError("supplier terms have no effective date")
        cursor = max(
            start.astimezone(dt_util.DEFAULT_TIME_ZONE).date(),
            date.fromisoformat(terms_valid_from),
        )

        # The edge endpoint deliberately caps one response. Chunking a deep
        # initial backfill keeps memory and public-source load bounded while
        # retaining the exact same server-owned tariff terms for every day.
        last_day = (end.astimezone(dt_util.DEFAULT_TIME_ZONE) - timedelta(
            microseconds=1
        )).date()
        hourly_prices: dict[datetime, dict[str, float]] = {}
        while cursor <= last_day:
            chunk_end = min(cursor + timedelta(days=61), last_day)
            payload = await self.client.prices(
                cursor.isoformat(), chunk_end.isoformat()
            )
            validate_supplier_prices(payload)
            hourly_prices.update(hourly_supplier_price_means(payload))
            cursor = chunk_end + timedelta(days=1)

        per_day: dict[str, dict[str, float]] = {}
        for reading in readings:
            prices = hourly_prices.get(reading.start)
            if prices is None:
                # No price for this hour: leave the day short rather than
                # valuing energy at a rate that was never quoted.
                continue
            import_price = prices["import"]
            export_price = prices["export"]
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
            totals["import_kwh"] += reading.import_kwh
            totals["import_cost_sek"] += reading.import_kwh * import_price
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
        supplier_costs_completed = True
        try:
            supplier_costs = await self._supplier_daily_costs(
                daily_entities_by_category, supplier_sweep_start, today_start
            )
        except (ShsApiError, SupplierPriceError, ValueError) as err:
            supplier_costs_completed = False
            supplier_costs = []
            self.last_price_error = str(err)
            _LOGGER.warning("Supplier-cost pricing failed: %s", err)
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
        if deep_sweep and supplier_costs_completed:
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
            OPT_GRID_IMPORT_LIMIT_W,
            OPT_GRID_EXPORT_LIMIT_W,
        ]
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
                OPT_BATTERY_EXPORT_ENABLED,
                OPT_BATTERY_EXPORT_RESERVE_SOC,
                OPT_BATTERY_EXPORT_MIN_PRICE,
                OPT_TERMINAL_SOC_MIN,
                OPT_TERMINAL_ENERGY_VALUE,
            ])
        missing = sorted(
            key for key in required
            if options.get(key) is None or options.get(key) == "" or options.get(key) == []
        )
        if options.get(OPT_FORECAST_RESOLUTION_MINUTES) not in (None, 15):
            missing.append("forecast resolution must be 15 minutes")
        labels = {
            "a whole-home meter or Energy Dashboard grid meter": (
                "Home Assistant Energy Dashboard grid meter is not configured"
            ),
            OPT_GRID_IMPORT_LIMIT_W: "Grid import limit is not configured",
            OPT_GRID_EXPORT_LIMIT_W: "Grid export limit is not configured",
        }
        self.optimisation_missing_inputs = [
            labels.get(value, value) for value in missing
        ]
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

    def _price_quarters(
        self,
        supplier_payload: dict[str, Any] | None,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """All-in quarter prices for a window, or none when either half is out.

        The portal has no historical price of its own and will not derive one,
        because reproducing the grid transfer and energy tax in TypeScript would
        be a second implementation free to drift from the one that actually
        spent the customer's money (ENERGY_OPTIMISATION_ARCHITECTURE.md
        §1.3.7.1). This is the single source.
        """
        if self.tariff_catalog is None or supplier_payload is None:
            return []
        try:
            grid_slots = {
                datetime.fromisoformat(value["start"]): value
                for value in grid_price_forecast(
                    self.tariff_catalog, start, end, 15
                )
            }
        except TariffError as err:
            _LOGGER.debug("Grid prices unavailable for price push: %s", err)
            return []
        window_start = start.astimezone(timezone.utc)
        window_end = end.astimezone(timezone.utc)
        return [
            slot for slot in all_in_price_slots(supplier_payload, grid_slots)
            if window_start
            <= datetime.fromisoformat(slot["start"])
            < window_end
        ]

    async def async_backfill_prices(self, days: int) -> dict[str, Any]:
        """Reprice a historical window and push it to the portal.

        Prices only start accumulating when this integration version ships, and
        a planner being tuned needs the history that already exists to carry a
        cost. Supplier prices are fetched for the requested dates rather than
        taken from the cached forecast, which only covers today and tomorrow;
        grid tariffs are effective-dated and published ahead, so a past quarter
        resolves exactly rather than being estimated.
        """
        if days < 1 or days > PRICE_BACKFILL_MAX_DAYS:
            raise OptimisationInputError(
                f"days must be between 1 and {PRICE_BACKFILL_MAX_DAYS}"
            )
        if self.tariff_catalog is None:
            raise OptimisationInputError("grid tariff catalogue is unavailable")
        start_of_today = dt_util.start_of_local_day()
        start = start_of_today - timedelta(days=days)
        end = quarter_start(dt_util.utcnow())
        pushed = 0
        cursor = start
        # One request per chunk keeps each spot fetch and each push inside the
        # server's own limits; the ingest upsert makes a repeated chunk free.
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=PRICE_BACKFILL_CHUNK_DAYS), end)
            payload = await self.client.prices(
                cursor.astimezone(tariff_timezone(self.tariff_catalog))
                .date().isoformat(),
                (chunk_end - timedelta(seconds=1))
                .astimezone(tariff_timezone(self.tariff_catalog))
                .date().isoformat(),
            )
            validate_supplier_prices(payload)
            slots = self._price_quarters(payload, cursor, chunk_end)
            if slots:
                await self.client.push_optimisation([], None, [], None, slots)
                pushed += len(slots)
            cursor = chunk_end
        self.async_update_listeners()
        return {
            "days": days,
            "from": start.astimezone(timezone.utc).isoformat(),
            "to": end.astimezone(timezone.utc).isoformat(),
            "price_slots_pushed": pushed,
        }

    async def _measured_soc_quarters(
        self,
        start: datetime,
        end: datetime,
    ) -> dict[str, dict[str, float]]:
        """Quarter-hour mean state of charge for the house and vehicle batteries.

        Both are ``measurement`` sensors, so the recorder keeps five-minute
        means for them exactly as it does for temperatures. Reading them here
        is what lets the portal draw a measured SOC line; deriving one from
        charge and discharge energy would be a second battery model, free to
        drift from the one the planner uses.
        """
        options = resolved_options(self.hass, dict(self.entry.options))
        wanted = {
            "battery_soc": options.get(OPT_BATTERY_SOC_ENTITY),
            "ev_soc": options.get(OPT_EV_SOC_ENTITY),
        }
        entity_ids = sorted({
            entity_id for entity_id in wanted.values()
            if isinstance(entity_id, str) and entity_id
        })
        if not entity_ids:
            return {}
        statistics = await self._statistics_means(entity_ids, start, end)
        by_start: dict[str, dict[str, float]] = {}
        for field, entity_id in wanted.items():
            if not isinstance(entity_id, str) or not entity_id:
                continue
            for moment, value in quarter_means(
                statistics.get(entity_id, [])
            ).items():
                try:
                    fraction = normalized_fraction(value, entity_id)
                except OptimisationInputError:
                    # A sensor briefly reporting something impossible is not a
                    # reason to drop the quarter's energy.
                    continue
                by_start.setdefault(moment.isoformat(), {})[field] = round(fraction, 6)
        return by_start

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
        # Attached to quarters that already carry energy: a row of nothing but
        # a battery level has no measurement the portal can use.
        soc_by_start = await self._measured_soc_quarters(start, end)
        for row in rows:
            row.update(soc_by_start.get(row["start"], {}))
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

    async def _daily_meter_totals(
        self,
        entities_by_category: dict[str, list[str]],
        start: datetime,
        end: datetime,
    ) -> dict[str, dict[str, float]]:
        """Return ``{local_date: {entity_id: change_kwh}}`` per configured meter.

        Services are sized from the individual meters they control, so this
        stays per meter rather than pre-summing a category whose members may
        belong to different planning models.
        """
        all_entities = sorted(
            {entity for values in entities_by_category.values() for entity in values}
        )
        return await self._daily_changes(all_entities, start, end)

    def _build_services(
        self,
        options: dict[str, Any],
        daily_changes: dict[str, dict[str, float]],
        device_actuals: list[dict[str, Any]],
        horizon: list[datetime],
        device_models: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any] | None]:
        """Inject Home Assistant's clock and entity reads into pure planning."""
        return build_services(
            options,
            daily_changes,
            device_actuals,
            horizon,
            device_models,
            read_entity=self._entity_payload,
            local_tz=dt_util.DEFAULT_TIME_ZONE,
            today=dt_util.now().date(),
        )

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
        battery_entity = (
            self._entity_payload(options[OPT_BATTERY_SOC_ENTITY])
            if options.get(OPT_BATTERY_SOC_ENTITY)
            else None
        )
        price_catalog = self.supplier_prices
        if not price_catalog or price_catalog.get("configuration") is None:
            raise OptimisationInputError(
                "supplier and price area must be configured on the website"
            )
        prices = supplier_price_forecast(price_catalog)
        import_prices = {
            start: values["import"] for start, values in prices.items()
        }
        export_prices = {
            start: values["export"] for start, values in prices.items()
        }
        price_area = price_catalog["configuration"]["price_area"]
        try:
            price_issued = datetime.fromisoformat(
                price_catalog["issued_at"]
            ).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError) as err:
            raise OptimisationInputError(
                "server supplier prices have no valid issue time"
            ) from err
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
        if pv_entities:
            require_fresh_source(
                pv_issued, captured, max_age=timedelta(hours=12), label="PV forecast"
            )
        require_fresh_source(
            price_issued,
            captured,
            max_age=timedelta(hours=48),
            label="server supplier price forecast",
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
        control_mappings = options.get(OPT_DEVICE_CONTROL_MAPPINGS, {})
        if not isinstance(control_mappings, dict):
            raise OptimisationInputError("device control mappings must be an object")
        device_models = build_device_models(
            devices,
            device_profile_actuals,
            horizon,
            control_mappings,
            mapped_power_w=self._mapped_power_w,
            local_tz=dt_util.DEFAULT_TIME_ZONE,
        )
        modelled_device_keys = tuple(
            str(model["key"]) for model in device_models
        )
        base_load = build_base_load_model(
            profile_actuals,
            str(dt_util.DEFAULT_TIME_ZONE),
            device_slots=device_profile_actuals,
            modelled_device_keys=modelled_device_keys,
            minimum_samples=2,
            now=captured,
        )
        profiles = base_load["by_weekday"]
        sample_count = int(base_load["sample_count"])

        daily_meter_totals = await self._daily_meter_totals(
            entities_by_category,
            dt_util.start_of_local_day() - timedelta(days=30),
            dt_util.start_of_local_day(),
        )
        services, service_samples, ev_battery = self._build_services(
            options,
            daily_meter_totals,
            device_profile_actuals,
            horizon,
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
            bucket = profiles[local.weekday()][local.hour * 4 + local.minute // 15]
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
        valid_prices = max(import_prices) + timedelta(minutes=15)
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
        # Derived from the same routing the services were built from, so a
        # capability can never claim a service the snapshot does not carry.
        planned_paths = {
            planning_path(model["control_type"], model["category"])
            for model in device_models
        }
        capabilities = {
            "pv": bool(pv_entities),
            "battery": battery is not None,
            "pool": "pool" in planned_paths,
            "boiler": "boiler" in planned_paths,
            "ev": "ev" in planned_paths,
        }
        # A capability that is off because no meter routes to it is a
        # configuration gap, not a house without the equipment. Say so rather
        # than publishing a snapshot that quietly omits the store.
        self.optimisation_unplanned_services = unplanned_services(
            options, planned_paths, self.device_configuration
        )
        self._sync_unplanned_service_issue()
        base_source_categories = (
            "total_consumption", "grid_import", "grid_export",
            "solar_production", "battery_charge", "battery_discharge",
        )
        # The pool's state, not its budget. A warm pool asks for nothing, which
        # a median daily kWh could never express (§8.3).
        pool_state: dict[str, Any] | None = None
        pool_entity = options.get(OPT_POOL_WATER_TEMPERATURE_ENTITY)
        if isinstance(pool_entity, str) and pool_entity.strip():
            pool_payload = self._entity_payload(pool_entity)
            pool_state = {
                "water_temperature_c": round(
                    parse_number(pool_payload["state"], pool_entity), 3
                ),
                "volume_m3": parse_number(
                    options.get(OPT_POOL_VOLUME_M3), OPT_POOL_VOLUME_M3
                ),
                "source_entity_ids": {"water_temperature": pool_entity},
            }

        snapshot = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "mode": "live",
            "capabilities": capabilities,
            "pool": pool_state,
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
                    "provider": "smart_home_solutions",
                    "entity_ids": ["shs:supplier_import"],
                    "issued_at": price_issued.isoformat(),
                    "valid_until": valid_prices.isoformat(),
                    "quality": "provider_raw",
                    "location": {"market_area": price_area},
                },
                "export_price": {
                    "provider": "smart_home_solutions",
                    "entity_ids": ["shs:supplier_export"],
                    "issued_at": price_issued.isoformat(),
                    "valid_until": valid_prices.isoformat(),
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
            "ev_battery": ev_battery,
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
                "battery_export_enabled": (
                    bool(options[OPT_BATTERY_EXPORT_ENABLED]) if battery else False
                ),
                "battery_export_reserve_soc": (
                    parse_number(
                        options[OPT_BATTERY_EXPORT_RESERVE_SOC],
                        OPT_BATTERY_EXPORT_RESERVE_SOC,
                    ) if battery else 0
                ),
                "battery_export_min_price_sek_per_kwh": (
                    parse_number(
                        options[OPT_BATTERY_EXPORT_MIN_PRICE],
                        OPT_BATTERY_EXPORT_MIN_PRICE,
                    ) if battery else 0
                ),
            },
            "device_models": device_models,
            "services": services,
            "service_requirement_sample_days": service_samples,
        }
        # Outdoor temperature is what makes a thermal projection forward-
        # looking. It is attached only when a provider actually covered the
        # horizon, so a short or missing forecast leaves the field absent
        # rather than flat-filled with today's weather.
        outdoor_forecast, outdoor_entity = await self._outdoor_forecast(
            options, horizon
        )
        if outdoor_forecast:
            snapshot["outdoor_temperature_c"] = [
                outdoor_forecast.get(start) for start in horizon
            ]
            snapshot["sources"]["outdoor_temperature"] = {
                "provider": "home_assistant_weather",
                "entity_ids": [outdoor_entity] if outdoor_entity else [],
                "issued_at": captured.isoformat(),
                "valid_until": horizon_end.isoformat(),
                "quality": "provider_raw",
                "sample_count": len(outdoor_forecast),
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
            previous_configuration = dict(
                stored.get("optimisation_device_configuration", {})
            )
            self.optimisation_plan = self.optimisation_plan or stored.get(
                "optimisation_plan"
            )
            snapshot_error: str | None = None
            try:
                entities = self._configured_entities()
                devices = await self._prepared_device_inventory(stored)
                for device in devices:
                    category_entities = entities.setdefault(device["category"], [])
                    if device["statistic_id"] not in category_entities:
                        category_entities.append(device["statistic_id"])
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
                # Thermal observations are independent of the electrical
                # watermark: a zone sensor can settle after its energy meter,
                # so the window is re-swept every push and de-duplicated by
                # the server's upsert rather than by a local high-water mark.
                thermal_options = resolved_options(
                    self.hass, dict(self.entry.options)
                )
                thermal_start = complete_end - timedelta(
                    hours=THERMAL_BACKFILL_HOURS
                )
                accepted_thermal = stored.get("thermal_slots_accepted_until")
                if accepted_thermal:
                    thermal_start = max(
                        thermal_start,
                        datetime.fromisoformat(accepted_thermal).astimezone(
                            timezone.utc
                        ) - timedelta(hours=1),
                    )
                try:
                    thermal_slots = await self._thermal_quarters(
                        thermal_options, devices, thermal_start, complete_end
                    )
                except (HomeAssistantError, OptimisationInputError, ValueError) as err:
                    # A missing thermal sensor must never stop the electrical
                    # plan; the website reports the gap on its readiness panel.
                    _LOGGER.debug("Thermal observations unavailable: %s", err)
                    thermal_slots = []
                try:
                    pool_slots = await self._pool_quarters(
                        thermal_options, thermal_start, complete_end
                    )
                except (HomeAssistantError, OptimisationInputError, ValueError) as err:
                    _LOGGER.debug("Pool observations unavailable: %s", err)
                    pool_slots = []
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
                        except OptimisationInputError as err:
                            snapshot_error = str(err)
                            if not self.optimisation_missing_inputs:
                                self.optimisation_missing_inputs = list(
                                    err.reasons or [snapshot_error]
                                )
                            self._sync_optimisation_issue()
                            _LOGGER.warning("Optimisation plan skipped: %s", err)
                        except (KeyError, TypeError, ValueError) as err:
                            # Not a missing input: a defect. Reporting it as a
                            # configuration gap sent the reader looking for a
                            # setting to fix, and the bare Python text named
                            # neither the file nor the line.
                            snapshot_error = (
                                "internal error while building the plan "
                                f"({type(err).__name__}: {err}); the Home "
                                "Assistant log holds the traceback"
                            )
                            if not self.optimisation_missing_inputs:
                                self.optimisation_missing_inputs = [snapshot_error]
                            self._sync_optimisation_issue()
                            _LOGGER.exception(
                                "Unable to build the optimisation snapshot"
                            )
                    else:
                        snapshot_error = (
                            f"unsupported planning mode {mode!r}; review the integration"
                        )
                        self.optimisation_missing_inputs = [snapshot_error]
                        self._sync_optimisation_issue()
                # Price the quarters just measured as well as the ones ahead, so
                # the portal's history tab is priced on the same exchange that
                # gives it the energy rather than a push behind.
                price_slots = self._price_quarters(
                    self.supplier_prices,
                    complete_end - timedelta(
                        hours=OPTIMISATION_ACTUAL_BACKFILL_HOURS
                    ),
                    quarter_start(dt_util.utcnow()) + timedelta(
                        hours=OPTIMISATION_HORIZON_HOURS
                    ),
                )
                if (
                    not actuals
                    and snapshot is None
                    and not devices
                    and not thermal_slots
                    and not price_slots
                    and not pool_slots
                ):
                    self.last_optimisation_error = snapshot_error
                    self.async_update_listeners()
                    return
                result = await self.client.push_optimisation(
                    actuals,
                    snapshot,
                    devices,
                    thermal_slots,
                    price_slots,
                    pool_slots,
                    device_inventory_complete=True,
                )
                configuration = self._record_device_exchange(
                    stored, devices, result
                )
                configuration_changed = configuration != previous_configuration
                self.last_actual_slots_accepted = int(
                    result.get("actual_slots_accepted") or 0
                )
                self.actuals_accepted_until = result.get(
                    "actuals_accepted_until"
                )
            except ShsSubscriptionInactiveError:
                self.last_optimisation_error = "subscription_inactive"
                self._sync_subscription_issue(False)
                return
            except (ShsApiError, OptimisationInputError, KeyError, TypeError, ValueError) as err:
                self.last_optimisation_error = str(err)
                _LOGGER.warning("Optimisation push skipped: %s", err)
                self.async_update_listeners()
                return

            # The exchange itself succeeded, so the upload watermarks below are
            # advanced whatever the plan turns out to be. Validating the plan
            # inside the try above meant one unreadable plan also abandoned the
            # actuals and thermal watermarks and the plan-due cache, so the same
            # quarters were re-uploaded and a full replan was requested every
            # quarter. An unusable plan must cost the plan, and nothing else.
            plan_error: str | None = None
            if result.get("plan"):
                try:
                    validate_plan_contract(result["plan"], dt_util.utcnow())
                except OptimisationInputError as err:
                    plan_error = str(err)
                    _LOGGER.warning("Optimisation plan refused: %s", err)

            self.last_optimisation_error = snapshot_error
            self.last_optimisation_push = dt_util.utcnow().isoformat()
            if result.get("actuals_accepted_until"):
                stored["optimisation_actuals_accepted_until"] = result[
                    "actuals_accepted_until"
                ]
            if result.get("thermal_slots_accepted_until"):
                stored["thermal_slots_accepted_until"] = result[
                    "thermal_slots_accepted_until"
                ]
            self.last_thermal_slots_accepted = int(
                result.get("thermal_slots_accepted") or 0
            )
            if plan_error is not None:
                # Keep whatever plan is already cached rather than replacing it
                # with one that failed its contract, and say so loudly enough
                # to be noticed: a refused plan means the executor is running
                # on nothing, which used to show up only as a log line.
                self.last_optimisation_error = plan_error
                self._sync_plan_refused_issue(plan_error)
            elif result.get("plan") and not configuration_changed:
                self.last_optimisation_error = None
                self.optimisation_plan = result["plan"]
                stored["optimisation_plan"] = self.optimisation_plan
                self._sync_plan_refused_issue(None)
            elif configuration_changed:
                # The returned plan was built from the preceding website
                # request. Never expose it after a role/control change; the
                # next exchange replans using the new effective base split.
                self.optimisation_plan = None
                stored.pop("optimisation_plan", None)
                self.last_optimisation_error = "device configuration changed; replan pending"
            elif self.optimisation_plan is None:
                self.optimisation_plan = stored.get("optimisation_plan")
            stored["last_optimisation_push"] = self.last_optimisation_push
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
