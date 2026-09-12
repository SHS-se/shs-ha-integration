"""Downloadable network diagnostics without home data or authentication secrets."""
from __future__ import annotations

from typing import Any
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api_contract import INTEGRATION_VERSION


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry,
) -> dict[str, Any]:
    """Expose counters only; do not serialize the config entry or cached plan."""
    return {
        "integration_version": INTEGRATION_VERSION,
        "network_traffic": entry.runtime_data.client.traffic.snapshot(),
        "controller_metrics": entry.runtime_data.controller.metrics.snapshot(),
    }
