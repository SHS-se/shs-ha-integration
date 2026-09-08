"""Execute the actual thin entry hook with injected adapters, without loading HA."""

from __future__ import annotations

import ast
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).parents[1] / "custom_components/shs_energy"
sys.path.insert(0, str(ROOT))
from const import CONFIG_ENTRY_VERSION
from migration import migrate_options, mapped_entity_ids


def entry_hook():
    # Load just the adapter function, not the HA package or fake HA modules.
    tree = ast.parse((ROOT / "__init__.py").read_text())
    hook = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "async_migrate_entry")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), hook], type_ignores=[])
    namespace = {
        "CONFIG_ENTRY_VERSION": CONFIG_ENTRY_VERSION,
        "migrate_options": Mock(wraps=migrate_options),
        "mapped_entity_ids": mapped_entity_ids,
        "entity_area_id": lambda _hass, _entity: None,
    }
    exec(compile(ast.fix_missing_locations(module), str(ROOT / "__init__.py"), "exec"), namespace)
    return namespace


class EntryMigrationHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_commits_version_and_options_once(self):
        ns = entry_hook()
        entry = SimpleNamespace(version=1, options={"_legacy_configuration_archive": {"ev_phase_count": 1}}, data={"token": "unchanged"})
        def update(target, **fields):
            for key, value in fields.items():
                setattr(target, key, value)
        adapter = Mock(side_effect=update)
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=adapter))
        self.assertTrue(await ns["async_migrate_entry"](hass, entry))
        self.assertEqual(entry.version, CONFIG_ENTRY_VERSION)
        self.assertEqual(entry.options["ev_phase_count"], 1)
        self.assertNotIn("_legacy_configuration_archive", entry.options)
        self.assertEqual(entry.data, {"token": "unchanged"})
        self.assertTrue(await ns["async_migrate_entry"](hass, entry))
        adapter.assert_called_once()
        ns["migrate_options"].assert_called_once()

    async def test_version_two_moves_room_source_once(self):
        ns = entry_hook()
        entry = SimpleNamespace(version=2, options={"device_control_mappings": {
            "sensor.heater": {"control_type": "setpoint", "room_area_id": "office",
                "actuator_entity_ids": ["climate.heater"], "temperature_entity_id": "sensor.temp"}}})
        def update(target, **fields):
            for key, value in fields.items():
                setattr(target, key, value)
        adapter = Mock(side_effect=update)
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=adapter))
        await ns["async_migrate_entry"](hass, entry)
        self.assertEqual(entry.version, CONFIG_ENTRY_VERSION)
        self.assertEqual(entry.options["rooms"]["office"]["temperature_entity_id"], "sensor.temp")
        self.assertNotIn("temperature_entity_id", entry.options["device_control_mappings"]["sensor.heater"])
        await ns["async_migrate_entry"](hass, entry)
        adapter.assert_called_once()

    async def test_future_versions_are_not_downgraded(self):
        ns = entry_hook()
        self.assertFalse(await ns["async_migrate_entry"](None, SimpleNamespace(version=CONFIG_ENTRY_VERSION + 1)))
        ns["migrate_options"].assert_not_called()

    async def test_failed_conversion_does_not_commit_a_new_version(self):
        ns = entry_hook()
        ns["migrate_options"].side_effect = ValueError("invalid stored data")
        entry = SimpleNamespace(version=1, options={})
        update = Mock()
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=update))
        with self.assertRaises(ValueError):
            await ns["async_migrate_entry"](hass, entry)
        self.assertEqual(entry.version, 1)
        update.assert_not_called()
