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

    async def test_fenced_entry_setup_cannot_construct_coordinator_or_call_services(self):
        import ast
        from types import SimpleNamespace
        from shs_core.command_journal import process_lease, _PROCESS_LEASES
        with WriterLease(self.lock_path) as lease:
            self.journal.stopped()
            self.journal.seal(lease,'migration')
        # Execute the actual setup function; HA imports/types are supplied at
        # its boundary. Any access past the migration gate is a test failure.
        source=Path(__file__).parents[1]/'custom_components/shs_energy/__init__.py'
        tree=ast.parse(source.read_text())
        setup=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='async_setup_entry')
        setup.returns=None
        for arg in setup.args.args:
            arg.annotation=None
        class EntryError(Exception):
            pass
        def forbidden(*args,**kwargs):
            raise AssertionError('Fenced setup reached runtime composition')
        namespace={'entry_paths':entry_paths,'process_lease':process_lease,'CommandJournal':CommandJournal,
                   'SourceFenced':SourceFenced,'WriterActive':WriterActive,'ConfigEntryError':EntryError,
                   'ConfigEntryNotReady':RuntimeError,'STORAGE_DIR':'.storage','INTEGRATION_VERSION':'preparation',
                   'CommandTransport':forbidden,'ShsApiClient':forbidden}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[setup],type_ignores=[])),str(source),'exec'),namespace)
        hass=SimpleNamespace(config=SimpleNamespace(path=lambda _:self.temp.name),async_add_executor_job=asyncio.to_thread)
        before=self.path.read_bytes()
        try:
            with self.assertRaisesRegex(EntryError,'fenced'):
                await namespace['async_setup_entry'](hass,SimpleNamespace(entry_id='entry'))
        finally:
            _PROCESS_LEASES.pop(self.lock_path.resolve()).close()
        self.assertEqual(self.path.read_bytes(),before)


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_unload_uses_the_native_transport_and_marks_clean_only_after_owners_stop(self):
        import ast
        from types import SimpleNamespace
        source = Path(__file__).parents[1]/'custom_components/shs_energy/__init__.py'
        tree = ast.parse(source.read_text())
        functions = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                     and node.name in ('_async_stop_runtime', 'async_unload_entry')]
        for node in functions:
            node.returns = None
            for arg in node.args.args:
                arg.annotation = None
        namespace = {'PLATFORMS': ['sensor', 'select']}
        exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])), str(source), 'exec'), namespace)
        order = []
        def sync(label):
            return lambda: order.append(label)
        def asynchronous(label, failure=False):
            async def call(*args, **kwargs):
                order.append(label)
                if failure:
                    raise OSError('fixture shutdown failure')
                return True
            return call
        coordinator = SimpleNamespace(
            controller=SimpleNamespace(native_executor=SimpleNamespace(transport=SimpleNamespace(
                stopping=asynchronous('stopping'), stopped=asynchronous('clean'))), async_stop=asynchronous('controller')),
            battery_runtime=SimpleNamespace(close=asynchronous('battery')),
            battery_live_inputs=SimpleNamespace(close=sync('observations')),
            battery_writer=SimpleNamespace(close=sync('writer')))
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_unload_platforms=asynchronous('platforms')))
        self.assertTrue(await namespace['async_unload_entry'](hass, SimpleNamespace(runtime_data=coordinator)))
        self.assertEqual(order, ['stopping', 'observations', 'battery', 'controller', 'clean', 'writer', 'platforms'])
        order.clear()
        coordinator.battery_runtime.close = asynchronous('battery', failure=True)
        with self.assertRaises(OSError):
            await namespace['async_unload_entry'](hass, SimpleNamespace(runtime_data=coordinator))
        self.assertEqual(order, ['stopping', 'observations', 'battery', 'writer'])
