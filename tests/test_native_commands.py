"""Exercise the physical gateway boundary with the real durable transport."""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
sys.path.append(str(Path(__file__).parents[1]/'custom_components/shs_energy'))
from shs_core.command_journal import Command, CommandJournal, NativeAction
from shs_core.command_transport import CommandTransport, CommandNotSent
from shs_core.native_commands import NativeExecutor, native_command


class NativeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = CommandJournal(Path(self.temp.name)/'commands.sqlite').open('test')
        self.state = SimpleNamespace(state='1', attributes={'min': 0, 'max': 16, 'step': 1})
        self.calls = []
        self.allowed = True
        async def send(*args):
            self.calls.append(args)
        self.transport = CommandTransport(self.journal, asyncio.to_thread)
        self.executor = NativeExecutor(self.transport, lambda entity: self.state, lambda: '°C', send)
        self.command = Command('test', 'controller', 'ev', 'plan', NativeAction('number.current', 'set_value', 10))

    def authorize(self):
        if not self.allowed:
            raise CommandNotSent('permission revoked')

    def status(self):
        with closing(sqlite3.connect(self.journal.path)) as db:
            return db.execute('SELECT status FROM commands').fetchone()[0]

    async def test_current_hardware_bounds_are_checked_after_prepare(self):
        async def execute(fn, *args):
            result = await asyncio.to_thread(fn, *args)
            if fn == self.journal.prepare:
                self.state.attributes['max'] = 6
            return result
        self.transport.executor = execute
        with self.assertRaisesRegex(CommandNotSent, 'hardware bounds'):
            await self.executor.execute(self.command, authorize=self.authorize, timeout=1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.status(), 'not_sent')

    async def test_revoke_after_prepare_is_not_sent_and_replay_does_not_retry(self):
        async def execute(fn, *args):
            result = await asyncio.to_thread(fn, *args)
            if fn == self.journal.prepare:
                self.allowed = False
            return result
        self.transport.executor = execute
        with self.assertRaisesRegex(CommandNotSent, 'permission revoked'):
            await self.executor.execute(self.command, authorize=self.authorize, timeout=1)
        self.allowed = True
        with self.assertRaises(CommandNotSent):
            await self.executor.execute(self.command, authorize=self.authorize, timeout=1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.status(), 'not_sent')

    async def test_completed_command_is_not_repeated_after_metadata_changes(self):
        await self.executor.execute(self.command, authorize=self.authorize, timeout=1)
        self.state.attributes['max'] = 5
        await self.executor.execute(self.command, authorize=self.authorize, timeout=1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.status(), 'service_returned')

    async def test_shared_validator_covers_native_steps_modes_and_celsius(self):
        with self.assertRaisesRegex(ValueError, 'supported step'):
            native_command('number.current', 1.5, self.state, temperature_unit=None)
        with self.assertRaisesRegex(ValueError, 'boolean'):
            native_command('number.current', True, self.state, temperature_unit=None)
        select = SimpleNamespace(state='idle', attributes={'options': ['idle', 'charge']})
        with self.assertRaisesRegex(ValueError, 'unsupported mode'):
            native_command('select.mode', 'other', select, temperature_unit=None)
        climate = SimpleNamespace(state='heat', attributes={'min_temp': 10, 'max_temp': 30, 'temperature': 20})
        with self.assertRaisesRegex(ValueError, 'Celsius'):
            native_command('climate.room', 21, climate, temperature_unit='°F')
        self.assertEqual(native_command('climate.room', 21, climate, temperature_unit='°C').action.service_call(),
                         ('climate', 'set_temperature', {'entity_id': 'climate.room', 'temperature': 21}))
