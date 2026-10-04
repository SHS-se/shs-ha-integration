"""Captured originals must survive writes, interruptions and reloads."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import unittest
sys.path.append(str(Path(__file__).parents[1]/'custom_components/shs_energy'))
from shs_core.device_ownership import DeviceOwnership, decode_ownership


class Store:
    def __init__(self):
        self.value = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
    async def async_save(self, value):
        self.started.set()
        await self.release.wait()
        self.value = deepcopy(value)
    async def async_load(self):
        return deepcopy(self.value)


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_is_durable_and_does_not_replace_old_mapping(self):
        store = Store()
        owner = DeviceOwnership(store)
        await owner.load()
        first = await owner.capture('pool', {'binding': 'switch.old'}, {'switch.old': 'off'})
        await owner.capture('pool', {'binding': 'switch.new'}, {'switch.new': 'on'})
        restored = DeviceOwnership(store)
        await restored.load()
        self.assertEqual(restored.records['pool'], first)
        first['originals']['switch.old'] = 'changed in memory'
        self.assertEqual(restored.records['pool']['originals']['switch.old'], 'off')

    async def test_save_captures_a_private_value_before_awaiting(self):
        store = Store()
        owner = DeviceOwnership(store)
        owner.overrides['pool'] = 'before'
        store.release.clear()
        writing = asyncio.create_task(owner.save())
        await store.started.wait()
        owner.overrides['pool'] = 'after'
        store.release.set()
        await writing
        self.assertEqual(store.value['overrides']['pool'], 'before')
        await owner.save()
        self.assertEqual(store.value['overrides']['pool'], 'after')

    async def test_dormant_decoder_discards_retired_run_clocks_without_changing_originals(self):
        source = {'records': {'battery': {'options': {}, 'originals': {},
                    'last_commands': {'number.limit': 0}}},
                  'runs': {'heater': {'since': '2026-09-27T12:00:00+00:00',
                            'minimum_seconds': 3600, 'active': True, 'pending_start': True}}}
        before = deepcopy(source)
        records, _, _ = decode_ownership(source)
        self.assertEqual(source, before)
        records['battery']['options']['new'] = True
        self.assertEqual(source, before)
        for value in (None, [], {'records': []}, {'records': {'pool': {}}}, {'unknown': {}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_ownership(value)
