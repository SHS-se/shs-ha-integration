"""Real agreement and executor against a journal-aware simulated HA plant."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import unittest

import test_control_agreement as agreement_tests
from battery_controller import BatteryController
from battery_policy import desired_state, native_ceiling, operation
from control_setup import PRESETS, describe_inventory
from contract_v1.validate import revision


class BatteryTests(unittest.IsolatedAsyncioTestCase):
    response = agreement_tests.AgreementTests.response
    grant = agreement_tests.AgreementTests.grant
    guard = agreement_tests.AgreementTests.guard
    advance = agreement_tests.AgreementTests.advance

    async def asyncSetUp(self):
        await agreement_tests.AgreementTests.asyncSetUp(self)
        self.definition['controls'][0]['desired']['objective']['allow_battery_export'] = True
        self.key = self.definition['controls'][0]['control_id']
        self.binding = self.setup.records[self.key]
        self.inv = self.setup.inventory()
        values = dict(authority='on', authority_confirmation='Remote EMS', mode='Maximum Self Consumption',
                      charge_ceiling=8000, discharge_ceiling=9000, battery_power=0, pv_power=5000, load_power=3000,
                      grid_import_power=0, grid_export_power=2000, soc=60, soc_floor=5)
        self.states = {}
        for role, registry in self.binding['spec']['roles'].items():
            entity = next(e for e in self.inv.values() if e['registry'] == registry)
            self.states[entity['entity_id']] = SimpleNamespace(state=str(values[role]), attributes=deepcopy(entity['attributes']), last_reported=self.wall)
        def inventory():
            raw = deepcopy(self.inv)
            for key, state in self.states.items():
                raw[key].update(state=state.state, attributes=state.attributes)
            return describe_inventory(raw)
        self.setup.inventory = inventory
        self.options = dict(battery_capacity_kwh=20, battery_charge_efficiency=.95, battery_discharge_efficiency=.95,
                            grid_import_limit_w=12000, grid_export_limit_w=12000)
        self.calls = []; self.hook = None; self.observe = True
        self.journal = agreement_tests.Store()
        async def service(domain, service, data, blocking):
            key = data['entity_id']; value = data.get('value', data.get('option', 'on' if service == 'turn_on' else 'off'))
            pending = self.journal.payload['records'][self.key]['pending']
            self.assertIsNotNone(pending, 'every service call must have a durable write-ahead record')
            self.assertEqual(pending['value'], value)
            self.assertIn(key, {'select.mode', 'switch.authority', 'number.charge_ceiling', 'number.discharge_ceiling'})
            if domain == 'number': self.assertGreaterEqual(value, 0)
            if domain == 'select': self.assertIn(value, PRESETS['sigenergy']['modes'].values())
            self.calls.append((key, value))
            if self.hook and await self.hook(key, value): return
            self.advance(.01)
            self.states[key].state = str(value); self.states[key].last_reported = self.wall
            if key == 'switch.authority':
                self.states['sensor.authority_confirmation'].state = 'Remote EMS'
                self.states['sensor.authority_confirmation'].last_reported = self.wall
            if self.observe:
                for name, state in self.states.items():
                    if name.startswith('sensor.'): state.last_reported = self.wall
        self.hass = SimpleNamespace(states=SimpleNamespace(get=self.states.get), services=SimpleNamespace(async_call=service))
        async def sleep(seconds): self.advance(seconds)
        self.sleep = sleep
        self.controller = self.new_controller()
        await self.controller.async_start()
        await self.grant()

    def new_controller(self):
        return BatteryController(self.hass, self.setup, self.agreement, self.journal, lambda: self.options,
                                 wall=lambda: self.wall, monotonic=lambda: self.mono, sleep=self.sleep, confirm_seconds=.5)

    async def intent(self, intent, charge=None, discharge=None):
        cmd = self.plan['commands'][0]
        cmd['instruction'] = dict(intent=intent,
            charge_ceiling_w=charge if charge is not None else (2000 if intent in ('self_consumption', 'solar_charge', 'charge_pv_first', 'charge_grid_first') else 0),
            discharge_ceiling_w=discharge if discharge is not None else (3000 if intent in ('self_consumption', 'discharge_pv_first') else 0))
        await self.grant()
        await self.controller.async_tick()

    def assert_normal(self):
        self.assertEqual(self.states['select.mode'].state, 'Maximum Self Consumption')
        self.assertEqual(float(self.states['number.charge_ceiling'].state), 8000)
        self.assertEqual(float(self.states['number.discharge_ceiling'].state), 9000)
        self.assertEqual(self.states['switch.authority'].state, 'on')
        self.assertFalse(self.controller.records)

    async def test_every_allowed_transition_uses_complete_state_and_never_forecast_target(self):
        for first in PRESETS['sigenergy']['modes']:
            for second, mode in PRESETS['sigenergy']['modes'].items():
                with self.subTest(first=first, second=second):
                    await self.intent(first)
                    self.assertEqual(self.controller.status['state'], 'observed', self.controller.status)
                    self.calls.clear()
                    await self.intent(second)
                    self.assertEqual(self.controller.status['state'], 'observed', self.controller.status)
                    self.assertEqual(self.states['select.mode'].state, mode)
                    self.assertEqual(self.controller.status['measured_power_w'], 0)
                    if any(key == 'select.mode' for key, _ in self.calls):
                        at = next(i for i, (key, _) in enumerate(self.calls) if key == 'select.mode')
                        # No relaxation before acknowledged mode; directional limits zero first.
                        for key, value in self.calls[:at]:
                            self.assertEqual(value, 0, (first, second, self.calls))
                    self.assertEqual(set(self.agreement.active['controls']), {self.key})

    async def test_claim_zeros_both_ceilings_before_authority_and_mode(self):
        self.states['switch.authority'].state = 'off'
        self.states['sensor.authority_confirmation'].state = 'Local'
        await self.intent('charge_grid_first')
        self.assertEqual(self.calls[:4], [('number.charge_ceiling', 0), ('number.discharge_ceiling', 0),
                                        ('switch.authority', 'on'), ('select.mode', 'Command Charging (Grid First)')])
        self.assertEqual(self.controller.status['state'], 'observed', self.controller.status)

    async def test_low_delivery_is_observation_not_failed_write(self):
        await self.intent('charge_grid_first')
        self.assertEqual(self.controller.status['delivery'], 'below_ceiling')
        self.assertTrue(self.controller.status['settings_acknowledged'])
        self.calls.clear(); await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        await self.intent('discharge_pv_first')
        self.assertEqual(self.controller.status['delivery'], 'below_ceiling')
        self.assertEqual(self.controller.status['applicable_ceiling_w'], 3000)

    async def test_settings_acknowledged_without_new_physical_observations_is_not_active(self):
        self.observe = False; self.advance(1)
        await self.intent('charge_grid_first')
        self.assertEqual(self.controller.status['state'], 'awaiting_observation', self.controller.status)
        self.assertTrue(self.controller.status['settings_acknowledged'])
        self.assertIsNone(self.agreement.active)

    async def test_expiry_disable_clock_jump_and_scope_change_restore_normal(self):
        for reason in ('permission', 'lease', 'clock', 'scope', 'no_plan'):
            with self.subTest(reason=reason):
                await self.asyncSetUp()
                await self.intent('charge_grid_first')
                if reason == 'permission': self.enabled = False
                elif reason == 'lease': self.advance(120)
                elif reason == 'clock': self.wall -= timedelta(seconds=30)
                elif reason == 'scope': self.options['grid_import_limit_w'] = 500
                else: self.agreement.revoke()
                # Physical reads still arrive independently of authority lease.
                for state in self.states.values(): state.last_reported = self.wall
                await self.controller.async_tick()
                self.assert_normal()
                self.assertIsNone(self.agreement.active)

    async def test_restart_hands_over_never_resumes_cached_dispatch(self):
        await self.intent('discharge_pv_first')
        self.controller = self.new_controller()
        await self.controller.async_start()
        self.assert_normal()
        self.assertIsNone(self.agreement.lease)

    async def test_shutdown_uses_journal_handover_without_execution_permission(self):
        await self.intent('hold'); self.enabled = False
        await self.controller.async_stop()
        self.assert_normal()

    async def test_interrupted_write_retains_pending_until_late_ack_then_handover(self):
        async def no_ack(key, value): return key == 'select.mode'
        await self.intent('hold'); self.calls.clear()
        self.hook = no_ack
        await self.intent('charge_grid_first')
        self.assertEqual(self.controller.status['state'], 'handover_pending')
        self.assertEqual(self.journal.payload['records'][self.key]['pending']['role'], 'mode')
        self.assertNotIn(('number.charge_ceiling', 2000), self.calls)
        self.hook = None; self.enabled = False
        self.states['select.mode'].state = 'Command Charging (Grid First)'
        self.states['select.mode'].last_reported = self.wall
        await self.controller.async_tick()
        self.assert_normal()

    async def test_service_exception_and_cancellation_keep_write_ahead_ownership(self):
        for failure in (OSError('service failed'), asyncio.CancelledError()):
            await self.asyncSetUp()
            async def fail(key, value): raise failure
            self.hook = fail
            if isinstance(failure, asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError): await self.intent('hold')
            else: await self.intent('hold')
            self.assertIsNotNone(self.journal.payload['records'][self.key]['pending'])
            self.assertEqual(len(self.calls), 1)

    async def test_failed_capture_never_calls_hardware_and_after_save_recovers(self):
        for fail in ('before', 'after'):
            await self.asyncSetUp(); self.journal.fail = fail
            if fail == 'after':
                with self.assertRaises(asyncio.CancelledError): await self.intent('hold')
            else: await self.intent('hold')
            self.assertEqual(self.calls, [])
            self.assertIsNone(self.agreement.active)
            self.journal.fail = None
            await self.controller.async_tick()
            self.assertFalse(self.controller.records)

    async def test_permission_revoked_during_mode_await_prevents_relaxation(self):
        async def revoke(key, value):
            if key == 'select.mode': self.enabled = False
            return False
        await self.intent('hold'); self.calls.clear()
        self.hook = revoke
        await self.intent('charge_grid_first')
        self.assertNotIn(('number.charge_ceiling', 2000), self.calls)
        self.assert_normal()

    async def test_manual_changes_latch_and_never_fight_owner(self):
        for entity, value in [('number.charge_ceiling', '1234'), ('select.mode', 'Standby'), ('switch.authority', 'off'), ('select.mode', 'Discharge (ESS First)'), ('number.charge_ceiling', '4294967295')]:
            await self.asyncSetUp(); await self.intent('charge_grid_first')
            self.calls.clear(); self.states[entity].state = value
            await self.controller.async_tick()
            self.assertEqual(self.calls, [])
            self.assertIn(self.key, self.controller.overrides)
            self.assertEqual(self.controller.status['state'], 'handover_pending')
            restarted = self.new_controller(); await restarted.async_start()
            self.assertIn(self.key, restarted.overrides)
            self.assertEqual(self.calls, [])

    async def test_review_requires_normal_profile_then_clears_override_without_writes(self):
        await self.intent('charge_grid_first')
        self.states['select.mode'].state = 'Standby'
        await self.controller.async_tick(); self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'normal profile'):
            await self.controller.async_review_handover(self.key, 1, True)
        for key, value in [('select.mode', 'Maximum Self Consumption'), ('number.charge_ceiling', '8000'), ('number.discharge_ceiling', '9000')]:
            self.states[key].state = value
        await self.controller.async_review_handover(self.key, 1, True)
        self.assert_normal(); self.assertFalse(self.controller.overrides)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.agreement.lease)

    async def test_authority_loss_and_stale_measurements_never_resume_dispatch(self):
        await self.intent('charge_grid_first'); self.calls.clear()
        self.states['sensor.authority_confirmation'].state = 'Local'
        await self.controller.async_tick()
        self.assertEqual(self.calls, []); self.assertIsNone(self.agreement.active)
        self.assertIn('authority', self.controller.status['reason'])

    async def test_unknown_mode_unset_sentinel_and_stale_sensors_refuse_capture(self):
        for entity, value in [('select.mode','Discharge (ESS First)'), ('number.charge_ceiling','4294967295'), ('sensor.soc','unavailable')]:
            await self.asyncSetUp(); self.states[entity].state = value
            await self.controller.async_tick()
            self.assertEqual(self.calls, [])
            self.assertFalse(self.controller.records)
        await self.asyncSetUp(); self.states['sensor.soc'].last_reported -= timedelta(seconds=121)
        await self.controller.async_tick(); self.assertEqual(self.calls, [])

    async def test_registry_rename_is_resolved_and_replacement_is_never_written(self):
        await self.intent('hold')
        self.inv['number.renamed'] = self.inv.pop('number.charge_ceiling')
        self.states['number.renamed'] = self.states.pop('number.charge_ceiling')
        self.hass.services.async_call = self.rename_service()
        self.enabled = False
        await self.controller.async_tick()
        self.assertFalse(self.controller.records)
        self.assertIn(('number.renamed', 8000), self.calls)
        await self.asyncSetUp(); await self.intent('hold'); self.calls.clear()
        self.inv['number.charge_ceiling']['registry']['unique_id'] = 'replacement'
        self.enabled = False; await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertTrue(self.controller.records)

    def rename_service(self):
        async def service(domain, service, data, blocking):
            key = data['entity_id']; value = data.get('value', data.get('option'))
            self.calls.append((key, value)); self.states[key].state = str(value)
            for state in self.states.values(): state.last_reported = self.wall
        return service

    async def test_corrupt_journal_cannot_write_or_be_overwritten(self):
        self.journal.payload = {'schema_version': 1, 'records': {}}
        restarted = self.new_controller()
        with self.assertRaises(ValueError): await restarted.async_start()
        await restarted.async_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.journal.payload, {'schema_version': 1, 'records': {}})

    async def test_soc_reserves_headroom_and_native_steps_narrow_explicit_limits(self):
        control = self.definition['controls'][0]
        m = dict(battery_power=0, pv_power=5000, load_power=3000, grid_import_power=0, grid_export_power=2000, soc=60, soc_floor=5)
        instruction = dict(intent='discharge_pv_first', charge_ceiling_w=0, discharge_ceiling_w=9000)
        for floor in (60, 70):
            result = desired_state(control, instruction, {**m, 'soc_floor':floor}, self.binding, self.options, 900)
            self.assertEqual(result['discharge_ceiling'], 0)
        reserve = control['desired']['objective']['export_reserve_soc_pct']
        result = desired_state(control, instruction, {**m,'soc':reserve}, self.binding, self.options, 900)
        self.assertEqual(result['discharge_ceiling'], 0)
        instruction.update(intent='charge_grid_first', charge_ceiling_w=8000, discharge_ceiling_w=0)
        result = desired_state(control, instruction, {**m,'soc':95}, self.binding, self.options, 900)
        self.assertEqual(result['charge_ceiling'], 0)
        result = desired_state(control, instruction, {**m,'pv_power':0,'load_power':11500,'grid_import_power':11500,'grid_export_power':0}, self.binding, self.options, 900)
        self.assertEqual(result['charge_ceiling'], 500)
        cap = deepcopy(next(c for c in self.binding['capabilities'] if c['role']=='charge_ceiling'))
        cap.update(unit='kW', maximum=12, step=.1)
        self.assertEqual(native_ceiling(cap, 2349), 2.3)
        for value in (-1, 4294967295, float('nan')):
            with self.assertRaises(ValueError): native_ceiling(cap, value)

    async def test_permissions_direction_and_physical_validation_are_independent(self):
        c = deepcopy(self.definition['controls'][0]); m = self.controller.measurements(self.binding)
        for intent, permission in [('charge_grid_first', 'allow_grid_charge'), ('discharge_pv_first','allow_battery_export')]:
            c['desired']['objective'][permission] = False
            with self.assertRaises(ValueError):
                desired_state(c, dict(intent=intent, charge_ceiling_w=1000 if intent.startswith('charge') else 0,
                                     discharge_ceiling_w=1000 if intent.startswith('discharge') else 0), m, self.binding, self.options, 900)
        for observations in ({**m,'battery_power':3000}, {**m,'soc':101}, {**m,'pv_power':-1}):
            with self.assertRaises(ValueError):
                operation('hold', dict(charge_ceiling=0,discharge_ceiling=0), observations, self.binding)

    async def test_failure_at_each_claim_transition_write_blocks_every_later_write(self):
        for fail_at in range(1, 6):
            with self.subTest(fail_at=fail_at):
                await self.asyncSetUp()
                self.states['switch.authority'].state = 'off'
                self.states['sensor.authority_confirmation'].state = 'Local'
                async def refuse(key, value): return len(self.calls) == fail_at
                self.hook = refuse
                await self.intent('charge_grid_first')
                self.assertEqual(len(self.calls), fail_at)
                self.assertEqual(self.controller.status['state'], 'handover_pending')
                self.assertIsNotNone(self.journal.payload['records'][self.key]['pending'])
                self.assertIsNone(self.agreement.active)

    async def test_revocation_during_write_ahead_cancels_unsubmitted_request(self):
        await self.intent('hold'); self.calls.clear()
        original = self.journal.async_save
        async def save(payload):
            await original(payload)
            r = payload['records'].get(self.key)
            if r and r['pending'] and r['pending']['value'] == 'Command Charging (Grid First)':
                self.enabled = False
        self.journal.async_save = save
        await self.intent('charge_grid_first')
        self.assertNotIn(('select.mode', 'Command Charging (Grid First)'), self.calls)
        self.assert_normal()

    async def test_binding_edit_during_service_restores_owned_profile_and_old_registry(self):
        old_normal = deepcopy(self.binding['spec']['limits'])
        async def edit(key, value):
            if key == 'select.mode' and value == 'Command Charging (Grid First)':
                spec = deepcopy(self.binding['spec']); spec['limits']['normal_charge_w'] = 1000
                await self.setup.async_save(spec, 1)
            return False
        await self.intent('hold'); self.calls.clear(); self.hook = edit
        await self.intent('charge_grid_first')
        self.assertEqual(self.setup.records[self.key]['binding_revision'], 2)
        self.assert_normal()  # The old 8000 W normal limit remains the release authority.
        self.assertEqual(float(self.states['number.charge_ceiling'].state), old_normal['normal_charge_w'])
        self.assertNotIn(('number.charge_ceiling', 2000), self.calls)

    async def test_storage_failure_after_hardware_ack_recovers_from_durable_pending(self):
        await self.intent('hold'); self.calls.clear()
        original = self.journal.async_save
        async def save(payload):
            r = payload['records'].get(self.key)
            if r and r['expected']['mode'] == 'Command Charging (Grid First)' and not r['pending']:
                raise OSError('disk failure after device acknowledgement')
            await original(payload)
        self.journal.async_save = save
        await self.intent('charge_grid_first')
        self.assertTrue(self.controller.storage_fault)
        self.assertIsNotNone(self.journal.payload['records'][self.key]['pending'])
        self.assertNotIn(('number.charge_ceiling', 2000), self.calls)
        self.journal.async_save = original
        await self.controller.async_tick()
        self.assert_normal()

    async def test_legacy_ownership_barrier_prevents_new_adapter_capture(self):
        def blocked(): raise ValueError('legacy_handover_required')
        self.controller.legacy_ready = blocked
        await self.intent('hold')
        self.assertEqual(self.calls, [])
        self.assertFalse(self.controller.records)
        self.assertIn('legacy_handover_required', self.controller.status['reason'])

    async def test_method_change_waits_for_old_customer_pool_release(self):
        prior = deepcopy(self.setup.records[self.definition['controls'][1]['control_id']])
        prior['spec']['control_id'] = self.key
        self.setup.runtime_records = lambda: {'pool:' + self.key: {'binding': prior}}
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertIn('previously owned adapter', self.controller.status['reason'])
