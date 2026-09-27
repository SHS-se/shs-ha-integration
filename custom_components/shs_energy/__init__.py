"""Smart Home Solutions Energy — pushes daily energy categories to the SHS portal."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady

from homeassistant.helpers.service import async_register_admin_service

from .refresh import set_reloading
from .config_panel import async_register_config_panel
from .control_configuration import configuration_request
from .shs_core.const import CONFIGURABLE_CATEGORIES, CONFIG_ENTRY_VERSION, PRICE_BACKFILL_MAX_DAYS, OPT_AUTOMATIC_SETUP, OPT_DISCOVERY_EVIDENCE, OPT_PLANNING_MODE, OPT_PREFIX_ENTITIES, DOMAIN
from .configuration import entity_area_id
from .migration import mapped_entity_ids, migrate_options

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SELECT]

ShsEnergyConfigEntry = ConfigEntry

SERVICE_DISCOVER_CONFIGURATION = "discover_configuration"
SERVICE_APPLY_CONFIGURATION = "apply_configuration"
SERVICE_BACKFILL_PRICES = "backfill_prices"


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
    from .gateway import register_gateway
    register_gateway(hass)

    async def discover(call: ServiceCall) -> dict[str, Any]:
        entry = _entry_for_call(hass, call)
        discovery = await configuration_request(entry, 'discover', {})
        return _configuration_response(
            discovery["configuration"], discovery
        )

    async def apply(call: ServiceCall) -> dict[str, Any]:
        entry = _entry_for_call(hass, call)
        await configuration_request(entry, 'save', {'configuration':dict(call.data['configuration'])})
        current = await configuration_request(entry,'get',{})
        return _configuration_response(current['configuration'])

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
    async def profile_resources(call: ServiceCall) -> dict[str, Any]:
        entry = _entry_for_call(hass, call)
        coordinator = getattr(entry, 'runtime_data', None)
        if coordinator is None:
            raise ValueError('SHS Energy is not loaded for that entry')
        return await coordinator.async_profile(call.data.get('allocation_seconds', 0))

    async_register_admin_service(hass, DOMAIN, 'profile_resources', profile_resources,
        schema=vol.Schema({vol.Optional('entry_id'):str,
            vol.Optional('allocation_seconds', default=0):vol.All(vol.Coerce(int),vol.Range(min=0,max=300))}),
        supports_response=SupportsResponse.ONLY)
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load only the seeded physical gateway; policy and archives live in the app."""
    from .gateway import open_gateway
    from .gateway_projection import GatewayProjection
    try:
        service = await open_gateway(hass, entry)
    except (OSError, ValueError, RuntimeError) as error:
        raise ConfigEntryNotReady(f"SHS app gateway is not ready: {error}") from error
    coordinator = GatewayProjection(hass, entry, service)
    entry.runtime_data = coordinator
    service.source.projection = coordinator
    try:
        await coordinator.restore()
    except BaseException:
        await _async_stop_runtime(coordinator)
        raise

    async def platforms():
        # Do not interpret an absent app projection as an empty device inventory:
        # that would remove the existing execution-mode selects during startup.
        await coordinator.ready.wait()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        coordinator.platforms_loaded = True

    entry.async_create_background_task(hass, platforms(), name='shs_gateway_entities')
    async def stopped(_event=None):
        await _async_stop_runtime(coordinator)
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stopped))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Publish canonical edits to the app without reloading physical ownership."""
    try:
        await entry.runtime_data.service.source.refresh_configuration()
    finally:
        set_reloading(hass, entry, False)


async def _async_stop_runtime(coordinator):
    service = coordinator.service
    if service.closed:
        return
    service.source.closed = True
    try:
        await service.close()
    finally:
        await service.stream.close()
        await coordinator.hass.async_add_executor_job(service.stream.journal.close)
        coordinator.hass.data.get('shs_energy_gateways', {}).pop(coordinator.entry.entry_id, None)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await _async_stop_runtime(entry.runtime_data)
    if not entry.runtime_data.platforms_loaded:
        return True
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
