"""Real socket/process separation and commit-before-ACK verification."""
import asyncio
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import aiohttp

sys.path.append(str(Path(__file__).parents[2]/'tests'))
from gateway_fixture import IDENTITY, seed
from shs_core.gateway_journal import GatewayConflict
from shs_core.receipt_inbox import ReceiptInbox
from shs_app.gateway_client import GatewayClient


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.gateway = seed(self.root)
        self.source_bytes = self.source.path.read_bytes()
        self.flag = self.root/'drop-ack'
        self.process = await asyncio.create_subprocess_exec(sys.executable,
            str(Path(__file__).with_name('gateway_server_fixture.py')), str(self.gateway.path), str(self.flag),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.addAsyncCleanup(self.stop_server)
        port = int(await asyncio.wait_for(self.process.stdout.readline(), 10))
        self.url = f'http://127.0.0.1:{port}/api/websocket'
        self.http = aiohttp.ClientSession()
        self.addAsyncCleanup(self.http.close)
        self.inbox = ReceiptInbox(self.root/'inbox.sqlite', IDENTITY).open()
        self.addCleanup(self.inbox.close)
        self.client = GatewayClient(self.http, self.url, 'fixture-token', IDENTITY, self.inbox)
        self.addAsyncCleanup(self.client.close)

    async def stop_server(self):
        if self.process.returncode is None:
            self.process.terminate()
        await self.process.wait()
        error = (await self.process.stderr.read()).decode()
        self.assertNotIn('Error handling request', error)

    def delivered(self, session):
        with closing(sqlite3.connect(self.gateway.path)) as db:
            return db.execute('SELECT delivered FROM sessions WHERE id=?', (session,)).fetchone()[0]

    async def test_lost_ack_reply_reconnects_new_epoch_from_committed_inbox(self):
        first = await self.client.connect()
        self.flag.touch()
        with self.assertRaises(GatewayConflict):
            await self.client.receive()
        self.assertIsNone(self.client.socket)
        self.assertEqual(self.inbox.through(), first['through'])
        self.assertEqual(self.delivered(first['session']), first['through'])
        prefix = self.inbox.after(0)
        self.inbox.close()
        self.inbox.open()
        second = await self.client.connect()
        self.assertGreater(second['generation'], first['generation'])
        await self.client.receive()
        self.assertEqual(self.inbox.after(0)[:len(prefix)], prefix)
        self.assertEqual(self.delivered(second['session']), self.inbox.through())
        self.assertIsNone((await self.client.snapshot())['activation'])
        self.assertEqual(self.source.path.read_bytes(), self.source_bytes)

    async def test_inbox_disk_failure_never_acknowledges_unsaved_receipts(self):
        first = await self.client.connect()
        with patch.object(self.inbox, 'receive', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                await self.client.receive()
        self.assertEqual(self.delivered(first['session']), 0)
        self.assertEqual(self.inbox.through(), 0)
        await self.client.receive()
        self.assertEqual(self.delivered(first['session']), self.inbox.through())

    async def test_replacement_socket_revokes_old_one_and_concurrent_receive_serializes(self):
        first = await self.client.connect()
        replacement = GatewayClient(self.http, self.url, 'fixture-token', IDENTITY, self.inbox)
        self.addAsyncCleanup(replacement.close)
        second = await replacement.connect()
        with self.assertRaises(GatewayConflict):
            await self.client.snapshot()
        await asyncio.gather(replacement.receive(limit=1), replacement.receive(limit=1))
        self.assertEqual(self.delivered(second['session']), 2)
        self.assertEqual(self.delivered(first['session']), 0)
        self.assertIsNone((await replacement.snapshot())['activation'])

    async def test_failed_authentication_or_pair_has_no_session(self):
        wrong = GatewayClient(self.http, self.url, 'wrong-token', IDENTITY, self.inbox)
        with self.assertRaises(aiohttp.ClientResponseError):
            await wrong.connect()
        self.assertIsNone(wrong.socket)
        wrong_pair = GatewayClient(self.http, self.url, 'fixture-token', dict(IDENTITY, export_sha256='different'), self.inbox)
        with self.assertRaises(GatewayConflict):
            await wrong_pair.connect()
        self.assertIsNone(wrong_pair.socket)

    async def test_cancelled_reply_closes_socket_before_next_request(self):
        first = await self.client.connect()
        self.flag.write_text('pause')
        pending = asyncio.create_task(self.client.snapshot())
        async with asyncio.timeout(5):
            while self.flag.exists():
                await asyncio.sleep(.01)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertIsNone(self.client.socket)
        with self.assertRaises(GatewayConflict):
            await self.client.snapshot()
        second = await self.client.connect()
        self.assertGreater(second['generation'], first['generation'])
        await self.client.receive()

    async def test_large_projection_crosses_default_four_megabyte_socket_limit(self):
        await self.client.connect()
        value = {'plan':'å'*(5*1024*1024)}
        await self.client.project(value)
        self.assertEqual(json.loads((self.root/'projection.json').read_text()),value)
        self.assertIsNone((await self.client.snapshot())['activation'])
