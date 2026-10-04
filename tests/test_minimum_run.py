"""Retired SHS run clocks never delay a planned stop or explicit handover."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import test_controller as fixtures
from shs_core.controller import ScheduledController
from command_fixture import native_executor, controller_inputs
from shs_core.verification import VerificationJournal
from shs_core.configuration_fields import control_fields
from shs_core.configuration_schema import save_device
from shs_core.device_controls import mapping_report

NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)

class Clock(datetime):
    value = NOW
    @classmethod
    def now(cls, tz=None):
        return cls.value

class RetiredMinimumRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        Clock.value = NOW
        for state in self.states.values():
            state.last_updated = state.last_reported = state.last_changed = NOW
        self.clock = patch('shs_core.controller.datetime', Clock)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        physical_clock = patch('shs_core.device_operations.datetime', Clock)
        physical_clock.start()
        self.addCleanup(physical_clock.stop)
        self.options['device_control_mappings']['pool']['minimum_on_seconds'] = 3600
        self.options['device_modes']['$pool'] = 'controlling'
        self.controller.verification = VerificationJournal(fixtures.Store(), fixtures.Store())

    def advance(self, **duration):
        Clock.value += timedelta(**duration)
        for state in self.states.values():
            state.last_updated = state.last_reported = Clock.value

    async def test_revocation_during_command_prepare_cancels_unsent_minimum_run(self):
        import asyncio
        transport=self.controller.native_executor.transport
        async def executor(fn,*args):
            result=await asyncio.to_thread(fn,*args)
            if fn==transport.journal.prepare:
                self.options['device_modes']['$pool']='control_verification'
            return result
        transport.executor=executor
        await self.controller.async_start()
        self.assertEqual(self.calls,[])
        self.assertNotIn('runs', self.controller.ownership.snapshot())
        self.assertEqual(self.states['switch.pool'].state,'off')


    async def test_verification_change_while_restoration_is_saved_still_hands_the_device_back(self):
        # Exclusion and Verification are both releases: changing from one to the
        # other in the middle of the handover does not leave the device running.
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
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('switch.pool', 'off')])
        self.assertEqual(self.states['switch.pool'].state, 'off')
        self.assertNotIn('pool', self.controller.ownership.records)








    async def test_old_four_hour_setting_and_journal_cannot_delay_replanned_stop(self):
        self.options['device_control_mappings']['pool']['minimum_on_seconds'] = 14400
        self.store.saved = {'records': {}, 'overrides': {}, 'runs': {'pool': {
            'since': NOW.isoformat(), 'minimum_seconds': 14400,
            'active': True, 'pending_start': True}}}
        await self.controller.async_start()
        self.assertEqual(self.states['switch.pool'].state, 'on')
        self.calls.clear()
        self.slot['pool_w'] = 0
        self.coordinator.optimisation_plan['plan_id'] = 'replanned'
        self.advance(minutes=1)
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('switch.pool', 'off')])
        self.assertNotIn('runs', self.controller.ownership.snapshot())

    async def test_verification_restores_immediately_after_start(self):
        await self.controller.async_start()
        self.calls.clear()
        self.options['device_modes']['$pool'] = 'control_verification'
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('switch.pool', 'off')])
        self.assertNotIn('pool', self.controller.ownership.records)


class RetiredRunConfigurationTests(unittest.TestCase):
    def test_all_methods_and_paths_omit_both_timer_editors(self):
        for kind in ('switch_schedule', 'permit_inhibit', 'setpoint', 'variable_power'):
            for path in (None, 'pool', 'ev', 'room', 'boiler'):
                keys = {field['key'] for field in control_fields(kind, path)}
                self.assertTrue(keys.isdisjoint({'minimum_on_seconds', 'minimum_off_seconds'}))

    def test_migration_removes_four_hour_setting_and_strict_save_rejects_it(self):
        from migration import migrate_options
        from shs_core.configuration_schema import validate_mapping_keys
        mapping = {'control_type': 'switch_schedule', 'actuator_entity_ids': ['switch.pool'],
                   'power': 12000, 'minimum_on_seconds': 14400, 'minimum_off_seconds': 60}
        migrated, changed = migrate_options({'device_control_mappings': {'pool': mapping}}, source_version=14)
        self.assertTrue(changed)
        saved = migrated['device_control_mappings']['pool']
        self.assertEqual(saved, {k: v for k, v in mapping.items() if not k.startswith('minimum_')})
        with self.assertRaisesRegex(ValueError, 'unknown device fields: minimum_off_seconds, minimum_on_seconds'):
            validate_mapping_keys(mapping)
        self.assertEqual(mapping['minimum_on_seconds'], 14400)
