"""Smart Home Solutions Energy — pushes daily energy categories to the SHS portal."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change

from . import const as shs_const
from .api import ShsApiClient
from .const import (
    CONFIGURABLE_CATEGORIES,
    CONF_BASE_URL,
    CONF_DEVICE_TOKEN,
    PUSH_TIME_HOUR,
    PUSH_TIME_MINUTE,
    OPTIMISATION_PUSH_SECOND,
    OPT_AUTOMATIC_SETUP,
    OPT_BOILER_DEFERRABLE_CONFIRMED,
    OPT_BOILER_PLANNING_ENABLED,
    OPT_CONFIGURATION_REVIEWED_AT,
    OPT_DISCOVERY_EVIDENCE,
    OPT_EV_DEFERRABLE_CONFIRMED,
    OPT_EV_ELECTRICAL_CONFIRMED,
    OPT_EV_PLANNING_ENABLED,
    OPT_PLANNING_MODE,
    OPT_POOL_DEFERRABLE_CONFIRMED,
    OPT_POOL_PLANNING_ENABLED,
    OPT_PREFIX_ENTITIES,
    PLANNING_MODE_LIVE,
    PLANNING_MODE_DISABLED,
    DOMAIN,
)
from .configuration import (
    async_discover_configuration,
    optimisation_defaults,
    resolved_options,
)
from .coordinator import ShsStatusCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

ShsEnergyConfigEntry = ConfigEntry[ShsStatusCoordinator]

SERVICE_DISCOVER_CONFIGURATION = "discover_configuration"
SERVICE_APPLY_CONFIGURATION = "apply_configuration"


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
    """Register a validated configuration surface for UI actions and MCP."""
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
        incoming = dict(call.data["configuration"])
        allowed = {
            value for name, value in vars(shs_const).items()
            if name.startswith("OPT_") and isinstance(value, str)
        } | set(optimisation_defaults(hass)) | {
            f"{OPT_PREFIX_ENTITIES}{category}" for category in CONFIGURABLE_CATEGORIES
        }
        # Existing keys are also allowed so an advanced option introduced by
        # this installed version can be round-tripped by an MCP client.
        allowed.update(entry.options)
        unknown = sorted(set(incoming) - allowed)
        if unknown:
            raise ValueError("unknown configuration keys: " + ", ".join(unknown))
        options = resolved_options(hass, {**dict(entry.options), **incoming})
        if options.get(OPT_PLANNING_MODE) not in {
            PLANNING_MODE_DISABLED, PLANNING_MODE_LIVE
        }:
            raise ValueError("planning_mode must be disabled or live")
        confirmations = (
            (
                OPT_POOL_PLANNING_ENABLED,
                OPT_POOL_DEFERRABLE_CONFIRMED,
                "pool deferrability",
            ),
            (
                OPT_BOILER_PLANNING_ENABLED,
                OPT_BOILER_DEFERRABLE_CONFIRMED,
                "water-heater deferrability",
            ),
            (
                OPT_EV_PLANNING_ENABLED,
                OPT_EV_DEFERRABLE_CONFIRMED,
                "EV deferrability",
            ),
            (
                OPT_EV_PLANNING_ENABLED,
                OPT_EV_ELECTRICAL_CONFIRMED,
                "EV phase count and voltage",
            ),
        )
        unconfirmed = [
            label
            for enabled_key, confirmation_key, label in confirmations
            if options.get(enabled_key) and not options.get(confirmation_key)
        ]
        if unconfirmed:
            raise ValueError(
                "explicit confirmation required: " + ", ".join(unconfirmed)
            )
        if options.get(OPT_PLANNING_MODE) == PLANNING_MODE_LIVE:
            options[OPT_CONFIGURATION_REVIEWED_AT] = datetime.now(
                timezone.utc
            ).isoformat()
        hass.config_entries.async_update_entry(entry, options=options)
        return _configuration_response(options)

    hass.services.async_register(
        DOMAIN,
        SERVICE_DISCOVER_CONFIGURATION,
        discover,
        schema=vol.Schema({vol.Optional("entry_id"): str}),
        supports_response=SupportsResponse.ONLY,
    )
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
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ShsEnergyConfigEntry) -> bool:
    """Set up from a config entry."""
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
        coordinator.async_optimisation_push(force_plan=True),
        name="shs_energy_startup_optimisation_push",
    )

    # React to a changed category→sensor mapping or price entity.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: ShsEnergyConfigEntry
) -> None:
    # A full reload, not just a re-push: entities subscribe to the configured
    # supplier price sensor when they are added, so an entity picked after
    # setup would never be watched and its total would only move on the hourly
    # coordinator poll. Setting up again re-pushes on its own.
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ShsEnergyConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
