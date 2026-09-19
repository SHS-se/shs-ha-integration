"""Lossless archive publication and replay beyond transport checkpoint limits."""
import json
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.append(str(Path(__file__).parents[1]/'custom_components'/'shs_energy'))
from execution_archive import ExecutionArchive, PageFiles, canonical, PAGE_BYTES
from home_runtime import ExecutionSession
from plan_execution import Account, MeterReceipt, StateObservation, admit_plan, balance, measured, objective_history, record_meter
from test_plan_execution import contract

class Store:
    def __init__(self):self.value=None
    async def async_save(self,value):self.value=value
    async def async_load(self):return self.value

class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_tail_grows_into_item_pages_without_losing_the_durable_prefix(self):
        from home_runtime import ExecutionTrace
        stores = {}
        archive = ExecutionArchive(lambda k: stores.setdefault(k, Store()))
        trace = ExecutionTrace(1, 0, 0, 0, None, 'x' * 70_000, '[]', None, None, None)
        session = ExecutionSession(traces=(trace,))
        first = await archive.save_session(session)
        updated = replace(session, traces=(trace, replace(trace, at_ms=2)))
        class FailedStore(Store):
            async def async_save(self, value): raise OSError('disk full')
        archive.store_for = lambda k: FailedStore()
        with self.assertRaisesRegex(OSError, 'disk full'):
            await archive.save_session(updated)
        archive.store_for = lambda k: stores.setdefault(k, Store())
        self.assertEqual(await archive.load_session(first), session)
        second = await archive.save_session(updated)
        self.assertEqual(await archive.load_session(first), session)
        self.assertEqual(await archive.load_session(second), updated)

    async def test_large_tail_reuses_item_pages_and_retains_corrections_after_collection(self):
        import execution_archive
        from home_runtime import ExecutionTrace
        stores = {}
        async def list_pages(): return list(stores)
        async def remove_pages(keys):
            for key in keys: stores.pop(key, None)
        archive = ExecutionArchive(lambda k: stores.setdefault(k, Store()), (list_pages, remove_pages))
        trace = ExecutionTrace(1, 0, 0, 0, None, 'x' * 140_000, '[]', None, None, None)
        traces = tuple(replace(trace, at_ms=i) for i in range(127))
        session = ExecutionSession(traces=traces)
        await archive.save_session(session)
        added = replace(trace, at_ms=128)
        updated = replace(session, traces=(*traces, added))
        with patch('execution_archive.encode_value', wraps=execution_archive.encode_value) as encode:
            root = await archive.save_session(updated)
        self.assertEqual([call.args[0] for call in encode.call_args_list], [added])
        await archive.collect(limit=len(stores))
        self.assertEqual(await archive.load_session(root), updated)
        # A same-length replacement must invalidate just that immutable item.
        corrected = replace(updated, traces=(replace(traces[0], input_json='changed'), *updated.traces[1:]))
        with patch('execution_archive.encode_value', wraps=execution_archive.encode_value) as encode:
            root = await archive.save_session(corrected)
        self.assertEqual([call.args[0] for call in encode.call_args_list], [corrected.traces[0]])
        await archive.collect(limit=len(stores))
        self.assertEqual(await archive.load_session(root), corrected)
        # Crossing the chunk boundary retains all earlier evidence too.
        newer = replace(corrected, traces=(*corrected.traces, replace(trace, at_ms=129)))
        root = await archive.save_session(newer)
        await archive.collect(limit=len(stores))
        self.assertEqual(await archive.load_session(root), newer)

    async def test_dropping_the_oldest_trace_pages_reuses_every_remaining_page(self):
        import execution_archive
        from home_runtime import ExecutionTrace
        stores = {}
        async def list_pages(): return list(stores)
        async def remove_pages(keys):
            for key in keys: stores.pop(key, None)
        archive = ExecutionArchive(lambda k: stores.setdefault(k, Store()), (list_pages, remove_pages))
        # Full 128-trace chunks exceed a page, so each trace has its own item page.
        trace = ExecutionTrace(0, 0, 0, 0, None, 'x' * 1_100, '[]', None, None, None)
        traces = tuple(replace(trace, at_ms=i) for i in range(3 * 128 + 5))
        first = await archive.save_session(ExecutionSession(traces=traces))
        dropped = pages_of(stores, first)
        added = replace(trace, at_ms=10 ** 6)
        trimmed = ExecutionSession(traces=(*traces[128:], added))
        with patch('execution_archive.encode_value', wraps=execution_archive.encode_value) as encode:
            root = await archive.save_session(trimmed)
        # Only the growing tail chunk is encoded again, never the retained full chunks.
        self.assertEqual([call.args[0] for call in encode.call_args_list], [(*traces[384:], added)])
        await archive.collect(limit=len(stores))
        self.assertEqual(set(stores), pages_of(stores, root))
        self.assertTrue(dropped - set(stores), "the oldest chunk's pages are removed")
        self.assertEqual(await ExecutionArchive(lambda k: stores[k]).load_session(root), trimmed)

    async def test_loading_reads_only_the_traces_the_runtime_retains(self):
        import execution_archive
        from home_runtime import ExecutionTrace
        stores = {}
        archive = ExecutionArchive(lambda k: stores.setdefault(k, Store()))
        trace = ExecutionTrace(0, 0, 0, 0, None, 'x' * 1_100, '[]', None, None, None)
        traces = tuple(replace(trace, at_ms=i) for i in range(1000))
        session = ExecutionSession(captured_feedback='captured', traces=traces)
        root = await archive.save_session(session)
        read = []
        with patch.object(execution_archive, 'MAX_EXECUTION_TRACES', 200):
            restored = await ExecutionArchive(lambda k: read.append(k) or stores[k]).load_session(root)
        self.assertEqual(restored, replace(session, traces=traces[-200:]))
        # One page per retained trace plus the structure; older traces are neither
        # read nor decoded, and their pages are later collected.
        self.assertLess(len(read), 200 + 30)
        self.assertLess(len(read), len(stores) // 4)

    async def test_incremental_save_does_not_reencode_unchanged_history(self):
        import execution_archive
        stores = {}
        archive = ExecutionArchive(lambda k: stores.setdefault(k, Store()))
        receipts = tuple(MeterReceipt(str(i), 'charge', 'charge', 'battery_dc', 'meter', i, i, i + 1)
                         for i in range(1024))
        session = ExecutionSession(account=Account(receipt=1024, meters=receipts))
        first = await archive.save_session(session)
        added = MeterReceipt('new', 'charge', 'charge', 'battery_dc', 'meter', 1024, 1024, 1025)
        updated = replace(session, account=replace(session.account, receipt=1025, meters=(*receipts, added)))
        with patch('execution_archive.encode_value', wraps=execution_archive.encode_value) as encode:
            second = await archive.save_session(updated)
        encoded_chunks = [call.args[0] for call in encode.call_args_list if isinstance(call.args[0], tuple)]
        self.assertEqual(encoded_chunks, [(added,)])
        self.assertEqual(await archive.load_session(first), session)
        self.assertEqual(await archive.load_session(second), updated)
        # Replacing an older receipt must rewrite its page, even at equal length.
        corrected = replace(updated, account=replace(updated.account,
            meters=(replace(receipts[0], total_mwh=99), *updated.account.meters[1:])))
        third = await archive.save_session(corrected)
        self.assertEqual(await archive.load_session(third), corrected)
        self.assertEqual(await archive.load_session(second), updated)

    async def test_many_receipts_without_replan_restore_exactly_and_accept_late_correction(self):
        stores={};archive=ExecutionArchive(lambda k:stores.setdefault(k,Store()))
        count=5000
        receipts=tuple(MeterReceipt(str(i),'charge','charge','battery_dc','meter',i*1000,i*100, i+1) for i in range(count))
        account=Account(receipt=count,meters=receipts)
        account=admit_plan(account,contract(),0,StateObservation(0,5000000,'soc'))
        session=ExecutionSession(account=account,captured_feedback='x'*1_100_000)
        root=await archive.save_session(session)
        self.assertGreater(len(canonical(__import__('runtime_json').encode_value(session))),1_000_000)
        self.assertTrue(all(len(canonical(s.value))<=PAGE_BYTES+10000 for s in stores.values()))
        restored=await ExecutionArchive(lambda k:stores[k]).load_session(root)
        self.assertEqual(restored,session)
        self.assertEqual(balance(restored.account,900000),balance(account,900000))
        self.assertEqual(objective_history(restored.account,900000),objective_history(account,900000))
        corrected=record_meter(restored.account,event_id='correction',stream='charge',direction='charge',boundary='battery_dc',epoch='meter',source_at_ms=0,total_mwh=50)
        self.assertEqual(measured(corrected.meters,'charge',0,4999000).low,499850)
        next_root=await archive.save_session(replace(session,account=corrected))
        self.assertEqual((await archive.load_session(root)).account,account)
        self.assertEqual((await archive.load_session(next_root)).account,corrected)

    async def test_incomplete_publication_preserves_previous_root_and_corruption_is_detected(self):
        stores={};archive=ExecutionArchive(lambda k:stores.setdefault(k,Store()))
        previous=ExecutionSession();root=await archive.save_session(previous)
        async def failed(page):raise OSError('disk full')
        class FailedStore(Store):async_save=staticmethod(failed)
        archive.store_for=lambda k:FailedStore()
        with self.assertRaises(OSError):await archive.save_session(replace(previous,status='new'))
        archive.store_for=lambda k:stores[k]
        self.assertEqual(await archive.load_session(root),previous)
        archive.store_for=lambda k:stores.setdefault(k,Store())
        updated=replace(previous,status='new')
        retried=await archive.save_session(updated)
        self.assertEqual(await archive.load_session(retried),updated)
        self.assertEqual(await archive.load_session(root),previous)
        stores[root].value['kind']='corrupt'
        with self.assertRaisesRegex(ValueError,'corrupt'):await archive.load_session(root)

    async def test_fixed_pages_reuse_completed_prefix_after_append(self):
        stores={};archive=ExecutionArchive(lambda k:stores.setdefault(k,Store()))
        items=[{'n':i,'text':'x'*100} for i in range(4096)]
        root=await archive.put(items);first=set(stores)
        newer=await archive.put([*items,{'n':4096,'text':'x'*100}])
        self.assertLess(len(set(stores)-first),6)
        self.assertEqual(await archive.get(root),items)
        self.assertEqual(len(await archive.get(newer)),4097)

    async def test_legacy_session_upgrade_and_rejection_are_lossless(self):
        from home_runtime import PlanRejection
        from runtime_json import encode_value
        stores={};archive=ExecutionArchive(lambda k:stores.setdefault(k,Store()))
        session=ExecutionSession(captured_feedback='captured request')
        legacy=encode_value(session);legacy.pop('plan_rejection')
        old=await archive.put(legacy)
        self.assertEqual(await archive.load_session(old),session)
        rejected=replace(session,plan_rejection=PlanRejection(123,'new-plan',2,'changed target'))
        root=await archive.save_session(rejected)
        self.assertEqual(await archive.load_session(root),rejected)
        self.assertEqual(await archive.load_session(old),session)


class PageFileTests(unittest.IsolatedAsyncioTestCase):
    prefix = 'shs_energy.execution_evidence.entry.'

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def files(self, dumps=lambda value: json.dumps(value).encode()):
        async def run(function, *args):
            return function(*args)
        return PageFiles(self.directory.name, self.prefix, run, dumps, json.loads)

    async def test_pages_are_home_assistant_storage_files_written_without_stores(self):
        from home_runtime import ExecutionTrace
        files = self.files()
        archive = ExecutionArchive(files.store_for, (files.list, files.remove))
        trace = ExecutionTrace(1, 0, 0, 0, None, 'x' * 140_000, '[]', None, None, None)
        session = ExecutionSession(captured_feedback='captured', traces=(trace, replace(trace, at_ms=2)))
        root = await archive.save_session(session)
        self.assertEqual(await ExecutionArchive(files.store_for).load_session(root), session)
        path = Path(self.directory.name, self.prefix + root)
        stored = json.loads(path.read_text())
        self.assertEqual({key: stored[key] for key in ('version', 'minor_version', 'key')},
                         {'version': 1, 'minor_version': 1, 'key': self.prefix + root})
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        listed = await files.list()
        self.assertEqual(listed, {name.removeprefix(self.prefix) for name in os.listdir(self.directory.name)})
        self.assertEqual(listed, pages_of({key: key for key in listed}, root,
                         lambda key: json.loads(Path(self.directory.name, self.prefix + key).read_text())['data']))
        # Pages Home Assistant's Store wrote (indented) remain readable.
        path.write_text(json.dumps(stored, indent=2))
        self.assertEqual(await files.store_for(root).async_load(), stored['data'])
        # Missing and unreadable pages are reported by the archive as missing or corrupt.
        path.write_text('{')
        with self.assertRaisesRegex(ValueError, 'missing or corrupt'):
            await ExecutionArchive(files.store_for).load_session(root)
        await files.remove([root, root])
        self.assertIsNone(await files.store_for(root).async_load())

    async def test_a_failed_write_raises_and_leaves_no_file(self):
        files = self.files(dumps=lambda value: 'not bytes')
        with self.assertRaises(TypeError):
            await ExecutionArchive(files.store_for).save_session(ExecutionSession())
        self.assertEqual(os.listdir(self.directory.name), [])


def pages_of(stores, root, read=lambda store: store.value):
    """Every page a root names, followed through the stored pages themselves."""
    seen, pending = set(), [root]
    while pending:
        key = pending.pop()
        if key not in seen:
            seen.add(key)
            page = read(stores[key])
            pending.extend(page['fields'].values() if page['kind'] == 'object' else page.get('children', []))
    return seen


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    def archive(self, stores, removed=None, gate=None):
        async def list_pages():
            return set(stores)

        async def remove_pages(keys):
            if gate is not None:
                await gate.wait()
            for key in keys:
                stores.pop(key, None)
                if removed is not None:
                    removed.append(key)
        return ExecutionArchive(lambda k: stores.setdefault(k, Store()), (list_pages, remove_pages))

    def session(self, count):
        return ExecutionSession(account=Account(receipt=count, meters=tuple(
            MeterReceipt(str(i), 'charge', 'charge', 'battery_dc', 'meter', i, i, i + 1) for i in range(count))))

    async def test_collection_keeps_exactly_the_pages_the_saved_root_reaches(self):
        orphan = '0' * 64
        stores, removed = {orphan: Store(), 'not-a-page': Store()}, []
        archive = self.archive(stores, removed)
        first = await archive.save_session(self.session(300))
        latest = self.session(301)
        root = await archive.save_session(latest)
        superseded = pages_of(stores, first) - pages_of(stores, root)
        self.assertTrue(superseded)
        await archive.collect()
        self.assertEqual(set(stores) - {'not-a-page'}, pages_of(stores, root))
        self.assertEqual(set(removed), superseded | {orphan})
        self.assertEqual(await ExecutionArchive(lambda k: stores[k]).load_session(root), latest)

    async def test_content_that_returns_after_removal_is_written_again(self):
        stores = {}
        archive = self.archive(stores)
        original = self.session(10)
        root = await archive.save_session(original)
        await archive.save_session(self.session(20))
        await archive.collect()
        self.assertNotIn(root, stores)
        self.assertEqual(await archive.save_session(original), root)
        await archive.collect()
        self.assertEqual(await ExecutionArchive(lambda k: stores[k]).load_session(root), original)

    async def test_removal_is_bounded_and_drains_over_later_checkpoints(self):
        stores = {f'{i:064x}': Store() for i in range(5)}
        archive = self.archive(stores)
        self.assertEqual(await archive.collect(), 0, 'nothing is removed before a tree is saved')
        await archive.save_session(self.session(3))
        self.assertEqual([await archive.collect(limit=2) for _ in range(4)], [2, 2, 1, 0])

    async def test_a_page_written_during_removal_waits_for_it_even_if_collection_is_cancelled(self):
        import asyncio
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                stores, gate = {}, asyncio.Event()
                archive = self.archive(stores, gate=gate)
                original = self.session(10)
                root = await archive.save_session(original)
                await archive.save_session(self.session(20))
                collecting = asyncio.create_task(archive.collect())
                await asyncio.sleep(0)
                if cancel:
                    collecting.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await collecting
                # The same content is needed again while its removal is still running.
                saving = asyncio.create_task(archive.save_session(original))
                for _ in range(3):
                    await asyncio.sleep(0)
                self.assertFalse(saving.done())
                gate.set()
                if not cancel:
                    await collecting
                self.assertEqual(await saving, root)
                self.assertEqual(await ExecutionArchive(lambda k: stores[k]).load_session(root), original)
