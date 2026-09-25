"""Lossless, content-addressed pages for execution evidence.

Immutable pages are durable before the command checkpoint publishes their root.
Crashes can leave unreferenced pages, never a checkpoint naming unfinished data.
The account's full history is retained (and hydrated for pure replay); execution
traces are limited to the latest `MAX_EXECUTION_TRACES` by the runtime. Each saved
tree records the pages it reaches. Once the checkpoint on disk names a newer root,
pages that only superseded trees reached are removed; the retained history stays
in the new tree.
"""
import asyncio
from contextlib import suppress
from dataclasses import fields, replace
from hashlib import sha256
import json
from itertools import islice
from operator import is_
import os
import tempfile
from typing import get_type_hints, get_args

if __package__:
    from . import plan_execution as execution
    from .home_runtime import ExecutionSession, ExecutionTrace, MAX_EXECUTION_TRACES
    from .home_runtime_checkpoint import upgrade_execution_session
    from .runtime_json import encode_value, decode_value
    from .resource_profiling import ResourceProfiler
else:
    import plan_execution as execution
    from home_runtime import ExecutionSession, ExecutionTrace, MAX_EXECUTION_TRACES
    from home_runtime_checkpoint import upgrade_execution_session
    from runtime_json import encode_value, decode_value
    from resource_profiling import ResourceProfiler

PAGE_BYTES = 128_000
# Pages removed after one checkpoint; an old backlog drains over later ones.
COLLECT_BATCH = 500


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _page_key(name):
    return len(name) == 64 and all(c in '0123456789abcdef' for c in name)


class ExecutionArchive:
    def __init__(self, store_for, pages=None, *, profiler=None):
        """`pages` is (list, remove) for this archive's page files; without it nothing is removed."""
        self.store_for = store_for
        self.pages = pages
        self.saved = set()
        self._session_cache = None
        self._listed = False
        self._removing = None
        self.profiler = profiler if profiler is not None else ResourceProfiler()

    def resource_counts(self):
        return {'archive_known_pages':len(self.saved),
                'archive_reachable_pages':len(self._session_cache[3]) if self._session_cache else 0}

    async def _page(self, page, reached=None):
        with self.profiler.measure('archive_encode'):
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
        # Most pages are retained history. Compare sets in C rather than walk
        # every retained page in Python after each meter/command checkpoint.
        garbage = list(islice(self.saved - reached, limit))
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

    async def _load(self, key):
        if not isinstance(key,str) or len(key)!=64:
            raise ValueError('invalid execution archive identity')
        page = await self.store_for(key).async_load()
        if page is None or sha256(canonical(page)).hexdigest()!=key:
            raise ValueError('execution evidence page is missing or corrupt: '+key)
        self.saved.add(key)
        return page

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
        # Chunks are found by their first record, which the previous tree still
        # holds. Dropping whole chunks from the front (the oldest traces) reuses
        # every chunk that remains instead of re-encoding the shifted sequence;
        # otherwise the chunk at the same position is compared item by item.
        by_first = {id(chunk[0][0]): chunk for chunk in old_chunks}
        chunks = []
        reached = set()
        for index, start in enumerate(range(0, len(value), 128)):
            chunk = value[start:start + 128]
            old = by_first.get(id(chunk[0])) or (old_chunks[index] if index < len(old_chunks) else None)
            if old is not None and len(old[0]) == len(chunk) and all(map(is_, old[0], chunk)):
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
        raw=upgrade_execution_session(await self._session(key))
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


class PageFiles:
    """Content-addressed page files, written and read directly.

    Files keep Home Assistant's storage layout, so pages it wrote stay readable,
    but bypass its Store: every page has a new key, and Home Assistant remembers
    each key a Store writes or removes until it restarts. Writes replace the
    file atomically and raise on failure. `run` runs a blocking call off the
    event loop; `dumps` returns JSON bytes.
    """
    def __init__(self, directory, prefix, run, dumps, loads):
        self.directory, self.prefix = directory, prefix
        self.run, self.dumps, self.loads = run, dumps, loads

    def store_for(self, key):
        return _PageFile(self, key)

    async def list(self):
        return await self.run(self._list)

    async def remove(self, keys):
        await self.run(self._remove, list(keys))

    def _path(self, key):
        return os.path.join(self.directory, self.prefix + key)

    def _list(self):
        with os.scandir(self.directory) as entries:
            return {entry.name.removeprefix(self.prefix) for entry in entries if entry.name.startswith(self.prefix)}

    def _remove(self, keys):
        for key in keys:
            try:
                os.unlink(self._path(key))
            except FileNotFoundError:
                pass

    def _write(self, key, page):
        data = self.dumps({'version': 1, 'minor_version': 1, 'key': self.prefix + key, 'data': page})
        handle = tempfile.NamedTemporaryFile('wb', dir=self.directory, delete=False)
        try:
            with handle:
                handle.write(data)
                os.fchmod(handle.fileno(), 0o644)
            os.replace(handle.name, self._path(key))
        except BaseException:
            with suppress(OSError):
                os.unlink(handle.name)
            raise

    def _read(self, key):
        try:
            with open(self._path(key), 'rb') as handle:
                data = self.loads(handle.read())
        except FileNotFoundError:
            return None
        except ValueError:
            return None  # Reported by the archive as a missing or corrupt page.
        return data.get('data') if isinstance(data, dict) else None


class _PageFile:
    def __init__(self, files, key):
        self.files, self.key = files, key

    async def async_save(self, page):
        await self.files.run(self.files._write, self.key, page)

    async def async_load(self):
        return await self.files.run(self.files._read, self.key)


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
