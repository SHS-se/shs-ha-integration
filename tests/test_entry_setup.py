"""Exercise pairing-data repair through the real setup entry point with adapters."""

from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).parents[1] / "custom_components/shs_energy"
sys.path.insert(0, str(ROOT))
from const import CONFIG_ENTRY_VERSION, CONF_BASE_URL, CONF_DEVICE_TOKEN, CONF_HOME_ID
from control_setup import opaque_id

HOME_ID = "11111111-1111-4111-8111-111111111111"


class ApiError(Exception):
    pass


class AuthError(ApiError):
    pass


class EntryError(Exception):
    pass


class EntryNotReady(EntryError):
    pass


class EntryAuthFailed(EntryError):
    pass


class SetupReached(Exception):
    """Stop before loading control journals or scheduling hardware work."""


def setup_hook():
    tree = ast.parse((ROOT / "__init__.py").read_text())
    module = ast.parse("from __future__ import annotations")
    module.body.extend(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                       and node.name in {"_async_complete_pairing_data", "async_setup_entry"})
    client = SimpleNamespace(status=AsyncMock(return_value={"home_id": HOME_ID}))
    namespace = {
        "asyncio": asyncio,
        "CONF_BASE_URL": CONF_BASE_URL,
        "CONF_DEVICE_TOKEN": CONF_DEVICE_TOKEN,
        "CONF_HOME_ID": CONF_HOME_ID,
        "ShsApiError": ApiError,
        "ShsAuthError": AuthError,
        "ConfigEntryError": EntryError,
        "ConfigEntryNotReady": EntryNotReady,
        "ConfigEntryAuthFailed": EntryAuthFailed,
        "opaque_id": opaque_id,
        "async_get_clientsession": Mock(),
        "ShsApiClient": Mock(return_value=client),
        "ShsStatusCoordinator": Mock(side_effect=lambda *args: SimpleNamespace()),
        "ScheduledController": Mock(),
        "Store": Mock(),
        "VerifiedBindingStore": Mock(),
        "ControlSetup": Mock(side_effect=SetupReached),
    }
    exec(compile(module, str(ROOT / "__init__.py"), "exec"), namespace)
    return namespace, client


class EntrySetupTests(unittest.IsolatedAsyncioTestCase):
    def entry(self, **data):
        return SimpleNamespace(
            entry_id="paired-entry", version=CONFIG_ENTRY_VERSION,
            data={CONF_BASE_URL: "https://example.test/functions/v1",
                  CONF_DEVICE_TOKEN: "existing-token", "device_token_id": "existing-id",
                  "customer_name": "Existing customer", **data},
            options={"planning_mode": "live", "entities_grid_import": ["sensor.import"]},
        )

    def hass(self):
        def update(entry, **fields):
            for key, value in fields.items():
                setattr(entry, key, value)
        return SimpleNamespace(data={}, config_entries=SimpleNamespace(
            async_update_entry=Mock(side_effect=update)))

    async def test_old_pairing_is_repaired_before_control_setup_and_only_once(self):
        ns, client = setup_hook()
        entry, hass = self.entry(), self.hass()
        original_data, original_options = deepcopy(entry.data), deepcopy(entry.options)
        for _ in range(2):
            with self.assertRaises(SetupReached):
                await ns["async_setup_entry"](hass, entry)
            self.assertEqual(ns["ControlSetup"].call_args.args[1], HOME_ID)
        self.assertEqual(entry.data, {**original_data, CONF_HOME_ID: HOME_ID})
        self.assertEqual(entry.options, original_options)
        self.assertEqual(entry.version, CONFIG_ENTRY_VERSION)
        client.status.assert_awaited_once()
        hass.config_entries.async_update_entry.assert_called_once()
        ns["ShsApiClient"].assert_called_with(
            ns["async_get_clientsession"].return_value,
            original_data[CONF_BASE_URL], original_data[CONF_DEVICE_TOKEN],
        )

    async def test_current_pairing_keeps_offline_local_startup(self):
        ns, client = setup_hook()
        entry, hass = self.entry(home_id=HOME_ID), self.hass()
        client.status.side_effect = ApiError("offline")
        with self.assertRaises(SetupReached):
            await ns["async_setup_entry"](hass, entry)
        client.status.assert_not_awaited()
        hass.config_entries.async_update_entry.assert_not_called()
        self.assertEqual(ns["ControlSetup"].call_args.args[1], HOME_ID)

    async def test_network_failure_requests_retry_without_initializing_controllers(self):
        ns, client = setup_hook()
        entry, hass = self.entry(), self.hass()
        client.status.side_effect = ApiError("offline")
        with self.assertRaisesRegex(EntryNotReady, "paired home identity"):
            await ns["async_setup_entry"](hass, entry)
        hass.config_entries.async_update_entry.assert_not_called()
        ns["ShsStatusCoordinator"].assert_not_called()
        ns["ScheduledController"].assert_not_called()
        self.assertNotIn(CONF_HOME_ID, entry.data)
        # The next HA attempt repairs the same entry once the service recovers.
        client.status.side_effect = None
        with self.assertRaises(SetupReached):
            await ns["async_setup_entry"](hass, entry)
        self.assertEqual(entry.data[CONF_HOME_ID], HOME_ID)

    async def test_rejected_credentials_are_an_authentication_error(self):
        ns, client = setup_hook()
        entry, hass = self.entry(), self.hass()
        client.status.side_effect = AuthError("device token rejected")
        with self.assertRaises(EntryAuthFailed):
            await ns["async_setup_entry"](hass, entry)
        hass.config_entries.async_update_entry.assert_not_called()
        ns["ShsStatusCoordinator"].assert_not_called()

    async def test_invalid_server_identity_is_never_saved_or_used(self):
        for status in ({}, {"home_id": None}, {"home_id": ""},
                       {"home_id": "not-a-home"}, {"home_id": 123},
                       {"home_id": "11111111-1111-1111-8111-111111111111"}):
            with self.subTest(status=status):
                ns, client = setup_hook()
                entry, hass = self.entry(), self.hass()
                client.status.return_value = status
                with self.assertRaisesRegex(EntryError, "valid paired home_id"):
                    await ns["async_setup_entry"](hass, entry)
                hass.config_entries.async_update_entry.assert_not_called()
                ns["ShsStatusCoordinator"].assert_not_called()
                self.assertNotIn(CONF_HOME_ID, entry.data)

    async def test_failed_entry_update_cannot_initialize_control_setup(self):
        ns, client = setup_hook()
        entry, hass = self.entry(), self.hass()
        hass.config_entries.async_update_entry.side_effect = RuntimeError("entry update failed")
        with self.assertRaisesRegex(RuntimeError, "entry update failed"):
            await ns["async_setup_entry"](hass, entry)
        ns["ShsStatusCoordinator"].assert_not_called()
        self.assertNotIn(CONF_HOME_ID, entry.data)
