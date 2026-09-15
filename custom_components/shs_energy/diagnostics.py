"""Downloadable operational diagnostics without authentication secrets."""
from __future__ import annotations

from typing import Any
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api_contract import INTEGRATION_VERSION


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry,
) -> dict[str, Any]:
    """Expose operational snapshots without serializing the config entry or plan."""
    return {
        "integration_version": INTEGRATION_VERSION,
        "network_traffic": entry.runtime_data.client.traffic.snapshot(),
        "battery_policy_delivery": entry.runtime_data.battery_policy_exchange.snapshot(),
        "battery_live_inputs": entry.runtime_data.battery_live_inputs.snapshot(),
        "battery_writer": entry.runtime_data.battery_writer.snapshot(),
        "controller_metrics": entry.runtime_data.controller.metrics.snapshot(),
    }
