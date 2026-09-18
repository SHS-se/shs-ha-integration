"""Lossless, content-addressed pages for execution evidence.

Immutable pages are durable before the command checkpoint publishes their root.
Crashes can leave unreferenced pages, never a checkpoint naming unfinished data.
Full history is retained (and hydrated for pure replay); this bounds disk records,
not the resident account or diagnostic download size. Each saved tree records the
pages it reaches. Once the checkpoint on disk names a newer root, pages that only
superseded trees reached are removed; the retained history stays in the new tree.
"""
import asyncio
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
# Pages removed after one checkpoint; an old backlog drains over later ones.
COLLECT_BATCH = 500


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _page_key(name):
    return len(name) == 64 and all(c in '0123456789abcdef' for c in name)


class ExecutionArchive:
    def __init__(self, store_for, pages=None):
        """`pages` is (list, remove) for this archive's page files; without it nothing is removed."""
        self.store_for = store_for
        self.pages = pages
        self.saved = set()
        self._session_cache = None
        self._listed = False
        self._removing = None

    async def _page(self, page, reached=None):
        encoded=canonical(page)
        key=sha256(encoded).hexdigest()
        if key not in self.saved:
            await self.store_for(key).async_save(page)
            self.saved.add(key)
        if reached is not None:
            reached.add(key)
        return key

    async def put(self, value):
        await self._settled()
        return await self._put(value, set())

    async def _put(self, value, reached):
        if isinstance(value,list) and len(value)>128:
            refs=[await self._put(value[i:i+128], reached) for i in range(0,len(value),128)]
            return await self._list_page(refs, reached)
        if len(canonical(value)) <= PAGE_BYTES:
            page = {'kind':'value', 'value':value}
        elif isinstance(value,list):
            page={'kind':'items','children':[await self._put(item, reached) for item in value]}
        elif isinstance(value, dict):
            page = {'kind':'object', 'fields':{key:await self._put(item, reached) for key,item in value.items()}}
        elif isinstance(value, str):
            half = len(value)//2
            page = {'kind':'text', 'children':[await self._put(value[:half], reached),await self._put(value[half:], reached)]}
        else:
            raise ValueError('unsupported oversized archive value')
        return await self._page(page, reached)

    async def _list_page(self, refs, reached):
        while len(refs) > 128:
            refs = [await self._page({'kind': 'list', 'children': refs[i:i + 128]}, reached)
                    for i in range(0, len(refs), 128)]
        return await self._page({'kind': 'list', 'children': refs}, reached)

    async def collect(self, limit=COLLECT_BATCH):
        """Remove up to `limit` pages the last saved tree does not reach.

        Call only once the checkpoint on disk names that tree's root. Pages
        leave `saved` before their files are removed, so a later save needing
        the same content writes it again, after the removal has finished.
        """
        if self.pages is None or self._session_cache is None:
            return 0
        list_pages, remove_pages = self.pages
        if not self._listed:
            # Pages from earlier runs, and crashes between pages and checkpoint.
            self.saved |= {name for name in await list_pages() if _page_key(name)}
            self._listed = True
        reached = self._session_cache[3]
        garbage = []
        for key in self.saved:
            if key not in reached:
                garbage.append(key)
                if len(garbage) >= limit:
                    break
        if not garbage:
            return 0
        self.saved.difference_update(garbage)
        # The removal finishes even if the caller is cancelled; see _settled.
        self._removing = asyncio.ensure_future(remove_pages(garbage))
        self._removing.add_done_callback(lambda task: task.cancelled() or task.exception())
        await asyncio.shield(self._removing)
        return len(garbage)

    async def _settled(self):
        """New pages wait for any removal, which may name the same content."""
        if self._removing is not None and not self._removing.done():
            await asyncio.wait((self._removing,))

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
        # meter/command event. Retain only the last successfully saved tree,
        # with the set of pages each of its parts reaches.
        await self._settled()
        cache = await self._save_record(session, self._session_cache)
        self._session_cache = cache
        return cache[1]

    async def _save_record(self, value, previous):
        if previous is not None and previous[0] is value:
            return previous
        old_fields = previous[2] if previous is not None else {}
        cached = {}
        reached = set()
        refs = {'type': await self._put(type(value).__name__, reached)}
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
                pages = set()
                saved = (item, await self._put(encode_value(item), pages), None, frozenset(pages))
            cached[field.name] = saved
            refs[field.name] = saved[1]
            reached |= saved[3]
        key = await self._page({'kind': 'object', 'fields': refs}, reached)
        return value, key, cached, frozenset(reached)

    async def _save_sequence(self, value, previous):
        old_chunks = previous[2] if previous is not None else []
        chunks = []
        reached = set()
        for index, start in enumerate(range(0, len(value), 128)):
            chunk = value[start:start + 128]
            old = old_chunks[index] if index < len(old_chunks) else None
            if old is not None and len(old[0]) == len(chunk) and all(a is b for a, b in zip(old[0], chunk)):
                chunks.append(old)
            else:
                pages = set()
                # Large trace/plan chunks already live in separate item pages.
                # Keep their identities too: appending one trace must not encode
                # and hash the preceding 127 (potentially very large) records.
                old_items = old[3] if old is not None else None
                encoded = None if old_items is not None else encode_value(chunk)
                if old_items is not None or len(canonical(encoded)) > PAGE_BYTES:
                    items = []
                    for position, item in enumerate(chunk):
                        prior = old_items[position] if old_items is not None and position < len(old_items) else None
                        if prior is not None and prior[0] is item:
                            saved = prior
                        else:
                            item_pages = set()
                            raw = encoded[position] if encoded is not None else encode_value(item)
                            saved = (item, await self._put(raw, item_pages), frozenset(item_pages))
                        items.append(saved)
                        pages |= saved[2]
                    key = await self._page({'kind': 'items', 'children': [item[1] for item in items]}, pages)
                else:
                    items = None
                    key = await self._page({'kind': 'value', 'value': encoded}, pages)
                chunks.append((chunk, key, frozenset(pages), items))
            reached |= chunks[-1][2]
        key = await self._list_page([chunk[1] for chunk in chunks], reached)
        return value, key, chunks, frozenset(reached)

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
