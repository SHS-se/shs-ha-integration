"""Read-only Sigen integration bridge for verified native register reports."""


async def refresh_sigen_readback(hass, entity_ids):
    """Read the actual Sigen coordinator, including unchanged holding registers.

    Refresh explicitly (not the debounced request-refresh path), check that every
    register exists, then publish the values. Missing registers must not inherit
    the vendor number entity's default zero. This port never writes Modbus.
    """
    from homeassistant.helpers.entity_platform import async_get_platforms
    expected=("plant_remote_ems_control_mode", "plant_ess_max_charging_limit",
              "plant_ess_max_discharging_limit")
    entities={entity_id:entity for platform in async_get_platforms(hass,"sigen")
              for entity_id,entity in platform.entities.items() if entity_id in entity_ids}
    if len(entities)!=3:
        raise ValueError("Sigen mode and ESS charge/discharge entities are required")
    selected=[entities[key] for key in entity_ids]
    coordinator=selected[0].coordinator
    if any(entity.coordinator is not coordinator or entity.entity_description.key!=key
           for entity,key in zip(selected,expected)):
        raise ValueError("Sigen control registers do not share the configured plant")
    await coordinator.async_refresh()
    plant=(coordinator.data or {}).get("plant",{})
    if not coordinator.last_update_success or any(plant.get(key) is None for key in expected):
        raise ValueError("Sigen register refresh failed or returned incomplete data")
    for entity in selected:
        if not entity.available:
            raise ValueError("Sigen remote control is unavailable")
        entity.async_write_ha_state()
