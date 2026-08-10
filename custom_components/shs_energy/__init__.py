"""Smart Home Solutions Energy — pushes daily energy categories to the SHS portal."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change

from .api import ShsApiClient
from .const import (
    CONF_BASE_URL,
    CONF_DEVICE_TOKEN,
    PUSH_TIME_HOUR,
    PUSH_TIME_MINUTE,
    OPTIMISATION_PUSH_SECOND,
)
from .coordinator import ShsStatusCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

ShsEnergyConfigEntry = ConfigEntry[ShsStatusCoordinator]


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
