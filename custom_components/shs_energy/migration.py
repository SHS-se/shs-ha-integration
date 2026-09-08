"""Pure option conversion; the entry adapter supplies registry observations."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

if __package__:
    from . import const as c
    from .device_controls import migrate_device_control_mappings, recover_legacy_ev_options
else:  # Flat imports used by the dependency-free test suite.
    import const as c
    from device_controls import migrate_device_control_mappings, recover_legacy_ev_options


def mapped_entity_ids(options: dict[str, Any]) -> set[str]:
    """Entities whose registry areas can supply a historical room association."""
    mappings = options.get(c.OPT_DEVICE_CONTROL_MAPPINGS)
    if not isinstance(mappings, dict):
        return set()
    return {
        entity_id
        for mapping in mappings.values()
        if isinstance(mapping, dict)
        for key, value in mapping.items()
        if key.endswith("_entity_id") or key.endswith("_entity_ids")
        for entity_id in (value if isinstance(value, list) else [value])
        if isinstance(entity_id, str) and entity_id
    }


def migrate_options(
    options: dict[str, Any],
    *,
    entity_area_ids: dict[str, str] | None = None,
    entity_limits: dict[str, tuple[Any, Any]] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Convert persisted options without defaults, IO, or changing the input.

    Active EV electrical settings were accidentally retired in beta.20.
    Recover an archived value only when no explicit current value exists,
    then remove that archive entry so subsequent starts cannot resurrect it.
    Remaining legacy retirement is unchanged until the Phase 1 migration.
    """
    migrated = deepcopy(options)
    archive = migrated.get(c.OPT_LEGACY_CONFIGURATION_ARCHIVE, {})
    archive = dict(archive) if isinstance(archive, dict) else {}
    for key in (c.OPT_EV_PHASE_COUNT, c.OPT_EV_CHARGE_EFFICIENCY):
        if key in archive:
            if key not in migrated:
                migrated[key] = archive[key]
            del archive[key]

    retired = c.RETIRED_SUPPLIER_PRICE_OPTIONS | c.RETIRED_PLANNING_OPTIONS
    for key in retired.intersection(migrated):
        archive.setdefault(key, migrated.pop(key))
    if archive:
        migrated[c.OPT_LEGACY_CONFIGURATION_ARCHIVE] = archive
    else:
        migrated.pop(c.OPT_LEGACY_CONFIGURATION_ARCHIVE, None)

    mappings = migrated.get(c.OPT_DEVICE_CONTROL_MAPPINGS)
    if isinstance(mappings, dict):
        mappings, _changed = migrate_device_control_mappings(
            mappings, entity_area_ids=entity_area_ids, entity_limits=entity_limits,
        )
        migrated[c.OPT_DEVICE_CONTROL_MAPPINGS] = mappings
        migrated, _changed = recover_legacy_ev_options(migrated, mappings)
    migrated[c.OPT_CONFIGURATION_SCHEMA_VERSION] = c.CONFIGURATION_SCHEMA_VERSION
    return migrated, migrated != options
