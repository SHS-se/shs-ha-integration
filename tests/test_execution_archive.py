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
