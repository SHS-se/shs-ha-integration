"""Durable delivery without pretending that receipt storage is domain processing."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

from .command_journal import WriterLease, encoded
from .gateway_journal import GatewayConflict, RECEIPT_KINDS, cursor, validate_identity


class ReceiptInbox:
    def __init__(self, path, identity):
        self.path = Path(path)
        self.identity = validate_identity(identity)
        self.lease = None

    def open(self):
        if self.lease:
            raise RuntimeError('Receipt inbox already open')
        if self.path.is_symlink():
            raise ValueError('Receipt inbox must be a regular file')
        lease = WriterLease(str(self.path)+'.lock')
        try:
            if not self.path.exists():
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(fd)
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute('PRAGMA synchronous=FULL')
                    db.execute('CREATE TABLE identity (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
                    db.execute('CREATE TABLE receipts (ordinal INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
                    db.execute('INSERT INTO identity VALUES (1,?)', (encoded(self.identity),))
                    db.execute('PRAGMA user_version=1')
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            with closing(self._connect()) as db:
                if json.loads(db.execute('SELECT payload FROM identity').fetchone()[0]) != self.identity:
                    raise GatewayConflict('Inbox belongs to another migration or release pair')
            self.lease = lease
            return self
        except BaseException:
            lease.close()
            raise

    def _connect(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError('Receipt inbox must be a regular file')
        db = sqlite3.connect(self.path.resolve(strict=True).as_uri() + '?mode=rw', uri=True)
        db.execute('PRAGMA synchronous=FULL')
        if db.execute('PRAGMA user_version').fetchone()[0] != 1:
            db.close()
            raise ValueError('Unsupported receipt inbox schema')
        db.execute('BEGIN IMMEDIATE')
        return db

    def close(self):
        if self.lease:
            self.lease.close()
            self.lease = None

    def _require_open(self):
        if not self.lease:
            raise GatewayConflict('Inbox is not open')
        self.lease.require_held()

    def through(self):
        self._require_open()
        with closing(self._connect()) as db:
            return db.execute('SELECT coalesce(max(ordinal),0) FROM receipts').fetchone()[0]

    def receive(self, page):
        """Commit the entire contiguous page before allowing a delivery ACK."""
        self._require_open()
        if type(page) is not dict or set(page) != {'receipts', 'through', 'high'} or type(page['receipts']) is not list:
            raise ValueError('Invalid receipt page')
        cursor(page['through']); cursor(page['high'])
        if page['through'] > page['high']:
            raise ValueError('Invalid gateway high-water mark')
        with closing(self._connect()) as db, db:
            high = db.execute('SELECT coalesce(max(ordinal),0) FROM receipts').fetchone()[0]
            last = None
            for row in page['receipts']:
                if type(row) is not dict or set(row) != {'ordinal', 'kind', 'payload'} or row['kind'] not in RECEIPT_KINDS or type(row['payload']) is not dict:
                    raise ValueError('Invalid receipt')
                ordinal = cursor(row['ordinal'])
                if ordinal == 0 or (last is not None and ordinal != last+1):
                    raise GatewayConflict('Receipt page is not in contiguous arrival order')
                last = ordinal
                content = encoded(row)
                if ordinal <= high:
                    old = db.execute('SELECT payload FROM receipts WHERE ordinal=?', (ordinal,)).fetchone()
                    if old is None or old[0] != content:
                        raise GatewayConflict('Receipt identity reused with different content')
                elif ordinal == high+1:
                    db.execute('INSERT INTO receipts VALUES (?,?)', (ordinal, content))
                    high = ordinal
                else:
                    raise GatewayConflict('Missing receipt prefix')
            if (last is not None and last != page['through']) or page['through'] > high:
                raise GatewayConflict('Receipt page cursor differs from its durable prefix')
            return high

    def after(self, through, limit=256):
        self._require_open()
        cursor(through)
        if type(limit) is not int or not 1 <= limit <= 4096:
            raise ValueError('Invalid receipt page size')
        with closing(self._connect()) as db:
            high = db.execute('SELECT coalesce(max(ordinal),0) FROM receipts').fetchone()[0]
            if through > high:
                raise GatewayConflict('Consumer checkpoint is ahead of its durable inbox')
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM receipts WHERE ordinal>? ORDER BY ordinal LIMIT ?', (through, limit))]


def processing_checkpoint(identity, receipt, sub_event, *, complete):
    """Put this value IN execution metadata passed to ExecutionStorage.save().

    It is deliberately not written to this inbox: execution's metadata, session
    and accounting commit in one transaction. A partially consumed receipt must
    resume at its next deterministic sub-event, not skip to the next receipt.
    """
    identity = validate_identity(identity)
    cursor(receipt); cursor(sub_event)
    if type(complete) is not bool:
        raise ValueError('Explicit processing completion required')
    return {'gateway_identity': identity, 'receipt': receipt, 'sub_event': sub_event, 'complete': complete}
