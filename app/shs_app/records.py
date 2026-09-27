"""Private app-owned records using the imported Home Assistant Store envelopes."""
import asyncio
from copy import deepcopy
import json
import logging
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
        self.delayed = None
        self.data_func = None
        self.writes = set()
        self.failure = None

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

    def async_delay_save(self, data_func, delay):
        self.data_func = data_func
        if self.delayed is not None:
            return
        def write():
            self.delayed = None
            callback, self.data_func = self.data_func, None
            task = asyncio.create_task(self._save(callback()))
            self.writes.add(task)
            def completed(task):
                self.writes.discard(task)
                if not task.cancelled() and (error := task.exception()) is not None:
                    self.failure = error
                    logging.getLogger(__name__).error('Delayed record write failed for %s: %s',self.path.name,error)
            task.add_done_callback(completed)
        self.delayed = asyncio.get_running_loop().call_later(delay,write)

    async def async_save(self, data):
        if self.delayed is not None:
            self.delayed.cancel()
            self.delayed, self.data_func = None, None
        await self._save(data)

    async def async_close(self):
        if self.delayed is not None:
            data = self.data_func()
            await self.async_save(data)
        await asyncio.gather(*tuple(self.writes))
        if self.failure:
            raise self.failure

    async def _save(self, data):
        if self.failure:
            raise self.failure
        snapshot = deepcopy(data)
        async with self.lock:
            await settled(self.executor, self._write, snapshot)
