"""Session fencing, receipt continuity and one-way cold seed evidence."""
from contextlib import closing
from copy import deepcopy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from gateway_fixture import IDENTITY, seed
from shs_core.command_journal import WriterActive
from shs_core.gateway_journal import GatewayConflict, GatewayJournal
from shs_core.receipt_inbox import ReceiptInbox, processing_checkpoint


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.gateway = seed(self.root)
        self.source_bytes = self.source.path.read_bytes()
        self.gateway.open()
        self.addCleanup(self.gateway.close)
        self.session = self.gateway.begin(IDENTITY, 'app')['session']

    def test_seed_preserves_uncertain_history_without_a_command_queue(self):
        with closing(self.gateway.connect(readonly=True)) as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT command_id,source_status,status FROM inherited_commands ORDER BY ordinal')],
                             [('pending', 'prepared', 'uncertain'), ('completed', 'service_returned', 'service_returned'), ('failed', 'not_sent', 'not_sent')])
        with closing(self.source.connect(readonly=True)) as db:
            GatewayJournal.seed(self.gateway.path, IDENTITY, {}, {}, db)
            with self.assertRaises(GatewayConflict):
                GatewayJournal.seed(self.gateway.path, IDENTITY, {'overrides': {'pool': 'changed'}}, {}, db)
        self.assertEqual(self.source.path.read_bytes(), self.source_bytes)
        self.assertEqual(self.source.state()['owner'], 'fenced')

    def test_exact_pair_reconnect_and_stale_disconnect(self):
        wrong = deepcopy(IDENTITY)
        wrong['pair']['app_version'] = 'other'
        with self.assertRaises(GatewayConflict):
            self.gateway.begin(wrong, 'app')
        new = self.gateway.begin(IDENTITY, 'replacement')
        self.assertEqual(new['generation'], 2)
        with self.assertRaises(GatewayConflict):
            self.gateway.read(self.session, 0)
        self.gateway.disconnect(self.session)
        self.gateway.read(new['session'], 0)
        self.gateway.disconnect(new['session'])
        with self.assertRaises(GatewayConflict):
            self.gateway.snapshot(new['session'])

    def test_restart_revokes_epoch_and_records_persistent_coverage_gap(self):
        before = self.gateway.snapshot(self.session)['through']
        with self.assertRaises(WriterActive):
            GatewayJournal(self.gateway.path).open()
        self.gateway.close()
        self.gateway.open()
        with self.assertRaises(GatewayConflict):
            self.gateway.read(self.session, 0)
        current = self.gateway.begin(IDENTITY, 'restarted')['session']
        page = self.gateway.read(current, before)
        self.assertEqual(page['receipts'][0]['payload']['reason'], 'gateway_restart')
        self.assertEqual(page['receipts'][0]['ordinal'], before+1)

    def test_arrival_order_retains_equal_and_decreasing_source_timestamps(self):
        for at, value in ((30, 1), (30, 2), (10, 3)):
            self.gateway.record('observation', dict(entity_id='sensor.power', source_at=at, value=value))
        rows = self.gateway.read(self.session, 0)['receipts'][-3:]
        self.assertEqual([r['payload']['value'] for r in rows], [1, 2, 3])
        self.assertEqual(self.gateway.snapshot(self.session)['observations']['sensor.power']['value']['value'], 3)

    def test_delivery_requires_offered_receipts_and_does_not_grant_activation(self):
        high = self.gateway.snapshot(self.session)['through']
        with self.assertRaises(GatewayConflict):
            self.gateway.acknowledge_delivery(self.session, high)
        page = self.gateway.read(self.session, 0, 1)
        with self.assertRaises(GatewayConflict):
            self.gateway.acknowledge_delivery(self.session, high)
        self.gateway.acknowledge_delivery(self.session, page['through'])
        self.gateway.acknowledge_delivery(self.session, page['through'])
        with self.assertRaises(GatewayConflict):
            self.gateway.acknowledge_delivery(self.session, 0)
        self.assertIsNone(self.gateway.snapshot(self.session)['activation'])

    def test_activation_commit_is_idempotent_and_configuration_revision_is_checked(self):
        revision = self.gateway.record('configuration', {'mode': 'controlling'})
        page = self.gateway.read(self.session, 0)
        self.gateway.acknowledge_delivery(self.session, page['through'])
        proof = dict(identity=IDENTITY, configuration_revision=revision, through=page['through'],
                     app_checkpoint_sha256='a'*64, physical_reconciliation_sha256='b'*64)
        result = self.gateway.activate(self.session, 'activation', proof)
        # A reply can be lost across reconnection; activation evidence remains queryable.
        replacement = self.gateway.begin(IDENTITY, 'replacement')['session']
        self.assertEqual(self.gateway.activate(replacement, 'activation', proof), result)
        with self.assertRaises(GatewayConflict):
            self.gateway.activate(replacement, 'activation', dict(proof, app_checkpoint_sha256='c'*64))
        self.assertEqual(self.source.path.read_bytes(), self.source_bytes)

    def test_configuration_edit_prevents_activation_of_older_snapshot(self):
        revision = self.gateway.record('configuration', {})
        page = self.gateway.read(self.session, 0)
        self.gateway.acknowledge_delivery(self.session, page['through'])
        self.gateway.record('configuration', {'changed': True})
        with self.assertRaisesRegex(GatewayConflict, 'Configuration changed'):
            self.gateway.activate(self.session, 'activation', dict(identity=IDENTITY,
                configuration_revision=revision, through=page['through'], app_checkpoint_sha256='a', physical_reconciliation_sha256='b'))
        self.assertIsNone(self.gateway.snapshot(self.session)['activation'])

    def test_symlinked_journal_and_replaced_lease_are_rejected(self):
        alias = self.root/'alias.sqlite'
        alias.symlink_to(self.gateway.path)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            GatewayJournal(alias).connect()
        self.gateway.lease.path.rename(self.root/'old-lock')
        self.gateway.lease.path.touch()
        with self.assertRaisesRegex(RuntimeError, 'original inode'):
            self.gateway.record('configuration', {})


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'inbox #1.sqlite'
        self.inbox = ReceiptInbox(self.path, IDENTITY).open()
        self.addCleanup(self.inbox.close)

    def page(self, *ordinals):
        return dict(receipts=[dict(ordinal=i, kind='observation', payload={'entity_id':'sensor.power', 'value':i}) for i in ordinals],
                    through=ordinals[-1], high=ordinals[-1])

    def test_durable_delivery_reopen_duplicate_and_distinct_processing_checkpoint(self):
        page = self.page(1, 2)
        self.assertEqual(self.inbox.receive(page), 2)
        self.inbox.close()
        self.inbox.open()
        self.assertEqual(self.inbox.receive(page), 2)
        self.inbox.receive(self.page(3))
        self.assertEqual(self.inbox.receive(page), 3)  # delayed identical page
        self.assertEqual([r['ordinal'] for r in self.inbox.after(1)], [2, 3])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        checkpoint = processing_checkpoint(IDENTITY, 2, 1, complete=False)
        self.assertFalse(checkpoint['complete'])
        self.assertEqual(self.inbox.through(), 3)  # storage does not claim consumption

    def test_gap_or_conflicting_duplicate_rolls_back_whole_page(self):
        self.inbox.receive(self.page(1))
        page = self.page(2, 4)
        with self.assertRaises(GatewayConflict):
            self.inbox.receive(page)
        self.assertEqual(self.inbox.through(), 1)
        changed = self.page(1, 2)
        changed['receipts'][0]['payload']['value'] = 99
        with self.assertRaises(GatewayConflict):
            self.inbox.receive(changed)
        self.assertEqual(self.inbox.through(), 1)
        with self.assertRaises(GatewayConflict):
            self.inbox.after(2)

    def test_disk_failure_does_not_advance_delivery_and_wrong_pair_cannot_open(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.inbox.receive(self.page(1))
        self.assertEqual(self.inbox.through(), 0)
        self.inbox.close()
        with self.assertRaises(GatewayConflict):
            ReceiptInbox(self.path, dict(IDENTITY, export_sha256='other')).open()
