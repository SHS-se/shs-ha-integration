"""Presentation progress that outlives a config-entry coordinator reload."""

RELOADS = "shs_energy_configuration_reloads"


def set_reloading(hass, entry, active):
    entries = hass.data.setdefault(RELOADS, set())
    if active:
        entries.add(entry.entry_id)
    else:
        entries.discard(entry.entry_id)


def refresh_in_progress(hass, entry):
    if (entry.entry_id in hass.data.get(RELOADS, ())
            or entry.entry_id in hass.data.get("shs_energy_configuration_writes", ())):
        return True
    coordinator = getattr(entry, "runtime_data", None)
    return bool(coordinator and (
        coordinator._recovering or coordinator._push_lock.locked()
    ))
