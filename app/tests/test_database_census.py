"""Minute gauges stay current without repeatedly scanning lifetime evidence."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from shs_app.database_census import DatabaseCensus


class CensusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'diagnostics.sqlite3'
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE transport (floor INTEGER, high INTEGER)')
            db.execute('INSERT INTO transport VALUES (1,4)')
            db.execute('CREATE TABLE resource_samples (sampled_at TEXT PRIMARY KEY, payload TEXT)')
            db.execute("INSERT INTO resource_samples VALUES ('now','{}')")
        self.now = 0
        self.census = DatabaseCensus(clock=lambda:self.now)
        self.statements = []
        connect = sqlite3.connect
        def traced(*args,**kwargs):
            db = connect(*args,**kwargs)
            db.set_trace_callback(self.statements.append)
            return db
        self.trace = patch('shs_app.database_census.sqlite3.connect',side_effect=traced)
        self.trace.start();self.addCleanup(self.trace.stop)

    def sample(self): return self.census.sample(self.root)['databases'][0]

    def test_hourly_details_fresh_minute_watermarks_and_detached_published_values(self):
        first = self.sample()
        self.assertTrue(any('dbstat' in sql for sql in self.statements))
        first['tables'][0]['rows'] = -1
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE transport SET high=9')
            db.execute("INSERT INTO resource_samples VALUES ('later','{}')")
        self.now = 60;self.statements.clear()
        second = self.sample()
        self.assertEqual(second['receipts']['pending'],8)
        self.assertEqual(next(row['rows'] for row in second['tables'] if row['name']=='resource_samples'),1)
        self.assertFalse(any('dbstat' in sql or 'count(*)' in sql or 'min(' in sql for sql in self.statements))
        self.assertEqual(first['details_sampled_at'],second['details_sampled_at'])
        self.now = 3600;self.statements.clear()
        third = self.sample()
        self.assertTrue(any('dbstat' in sql for sql in self.statements))
        self.assertEqual(next(row['rows'] for row in third['tables'] if row['name']=='resource_samples'),2)

    def test_schema_change_and_file_replacement_force_details_refresh(self):
        self.sample();self.now = 60
        with closing(sqlite3.connect(self.path)) as db, db: db.execute('CREATE TABLE added (value INTEGER)')
        self.statements.clear()
        self.assertIn('added',[row['name'] for row in self.sample()['tables']])
        self.assertTrue(any('dbstat' in sql for sql in self.statements))
        replacement = self.root/'replacement.sqlite'
        with closing(sqlite3.connect(replacement)) as db, db: db.execute('CREATE TABLE replaced (value INTEGER)')
        replacement.replace(self.path);self.statements.clear()
        self.assertEqual([row['name'] for row in self.sample()['tables']],['replaced'])
        self.assertTrue(any('dbstat' in sql for sql in self.statements))

    def test_failed_refresh_exposes_error_and_discards_old_detail_cache(self):
        self.sample();self.now = 3600
        with patch.object(self.census,'inspect_database',side_effect=sqlite3.OperationalError('scan failed')):
            result = self.sample()
        self.assertIn('scan failed',result['error'])
        self.assertNotIn('tables',result)
        self.assertEqual(self.census.details,{})
        self.statements.clear();self.sample()
        self.assertTrue(any('dbstat' in sql for sql in self.statements))

    def test_hourly_refresh_skips_full_scans_on_live_rollback_journals(self):
        stores = self.root/'stores'
        stores.mkdir()
        path = stores/'shs_energy.execution.home.sqlite'
        self.path.rename(path)
        self.census.inspect_database(path, 'Operational accounting')
        self.now = 3600
        self.statements.clear()
        with closing(sqlite3.connect(path)) as writer:
            writer.execute('PRAGMA busy_timeout=0')
            writer.execute('UPDATE transport SET high=9')
            result, = self.census.sample(self.root, runtime=self.root, entry='home', ha_config=self.root)['databases']
            writer.commit()
        self.assertEqual(result['journal_mode'], 'delete')
        self.assertEqual(result['receipts']['received_through'], 4)
        self.assertNotIn('tables', result)
        self.assertNotIn(path.resolve(), self.census.details)
        self.assertFalse(any('dbstat' in sql or 'count(*)' in sql for sql in self.statements))

    def test_live_wal_database_keeps_detailed_measurements(self):
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        result = self.census.inspect_database(self.path, 'HA companion', live_runtime=True)
        self.assertEqual(result['journal_mode'], 'wal')
        self.assertEqual({table['name'] for table in result['tables']}, {'transport', 'resource_samples'})
