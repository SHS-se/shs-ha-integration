"""Migration-bound gateway sessions and durable, arrival-ordered receipts.

This is a new database. Source command journals remain sealed evidence; none of
these methods can unseal one, run a controller, or dispatch a native service.
"""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from .command_journal import WriterLease, encoded
from .device_ownership import decode_ownership

PROTOCOL = 2
RECEIPT_KINDS = frozenset(('gap', 'session', 'configuration', 'observation', 'outcome', 'activation'))


class GatewayConflict(ValueError):
    """A request does not belong to the current durable gateway state."""


def digest(value):
    return sha256(encoded(value).encode()).hexdigest()


def validate_identity(identity):
    if type(identity) is not dict or set(identity) != {'entry_id', 'migration_id', 'export_sha256', 'pair'}:
        raise ValueError('Exact migration identity required')
    if any(type(identity[key]) is not str or not identity[key] for key in ('entry_id', 'migration_id', 'export_sha256')):
        raise ValueError('Invalid migration identity')
    pair = identity['pair']
    if (type(pair) is not dict or set(pair) != {'protocol', 'app_version', 'integration_version', 'core_sha256'}
            or type(pair['protocol']) is not int or pair['protocol'] != PROTOCOL
            or any(type(pair[key]) is not str or not pair[key] for key in ('app_version', 'integration_version', 'core_sha256'))):
        raise ValueError('Exact gateway protocol/release pair required')
    return json.loads(encoded(identity))


def cursor(value):
    if type(value) is not int or value < 0:
        raise ValueError('Receipt cursor must be a non-negative integer')
    return value


class GatewayJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.lease = None
        self.boot = None

    def connect(self, *, readonly=False):
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError('Gateway journal must be a regular file')
        db = sqlite3.connect(self.path.resolve(strict=True).as_uri() +
                             ('?mode=ro' if readonly else '?mode=rw'), uri=True)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=5000')
        if not readonly:
            db.execute('PRAGMA synchronous=FULL')
        if db.execute('PRAGMA user_version').fetchone()[0] != 1:
            db.close()
            raise ValueError('Unsupported gateway journal')
        db.execute('BEGIN' if readonly else 'BEGIN IMMEDIATE')
        return db

    @classmethod
    def seed(cls, path, identity, ownership, battery_writer, source_commands):
        """Called only with verified cold-export evidence under the source lease.

        source_commands is a read-only connection to the sealed source. Keep its
        payloads/outcomes as provenance; no imported row becomes a runnable command.
        """
        identity = validate_identity(identity)
        decode_ownership(ownership)
        if type(battery_writer) is not dict:
            raise ValueError('Invalid battery writer evidence')
        source = dict(source_commands.execute('SELECT * FROM authority WHERE id=1').fetchone())
        if source['owner'] != 'fenced' or source['migration_id'] != identity['migration_id'] or source['clean_stop'] != 1:
            raise ValueError('Gateway seed requires the sealed, cleanly stopped source')
        seed = dict(identity=identity, ownership=ownership, battery_writer=battery_writer, source_authority=source)
        # Hash the complete command sequence while streaming it; no in-memory ledger copy.
        command_hash = sha256()
        count = 0
        for row in source_commands.execute('SELECT * FROM commands ORDER BY ordinal'):
            command_hash.update(encoded(dict(row)).encode()+b'\n')
            count += 1
        seed.update(commands_sha256=command_hash.hexdigest(), command_count=count)
        path = Path(path)
        if path.is_symlink():
            raise ValueError('Gateway journal must be a regular file')
        if path.exists():
            with closing(cls(path).connect(readonly=True)) as db:
                previous = json.loads(db.execute('SELECT seed FROM authority').fetchone()[0])
                if previous != seed:
                    raise GatewayConflict('Gateway already belongs to different migration evidence')
            return cls(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        # A failed seed remains evidence and fails closed on retry, never replaced.
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE authority (id INTEGER PRIMARY KEY CHECK(id=1), seed TEXT NOT NULL, boot TEXT, session TEXT, generation INTEGER NOT NULL, configuration_revision INTEGER NOT NULL, activation TEXT)')
            db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, instance TEXT NOT NULL, generation INTEGER NOT NULL, status TEXT NOT NULL, delivered INTEGER NOT NULL, offered INTEGER NOT NULL)')
            db.execute('CREATE TABLE receipts (ordinal INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE latest (entity TEXT PRIMARY KEY, receipt INTEGER NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE configuration (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE activations (id TEXT PRIMARY KEY, proof TEXT NOT NULL, receipt INTEGER NOT NULL)')
            db.execute('CREATE TABLE inherited_commands (ordinal INTEGER PRIMARY KEY, command_id TEXT UNIQUE NOT NULL, source_status TEXT NOT NULL, status TEXT NOT NULL, evidence TEXT NOT NULL)')
            db.execute('CREATE TABLE ownership (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            db.execute('INSERT INTO authority VALUES (1,?,NULL,NULL,0,0,NULL)', (encoded(seed),))
            db.execute('INSERT INTO ownership VALUES (1,?)', (encoded(ownership),))
            for row in source_commands.execute('SELECT * FROM commands ORDER BY ordinal'):
                row = dict(row)
                db.execute('INSERT INTO inherited_commands VALUES (?,?,?,?,?)', (
                    row['ordinal'], row['command_id'], row['status'],
                    'uncertain' if row['status'] == 'prepared' else row['status'], encoded(row)))
            db.execute('PRAGMA user_version=1')
            db.commit()
        # SQLite's commit fsyncs the database; persist the new directory entry too.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return cls(path)

    @staticmethod
    def _append(db, kind, value):
        if kind not in RECEIPT_KINDS:
            raise ValueError('Unknown gateway receipt kind')
        return db.execute('INSERT INTO receipts(kind,payload) VALUES (?,?)', (kind, encoded(value))).lastrowid

    def open(self):
        if self.lease is not None:
            raise RuntimeError('Gateway already open')
        lease = WriterLease(str(self.path)+'.lock')
        try:
            with closing(self.connect()) as db, db:
                old = db.execute('SELECT boot,session FROM authority').fetchone()
                self.boot = uuid4().hex
                db.execute("UPDATE sessions SET status='revoked' WHERE status='connected'")
                db.execute('UPDATE authority SET boot=?,session=NULL', (self.boot,))
                self._append(db, 'gap', {'reason': 'gateway_restart' if old['boot'] else 'migration_cutover', 'previous_session': old['session'], 'boot': self.boot})
            self.lease = lease
            return self
        except BaseException:
            lease.close()
            self.boot = None
            raise

    def close(self):
        if self.lease:
            try:
                with closing(self.connect()) as db, db:
                    db.execute("UPDATE sessions SET status='revoked' WHERE status='connected'")
                    db.execute('UPDATE authority SET session=NULL WHERE boot=?', (self.boot,))
            finally:
                self.lease.close()
                self.lease = None
                self.boot = None

    def _owner(self, db):
        if not self.lease:
            raise GatewayConflict('Gateway is not running')
        self.lease.require_held()
        row = db.execute('SELECT * FROM authority').fetchone()
        if row['boot'] != self.boot:
            raise GatewayConflict('Gateway incarnation changed')
        return row

    def _session(self, db, session):
        row = self._owner(db)
        current = db.execute('SELECT * FROM sessions WHERE id=?', (session,)).fetchone()
        if row['session'] != session or current is None or current['status'] != 'connected':
            raise GatewayConflict('Session is revoked; reconnect and reconcile')
        return row, current

    def begin(self, identity, instance):
        if type(instance) is not str or not instance:
            raise ValueError('App instance identity required')
        with closing(self.connect()) as db, db:
            row = self._owner(db)
            if json.loads(row['seed'])['identity'] != validate_identity(identity):
                raise GatewayConflict('Migration or release pair mismatch')
            previous = row['session']
            db.execute("UPDATE sessions SET status='revoked' WHERE status='connected'")
            session = uuid4().hex
            generation = row['generation'] + 1
            db.execute('INSERT INTO sessions VALUES (?,?,?,\'connected\',0,0)', (session, instance, generation))
            db.execute('UPDATE authority SET session=?,generation=?', (session, generation))
            self._append(db, 'gap', {'reason': 'session_replacement' if previous else 'session_connect', 'previous_session': previous})
            receipt = self._append(db, 'session', {'session': session, 'instance': instance, 'generation': generation})
            return {'session': session, 'generation': generation, 'through': receipt,
                    'configuration_revision': row['configuration_revision'], 'activation': row['activation']}

    def disconnect(self, session):
        with closing(self.connect()) as db, db:
            self._owner(db)
            row = db.execute('SELECT session FROM authority').fetchone()
            if row['session'] != session:
                return  # An old socket must not revoke its replacement.
            db.execute("UPDATE sessions SET status='revoked' WHERE id=?", (session,))
            db.execute('UPDATE authority SET session=NULL')
            self._append(db, 'gap', {'reason': 'session_disconnect', 'previous_session': session})

    def record(self, kind, payload):
        if kind not in ('observation', 'configuration', 'outcome'):
            raise ValueError('Only source observations, configuration or command outcomes may be recorded')
        if type(payload) is not dict:
            raise ValueError('Receipt payload must be an object')
        with closing(self.connect()) as db, db:
            self._owner(db)
            ordinal = self._append(db, kind, payload)
            if kind == 'configuration':
                # The revision is local receipt order, never a source timestamp.
                db.execute('UPDATE authority SET configuration_revision=?', (ordinal,))
                db.execute('INSERT OR REPLACE INTO configuration VALUES (1,?,?)', (ordinal, encoded(payload)))
            elif kind == 'observation':
                entity = payload.get('entity_id')
                if type(entity) is not str or not entity:
                    raise ValueError('Observation entity required')
                db.execute('INSERT OR REPLACE INTO latest VALUES (?,?,?)', (entity, ordinal, encoded(payload)))
            return ordinal

    def read(self, session, after, limit=256):
        cursor(after)
        if type(limit) is not int or not 1 <= limit <= 4096:
            raise ValueError('Invalid receipt page size')
        with closing(self.connect()) as db, db:
            self._session(db, session)
            high = db.execute('SELECT coalesce(max(ordinal),0) FROM receipts').fetchone()[0]
            if after > high:
                raise GatewayConflict('Receipt cursor is ahead of the gateway')
            rows = db.execute('SELECT * FROM receipts WHERE ordinal>? ORDER BY ordinal LIMIT ?', (after, limit)).fetchall()
            through = rows[-1]['ordinal'] if rows else after
            db.execute('UPDATE sessions SET offered=max(offered,?) WHERE id=?', (through, session))
            return {'receipts': [dict(ordinal=r['ordinal'], kind=r['kind'], payload=json.loads(r['payload'])) for r in rows], 'through': through, 'high': high}

    def acknowledge_delivery(self, session, through):
        cursor(through)
        with closing(self.connect()) as db, db:
            _, current = self._session(db, session)
            if through < current['delivered'] or through > current['offered']:
                raise GatewayConflict('Delivery acknowledgement is outside offered receipts')
            db.execute('UPDATE sessions SET delivered=? WHERE id=?', (through, session))
            return through

    def snapshot(self, session):
        with closing(self.connect(readonly=True)) as db:
            authority, _ = self._session(db, session)
            config = db.execute('SELECT payload FROM configuration').fetchone()
            return {'through': db.execute('SELECT coalesce(max(ordinal),0) FROM receipts').fetchone()[0],
                    'configuration_revision': authority['configuration_revision'],
                    'configuration': json.loads(config[0]) if config else None,
                    'observations': {r['entity']: {'receipt': r['receipt'], 'value': json.loads(r['payload'])} for r in db.execute('SELECT * FROM latest')},
                    'activation': authority['activation']}

    def activate(self, session, activation_id, proof):
        """Commit a locally verified reconciliation proof; not a public RPC.

        The composition must validate dormant app state and physical obligations
        before calling this. Neither begin() nor a delivery ACK grants authority.
        """
        if type(activation_id) is not str or not activation_id or type(proof) is not dict:
            raise ValueError('Activation identity and reconciliation proof required')
        required = {'identity', 'configuration_revision', 'through', 'app_checkpoint_sha256', 'physical_reconciliation_sha256'}
        if set(proof) != required or any(type(proof[k]) is not str or not proof[k] for k in ('app_checkpoint_sha256', 'physical_reconciliation_sha256')):
            raise ValueError('Incomplete reconciliation proof')
        cursor(proof['configuration_revision']); cursor(proof['through'])
        with closing(self.connect()) as db, db:
            row, current = self._session(db, session)
            if proof['identity'] != json.loads(row['seed'])['identity']:
                raise GatewayConflict('Activation belongs to another migration')
            prior = db.execute('SELECT proof,receipt FROM activations WHERE id=?', (activation_id,)).fetchone()
            if prior:
                if prior['proof'] != encoded(proof):
                    raise GatewayConflict('Activation identity reused with different proof')
                return {'activation': activation_id, 'receipt': prior['receipt']}
            if row['activation'] is not None:
                raise GatewayConflict('Migration already activated with another identity')
            if not row['configuration_revision'] or row['configuration_revision'] != proof['configuration_revision']:
                raise GatewayConflict('Configuration changed before activation')
            high = db.execute('SELECT coalesce(max(ordinal),0) FROM receipts').fetchone()[0]
            if proof['through'] != high or current['delivered'] < high:
                raise GatewayConflict('Reconcile and durably receive the current receipt prefix before activation')
            receipt = self._append(db, 'activation', {'activation': activation_id, 'proof_sha256': digest(proof)})
            db.execute('INSERT INTO activations VALUES (?,?,?)', (activation_id, encoded(proof), receipt))
            db.execute('UPDATE authority SET activation=?', (activation_id,))
            return {'activation': activation_id, 'receipt': receipt}
