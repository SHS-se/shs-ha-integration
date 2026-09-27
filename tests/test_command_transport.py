"""Real SQLite, event-loop scheduling and process-lease regression coverage."""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.append(str(Path(__file__).parents[1]/'custom_components/shs_energy'))
from shs_core.command_journal import Command, CommandJournal, NativeAction, SourceFenced, WriterActive, WriterLease, entry_paths
from shs_core.command_transport import CommandTransport, CommandNotSent, CommandUncertain


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path, self.lock_path = entry_paths(self.temp.name, 'entry')
        self.journal = CommandJournal(self.path).open('preparation')
        self.transport = CommandTransport(self.journal, asyncio.to_thread)
        self.command = Command('one','controller','pool','plan',NativeAction('switch.pool','turn_on'))
        self.calls = []

    def outcome(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT status FROM commands').fetchone()[0]

    async def send(self, *args):
        self.assertEqual(self.outcome(), 'prepared')
        self.calls.append(args)

    async def execute(self, **kwargs):
        await self.transport.execute(self.command, authorize=lambda: None, send=self.send, timeout=1, **kwargs)

    async def test_durable_prepare_replay_and_content_conflict(self):
        await self.execute()
        await self.execute()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.outcome(), 'service_returned')
        different = Command('one','controller','pool','plan',NativeAction('switch.pool','turn_off'))
        with self.assertRaises(CommandNotSent):
            await self.transport.execute(different,authorize=lambda:None,send=self.send,timeout=1)
        self.assertEqual(len(self.calls), 1)

    async def test_revocation_in_scheduling_turn_after_prepare(self):
        permitted = True
        original = self.journal.prepare
        async def executor(fn, *args):
            nonlocal permitted
            result = fn(*args)
            if fn == original:
                asyncio.get_running_loop().call_soon(lambda: revoke())
            return result
        def revoke():
            nonlocal permitted
            permitted = False
        def authorize():
            if not permitted:
                raise ValueError('permission revoked')
        transport = CommandTransport(self.journal, executor)
        with self.assertRaisesRegex(ValueError, 'permission revoked'):
            await transport.execute(self.command,authorize=authorize,send=self.send,timeout=1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.outcome(), 'not_sent')

    async def test_cancel_prepare_settles_worker_before_releasing_lock(self):
        entered, release = threading.Event(), threading.Event()
        original = self.journal.prepare
        def slow(command):
            entered.set()
            release.wait(5)
            return original(command)
        with patch.object(self.journal, 'prepare', slow):
            task = asyncio.create_task(self.execute())
            await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.calls, [])
        self.assertEqual(self.outcome(), 'not_sent')

    async def test_cancel_service_is_uncertain_and_never_replayed(self):
        entered = asyncio.Event()
        async def send(*args):
            self.calls.append(args)
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(self.transport.execute(self.command,authorize=lambda:None,send=send,timeout=10))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.outcome(), 'uncertain')
        with self.assertRaises(CommandUncertain):
            await self.execute()
        self.assertEqual(len(self.calls), 1)

    async def test_outcome_disk_failure_faults_transport_and_preserves_uncertainty(self):
        with patch.object(self.journal,'finish',side_effect=OSError('disk full')):
            with self.assertRaises(CommandUncertain):
                await self.execute()
        self.assertEqual(self.outcome(), 'prepared')
        with self.assertRaises(CommandNotSent):
            await self.execute()
        self.assertEqual(len(self.calls),1)
        CommandJournal(self.path).open('preparation')
        self.assertEqual(self.outcome(),'uncertain')

    async def test_seal_requires_clean_stop_matching_lease_and_prevents_open_without_write(self):
        with WriterLease(self.lock_path) as lease:
            with self.assertRaisesRegex(ValueError,'cleanly'):
                self.journal.seal(lease,'migration')
            await self.transport.stopped()
            self.journal.seal(lease,'migration')
            self.journal.seal(lease,'migration')
            with self.assertRaises(SourceFenced):
                self.journal.seal(lease,'other')
        before = self.path.read_bytes()
        with self.assertRaises(SourceFenced):
            CommandJournal(self.path).open('preparation')
        self.assertEqual(before,self.path.read_bytes())

    async def test_lease_is_held_until_process_exit_even_after_setup_retries(self):
        script = "from shs_core.command_journal import process_lease; import sys; process_lease(sys.argv[1]); process_lease(sys.argv[1]); print('held',flush=True); sys.stdin.readline()"
        env = dict(__import__('os').environ, PYTHONPATH=str(Path(__file__).parents[1]/'custom_components/shs_energy'))
        # command_journal does not import select; subprocess was imported first.
        process = subprocess.Popen([sys.executable,'-c',script,str(self.lock_path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,env=env)
        try:
            self.assertEqual(process.stdout.readline().strip(),'held')
            with self.assertRaises(WriterActive):
                WriterLease(self.lock_path)
        finally:
            process.communicate('\n',timeout=5)
        with WriterLease(self.lock_path):
            pass

    async def test_companion_without_a_seed_cannot_construct_a_runtime(self):
        import ast
        from types import SimpleNamespace
        source=Path(__file__).parents[1]/'custom_components/shs_energy/__init__.py'
        setup=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='async_setup_entry')
        setup.returns=None
        for arg in setup.args.args:arg.annotation=None
        setup.body=[n for n in setup.body if not isinstance(n,ast.ImportFrom)]
        async def missing(hass,entry):raise ValueError('gateway seed missing')
        def forbidden(*args,**kwargs):raise AssertionError('missing seed reached runtime composition')
        namespace={'open_gateway':missing,'GatewayProjection':forbidden,'ConfigEntryNotReady':RuntimeError}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[setup],type_ignores=[])),str(source),'exec'),namespace)
        before=self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError,'gateway seed missing'):
            await namespace['async_setup_entry'](SimpleNamespace(),SimpleNamespace(entry_id='entry'))
        self.assertEqual(self.path.read_bytes(),before)


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_unload_revokes_and_settles_gateway_before_closing_its_journal(self):
        import ast
        from types import SimpleNamespace
        source=Path(__file__).parents[1]/'custom_components/shs_energy/__init__.py'
        functions=[n for n in ast.parse(source.read_text()).body if isinstance(n,ast.AsyncFunctionDef)
                   and n.name in ('_async_stop_runtime','async_unload_entry')]
        for node in functions:
            node.returns=None
            for arg in node.args.args:arg.annotation=None
        namespace={'PLATFORMS':['sensor','select']}
        exec(compile(ast.fix_missing_locations(ast.Module(body=functions,type_ignores=[])),str(source),'exec'),namespace)
        order=[]
        async def stream_close():order.append('stream')
        def journal_close():order.append('journal')
        async def service_close():
            self.assertTrue(service.source.closed)
            order.append('revoke_and_settle')
        async def platforms(*args):order.append('platforms');return True
        service=SimpleNamespace(closed=False,source=SimpleNamespace(closed=False),close=service_close,
            stream=SimpleNamespace(close=stream_close,journal=SimpleNamespace(close=journal_close)))
        hass=SimpleNamespace(data={'shs_energy_gateways':{'entry':service}},async_add_executor_job=asyncio.to_thread,
            config_entries=SimpleNamespace(async_unload_platforms=platforms))
        coordinator=SimpleNamespace(service=service,hass=hass,entry=SimpleNamespace(entry_id='entry'),platforms_loaded=True)
        self.assertTrue(await namespace['async_unload_entry'](hass,SimpleNamespace(runtime_data=coordinator)))
        self.assertEqual(order,['revoke_and_settle','stream','journal','platforms'])
        self.assertFalse(hass.data['shs_energy_gateways'])
        order.clear()
        async def failed():raise OSError('physical handback failed')
        service.close=failed
        with self.assertRaises(OSError):
            await namespace['async_unload_entry'](hass,SimpleNamespace(runtime_data=coordinator))
        self.assertEqual(order,['stream','journal'])
