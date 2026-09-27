"""Imported record metadata and atomic/cancelled app writes."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from shs_app.records import RecordStore


class RecordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'shs_energy.entry'
        self.envelope = dict(version=1, minor_version=3, key=self.path.name, data={'old':True})
        self.path.write_text(json.dumps(self.envelope))
        self.store = RecordStore(self.path)

    async def test_imported_envelope_survives_and_new_bytes_are_private(self):
        self.assertEqual(await self.store.async_load(), {'old':True})
        await self.store.async_save({'new':True})
        self.assertEqual(json.loads(self.path.read_bytes()), dict(self.envelope, data={'new':True}))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_failure_before_replace_preserves_old_record(self):
        with patch('shs_app.records.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                await self.store.async_save({'new':True})
        self.assertEqual(await self.store.async_load(), {'old':True})
        self.assertEqual(len(list(self.path.parent.glob('.shs_energy.entry.*'))), 1)

    async def test_cancelled_write_settles_before_another_write_can_start(self):
        started, finish = threading.Event(), threading.Event()
        write = self.store._write
        def blocked(data):
            write(data)
            started.set()
            finish.wait(5)
        with patch.object(self.store, '_write', side_effect=blocked):
            first = asyncio.create_task(self.store.async_save({'first':True}))
            await asyncio.to_thread(started.wait, 2)
            first.cancel()
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
        self.assertEqual(await self.store.async_load(), {'first':True})

    async def test_invalid_envelope_fails_without_overwriting_source(self):
        self.path.write_text('{}')
        with self.assertRaises(ValueError):
            await self.store.async_save({})
        self.assertEqual(self.path.read_text(), '{}')
