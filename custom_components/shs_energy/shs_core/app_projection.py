"""Read-only app contract. No control, configuration or persistence mutations."""
from copy import deepcopy
from urllib.parse import urlencode

PROTOCOL_VERSION = 2
SLOT_FIELDS = (
    "start", "duration_hours", "binding", "pv_w", "base_w", "load_w",
    "shadow_import_sek_per_kwh", "shadow_export_sek_per_kwh",
    "grid_import_w", "grid_export_w", "battery_charge_w", "battery_discharge_w",
    "battery_soc", "ev_soc", "import_cost_sek", "export_revenue_sek",
    "device_loads_w", "device_commands", "battery_command", "ev_w", "pool_w",
    "boiler_expected_w", "room_heating_w",
)


def schedule(plan, operation):
    """Keep household forecasts and execution forecasts explicitly separate."""
    if not plan or operation["state"] not in {"ready", "advisory_only"}:
        return None
    def series(source):
        return [{**{key: deepcopy(slot.get(key)) for key in SLOT_FIELDS},
                 "temperatures_c": {"pool": slot.get("pool_temperature_c")}}
                for slot in source["plans"]["priority"]["slots"]]
    # Adapt published temperatures to named series; the chart need not know
    # which device produced them. Missing forecasts stay missing, never inferred.
    pool = plan.get("pool")
    from_target = any(store.get("key") == "pool" and store.get("derivation") is not None
                      for store in plan.get("resolved_value_stores", []))
    stop = pool.get("stop_temperature_c") if pool else None
    temperatures = [{"key": "pool", "name": "Pool",
                     # The planner's pool stop is the owner's target + 2 °C,
                     # exactly as in the website's plannedPoolTarget projection.
                     "target_c": stop - 2 if from_target and stop is not None else None}]
    if not pool and not any(slot.get("pool_temperature_c") is not None
                            for slot in plan["plans"]["priority"]["slots"]):
        temperatures = []
    return {
        **{key: plan.get(key) for key in ("plan_id", "issued_at", "valid_until", "binding_until", "timezone")},
        "temperatures": temperatures,
        "currency": "SEK",  # The current API contract explicitly uses *_sek fields.
        "household": series(plan),
        "execution": series(plan["execution_plan"]) if plan.get("execution_plan") else None,
        "devices": [{key: d.get(key) for key in ("key", "name", "category")}
                    for d in plan.get("device_models", [])],
    }


def attention_links(items, entry_id):
    """Carry every structured correction target to the app editor."""
    result = deepcopy(items)
    for item in result:
        for field in item.get("fix", {}).get("fields", []):
            field["url"] = "#settings?" + urlencode({
                "config_entry": entry_id, "field": field["key"],
                "scope": field.get("scope", "configuration"),
                "device": field.get("device_key", ""),
            })
    return result
