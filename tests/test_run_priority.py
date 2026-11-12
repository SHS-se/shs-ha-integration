"""Live priority across whole runs (user requirement, 10 November 2026)."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.append(str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from shs_core import run_priority
from shs_core.controller import ScheduledController
from shs_core.device_port import device_intent
from shs_core.household import Household
from shs_core.run_priority import (QUARTER_MS, ChargeOutlook, RecoveryWindow, advance_delivery, charge_outlook, charge_slack_h, heat_slack_h,
    share_budget, share_recovery, summarize_runs, valid_run)

START = datetime(2026, 11, 10, 1, tzinfo=timezone.utc)
T0 = int(START.timestamp() * 1000)
FIXTURE = Path(__file__).parent / 'fixtures/schema-10-mixed-mode-plan.json'


class CapacityEvidenceTests(unittest.TestCase):
    def test_small_trim_is_local_and_a_full_current_step_quarter_can_move(self):
        step = run_priority.EV_WATTS_PER_AMP
        car = {'kind': 'ev', 'want_w': 10 * step, 'step_w': step, 'slack_wh': -4.25}
        self.assertFalse(run_priority.material_capacity_shortfall(car, 9 * step))
        self.assertTrue(run_priority.material_capacity_shortfall({**car, 'slack_wh': -step / 4}, 9 * step))
        self.assertFalse(run_priority.material_capacity_shortfall({**car, 'slack_wh': -step / 4}, 10 * step))

    def test_room_recovery_uses_one_quarter_of_thermal_slack(self):
        room = {'kind': 'heat', 'want_w': 1500, 'slack_h': -0.02}
        self.assertFalse(run_priority.material_capacity_shortfall(room, 0))
        self.assertTrue(run_priority.material_capacity_shortfall({**room, 'slack_h': -0.25}, 0))


def slot(index, **values):
    return {'start': (START + timedelta(minutes=15 * index)).isoformat().replace('+00:00', 'Z'),
            'ev_target_current_a': 0, 'ev_max_current_a': 0, 'ev_w': 0, 'grid_import_w': 2000, **values}


def plan():
    charging = dict(ev_target_current_a=10, ev_max_current_a=16, ev_w=6900)
    slots = [slot(0, **charging, grid_import_w=11010), slot(1, **charging, grid_import_w=11090),
             slot(2, **charging, grid_import_w=14000), slot(3), slot(4, **charging, grid_import_w=9000)]
    return {'plan_id': 'plan', 'grid': {'import_limit_w': 15000}, 'plans': {'priority': {'slots': slots}},
            'thermal_projection': {'starts': [row['start'] for row in slots], 'zones': [{
                'key': 'bathroom', 'device_keys': ['floor'], 'rated_power_w': 1000,
                'comfort_min_c': [19, 19, 19, 21, 21], 'planned_temperature_c': [20.0, 19.8, 19.6, 20.1, 20.6],
                'planned_power_w': [0, 0, 0, 1000, 1000]}]}}


class SummaryTests(unittest.TestCase):
    def test_runs_are_summarised_once_into_a_few_rows(self):
        runs = summarize_runs(plan())
        self.assertEqual(runs['ev'], {'kind': 'charge', 'id': 'plan:ev', 'windows': [
            [T0, T0 + 2 * QUARTER_MS, 6900, 11040, 3900],
            [T0 + 2 * QUARTER_MS, T0 + 3 * QUARTER_MS, 6900, 11040, 1000],
            [T0 + 4 * QUARTER_MS, T0 + 5 * QUARTER_MS, 6900, 11040, 6000]]})
        self.assertEqual(runs['device:floor'], {'kind': 'heat', 'floor': [
            [T0, T0 + 3 * QUARTER_MS, 19.0], [T0 + 3 * QUARTER_MS, T0 + 5 * QUARTER_MS, 21.0]],
            'warm_c_per_h': 2.0, 'cool_c_per_h': 0.8})
        self.assertTrue(all(valid_run(run) for run in runs.values()))

    def test_battery_run_comes_from_its_stored_energy_objectives(self):
        source = json.loads(FIXTURE.read_text())['plan']
        run = summarize_runs(source)['battery']
        contract = source['battery_execution']
        objective = next(row for row in contract['objectives'] if row['kind'] == 'stored_energy')
        self.assertIn([objective['start_ms'], objective['deadline_ms'], objective['target_mwh'] // 1000], run['targets'])
        self.assertEqual(run['capacity_wh'], contract['capacity_mwh'] // 1000)
        self.assertEqual(run['windows'][0][2:4], [8800, 8800], 'Two charging quarters coalesce')
        recovery = contract['recovery'][0]
        self.assertIn([recovery['start_ms'], recovery['end_ms'], 0, 4180, 7800], run['windows'],
                      'Authorised recovery uses the forecast spare from the same quarter')
        self.assertLess(len(run['windows']), len(contract['intervals']))
        self.assertTrue(valid_run(run))

    def test_unreadable_plans_and_rooms_leave_other_runs_intact(self):
        self.assertEqual(summarize_runs({}), {})
        self.assertEqual(summarize_runs({'plans': {'priority': {'slots': [{'start': None}]}}}), {})
        broken = plan()
        broken['thermal_projection']['zones'][0].pop('planned_power_w')
        self.assertEqual(set(summarize_runs(broken)), {'ev'})

    def test_summaries_crossing_the_gateway_are_checked(self):
        run = summarize_runs(plan())['ev']
        for bad in ('run', {**run, 'extra': 1}, {**run, 'windows': [[1, 0, 1, 1, 1]]},
                    {**run, 'windows': [[0, 1, 'x', 1, 1]]}, {'kind': 'heat', 'floor': []}):
            self.assertFalse(valid_run(bad), bad)
        self.assertTrue(valid_run(None))


class SlackTests(unittest.TestCase):
    def test_nearly_full_battery_is_urgent_when_its_window_is_about_to_close(self):
        run = {'kind': 'charge', 'id': 'c', 'efficiency': 1.0, 'capacity_wh': 18000,
               'windows': [[T0, T0 + 8 * QUARTER_MS, 4000, 8800, 2000]], 'targets': [[T0, T0 + 8 * QUARTER_MS, 9900]]}
        closing = charge_outlook(run, T0 + 8 * QUARTER_MS - 6 * 60_000, stored_wh=9500)
        early = charge_outlook(run, T0, stored_wh=5000)
        self.assertEqual(closing, ChargeOutlook(400, 400, (RecoveryWindow(
            T0 + 8 * QUARTER_MS - 6 * 60_000, T0 + 8 * QUARTER_MS, 2000, 2000),)))
        slack = share_recovery({'closing': closing})['closing']
        self.assertLess(charge_slack_h(slack, 4000), .1, '96% full, minutes left')
        self.assertGreater(charge_slack_h(share_recovery({'early': early})['early'], 4000), 1,
                           'Half empty with two hours of spare capacity')

    def test_shortfall_is_carried_across_quarters_from_measured_delivery(self):
        run = summarize_runs(plan())['ev']
        progress = advance_delivery(None, run, T0, 6900)
        for minute in range(4, 44, 4):  # Ten reports at 6 A through three quarters.
            progress = advance_delivery(progress, run, T0 + minute * 60_000, 4140)
        self.assertAlmostEqual(progress['shortfall_wh'], (6900 - 4140) * 36 / 60, places=6)
        outlook = charge_outlook(run, T0 + 40 * 60_000, shortfall_wh=progress['shortfall_wh'])
        self.assertAlmostEqual(outlook.planned_wh, 6900 * 5 / 60)
        self.assertAlmostEqual(outlook.required_wh, 6900 * 5 / 60 + 1656)
        self.assertLess(share_recovery({'ev': outlook})['ev'], 0, 'More than the contiguous run can still make up')
        # A restart or silent meter is unknown, never measured zero.
        resumed = advance_delivery(progress, run, progress['at_ms'] + run_priority.DELIVERY_GAP_MS + 1, 4140)
        self.assertEqual(resumed['shortfall_wh'], progress['shortfall_wh'])
        # A boost above the plan earns credit that makes it easy to reclaim.
        boosted = advance_delivery(advance_delivery(None, run, T0, 9660), run, T0 + 240_000, 9660)
        self.assertLess(boosted['shortfall_wh'], 0)
        # A replacement plan already holds the measured state.
        self.assertEqual(advance_delivery(progress, {**run, 'id': 'next:ev'}, T0 + QUARTER_MS, 0)['shortfall_wh'], 0)

    def test_a_run_ends_where_its_authorised_windows_stop(self):
        run = summarize_runs(plan())['ev']
        outlook = charge_outlook(run, T0)
        self.assertAlmostEqual(outlook.planned_wh, 6900 * .75, msg='The later block is the planner’s to offer')
        self.assertEqual(charge_outlook(run, T0 + 3 * QUARTER_MS).planned_wh, 0, 'Nothing is authorised now')

    def test_recovery_capacity_is_spent_once_across_runs(self):
        run = {'windows': [[0, 3_600_000, 1000, 1800, 800]]}
        slack = share_recovery({'ev': charge_outlook(run, 0, shortfall_wh=500),
                                'battery': charge_outlook(run, 0, shortfall_wh=200)})
        self.assertEqual(slack, {'ev': 300.0, 'battery': 100.0}, 'The battery sees the pool less what the car needs')

    def test_disjoint_recovery_windows_do_not_compete(self):
        hour = 3_600_000
        ev = {'windows': [[0, hour, 1000, 1000, 0], [hour, 2 * hour, 1000, 2000, 1000]]}
        battery = {'windows': [[0, hour, 1000, 1000, 0], [2 * hour, 3 * hour, 1000, 2000, 1000]],
                   'targets': [[0, 3 * hour, 2500]], 'efficiency': 1.0}
        outlooks = {'ev': charge_outlook(ev, 0, shortfall_wh=1000),
                    'battery': charge_outlook(battery, 0, stored_wh=0)}
        self.assertEqual(share_recovery(outlooks), {'ev': 0, 'battery': 500})

    def test_recovery_keeps_alternative_windows_available(self):
        hour = 3_600_000
        ev = {'windows': [[0, 2 * hour, 1000, 2000, 1000]]}
        battery = {'windows': [[0, hour, 1000, 2000, 1000]],
                   'targets': [[0, hour, 1300]], 'efficiency': 1.0}
        outlooks = {'ev': charge_outlook(ev, 0, shortfall_wh=1500),
                    'battery': charge_outlook(battery, 0, stored_wh=0)}
        self.assertEqual(share_recovery(outlooks), {'ev': 500, 'battery': 200})

    def test_overlapping_recovery_offers_share_one_grid_limit(self):
        hour = 3_600_000
        ev = {'windows': [[0, hour, 1000, 1800, 1000]]}
        battery = {'windows': [[0, hour, 1000, 1600, 1000], [0, hour, 0, 500, 1000]],
                   'targets': [[0, hour, 700]], 'efficiency': 1.0}
        outlooks = {'ev': charge_outlook(ev, hour // 2, shortfall_wh=350),
                    'battery': charge_outlook(battery, hour // 2, stored_wh=0)}
        self.assertEqual(share_recovery(outlooks), {'ev': 50, 'battery': -50})

    def test_room_slack_follows_its_floor_and_how_fast_it_recovers(self):
        run = summarize_runs(plan())['device:floor']
        stable = {**run, 'floor': run['floor'][:1]}
        warm, near = heat_slack_h(stable, T0, 21.0), heat_slack_h(stable, T0, 19.1)
        self.assertAlmostEqual(warm, (2 / .8) / (1 + .8 / 2))
        self.assertLess(near, .1)
        self.assertLess(heat_slack_h(run, T0 + 3 * QUARTER_MS, 20.5), 0, 'Below the later, higher floor')
        slow = heat_slack_h({**stable, 'warm_c_per_h': .4}, T0, 21.0)
        self.assertLess(slow, warm, 'A slow room needs longer to recover the same setback')
        self.assertEqual(heat_slack_h(None, T0, 25.0), 0, 'No known floor is no invented margin')
        self.assertEqual(heat_slack_h({**run, 'cool_c_per_h': None}, T0, 25.0), 0)

    def test_rising_comfort_floor_accounts_for_warm_up_time(self):
        run = {'floor': [[0, 2 * QUARTER_MS, 19]], 'cool_c_per_h': 1, 'warm_c_per_h': 2}
        self.assertAlmostEqual(heat_slack_h(run, 0, 20), 2 / 3)
        rising = {**run, 'floor': [[0, QUARTER_MS, 19], [QUARTER_MS, 2 * QUARTER_MS, 21]]}
        self.assertAlmostEqual(heat_slack_h(rising, 0, 20), -1 / 6)
        self.assertEqual(heat_slack_h({**rising, 'warm_c_per_h': None}, 0, 20), 0)

    def test_future_floor_only_binds_while_it_is_in_effect(self):
        quarter = QUARTER_MS
        run = {'floor': [[0, quarter, 18], [quarter, 2 * quarter, 19.6]],
               'cool_c_per_h': 1, 'warm_c_per_h': 2}
        self.assertAlmostEqual(heat_slack_h(run, 0, 20), .4)
        run['floor'][1][1] = quarter + 300_000
        self.assertAlmostEqual(heat_slack_h(run, 0, 20), 4 / 3)


class BudgetTests(unittest.TestCase):
    @staticmethod
    def load(owner, kind, slack, want, *, step=0, floor=0, trimmed=False):
        return dict(owner=owner, kind=kind, slack_h=slack, want_w=want, step_w=step, floor_w=floor, trimmed=trimmed)

    def test_the_least_pressing_run_yields_the_smallest_supported_step(self):
        car = self.load('ev', 'ev', 2.0, 6900, step=690, floor=3450)
        battery = self.load('battery', 'battery', .2, 4000)
        room = self.load('device:room', 'heat', .1, 1000, floor=1000)
        shares, left = share_budget(11500, [car, battery, room])
        self.assertEqual(shares, {'device:room': 1000, 'battery': 4000, 'ev': 6210})
        self.assertEqual(left, 290)
        shares, _ = share_budget(7000, [car, battery, room])
        self.assertEqual(shares['ev'], 0, 'Below its minimum current the car pauses')
        shares, _ = share_budget(11500, [{**car, 'slack_h': .05}, battery, {**room, 'slack_h': 3.0}])
        self.assertEqual(shares, {'ev': 6900, 'battery': 4000, 'device:room': 0}, 'A heater is all or nothing')

    def test_effectively_equal_impact_keeps_the_current_allocation(self):
        car = self.load('ev', 'ev', 1.0, 6900, step=690, floor=3450, trimmed=True)
        battery = self.load('battery', 'battery', 1.05, 4000)
        self.assertEqual(share_budget(10000, [car, battery])[0], {'battery': 4000, 'ev': 5520})
        self.assertEqual(share_budget(10000, [car, {**battery, 'slack_h': 1.5}])[0], {'ev': 6900, 'battery': 3100})

    def test_without_evidence_the_earlier_order_decides(self):
        loads = [self.load('battery', 'battery', 0.0, 4000), self.load('ev', 'ev', 0.0, 6900, step=690, floor=3450),
                 self.load('device:room', 'heat', 0.0, 1000, floor=1000)]
        self.assertEqual(share_budget(9000, loads)[0], {'device:room': 1000, 'ev': 6900, 'battery': 1100})


class PlumbingTests(unittest.TestCase):
    def test_intents_carry_the_run_summary(self):
        run = summarize_runs(plan())['ev']
        ev = device_intent('ev', 'apply', {'device_models': []}, {'start': 'now', 'ev_target_current_a': 10,
            'ev_min_current_a': 5, 'ev_max_current_a': 16, 'run': run}, configuration_revision=1, policy_revision=1)
        self.assertEqual(ev['parameters']['run'], run)
        plain = device_intent('device:floor', 'apply', {'device_models': []}, {'start': 'now', 'device_commands': {}},
                              configuration_revision=1, policy_revision=1)
        self.assertNotIn('run', plain['parameters'])

    def test_controller_attaches_the_summary_prepared_with_the_plan(self):
        run = {'kind': 'heat'}
        owner = SimpleNamespace(coordinator=SimpleNamespace(
            run_summary=lambda device, options: run if device == 'device:floor' else None))
        self.assertEqual(ScheduledController.with_run(owner, 'device:floor', {}, {'start': 's'}), {'start': 's', 'run': run})
        self.assertEqual(ScheduledController.with_run(owner, 'ev', {}, {'start': 's'}), {'start': 's'})
        owner.coordinator = SimpleNamespace()
        self.assertEqual(ScheduledController.with_run(owner, 'ev', {}, {'start': 's'}), {'start': 's'})

    def test_household_summarises_each_accepted_plan_once(self):
        source = json.loads(FIXTURE.read_text())['plan']
        options = {'device_modes': deepcopy(source['operating_scope']['modes'])}
        household = SimpleNamespace(optimisation_plan=source, _run_summaries={})
        calls = []
        original = run_priority.summarize_runs
        run_priority.summarize_runs = lambda value: calls.append(1) or original(value)
        self.addCleanup(setattr, run_priority, 'summarize_runs', original)
        first = Household.run_summary(household, 'battery', options)
        self.assertIs(Household.run_summary(household, 'battery', options), first)
        self.assertEqual(len(calls), 1, 'Live control never scans the plan again')
        self.assertEqual(first['kind'], 'charge')
        household.optimisation_plan = deepcopy(source)
        self.assertEqual(Household.run_summary(household, 'battery', options), first)
        self.assertEqual(len(calls), 2, 'A newly accepted plan is summarised again')
        household.optimisation_plan = None
        self.assertIsNone(Household.run_summary(household, 'battery', options))


if __name__ == '__main__':
    unittest.main()
