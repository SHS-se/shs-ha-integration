"""The coordinator's durable record is parsed once and refreshed only by its own saves."""
import asyncio
import json
from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from durable_record import DurableRecord


class FileStore:
    """Round-trips through JSON like Home Assistant's store does."""
    def __init__(self, record=None):
        self.file = None if record is None else json.dumps(record)
        self.loads = 0
        self.release = None

    async def async_load(self):
        self.loads += 1
        text = self.file
        if self.release is not None:
            await self.release.wait()
        return None if text is None else json.loads(text)

    async def async_save(self, record):
        self.file = json.dumps(record)


class DurableRecordTests(unittest.IsolatedAsyncioTestCase):
    def record(self, store):
        return DurableRecord(store, json.dumps, json.loads)

    async def test_reads_parse_the_file_once(self):
        store = FileStore({'plan': {'slots': [1, 2]}})
        record = self.record(store)
        first = await record.async_read()
        for _ in range(20):
            self.assertIs(await record.async_read(), first)
        self.assertEqual(store.loads, 1)
        self.assertEqual(await self.record(FileStore()).async_read(), {})

    async def test_writers_change_a_private_copy_until_they_save(self):
        store = FileStore({'attempt': 1, 'nested': {'value': 1}})
        record = self.record(store)
        shared = await record.async_read()
        draft = await record.async_load()
        draft['attempt'] = 2
        draft['nested']['value'] = 2
        self.assertEqual(await record.async_read(), {'attempt': 1, 'nested': {'value': 1}})
        await record.async_save(draft)
        self.assertEqual(await record.async_read(), json.loads(store.file))
        self.assertEqual(shared, {'attempt': 1, 'nested': {'value': 1}}, 'earlier readers keep what they read')
        # Later edits to the saved dict are not visible until they are saved again.
        draft['attempt'] = 3
        self.assertEqual((await record.async_read())['attempt'], 2)
        self.assertEqual(store.loads, 1)

    async def test_readers_see_what_a_reload_would_return(self):
        record = self.record(FileStore({}))
        await record.async_save({'pair': (1, 2), 1: 'numeric key'})
        self.assertEqual(await record.async_read(), {'pair': [1, 2], '1': 'numeric key'})

    async def test_a_save_during_the_first_read_is_not_replaced_by_the_older_file(self):
        store = FileStore({'version': 'old'})
        store.release = asyncio.Event()
        record = self.record(store)
        reading = asyncio.create_task(record.async_read())
        await asyncio.sleep(0)
        await record.async_save({'version': 'new'})
        store.release.set()
        self.assertEqual(await reading, {'version': 'new'})
        self.assertEqual(await record.async_read(), {'version': 'new'})

    async def test_an_unserializable_save_rereads_what_the_file_holds(self):
        class Refusing(FileStore):
            async def async_save(self, record):
                pass  # Home Assistant logs the serialization error and keeps the file.
        store = Refusing({'kept': True})
        record = self.record(store)
        await record.async_read()
        await record.async_save({'bad': object()})
        self.assertEqual(await record.async_read(), {'kept': True})
        self.assertEqual(store.loads, 2)


if __name__ == '__main__':
    unittest.main()
