"""The battery keeps the last settings SHS sent; only an explicit setting releases it.

User requirement, 3 October 2026: the battery is handed back only when its
control mode says so. A restart, a lost plan, stale or failing readings and a
settings change all leave it in the mode the controller last set.
"""
from copy import deepcopy
import unittest

import test_battery_runtime as fixtures

Rig, iso = fixtures.Rig, fixtures.iso
BASELINE = ('Maximum Self Consumption', 4.0, 4.0)


def settings(r):
    return (r.rows['select.mode']['state'], float(r.rows['number.charge']['state']),
            float(r.rows['number.discharge']['state']))


class BatteryContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def commanded(self):
        r = Rig()
        await r.start()
        self.addAsyncCleanup(r.runtime.close)
        for _ in range(2):
            await r.advance(5000)
        self.assertEqual(settings(r)[0], 'Command Charging (PV First)', r.runtime.snapshot())
        self.assertTrue(r.runtime.host.state.groups[0].owned)
        r.calls.clear()
        return r, settings(r)

    def held(self, r, before):
        self.assertEqual(r.calls, [], r.runtime.snapshot())
        self.assertEqual(settings(r), before)
        group = r.runtime.host.state.groups[0]
        self.assertTrue(group.owned)
        self.assertFalse(group.release_pending)

    async def test_expired_plan_holds_the_last_settings(self):
        r, before = await self.commanded()
        r.now = 900001
        for _ in range(36):
            await r.advance(10000)
        self.held(r, before)
        status = r.runtime.snapshot()
        self.assertEqual(status['state'], 'idle', status)
        self.assertIn('holding the last settings SHS sent', status['reason'])

    async def test_missing_schedule_holds_the_last_settings(self):
        r, before = await self.commanded()
        r.coordinator.binding_plan_for = lambda device,options:({},None)
        for _ in range(4):
            await r.advance(10000)
        self.held(r, before)

    async def test_plan_without_battery_instructions_holds_and_asks_for_one(self):
        r, before = await self.commanded()
        del r.plan['battery_execution']
        for _ in range(4):
            await r.advance(10000)
        self.held(r, before)
        self.assertTrue(r.replans)

    async def test_stale_measurements_hold_the_last_settings(self):
        r, before = await self.commanded()
        for _ in range(12):
            r.now += 10000  # No source reports again.
            await r.runtime.refresh()
            await r.runtime.host.idle()
        self.held(r, before)
        status = r.runtime.snapshot()
        self.assertEqual(status['state'], 'limited', status)
        self.assertIn('holding the last settings SHS sent', status['reason'])

    async def test_failing_refresh_holds_the_last_settings(self):
        r, before = await self.commanded()
        async def unavailable():
            raise RuntimeError('website choices cannot be read')
        r.coordinator.async_battery_planned_devices = unavailable
        for _ in range(4):
            await r.advance(10000)
        self.held(r, before)
        status = r.runtime.snapshot()
        self.assertEqual(status['state'], 'fault', status)
        self.assertIn('holding the last settings SHS sent', status['reason'])

    async def test_restart_resumes_the_same_settings_with_or_without_a_plan(self):
        for plan in (True, False):
            with self.subTest(plan=plan):
                r, before = await self.commanded()
                await r.runtime.close()
                restarted = Rig()
                restarted.now, restarted.rows = r.now+60000, deepcopy(r.rows)
                for row in restarted.rows.values():
                    row['last_reported'] = iso(restarted.now)
                restarted.store.saved, restarted.store.session = deepcopy(r.store.saved), r.store.session
                restarted.fence_store.saved = deepcopy(r.fence_store.saved)
                if not plan:
                    restarted.coordinator.binding_plan_for = lambda device,options:({},None)
                await restarted.fence.open()
                await restarted.runtime.open()
                self.addAsyncCleanup(restarted.runtime.close)
                for _ in range(4):
                    await restarted.advance(5000)
                self.assertFalse([call for call in restarted.calls if call[2].get('option')], restarted.runtime.snapshot())
                self.assertEqual(settings(restarted)[0], before[0])
                self.assertTrue(restarted.runtime.host.state.groups[0].owned)
                if plan:
                    self.assertEqual(restarted.runtime.snapshot()['state'], 'controlling', restarted.runtime.snapshot())

    async def test_settings_change_rebinds_without_a_handover(self):
        r, before = await self.commanded()
        host = r.runtime.host
        r.options['unrelated_pool_setting'] = 28
        for _ in range(3):
            await r.advance(5000)
        self.held(r, before)
        self.assertIs(r.runtime.host, host)
        r.options['battery_charge_efficiency'] = .9
        for _ in range(3):
            await r.advance(5000)
        self.assertFalse([call for call in r.calls if call[2].get('option')], r.runtime.snapshot())
        self.assertEqual(settings(r)[0], before[0])
        self.assertTrue(r.runtime.host.state.groups[0].owned)
        self.assertEqual(r.runtime.snapshot()['state'], 'controlling', r.runtime.snapshot())

    async def test_changed_meters_keep_the_command_journal(self):
        r, before = await self.commanded()
        ledger = r.runtime.host.state.ledger.mapping_revision
        r.rows['sensor.grid_import_new'] = deepcopy(r.rows['sensor.grid_import'])
        r.options['entities_grid_import'] = ['sensor.grid_import_new']
        for _ in range(3):
            await r.advance(5000)
        self.held(r, before)
        self.assertNotEqual(r.runtime.host.state.ledger.mapping_revision, ledger)
        self.assertEqual(r.runtime.snapshot()['state'], 'controlling', r.runtime.snapshot())

    async def test_changed_control_entity_is_refused_and_holds(self):
        r, before = await self.commanded()
        r.rows['number.charge_new'] = deepcopy(r.rows['number.charge'])
        r.options['battery_charge_limit_entity'] = 'number.charge_new'
        for _ in range(3):
            await r.advance(5000)
        self.held(r, before)
        status = r.runtime.snapshot()
        self.assertEqual(status['state'], 'fault', status)
        self.assertIn('Set the battery to Verification, then back to Controlling', status['reason'])

    async def test_unreadable_manual_override_holds(self):
        r, before = await self.commanded()
        r.options['battery_control_override_entity'] = 'input_boolean.manual_battery'
        for state in (None, 'unavailable', 'unknown'):
            if state:
                r.rows['input_boolean.manual_battery'] = {'state':state,'attributes':{},'last_reported':iso(r.now)}
            for _ in range(2):
                await r.advance(5000)
            self.held(r, before)

    async def test_explicit_settings_still_return_the_battery_to_its_baseline(self):
        def monitoring(r): r.options['device_modes']['$battery'] = 'monitoring'
        def disabled(r): r.options['battery_enabled'] = False
        def excluded(r): r.options['excluded_device_readings'] = ['$battery']
        def overridden(r):
            r.options['battery_control_override_entity'] = 'input_boolean.manual_battery'
            r.rows['input_boolean.manual_battery'] = {'state':'on','attributes':{},'last_reported':iso(r.now)}
        for release in (monitoring, disabled, excluded, overridden):
            with self.subTest(release=release.__name__):
                r, before = await self.commanded()
                release(r)
                for _ in range(40):
                    await r.advance(10000)
                    if not r.runtime.host.state.groups[0].owned:
                        break
                self.assertEqual(settings(r), BASELINE, r.runtime.snapshot())
                self.assertFalse(r.runtime.host.state.groups[0].owned)
