"""Private app-owned records using the imported Home Assistant Store envelopes."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile

from shs_core.command_journal import encoded
from shs_core.command_transport import settled


class RecordStore:
    def __init__(self, path, *, executor=asyncio.to_thread):
        self.path = Path(path)
        self.executor = executor
        self.lock = asyncio.Lock()

    def _read(self):
        if self.path.is_symlink():
            raise ValueError('Record must not be a symbolic link')
        if not self.path.exists():
            return None
        value = json.loads(self.path.read_bytes())
        if type(value) is not dict or value.get('version') != 1 or value.get('key') != self.path.name or 'data' not in value:
            raise ValueError('Unsupported imported record envelope')
        return value

    async def async_load(self):
        async with self.lock:
            value = await settled(self.executor, self._read)
            return value['data'] if value is not None else None

    def _write(self, data):
        envelope = self._read()
        if envelope is None:
            envelope = {'version': 1, 'minor_version': 1, 'key': self.path.name}
        content = encoded({**envelope, 'data': data}).encode()
        descriptor, name = tempfile.mkstemp(prefix='.'+self.path.name+'.', dir=self.path.parent)
        # Interrupted writes are retained as evidence; they never replace a record
        # until serialization and the file fsync have both succeeded.
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    async def async_save(self, data):
        snapshot = deepcopy(data)
        async with self.lock:
            await settled(self.executor, self._write, snapshot)
