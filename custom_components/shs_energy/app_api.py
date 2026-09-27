"""Authenticated snapshot bridge for the SHS Home Assistant app."""
from copy import deepcopy
from datetime import datetime, timezone
import math

from homeassistant.components.http import HomeAssistantView, KEY_HASS, require_admin

from .api_contract import INTEGRATION_VERSION
from .app_projection import PROTOCOL_VERSION, schedule, attention_links
from .shs_core.const import DOMAIN


class AppSnapshotView(HomeAssistantView):
    url = "/api/shs_energy/app"
    name = "api:shs_energy:app"

    @require_admin
    async def get(self, request):
        hass = request.app[KEY_HASS]
        entries = hass.config_entries.async_entries(DOMAIN)
        result = {"protocol": PROTOCOL_VERSION, "integration_version": INTEGRATION_VERSION,
                  "sampled_at": datetime.now(timezone.utc).isoformat(), "entries": []}
        for entry in entries:
            state = entry.state.value
            row = {"id": entry.entry_id, "title": entry.title, "state": state}
            if state == "loaded":
                coordinator = entry.runtime_data
                operation = deepcopy(coordinator.operational_status)
                row.update(operation=operation, schedule=schedule(coordinator.optimisation_plan, operation),
                           controllers=deepcopy(coordinator.controller.status),
                           attention=attention_links(coordinator.attention_items, entry.entry_id),
                           measurements={})
                for name, key, unit in (
                    ("house", "house_consumption_power_entity", "W"),
                    ("solar", "solar_production_power_entity", "W"),
                    ("grid", "grid_power_entity", "W"),
                    ("battery", "battery_power_measurement_entity", "W"),
                    ("battery_soc", "battery_soc_entity", "%"),
                ):
                    entity = hass.states.get(entry.options.get(key, ""))
                    value = None
                    if entity is not None:
                        source_unit = entity.attributes.get("unit_of_measurement")
                        try:
                            raw = float(entity.state)
                            if math.isfinite(raw) and source_unit in ({"W", "kW"} if unit == "W" else {"%"}):
                                value = raw * (1000 if source_unit == "kW" else 1)
                        except (TypeError, ValueError):
                            pass
                    row["measurements"][name] = {"value": value, "unit": unit,
                        "entity_id": entity.entity_id if entity else None,
                        "observed_at": entity.last_updated.isoformat() if entity else None}
            result["entries"].append(row)
        response = self.json(result)
        response.headers["Cache-Control"] = "no-store"
        return response
