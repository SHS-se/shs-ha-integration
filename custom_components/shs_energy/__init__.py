"""Smart Home Solutions Energy — pushes daily energy categories to the SHS portal."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)

from homeassistant.helpers.storage import STORAGE_DIR, Store
from homeassistant.helpers.json import json_bytes
from homeassistant.util.json import json_loads
from homeassistant.helpers import entity_registry as er

from .api import ShsApiClient
from .controller_events import attach_controller_events
from .config_panel import async_apply_configuration, async_register_config_panel
from .const import (
    CONFIGURABLE_CATEGORIES,
    CONFIG_ENTRY_VERSION,
    CONF_BASE_URL,
    CONF_DEVICE_TOKEN,
    PUSH_TIME_HOUR,
    PUSH_TIME_MINUTE,
    PRICE_REFRESH_SECOND,
    OPTIMISATION_STARTUP_DELAY_SECONDS,
    PRICE_BACKFILL_MAX_DAYS,
    PLAN_EXCHANGE_INTERVAL_MINUTES,
    OPT_AUTOMATIC_SETUP,
    OPT_DEVICE_CONTROL_MAPPINGS,
    OPT_DISCOVERY_EVIDENCE,
    OPT_PLANNING_MODE,
    OPT_PREFIX_ENTITIES,
    DOMAIN,
)
from .configuration import (
    async_discover_configuration,
    entity_area_id,
    resolved_options,
)
from .controller import ScheduledController
from .battery_writer import BatteryWriterFence
from .battery_runtime import BatteryRuntime, NativeReadbackPending
from .execution_archive import PageFiles
from .verification import VerificationJournal
from .verification_storage import VerificationStorage
from .configuration_schema import ConfigurationReader
from .coordinator import ShsStatusCoordinator
from .migration import mapped_entity_ids, migrate_options

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SELECT]

ShsEnergyConfigEntry = ConfigEntry[ShsStatusCoordinator]

SERVICE_DISCOVER_CONFIGURATION = "discover_configuration"
SERVICE_APPLY_CONFIGURATION = "apply_configuration"
SERVICE_BACKFILL_PRICES = "backfill_prices"


async def _async_delayed_startup_optimisation_push(
    coordinator: ShsStatusCoordinator,
) -> None:
    """Give entity providers time to start, then exchange once."""
    await asyncio.sleep(OPTIMISATION_STARTUP_DELAY_SECONDS)
    await coordinator.async_replan_poll()


def _entry_for_call(hass: HomeAssistant, call: ServiceCall) -> ConfigEntry:
    entries = hass.config_entries.async_entries(DOMAIN)
    entry_id = call.data.get("entry_id")
    if entry_id:
        entries = [entry for entry in entries if entry.entry_id == entry_id]
    if len(entries) != 1:
        raise ValueError("entry_id is required when there is not exactly one SHS Energy entry")
    return entries[0]


def _configuration_response(
    options: dict[str, Any], discovery: dict[str, Any] | None = None
) -> dict[str, Any]:
    categories = {
        category: list(options.get(f"{OPT_PREFIX_ENTITIES}{category}", []))
        for category in CONFIGURABLE_CATEGORIES
    }
    response = {
        "planning_mode": options.get(OPT_PLANNING_MODE),
        "automatic_setup": bool(options.get(OPT_AUTOMATIC_SETUP)),
        "categories": categories,
        "configuration": options,
        "evidence": options.get(OPT_DISCOVERY_EVIDENCE, {}),
    }
    if discovery is not None:
        response["capabilities"] = discovery["capabilities"]
        response["review_required"] = discovery["review_required"]
    return response


async def async_setup(hass: HomeAssistant, _config: dict[str, Any]) -> bool:
    """Register the full-page configuration panel and automation services."""
    await async_register_config_panel(hass)

    async def discover(call: ServiceCall) -> dict[str, Any]:
        entry = _entry_for_call(hass, call)
        discovery = await async_discover_configuration(
            hass, dict(entry.options)
        )
        return _configuration_response(
            discovery["configuration"], discovery
        )

    async def apply(call: ServiceCall) -> dict[str, Any]:
        entry = _entry_for_call(hass, call)
        options = await async_apply_configuration(
            hass, entry, dict(call.data["configuration"])
        )
        return _configuration_response(options)

    hass.services.async_register(
        DOMAIN,
        SERVICE_DISCOVER_CONFIGURATION,
        discover,
        schema=vol.Schema({vol.Optional("entry_id"): str}),
        supports_response=SupportsResponse.ONLY,
    )
    async def backfill_prices(call: ServiceCall) -> dict[str, Any]:
        entry = _entry_for_call(hass, call)
        coordinator = getattr(entry, "runtime_data", None)
        if coordinator is None:
            raise ValueError("SHS Energy is not loaded for that entry")
        return await coordinator.async_backfill_prices(int(call.data["days"]))

    hass.services.async_register(
        DOMAIN,
        SERVICE_APPLY_CONFIGURATION,
        apply,
        schema=vol.Schema({
            vol.Optional("entry_id"): str,
            vol.Required("configuration"): dict,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL_PRICES,
        backfill_prices,
        schema=vol.Schema({
            vol.Optional("entry_id"): str,
            vol.Required("days"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=PRICE_BACKFILL_MAX_DAYS)
            ),
        }),
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ShsEnergyConfigEntry) -> bool:
    """Convert older configuration records once before setting up the integration."""
    if entry.version > CONFIG_ENTRY_VERSION:
        return False
    if entry.version == CONFIG_ENTRY_VERSION:
        return True
    options = dict(entry.options)
    entity_area_ids = {
        entity_id: area_id
        for entity_id in mapped_entity_ids(options)
        if (area_id := entity_area_id(hass, entity_id)) is not None
    }
    migrated_options, _changed = migrate_options(
        options, entity_area_ids=entity_area_ids, source_version=entry.version,
    )
    hass.config_entries.async_update_entry(
        entry, options=migrated_options, version=CONFIG_ENTRY_VERSION, minor_version=1,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ShsEnergyConfigEntry) -> bool:
    """Set up the current configuration; migration is owned by the entry hook."""
    client = ShsApiClient(
        async_get_clientsession(hass),
        entry.data[CONF_BASE_URL],
        entry.data[CONF_DEVICE_TOKEN],
    )
    coordinator = ShsStatusCoordinator(hass, entry, client)
    entry.runtime_data = coordinator
    options = ConfigurationReader(lambda: entry.options,
        lambda: (hass.config.latitude, hass.config.longitude), json_bytes, json_loads)
    verification_store = VerificationStorage(
        hass.config.path(STORAGE_DIR, f"shs_energy.verification.{entry.entry_id}.sqlite"),
        hass.async_add_executor_job, Store(hass, 1, f"shs_energy.verification.{entry.entry_id}"), json_bytes)
    controller = ScheduledController(
        hass, coordinator, Store(hass, 1, f"shs_energy.controller.{entry.entry_id}"),
        options,
        VerificationJournal(verification_store,
                            Store(hass, 1, f"shs_energy.verification_samples.{entry.entry_id}")),
        entity_registry=er.async_get(hass),
    )
    coordinator.controller = controller
    controller.metrics.performance = {'verification_storage': verification_store.metrics,
                                      'configuration_reads': options.metrics}
    # Evidence pages are files named by content, not Stores: Home Assistant
    # keeps every Store key it has written until restart.
    evidence = PageFiles(hass.config.path(STORAGE_DIR), f"shs_energy.execution_evidence.{entry.entry_id}.",
                         hass.async_add_executor_job, json_bytes, json_loads)
    coordinator.battery_runtime = BatteryRuntime(coordinator, controller,
        Store(hass, 1, f"shs_energy.battery_runtime.{entry.entry_id}"),
        lambda: int(datetime.now(timezone.utc).timestamp() * 1000),
        evidence.store_for, (evidence.list, evidence.remove))
    coordinator.battery_writer = BatteryWriterFence(
        Store(hass, 1, f"shs_energy.battery_writer.{entry.entry_id}"), controller.lock,
        options,
        lambda: int(datetime.now(timezone.utc).timestamp() * 1000),
        coordinator.battery_runtime.identity,
    )
    controller.battery_writer_fence = coordinator.battery_writer
    await coordinator.battery_writer.open()
    try:
        await coordinator.battery_runtime.open()
    except NativeReadbackPending as error:
        coordinator.battery_writer.close()
        raise ConfigEntryNotReady(str(error)) from error
    entry.async_on_unload(coordinator.battery_live_inputs.close)
    entry.async_on_unload(coordinator.battery_writer.close)

    async def stop_controller(_event=None):
        coordinator.battery_live_inputs.close()
        try:
            await coordinator.battery_runtime.close(release=True)
            await controller.async_stop()
        finally:
            coordinator.battery_writer.close()

    scheduler = attach_controller_events(hass, entry, controller)
    # Recover local ownership before contacting the cloud. A network outage
    # must not prevent restoration of commands left by the previous process.
    try:
        await coordinator.async_restore_plan()
        await controller.async_start(reason="integration_load" if hass.is_running else "homeassistant_startup")
        await coordinator.async_config_entry_first_refresh()
    except BaseException:
        await stop_controller()
        raise

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_controller))
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await stop_controller()
        raise
    scheduler.coordinator_updated()
    entry.async_on_unload(async_track_time_interval(
        hass, coordinator.async_battery_inputs_refresh, timedelta(seconds=5)))
    entry.async_create_background_task(hass, coordinator.async_battery_inputs_refresh(),
        name="shs_energy_battery_live_inputs")

    # Nightly push shortly after midnight; also catch up on startup in case
    # HA was down at the scheduled time.
    entry.async_on_unload(
        async_track_time_change(
            hass,
            coordinator.async_scheduled_push,
            hour=PUSH_TIME_HOUR,
            minute=PUSH_TIME_MINUTE,
            second=0,
        )
    )
    entry.async_create_background_task(
        hass, coordinator.async_scheduled_push(), name="shs_energy_startup_push"
    )
    # Advance cached price values on market quarters without a network request.
    entry.async_on_unload(
        async_track_time_change(
            hass,
            coordinator.async_price_refresh,
            minute=[0, 15, 30, 45],
            second=PRICE_REFRESH_SECOND,
        )
    )
    entry.async_create_background_task(
        hass, coordinator.async_replan_listener(), name="shs_energy_replan_notifications"
    )
    # Relative to this integration's startup, not shared wall-clock quarters.
    entry.async_on_unload(
        async_track_time_interval(
            hass, coordinator.async_replan_poll,
            timedelta(minutes=PLAN_EXCHANGE_INTERVAL_MINUTES),
        )
    )
    entry.async_create_background_task(
        hass,
        _async_delayed_startup_optimisation_push(coordinator),
        name="shs_energy_startup_optimisation_push",
    )

    # React to changed local meter and device-control mappings.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: ShsEnergyConfigEntry
) -> None:
    if not entry.runtime_data.options_update_requires_reload():
        return
    # A full reload ensures changed entity mappings are reflected by all
    # platforms before the next recorder aggregation.
    if not await hass.config_entries.async_reload(entry.entry_id):
        return
    reloaded = hass.config_entries.async_get_entry(entry.entry_id)
    coordinator = getattr(reloaded, "runtime_data", None)
    if coordinator is not None:
        await coordinator.async_optimisation_push(force_plan=True)


async def async_unload_entry(hass: HomeAssistant, entry: ShsEnergyConfigEntry) -> bool:
    """Unload a config entry."""
    await entry.runtime_data.battery_runtime.close(release=True)
    entry.runtime_data.battery_live_inputs.close()
    try:
        await entry.runtime_data.controller.async_stop()
    finally:
        entry.runtime_data.battery_writer.close()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
