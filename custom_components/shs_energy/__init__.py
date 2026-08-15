"""Smart Home Solutions Energy — pushes daily energy categories to the SHS portal."""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change

from .api import ShsApiClient
from .config_panel import async_apply_configuration, async_register_config_panel
from .const import (
    CONFIGURATION_SCHEMA_VERSION,
    CONFIGURABLE_CATEGORIES,
    CONF_BASE_URL,
    CONF_DEVICE_TOKEN,
    PUSH_TIME_HOUR,
    PUSH_TIME_MINUTE,
    PRICE_REFRESH_SECOND,
    RETIRED_PLANNING_OPTIONS,
    RETIRED_SUPPLIER_PRICE_OPTIONS,
    OPTIMISATION_PUSH_SECOND,
    OPTIMISATION_STARTUP_DELAY_SECONDS,
    OPTIMISATION_STARTUP_ISSUE_GRACE_SECONDS,
    OPTIMISATION_STARTUP_RETRY_SECONDS,
    PRICE_BACKFILL_MAX_DAYS,
    OPT_AUTOMATIC_SETUP,
    OPT_CONFIGURATION_SCHEMA_VERSION,
    OPT_DEVICE_CONTROL_MAPPINGS,
    OPT_DISCOVERY_EVIDENCE,
    OPT_LEGACY_CONFIGURATION_ARCHIVE,
    OPT_PLANNING_MODE,
    OPT_PREFIX_ENTITIES,
    DOMAIN,
)
from .configuration import (
    async_discover_configuration,
    entity_area_id,
)
from .coordinator import ShsStatusCoordinator
from .device_controls import (
    migrate_device_control_mappings,
    recover_legacy_ev_options,
)

PLATFORMS: list[Platform] = [Platform.SENSOR]

ShsEnergyConfigEntry = ConfigEntry[ShsStatusCoordinator]

SERVICE_DISCOVER_CONFIGURATION = "discover_configuration"
SERVICE_APPLY_CONFIGURATION = "apply_configuration"
SERVICE_BACKFILL_PRICES = "backfill_prices"


async def _async_delayed_startup_optimisation_push(
    coordinator: ShsStatusCoordinator,
) -> None:
    """Wait for state-providing integrations, retrying only startup gaps."""
    await asyncio.sleep(OPTIMISATION_STARTUP_DELAY_SECONDS)
    attempts = 1 + max(
        0,
        (
            OPTIMISATION_STARTUP_ISSUE_GRACE_SECONDS
            - OPTIMISATION_STARTUP_DELAY_SECONDS
        )
        // OPTIMISATION_STARTUP_RETRY_SECONDS,
    )
    for attempt in range(attempts):
        await coordinator.async_optimisation_push(force_plan=True)
        if not coordinator.optimisation_input_gap_is_transient():
            return
        if attempt + 1 < attempts:
            await asyncio.sleep(OPTIMISATION_STARTUP_RETRY_SECONDS)


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


async def async_setup_entry(hass: HomeAssistant, entry: ShsEnergyConfigEntry) -> bool:
    """Set up from a config entry."""
    migrated_options = dict(entry.options)
    options_changed = False
    legacy_archive = migrated_options.get(OPT_LEGACY_CONFIGURATION_ARCHIVE, {})
    legacy_archive = dict(legacy_archive) if isinstance(legacy_archive, dict) else {}
    for key in RETIRED_SUPPLIER_PRICE_OPTIONS.intersection(migrated_options):
        legacy_archive.setdefault(key, migrated_options[key])
        migrated_options.pop(key)
        options_changed = True
    for key in RETIRED_PLANNING_OPTIONS.intersection(migrated_options):
        legacy_archive.setdefault(key, migrated_options[key])
        migrated_options.pop(key)
        options_changed = True
    if legacy_archive:
        migrated_options[OPT_LEGACY_CONFIGURATION_ARCHIVE] = legacy_archive
    mappings = migrated_options.get(OPT_DEVICE_CONTROL_MAPPINGS)
    if isinstance(mappings, dict):
        mapped_entity_ids = {
            entity_id
            for mapping in mappings.values()
            if isinstance(mapping, dict)
            for key, value in mapping.items()
            if key.endswith("_entity_id") or key.endswith("_entity_ids")
            for entity_id in (value if isinstance(value, list) else [value])
            if isinstance(entity_id, str) and entity_id
        }
        entity_area_ids = {
            entity_id: area_id
            for entity_id in mapped_entity_ids
            if (area_id := entity_area_id(hass, entity_id)) is not None
        }
        entity_limits = {
            state.entity_id: (
                state.attributes.get("min"),
                state.attributes.get("max"),
            )
            for state in hass.states.async_all()
        }
        migrated_mappings, mappings_changed = migrate_device_control_mappings(
            mappings,
            entity_area_ids=entity_area_ids,
            entity_limits=entity_limits,
        )
        migrated_options[OPT_DEVICE_CONTROL_MAPPINGS] = migrated_mappings
        options_changed = options_changed or mappings_changed
        migrated_options, ev_options_changed = recover_legacy_ev_options(
            migrated_options,
            migrated_mappings,
        )
        options_changed = options_changed or ev_options_changed
    if (
        migrated_options.get(OPT_CONFIGURATION_SCHEMA_VERSION)
        != CONFIGURATION_SCHEMA_VERSION
    ):
        migrated_options[OPT_CONFIGURATION_SCHEMA_VERSION] = (
            CONFIGURATION_SCHEMA_VERSION
        )
        options_changed = True
    if options_changed:
        hass.config_entries.async_update_entry(
            entry,
            options=migrated_options,
        )
    client = ShsApiClient(
        async_get_clientsession(hass),
        entry.data[CONF_BASE_URL],
        entry.data[CONF_DEVICE_TOKEN],
    )
    coordinator = ShsStatusCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

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
    # The public market and supplier terms are native quarter-hour series. Keep
    # total-price sensors aligned even when the integration was loaded between
    # quarter boundaries.
    entry.async_on_unload(
        async_track_time_change(
            hass,
            coordinator.async_price_refresh,
            minute=[0, 15, 30, 45],
            second=PRICE_REFRESH_SECOND,
        )
    )
    # Quarter-hour exchange. Recorder samples are aggregated locally; the
    # website receives one completed 15-minute row, never per-second history.
    entry.async_on_unload(
        async_track_time_change(
            hass,
            coordinator.async_optimisation_push,
            minute=[0, 15, 30, 45],
            second=OPTIMISATION_PUSH_SECOND,
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
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
