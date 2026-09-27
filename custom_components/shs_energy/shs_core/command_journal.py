"""Durable actuator evidence and a one-way source migration fence."""
from contextlib import closing
from dataclasses import dataclass, asdict
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import time


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def now_ms():
    return time.time_ns() // 1_000_000


def entry_paths(storage, entry):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', entry):
        raise ValueError('Invalid entry identity')
    base = Path(storage) / f'shs_energy.commands.{entry}'
    return base.with_suffix(base.suffix + '.sqlite'), base.with_suffix(base.suffix + '.lock')


class SourceFenced(RuntimeError):
    pass


class WriterActive(RuntimeError):
    pass


class WriterLease:
    """Never unlink the lock file: all processes must lock the same inode."""
    def __init__(self, path):
        self.path = Path(path)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise WriterActive('SHS source process is still active; stop Core before exporting') from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def require_held(self):
        if self.fd is None or os.fstat(self.fd).st_ino != self.path.stat().st_ino:
            raise RuntimeError('Source lease is no longer held on the original inode')

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


# Deliberately not released on integration unload or failed setup. Entry-owned
# background tasks can outlive those callbacks. Only process exit releases it.
_PROCESS_LEASES = {}


def process_lease(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Source lease must not be a symbolic link')
    path = path.resolve()
    if path not in _PROCESS_LEASES:
        _PROCESS_LEASES[path] = WriterLease(path)
    _PROCESS_LEASES[path].require_held()
    return _PROCESS_LEASES[path]


@dataclass(frozen=True)
class NativeAction:
    entity: str
    service: str
    value: str | float | None = None

    def service_call(self):
        domain = self.entity.split('.')[0]
        allowed = {'number': {'set_value': 'value'}, 'input_number': {'set_value': 'value'},
                   'select': {'select_option': 'option'}, 'input_select': {'select_option': 'option'},
                   'climate': {'set_temperature': 'temperature'},
                   'switch': {'turn_on': None, 'turn_off': None},
                   'input_boolean': {'turn_on': None, 'turn_off': None}}
        if self.service not in allowed.get(domain, {}):
            raise ValueError('Unsupported native actuator action')
        field = allowed[domain][self.service]
        if field in ('value', 'temperature') and (isinstance(self.value, bool) or not isinstance(self.value, (int, float))):
            raise ValueError('Numeric action requires a number')
        if field == 'option' and not isinstance(self.value, str):
            raise ValueError('Select action requires an option')
        if field is None and self.value is not None:
            raise ValueError('Switch action carries no value')
        data = {'entity_id': self.entity}
        if field:
            data[field] = self.value
        encoded(data)  # no non-finite JSON
        return domain, self.service, data


@dataclass(frozen=True)
class Command:
    id: str
    origin: str
    device: str
    phase: str
    action: NativeAction

    def payload(self):
        self.action.service_call()
        if not self.id or self.origin not in ('controller', 'battery'):
            raise ValueError('Unsupported command identity or origin')
        return encoded(asdict(self))


class CommandJournal:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self, *, readonly=False):
        if self.path.is_symlink():
            raise ValueError('Command journal must not be a symbolic link')
        if readonly:
            db = sqlite3.connect(self.path.resolve(strict=True).as_uri() + '?mode=ro', uri=True)
            db.execute('PRAGMA query_only=ON')
        else:
            db = sqlite3.connect(self.path)
            db.execute('PRAGMA synchronous=FULL')
        db.row_factory = sqlite3.Row
        return db

    def open(self, release):
        if self.path.exists():
            self.require_source()
        else:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            with closing(self.connect()) as db, db:
                db.executescript('''
                    PRAGMA user_version=1;
                    CREATE TABLE authority (id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL,
                        migration_id TEXT, sealed_at_ms INTEGER, release TEXT NOT NULL,
                        clean_stop INTEGER NOT NULL, stopped_at_ms INTEGER, stopping_at_ms INTEGER);
                    CREATE TABLE commands (ordinal INTEGER PRIMARY KEY, command_id TEXT UNIQUE NOT NULL,
                        digest TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                        prepared_at_ms INTEGER NOT NULL, finished_at_ms INTEGER, reason TEXT);
                ''')
                db.execute("INSERT INTO authority VALUES (1,'integration',NULL,NULL,?,0,NULL,NULL)", (release,))
        with closing(self.connect()) as db, db:
            db.execute("UPDATE commands SET status='uncertain', reason='process_interrupted', finished_at_ms=? WHERE status='prepared'", (now_ms(),))
            db.execute('UPDATE authority SET release=?, clean_stop=0, stopped_at_ms=NULL, stopping_at_ms=NULL', (release,))
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return self

    def state(self):
        with closing(self.connect(readonly=True)) as db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('Unsupported command journal schema')
            return dict(db.execute('SELECT * FROM authority WHERE id=1').fetchone())

    def require_source(self):
        if self.state()['owner'] != 'integration':
            raise SourceFenced('SHS source was fenced for migration. Continue the export and app activation; do not restart source control.')

    def prepare(self, command):
        payload = command.payload()
        digest = sha256(payload.encode()).hexdigest()
        with closing(self.connect()) as db, db:
            if db.execute('SELECT owner FROM authority').fetchone()[0] != 'integration':
                raise SourceFenced('SHS source is fenced for migration')
            row = db.execute('SELECT * FROM commands WHERE command_id=?', (command.id,)).fetchone()
            if row:
                if row['digest'] != digest:
                    raise ValueError('Command identity reused with different content')
                return row['status']
            db.execute("INSERT INTO commands(command_id,digest,payload,status,prepared_at_ms) VALUES (?,?,?,'prepared',?)",
                       (command.id, digest, payload, now_ms()))
        return 'new'

    def finish(self, command, status, reason=None):
        if status not in ('not_sent', 'service_returned', 'uncertain'):
            raise ValueError('Unknown command outcome')
        with closing(self.connect()) as db, db:
            db.execute("UPDATE commands SET status=?,finished_at_ms=?,reason=? WHERE command_id=? AND digest=? AND status='prepared'",
                       (status, now_ms(), reason, command.id, sha256(command.payload().encode()).hexdigest()))

    def stopping(self):
        with closing(self.connect()) as db, db:
            db.execute('UPDATE authority SET clean_stop=0, stopping_at_ms=coalesce(stopping_at_ms,?)', (now_ms(),))

    def stopped(self):
        with closing(self.connect()) as db, db:
            db.execute('UPDATE authority SET clean_stop=1, stopped_at_ms=?, stopping_at_ms=coalesce(stopping_at_ms,?)', (now_ms(), now_ms()))

    def seal(self, lease, migration_id):
        lease.require_held()
        if lease.path != self.path.with_suffix('.lock'):
            raise ValueError('Lease does not belong to this journal')
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT * FROM authority').fetchone()
            if row['owner'] == 'fenced':
                if row['migration_id'] != migration_id:
                    raise SourceFenced('Source belongs to a different migration')
                return
            if not row['clean_stop']:
                raise ValueError('Source did not stop cleanly; restart and stop the preparation integration first')
            db.execute("UPDATE authority SET owner='fenced',migration_id=?,sealed_at_ms=?", (migration_id, now_ms()))
