"""Lossless archive publication and replay beyond transport checkpoint limits."""
import json
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.append(str(Path(__file__).parents[1]/'custom_components'/'shs_energy'))
from execution_archive import ExecutionArchive, canonical, PAGE_BYTES
from home_runtime import ExecutionSession
from plan_execution import Account, MeterReceipt, StateObservation, admit_plan, balance, measured, objective_history, record_meter
from test_plan_execution import contract

class Store:
    def __init__(self):self.value=None
    async def async_save(self,value):self.value=value
    async def async_load(self):return self.value

class ArchiveTests(unittest.IsolatedAsyncioTestCase):
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
