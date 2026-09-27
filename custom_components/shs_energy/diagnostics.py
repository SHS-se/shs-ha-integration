"""Downloadable operational diagnostics without authentication secrets."""
from __future__ import annotations

from typing import Any
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .shs_core.api_contract import INTEGRATION_VERSION


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry,
) -> dict[str, Any]:
    """Expose operational snapshots without serializing the config entry or plan."""
    return {'integration_version':INTEGRATION_VERSION, **await entry.runtime_data.async_diagnostics()}
