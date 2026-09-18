"""Lossless, content-addressed pages for execution evidence.

Immutable pages are durable before the command checkpoint publishes their root.
Crashes can leave unreferenced pages, never a checkpoint naming unfinished data.
Full history is retained (and hydrated for pure replay); this bounds disk records,
not the resident account or diagnostic download size.
"""
from dataclasses import fields, replace
from hashlib import sha256
import json
from typing import get_type_hints, get_args

if __package__:
    from . import plan_execution as execution
    from .home_runtime import ExecutionSession, ExecutionTrace
    from .home_runtime_checkpoint import upgrade_execution_session
    from .runtime_json import encode_value, decode_value
else:
    import plan_execution as execution
    from home_runtime import ExecutionSession, ExecutionTrace
    from home_runtime_checkpoint import upgrade_execution_session
    from runtime_json import encode_value, decode_value

PAGE_BYTES = 128_000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


class ExecutionArchive:
    def __init__(self, store_for):
        self.store_for = store_for
        self.saved = set()
        self._session_cache = None

    async def _page(self, page):
        encoded=canonical(page)
        key=sha256(encoded).hexdigest()
        if key not in self.saved:
            await self.store_for(key).async_save(page)
            self.saved.add(key)
        return key

    async def put(self, value):
        raw = canonical(value)
        if isinstance(value,list) and len(value)>128:
            refs=[await self.put(value[i:i+128]) for i in range(0,len(value),128)]
            return await self._list_page(refs)
        elif len(raw) <= PAGE_BYTES:
            page = {'kind':'value', 'value':value}
        elif isinstance(value,list):
            page={'kind':'items','children':[await self.put(item) for item in value]}
        elif isinstance(value, dict):
            page = {'kind':'object', 'fields':{key:await self.put(item) for key,item in value.items()}}
        elif isinstance(value, str):
            half = len(value)//2
            page = {'kind':'text', 'children':[await self.put(value[:half]),await self.put(value[half:])]}
        else:
            raise ValueError('unsupported oversized archive value')
        return await self._page(page)

    async def _list_page(self, refs):
        while len(refs) > 128:
            refs = [await self._page({'kind': 'list', 'children': refs[i:i + 128]})
                    for i in range(0, len(refs), 128)]
        return await self._page({'kind': 'list', 'children': refs})

    async def get(self, key):
        if not isinstance(key,str) or len(key)!=64:
            raise ValueError('invalid execution archive identity')
        page = await self.store_for(key).async_load()
        if page is None or sha256(canonical(page)).hexdigest()!=key:
            raise ValueError('execution evidence page is missing or corrupt: '+key)
        self.saved.add(key)
        kind=page['kind']
        if kind=='value':return page['value']
        if kind=='object':return {k:await self.get(v) for k,v in page['fields'].items()}
        children=[await self.get(v) for v in page['children']]
        if kind=='list':return [item for child in children for item in child]
        if kind=='items':return children
        if kind=='text':return ''.join(children)
        raise ValueError('unknown execution evidence page kind')

    async def save_session(self, session):
        # Domain records and their tuples are immutable. Reuse pages by object
        # identity, without re-encoding or hashing historical evidence on every
        # meter/command event. Retain only the last successfully saved tree.
        cache = await self._save_record(session, self._session_cache)
        self._session_cache = cache
        return cache[1]

    async def _save_record(self, value, previous):
        if previous is not None and previous[0] is value:
            return previous
        old_fields = previous[2] if previous is not None else {}
        cached = {}
        refs = {'type': await self.put(type(value).__name__)}
        for field in fields(value):
            item = getattr(value, field.name)
            old = old_fields.get(field.name)
            if old is not None and old[0] is item:
                saved = old
            elif isinstance(item, execution.Account):
                saved = await self._save_record(item, old)
            elif isinstance(item, tuple):
                saved = await self._save_sequence(item, old)
            else:
                saved = (item, await self.put(encode_value(item)), None)
            cached[field.name] = saved
            refs[field.name] = saved[1]
        return value, await self._page({'kind': 'object', 'fields': refs}), cached

    async def _save_sequence(self, value, previous):
        old_chunks = previous[2] if previous is not None else []
        chunks = []
        for index, start in enumerate(range(0, len(value), 128)):
            chunk = value[start:start + 128]
            old = old_chunks[index] if index < len(old_chunks) else None
            if old is not None and len(old[0]) == len(chunk) and all(a is b for a, b in zip(old[0], chunk)):
                chunks.append(old)
            else:
                chunks.append((chunk, await self.put(encode_value(chunk))))
        return value, await self._list_page([chunk[1] for chunk in chunks]), chunks

    async def load_session(self, key):
        raw=upgrade_execution_session(await self.get(key))
        if not isinstance(raw,dict) or raw.get('type')!='ExecutionSession':
            raise ValueError('archive does not contain an execution session')
        hydrated=read_account(raw['account'])
        captured=raw['captured_feedback']
        traces=tuple(decode_value(v,ExecutionTrace) for v in raw['traces'])
        shell={**raw,'account':encode_value(execution.Account()),'captured_feedback':None,'traces':[]}
        return replace(decode_value(shell,ExecutionSession),account=hydrated,captured_feedback=captured,traces=traces)


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
