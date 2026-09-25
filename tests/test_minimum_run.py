"""Minimum runtime follows real hardware across plans, ownership and restarts."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import test_controller as fixtures
from controller import ScheduledController
from minimum_run import MinimumRuns, minimum_run_errors
from verification import VerificationJournal
from configuration_fields import control_fields
from configuration_schema import save_device
from device_controls import mapping_report

NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)

class Clock(datetime):
    value = NOW
    @classmethod
    def now(cls, tz=None):
        return cls.value

class RunJournalTests(unittest.TestCase):
    def setUp(self):
        self.options = {'device_control_mappings': {'heater': {'control_type': 'switch_schedule',
            'actuator_entity_ids': ['switch.heater'], 'minimum_on_seconds': 3600}}}
        self.state = fixtures.State('off')
        self.state.last_changed = NOW - timedelta(hours=1)
        self.read = lambda _: self.state
        self.runs = MinimumRuns()
        self.runs.configure(self.options, [], NOW)

    def test_real_events_count_receipt_order_and_unknown_preserves_run(self):
        self.runs.observe(self.read, NOW)
        self.state.state = 'on'
        self.runs.observe(self.read, NOW, entity='switch.heater', received=True)
        deadline = self.runs.release_at('heater')
        self.assertEqual(deadline, NOW + timedelta(hours=1))
        self.runs.observe(self.read, NOW + timedelta(minutes=5))
        self.state.state = 'unavailable'
        self.runs.observe(self.read, NOW + timedelta(minutes=10), received=True)
        self.assertEqual(self.runs.release_at('heater'), deadline)
        self.state.state = 'off'
        self.runs.observe(self.read, NOW + timedelta(minutes=11), received=True)
        self.state.state = 'on'
        self.runs.observe(self.read, NOW + timedelta(minutes=12), received=True)
        self.assertEqual(self.runs.release_at('heater'), NOW + timedelta(minutes=72))

    def test_restart_preserves_elapsed_time_and_setting_removal_preserves_commitment(self):
        self.state.state = 'on'
        self.state.last_changed = NOW - timedelta(minutes=10)
        self.runs.observe(self.read, NOW)
        restored = MinimumRuns(self.runs.records)
        self.options['device_control_mappings']['heater'].pop('minimum_on_seconds')
        restored.configure(self.options, [], NOW)
        self.state.last_changed = NOW
        restored.observe(self.read, NOW + timedelta(minutes=5))
        self.assertEqual(restored.snapshot(NOW)['heater']['remaining_seconds'], 3000)
        self.assertEqual(restored.blocked_until('switch.heater', 'off', self.read, NOW), NOW + timedelta(minutes=50))

    def test_replacement_and_shorter_setting_cannot_erase_an_existing_promise(self):
        self.state.state = 'on'
        self.state.last_changed = NOW
        self.runs.observe(self.read, NOW)
        mapping = self.options['device_control_mappings']['heater']
        mapping['minimum_on_seconds'] = 60
        self.runs.configure(self.options, [], NOW)
        self.runs.observe(self.read, NOW + timedelta(minutes=2))
        self.assertEqual(self.runs.release_at('heater'), NOW + timedelta(hours=1))
        mapping['actuator_entity_ids'] = ['switch.replacement']
        self.runs.configure(self.options, [], NOW + timedelta(minutes=2))
        replacement = fixtures.State('off')
        read = lambda entity: replacement if entity == 'switch.replacement' else self.state
        self.runs.observe(read, NOW + timedelta(minutes=2))
        self.assertIsNone(self.runs.release_at('heater'))
        self.assertEqual(self.runs.blocked_until('switch.heater', 'off', read, NOW), NOW + timedelta(hours=1))
        self.runs.configure(self.options, [], NOW + timedelta(hours=1))
        self.assertNotIn('switch.heater', self.runs.entities)

    def test_pending_start_survives_command_failure_and_crash_before_feedback(self):
        self.runs.observe(self.read, NOW)
        self.runs.prepare_start('switch.heater', 'on', self.read, NOW)
        self.runs.observe(self.read, NOW + timedelta(seconds=1))
        restored = MinimumRuns(self.runs.records)
        restored.configure(self.options, [], NOW)
        self.state.state = 'on'
        restored.observe(self.read, NOW + timedelta(minutes=1))
        self.assertEqual(restored.release_at('heater'), NOW + timedelta(minutes=61))

    def test_all_methods_offer_optional_minutes_without_a_duration_cap(self):
        for kind in ('switch_schedule','permit_inhibit','setpoint','variable_power'):
            for path in (None, 'pool', 'ev', 'room', 'boiler'):
                field = next(f for f in control_fields(kind, path) if f['key'] == 'minimum_on_seconds')
                self.assertFalse(field['required'])
                self.assertEqual(field['unit'], 'min')
                self.assertNotIn('maximum', field)
        self.assertEqual(minimum_run_errors({'minimum_on_seconds': 86400}), {})
        for value in (True, -1, float('inf'), float('nan'), '60'):
            self.assertIn('minimum_on_seconds', minimum_run_errors({'minimum_on_seconds': value}))

    def test_invalid_duration_has_correct_mapping_field_and_clears_after_correction(self):
        mapping = {**self.options['device_control_mappings']['heater'], 'power': 1000, 'minimum_on_seconds': -1}
        bad = mapping_report('switch_schedule', mapping)
        self.assertEqual(bad['mapping_status'], 'invalid')
        self.assertIn('minimum_on_seconds', bad['field_errors'])
        mapping['minimum_on_seconds'] = 7200
        saved = save_device({}, 'heater', mapping,
            {'control_type': 'switch_schedule', 'category': 'household', 'name': 'Heater'},
            lambda entity: {'state':'on', 'attributes':{}} if entity == 'switch.heater' else None, entity_names={'switch.heater':'Heater'}, area_names={}, entity_area_ids={})
        good = mapping_report('switch_schedule', saved['device_control_mappings']['heater'])
        self.assertEqual(good['mapping_status'], 'ready')
        self.assertEqual(good['field_errors'], {})

class MinimumRunControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        Clock.value = NOW
        for state in self.states.values():
            state.last_updated = state.last_reported = state.last_changed = NOW
        self.clock = patch('controller.datetime', Clock)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.options['device_control_mappings']['pool']['minimum_on_seconds'] = 3600
        self.options['device_modes']['$pool'] = 'controlling'
        self.controller.verification = VerificationJournal(fixtures.Store(), fixtures.Store())

    def advance(self, **duration):
        Clock.value += timedelta(**duration)
        for state in self.states.values():
            state.last_updated = state.last_reported = Clock.value

    async def test_switching_to_verification_while_start_is_saved_prevents_the_write(self):
        save = self.store.async_save
        async def change_mode(value):
            await save(value)
            if value.get('runs', {}).get('pool', {}).get('pending_start'):
                self.options['device_modes']['$pool'] = 'control_verification'
        self.store.async_save = change_mode
        await self.controller.async_start()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.states['switch.pool'].state, 'off')
        self.assertFalse(self.store.saved['runs']['pool']['active'])
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertNotIn('pool', self.controller.records)

    async def test_verification_change_while_restoration_is_saved_leaves_device_on(self):
        await self.controller.async_start()
        self.calls.clear()
        self.advance(hours=2)
        self.options['excluded_device_readings'] = ['$pool']
        save = self.store.async_save
        async def change_mode(value):
            await save(value)
            if value.get('records', {}).get('pool', {}).get('restoration_pending'):
                self.options['device_modes']['$pool'] = 'control_verification'
        self.store.async_save = change_mode
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.states['switch.pool'].state, 'on')
        await self.controller.async_tick()
        self.assertNotIn('pool', self.controller.records)

    async def test_pool_stop_temperature_replan_restart_and_expiry(self):
        await self.controller.async_start()
        self.assertEqual(self.states['switch.pool'].state, 'on')
        since = self.controller.runs.records['pool']['since']
        self.calls.clear()
        self.slot['pool_w'] = 0
        self.coordinator.optimisation_plan['plan_id'] = 'replanned'
        self.states['sensor.water'].state = '32'
        self.advance(minutes=10)
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'pending')
        await self.controller.async_stop()
        self.assertEqual(self.calls, [])
        restarted = ScheduledController(self.hass, self.coordinator, self.store, lambda: deepcopy(self.options))
        await restarted.async_start()
        self.assertEqual(self.calls, [])
        self.assertEqual(restarted.runs.records['pool']['since'], since)
        self.advance(minutes=50)
        await restarted.async_tick()
        self.assertEqual(self.calls, [('switch.pool', 'off')])

    async def test_verification_observes_external_start_and_never_resets_it(self):
        self.options['device_modes']['$pool'] = 'control_verification'
        self.slot['pool_w'] = 0
        await self.controller.async_start()
        self.states['switch.pool'].state = 'on'
        self.advance(minutes=5)
        await self.controller.async_tick()
        since = self.controller.runs.records['pool']['since']
        self.assertEqual(self.calls, [])
        self.options['device_modes']['$pool'] = 'controlling'
        self.advance(minutes=10)
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.runs.records['pool']['since'], since)
        self.assertEqual(self.controller.status['pool']['state'], 'pending')
        snapshot = await self.controller.minimum_run_snapshot(self.options, self.coordinator.optimisation_plan['device_models'])
        self.assertEqual(snapshot['pool']['remaining_seconds'], 3000)

    async def test_verification_handoff_never_restores_even_after_minimum_has_elapsed(self):
        await self.controller.async_start()
        self.calls.clear()
        self.advance(hours=2)
        self.options['device_modes']['$pool'] = 'control_verification'
        self.slot['pool_w'] = 0
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.states['switch.pool'].state, 'on')
        self.assertNotIn('pool', self.controller.records)

    async def test_override_and_exclusion_restoration_cannot_stop_a_locked_run(self):
        await self.controller.async_start()
        self.calls.clear()
        self.options['excluded_device_readings'] = ['$pool']
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertTrue(self.controller.records['pool']['restoration_pending'])
        self.assertEqual(self.controller.status['pool']['state'], 'pending')

    async def test_ev_target_and_disconnect_cannot_stop_a_locked_run(self):
        self.options['device_modes'] = {'$ev': 'controlling'}
        self.options['device_control_mappings']['charger']['minimum_on_seconds'] = 1800
        await self.controller.async_start()
        self.calls.clear()
        self.states['sensor.ev_soc'].state = '90'
        self.states['binary_sensor.connected'].state = 'off'
        self.slot['ev_target_current_a'] = 0
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.states['switch.charge'].state, 'on')
        self.assertEqual(self.controller.status['ev']['state'], 'pending')

    async def test_invalid_duration_never_writes_and_names_its_editor(self):
        self.options['device_control_mappings']['pool']['minimum_on_seconds'] = -1
        await self.controller.async_start()
        self.assertEqual(self.calls, [])
        fix = self.controller.status['pool']['fix']
        self.assertEqual(fix['fields'][0]['key'], 'minimum_on_seconds')
        self.assertEqual(fix['fields'][0]['device_key'], 'pool')
        self.assertEqual(fix['fields'][0]['scope'], 'mapping')

    async def test_setpoint_reduction_is_held_without_a_switch_command(self):
        from unittest.mock import AsyncMock
        self.options['device_modes'] = {'room': 'controlling'}
        self.states['climate.room'] = fixtures.State('heat', temperature=24, min_temp=5, max_temp=35)
        self.states['climate.room'].last_changed = NOW
        self.hass.config = SimpleNamespace(units=SimpleNamespace(temperature_unit='°C'))
        self.options['device_control_mappings']['room'] = {'control_type':'setpoint',
            'actuator_entity_ids':['climate.room'], 'minimum_temperature_c':5, 'maximum_temperature_c':35,
            'minimum_on_seconds':3600}
        self.coordinator.optimisation_plan['device_models'] = [{'key':'room','control_type':'setpoint'}]
        self.coordinator.async_cached_device_configuration = AsyncMock(return_value=self.coordinator.optimisation_plan['device_models'])
        self.slot['device_commands'] = {'room':{'type':'setpoint','target_c':18,'minimum_c':5,'maximum_c':35}}
        await self.controller.async_start()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.status['device:room']['state'], 'pending')
