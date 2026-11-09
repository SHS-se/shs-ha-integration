"""Lossless upgrades preserve retry evidence, with crash-safe page reclamation."""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zlib

from gateway_fixture import IDENTITY, seed
from shs_core.command_journal import Command, NativeAction, encoded
from shs_core.device_controls import pool_control_mapping
from shs_core.device_port import device_intent
from shs_core.gateway_journal import GatewayJournal, GatewayConflict, JournalStorageError, _unpack
from shs_core.grid_capacity import empty_capacity


class GatewayStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        _, self.journal = seed(self.temp.name)
        self.addCleanup(self.journal.close)
        self.operation = dict(request_id='request', parameters={'name':'Värme 🔋', 'evidence':['unchanged']*300})
        self.result = dict(ownership={'captured':'on'}, diagnostics=['same']*300)
        self.route = dict(effect={'purpose':'optimise'}, proposal={'steps':[]}, context={'original':'policy'})
        self.command = Command('command', 'controller', 'pool', 'plan', NativeAction('switch.pool','turn_on'))
        with closing(sqlite3.connect(self.journal.path)) as db, db:
            db.execute('CREATE TABLE transport (id INTEGER PRIMARY KEY CHECK(id=1), floor INTEGER NOT NULL, high INTEGER NOT NULL)')
            db.execute('INSERT INTO transport VALUES (1,0,0)')
            db.execute('PRAGMA user_version=2')
            db.execute('INSERT INTO operations VALUES (?,?,?,?)', ('request','old',encoded(self.operation),encoded(self.result)))
            db.execute('INSERT INTO operations VALUES (?,?,?,NULL)', ('interrupted','old',encoded(dict(request_id='interrupted'))))
            db.execute("INSERT INTO routes VALUES (?,?,?,0,'active')", ('route','old',encoded(self.route)))
            db.execute("INSERT INTO commands VALUES (?,?,?,?,'prepared',1,NULL,NULL,NULL,NULL)",
                ('command',sha256(self.command.payload().encode()).hexdigest(),self.command.payload(),'old'))

    def test_upgrade_preserves_results_conflicts_and_uncertain_command_identity(self):
        self.journal.open()
        session = self.journal.begin(IDENTITY,'app')['session']
        self.assertEqual(self.journal.begin_operation(session,self.operation), dict(state='completed',result=self.result))
        self.assertEqual(self.journal.begin_operation(session,dict(request_id='interrupted')), dict(state='interrupted'))
        with self.assertRaises(GatewayConflict):
            self.journal.begin_operation(session,dict(self.operation,parameters={}))
        self.assertEqual(self.journal.prepare_command(self.command)['status'],'uncertain')
        self.assertEqual(self.journal.command_outcome('command'),dict(status='uncertain',reason='gateway_interrupted'))
        with closing(self.journal.connect(readonly=True)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)
            self.assertEqual(db.execute('SELECT reclaim_pending FROM storage_maintenance').fetchone()[0],0)
            self.assertEqual(_unpack(db.execute('SELECT payload FROM routes').fetchone()[0]),encoded(self.route))
            self.assertEqual(_unpack(db.execute('SELECT payload FROM commands').fetchone()[0]),self.command.payload())
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.journal.close(); self.journal.open()
        replacement = self.journal.begin(IDENTITY,'replacement')['session']
        self.assertEqual(self.journal.begin_operation(replacement,self.operation)['result'],self.result)
        self.assertEqual(self.journal.prepare_command(self.command)['status'],'uncertain')
        with patch.object(self.journal,'_reclaim_storage',wraps=self.journal._reclaim_storage) as reclaim:
            self.journal.close(); self.journal.open()
            self.assertEqual(reclaim.call_count,1)  # Checks the cleared marker, no VACUUM.

    def test_failed_copy_rolls_back_schema_rows_and_maintenance_marker(self):
        with patch('shs_core.gateway_journal._pack',side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError,'disk full'): self.journal.open()
        with closing(sqlite3.connect(self.journal.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT payload FROM operations WHERE id=?',('request',)).fetchone()[0],encoded(self.operation))
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_packed' OR name='storage_maintenance'").fetchall())
        self.journal.open()

    def test_committed_migration_resumes_reclamation_without_repacking(self):
        with patch.object(self.journal,'_reclaim_storage',side_effect=OSError('interrupted vacuum')):
            with self.assertRaises(OSError): self.journal.open()
        with closing(sqlite3.connect(self.journal.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)
            self.assertEqual(db.execute('SELECT reclaim_pending FROM storage_maintenance').fetchone()[0],1)
        with patch('shs_core.gateway_journal._pack',side_effect=AssertionError('migration repeated')):
            self.journal.open()
        with closing(self.journal.connect(readonly=True)) as db:
            self.assertEqual(db.execute('SELECT reclaim_pending FROM storage_maintenance').fetchone()[0],0)

    def test_held_reader_leaves_reclamation_pending_until_checkpoint_can_finish(self):
        with closing(self.journal._connect()) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            self.journal._upgrade_storage(writer)
            with closing(self.journal.connect(readonly=True)) as reader:
                reader.execute('SELECT * FROM operations').fetchall()
                writer.execute('PRAGMA busy_timeout=1')
                with self.assertRaisesRegex(JournalStorageError,'blocked by a reader'):
                    self.journal._reclaim_storage(writer)
            self.assertEqual(writer.execute('SELECT reclaim_pending FROM storage_maintenance').fetchone()[0],1)
        self.journal.open()
        with closing(self.journal.connect(readonly=True)) as db:
            self.assertEqual(db.execute('SELECT reclaim_pending FROM storage_maintenance').fetchone()[0],0)

    def test_populated_journal_shrinks_without_retiring_any_rows(self):
        with closing(sqlite3.connect(self.journal.path)) as db, db:
            db.executemany('INSERT INTO operations VALUES (?,?,?,?)',
                [(str(i),'old',encoded(dict(self.operation,request_id=str(i))),encoded(self.result)) for i in range(250)])
        before = self.journal.path.stat().st_size
        self.journal.open()
        self.assertLess(self.journal.path.stat().st_size,before/2)
        with closing(self.journal.connect(readonly=True)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM operations').fetchone()[0],252)

    def test_new_results_routes_and_duplicate_requests_roundtrip_after_upgrade(self):
        self.journal.open()
        session = self.journal.begin(IDENTITY,'app')['session']
        operation = dict(request_id='new',parameters={'unicode':'å'})
        self.assertEqual(self.journal.begin_operation(session,operation)['state'],'new')
        self.journal.finish_operation(operation,{})
        self.assertEqual(self.journal.begin_operation(session,operation),dict(state='completed',result={}))
        self.journal.admit_route(session,'new-route',self.route)
        self.journal.admit_route(session,'new-route',deepcopy(self.route))
        self.assertEqual(self.journal.read_route(session,'new-route')['payload'],self.route)
        with self.assertRaises(GatewayConflict):
            self.journal.admit_route(session,'new-route',dict(self.route,context={}))
        with self.assertRaises(GatewayConflict): self.journal.admit_route(session,'route',self.route)

    def test_capacity_intents_shrink_on_restart_and_keep_native_routing(self):
        self.journal.open()
        models = [
            {'key':'heater', 'category':'heating', 'control_type':'switch_schedule', 'forecast':[1000]*96},
            {'key':'pool', 'category':'pool_heating', 'control_type':'setpoint',
             'mapping':{'temperature_entity_id':'sensor.water'}, 'forecast':[0]*96},
        ]
        command = {'type':'room_relay', 'room_key':'laundry', 'target_c':21}
        commands = {'heater':command, 'pool':{'type':'setpoint', 'target_c':26}}
        current = device_intent('device:heater', 'apply', {'device_models':models},
                                {'start':'2026-11-09T00:00:00Z', 'device_commands':commands},
                                configuration_revision=1, policy_revision=2)
        legacy = {**current, 'parameters':{**current['parameters'], 'models':models, 'commands':commands}}
        capacity = empty_capacity()
        capacity['coordination'] = {'withdrawal':None, 'caps':{}, 'intents':{'device:heater':legacy}}
        self.journal.save_record('grid_capacity', capacity)
        self.journal.close()
        self.journal.open()
        self.assertEqual(self.journal.load_record('grid_capacity')['coordination']['intents']['device:heater'], current)
        self.assertEqual(current['parameters']['commands'], {'heater':command})
        self.assertTrue(all('forecast' not in model for model in current['parameters']['models']))
        options = {'pool_water_temperature_entity':'sensor.water',
                   'device_control_mappings':{'pool':{'actuator_entity_ids':['switch.pool']}}}
        self.assertEqual(pool_control_mapping(options, current['parameters']['models']),
                         pool_control_mapping(options, models))
        self.assertIn('forecast', models[0])

    def test_corrupt_or_mixed_codec_evidence_fails_as_storage_not_request_error(self):
        for blob in (b'bad',zlib.compress(b'{}')[:-1],zlib.compress(b'{}')+b'trailing','{}'):
            with self.subTest(blob=blob), self.assertRaises(JournalStorageError): _unpack(blob)
        self.journal.open()
        session = self.journal.begin(IDENTITY,'app')['session']
        with closing(self.journal.connect()) as db, db:
            db.execute('UPDATE operations SET result=? WHERE id=?',(b'corrupt','request'))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE routes SET payload='{}'")
        with self.assertRaises(JournalStorageError): self.journal.begin_operation(session,self.operation)
