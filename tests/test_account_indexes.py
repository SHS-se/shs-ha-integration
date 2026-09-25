"""Indexed accounting agrees with complete replay, including late corrections."""
from dataclasses import replace
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch

sys.path.append(str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
import plan_execution as e
from runtime_json import encode_value
from execution_archive import read_account
from test_plan_execution import opening_account, meter, contract


class AccountIndexTests(unittest.TestCase):
    def test_indexed_arrivals_corrections_resets_and_old_snapshots_match_replay(self):
        rng = random.Random(412)
        account = e.Account()
        snapshots = []
        for index in range(180):
            at = rng.randrange(30) * 1000
            direction = rng.choice(('charge', 'discharge'))
            account = e.record_meter(account, event_id=str(index), stream=direction,
                direction=direction, boundary='battery_dc', epoch=str(rng.randrange(2)),
                source_at_ms=at, total_mwh=rng.randrange(1000))
            snapshots.append(account)
            oracle = e.MeterIndex(account.meters)
            for stream in ('charge', 'discharge'):
                self.assertEqual(account.meter_index.neighbours(stream, at), oracle.neighbours(stream, at))
                for start, end in ((0, 29000), (3500, 13500), (10000, 20000), (0, 40000)):
                    self.assertEqual(account.meter_index.measure(stream, start, end), oracle.measure(stream, start, end))
        for old in snapshots[::13]:
            self.assertEqual(old.meter_index.measure('charge', 0, 20000),
                             e.measured(old.meters, 'charge', 0, 20000))
            self.assertEqual(read_account(encode_value(old)), old)

    def test_observations_and_requests_reuse_meter_history_without_rebuilding_it(self):
        account = opening_account()
        original_index = account.meter_index
        with patch.object(e.MeterIndex, '__init__', side_effect=AssertionError('full rebuild')):
            for at in range(100):
                account = e.observe_state(account, e.StateObservation(at, 5000000, 'SOC'))
                account = e.request_replan(account)
                self.assertIs(account.meter_index, original_index)
            account = meter(account, 'charge', 1000, 100)
        self.assertEqual(account.meter_index.measure('charge', 0, 1000), e.Bounds(100, 100))
        self.assertEqual(original_index.measure('charge', 0, 1000), e.Bounds(0, None))

    def test_warm_live_view_matches_a_cold_restore_after_late_evidence_and_handover(self):
        account = opening_account()
        for at, total in ((100, 10), (200, 20), (100, 15), (50, 2)):
            e.live_feedback(account, 200)
            account = meter(account, 'charge', at, total)
            account = e.observe_state(account, e.StateObservation(at, 5000000 + total, 'SOC'))
            cold = read_account(encode_value(account))
            self.assertEqual(e.live_feedback(account, 200), e.live_feedback(cold, 200))
        account = e.request_replan(account)
        next_plan = replace(contract(generation=account.requested_generation, previous=account.contract.id),
                            source_receipt=account.requests[-1].source_receipt)
        account = e.admit_plan(account, next_plan, 200, e.StateObservation(200, 5000020, 'SOC'))
        e.live_feedback(account, 200)
        account = meter(account, 'charge', 50, 3)
        self.assertEqual(e.live_feedback(account, 200), e.live_feedback(read_account(encode_value(account)), 200))
        # Historical reads must not use the newer admission catalog.
        self.assertEqual(e.live_feedback(account, 100), e.live_feedback(read_account(encode_value(account)), 100))

    def test_observed_and_measured_corrections_remain_distinct(self):
        account = opening_account()
        old = e.observe_state(account, e.StateObservation(50, 4900000, 'SOC'))
        new = e.observe_state(old, e.StateObservation(50, 4800000, 'inferred', False))
        self.assertEqual(new._observed_at.get(50).stored_mwh, 4800000)
        self.assertEqual(new._measured_at.get(50).stored_mwh, 4900000)
        self.assertEqual(old._observed_at.get(50).stored_mwh, 4900000)

    def test_historical_physical_mapping_still_prevents_double_counting(self):
        account = e.Account()
        for index, physical in enumerate(('first', 'second')):
            account = e.record_meter(account, event_id=str(index), stream='a', direction='charge',
                boundary='battery_dc', epoch='0', source_at_ms=index, total_mwh=index, physical_id=physical)
        with self.assertRaisesRegex(ValueError, 'counted twice'):
            e.record_meter(account, event_id='third', stream='b', direction='charge',
                boundary='battery_dc', epoch='0', source_at_ms=3, total_mwh=3, physical_id='first')

    def test_public_restore_still_rejects_broken_history_and_duplicate_conflicts(self):
        account = opening_account()
        with self.assertRaisesRegex(ValueError, 'unique'):
            replace(account, meters=account.meters + account.meters[:1])
        row = account.meters[0]
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            e.record_meter(account, event_id=row.event_id, stream=row.stream, direction=row.direction,
                boundary=row.boundary, epoch=row.epoch, source_at_ms=row.source_at_ms, total_mwh=999)
