"""One-way reader for the retired content-addressed execution archive.

Runtime migration and offline replay tools use this reader. No writer or live
page cache remains.
Old files are removed only after the SQLite checkpoint has been verified.
"""
from dataclasses import fields, replace
from hashlib import sha256
import json
from pathlib import Path
from typing import get_type_hints, get_args

if __package__:
    from . import plan_execution as execution
    from .home_runtime import ExecutionSession, ExecutionTrace, MAX_EXECUTION_TRACES
    from .home_runtime_checkpoint import upgrade_execution_session, decode_checkpoint
    from .runtime_json import encode_value, decode_value
else:
    import plan_execution as execution
    from home_runtime import ExecutionSession, ExecutionTrace, MAX_EXECUTION_TRACES
    from home_runtime_checkpoint import upgrade_execution_session, decode_checkpoint
    from runtime_json import encode_value, decode_value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _page_key(name):
    return len(name) == 64 and all(c in '0123456789abcdef' for c in name)


class _ArchiveReader:
    def __init__(self, directory, prefix, run):
        self.directory, self.prefix, self.run = directory, prefix, run

    async def _load(self, key):
        if not isinstance(key, str) or not _page_key(key):
            raise ValueError('invalid execution archive identity')
        def read():
            with (self.directory / (self.prefix + key)).open('rb') as handle:
                page = json.load(handle)['data']
            if page is None or sha256(canonical(page)).hexdigest() != key:
                raise ValueError('execution evidence page is missing or corrupt: ' + key)
            return page
        return await self.run(read)

    async def get(self, key):
        return await self._value(await self._load(key))

    async def _value(self, page):
        kind=page['kind']
        if kind=='value':return page['value']
        if kind=='object':return {k:await self.get(v) for k,v in page['fields'].items()}
        children=[await self.get(v) for v in page['children']]
        if kind=='list':return [item for child in children for item in child]
        if kind=='items':return children
        if kind=='text':return ''.join(children)
        raise ValueError('unknown execution evidence page kind')

    async def _tail(self, key, count):
        """The last `count` items of a stored list, reading only the pages that hold them."""
        if count <= 0:
            return []
        page = await self._load(key)
        kind = page['kind']
        if kind == 'value' and isinstance(page['value'], list):
            return page['value'][-count:]
        if kind == 'items':
            return [await self.get(child) for child in page['children'][-count:]]
        if kind == 'list':
            items = []
            for child in reversed(page['children']):
                if len(items) >= count:
                    break
                items[:0] = await self._tail(child, count - len(items))
            return items
        raise ValueError('execution evidence page is not a list')

    async def load_session(self, key):
        raw=await self._session(key)
        return await self.run(lambda: self._hydrate(raw))

    @staticmethod
    def _hydrate(raw):
        raw=upgrade_execution_session(raw)
        if not isinstance(raw,dict) or raw.get('type')!='ExecutionSession':
            raise ValueError('archive does not contain an execution session')
        hydrated=read_account(raw['account'])
        captured=raw['captured_feedback']
        traces=tuple(decode_value(v,ExecutionTrace) for v in raw['traces'][-MAX_EXECUTION_TRACES:])
        shell={**raw,'account':encode_value(execution.Account()),'captured_feedback':None,'traces':[]}
        return replace(decode_value(shell,ExecutionSession),account=hydrated,captured_feedback=captured,traces=traces)

    async def _session(self, key):
        """A stored session with only the traces the runtime retains.

        Older archives kept every trace; loading only the latest avoids reading
        and decoding the rest, whose pages are then removed as unreachable.
        """
        page = await self._load(key)
        if page['kind'] != 'object' or 'traces' not in page['fields']:
            return await self._value(page)
        return {name: await (self._tail(ref, MAX_EXECUTION_TRACES) if name == 'traces' else self.get(ref))
                for name, ref in page['fields'].items()}

def read_account(account):
    """Read each immutable record independently of transport array limits."""
    hints=get_type_hints(execution.Account)
    if not isinstance(account,dict) or account.get('type')!='Account' or set(account)!={'type',*(f.name for f in fields(execution.Account))}:
        raise ValueError('unknown account archive fields')
    values={}
    for field in fields(execution.Account):
        value=account[field.name];kind=hints[field.name]
        values[field.name]=(tuple(decode_value(v,get_args(kind)[0]) for v in value)
                            if isinstance(value,list) else decode_value(value,kind))
    return execution.Account(**values)


class LegacyExecution:
    def __init__(self, directory, entry_id, store, run):
        self.directory, self.store, self.run = Path(directory), store, run
        self.prefix = f"shs_energy.execution_evidence.{entry_id}."
        self.checkpoint_path = self.directory / f"shs_energy.battery_runtime.{entry_id}"

    async def load(self):
        value = await self.store.async_load()
        if value is None:
            return None
        if value.get('schema') not in ('battery-runtime-v2', 'battery-runtime-v3'):
            raise ValueError('Unsupported legacy battery runtime journal')
        if value.get('execution_root'):
            session = await _ArchiveReader(self.directory, self.prefix, self.run).load_session(value['execution_root'])
        elif value.get('checkpoint') is not None:
            session = decode_checkpoint(value['checkpoint'].encode()).execution
        else:
            session = ExecutionSession(account=decode_value(value['account'], execution.Account)
                if value.get('account') is not None else execution.Account())
        metadata = {key: value[key] for key in ('checkpoint', 'options', 'devices', 'model_sources', 'ratings')}
        metadata['schema'] = 'battery-runtime-v4'
        return metadata, session

    async def cleanup(self):
        def remove():
            for path in self.directory.iterdir():
                if path.name.startswith(self.prefix) and _page_key(path.name[len(self.prefix):]):
                    path.unlink(missing_ok=True)
            self.checkpoint_path.unlink(missing_ok=True)
        await self.run(remove)
