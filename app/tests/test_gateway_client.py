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
        self.client = GatewayClient(self.http, self.url, 'fixture-token', IDENTITY, self.inbox, paired_release={'app_version':'independent-app'})
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
        replacement = GatewayClient(self.http, self.url, 'fixture-token', IDENTITY, self.inbox, paired_release={'app_version':'newer-independent-app'})
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
        wrong_pair = GatewayClient(self.http, self.url, 'fixture-token', dict(IDENTITY, export_sha256='different'), self.inbox, paired_release={'app_version':'independent-app'})
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

    async def test_configuration_waits_for_admitted_calls_and_preserves_receipt_multiplexing(self):
        active=asyncio.Event();finish=asyncio.Event();installing=asyncio.Event();installed=asyncio.Event()
        entered=[]
        async def exchange(operation,body):
            entered.append(operation)
            if operation=='device':
                active.set();await finish.wait()
            if operation=='source' and body.get('operation')=='configure':
                installing.set();await installed.wait()
            if operation=='requests':
                self.assertTrue(installed.is_set(),'A poll reached HA while command admission was revoked')
            return {}
        with patch.object(self.client,'_exchange',side_effect=exchange):
            command=asyncio.create_task(self.client.call('device',{}))
            await active.wait()
            change=asyncio.create_task(self.client.call('source',{'operation':'configure'}))
            await asyncio.sleep(0)
            poll=asyncio.create_task(self.client.call('requests',{}))
            await self.client.call('receipts',{})
            self.assertFalse(installing.is_set())
            finish.set();await command;await installing.wait()
            await self.client.call('source',{'operation':'statistics'})
            self.assertNotIn('requests',entered)
            installed.set();await change;await poll
            self.assertEqual(entered,['device','receipts','source','source','requests'])

    async def test_cancelled_configuration_wait_releases_admission(self):
        active=asyncio.Event();finish=asyncio.Event()
        async def exchange(operation,body):
            if operation=='device':active.set();await finish.wait()
            return {}
        with patch.object(self.client,'_exchange',side_effect=exchange):
            command=asyncio.create_task(self.client.call('device',{}));await active.wait()
            change=asyncio.create_task(self.client.call('source',{'operation':'configure'}))
            await asyncio.sleep(0);change.cancel()
            with self.assertRaises(asyncio.CancelledError):await change
            finish.set();await command
            async with asyncio.timeout(1):
                await self.client.call('requests',{})
                await self.client.call('source',{'operation':'configure'})
            self.assertEqual(self.client.admitted_calls,0)
