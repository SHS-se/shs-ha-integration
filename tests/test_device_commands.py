"""Execute the real per-device controller through an in-memory HA boundary."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest
import test_controller as fixtures
State = fixtures.State
from controller import ScheduledController
from device_commands import validate_commands


class DeviceExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.states['switch.heater'] = State('on')
        self.states['number.target'] = State(20, min=5, max=35, step=.5, unit_of_measurement='°C')
        self.options['device_control_mappings']['heater'] = {
            'control_type': 'permit_inhibit', 'actuator_entity_ids': ['switch.heater'],
            'max_inhibit_slots': 2}
        self.options['device_modes']['heater'] = 'controlling'
        self.coordinator.optimisation_plan.update(schema_version=7, device_models=[
            {'key': 'heater', 'control_type': 'permit_inhibit'}])
        self.coordinator.async_cached_device_configuration = AsyncMock(return_value=[{'key': 'heater', 'control_type': 'permit_inhibit'}])
        self.slot['device_commands'] = {'heater': {'type': 'permit_inhibit', 'permitted': False}}

    async def test_permission_disable_restores_and_acknowledgement_is_not_power(self):
        await self.controller.async_start()
        self.assertEqual(self.states['switch.heater'].state, 'off')
        self.assertEqual(self.controller.status['device:heater']['state'], 'commanded')
        self.assertNotIn('measured_power_w', self.controller.status['device:heater'])
        self.options['device_modes']['heater'] = 'planning'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.assertNotIn('device:heater', self.controller.records)

    async def test_website_exclusion_restores_even_if_old_plan_remains(self):
        await self.controller.async_start()
        self.coordinator.async_cached_device_configuration.return_value = []
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.assertEqual(self.controller.status['device:heater']['state'], 'disabled')

    async def test_external_change_is_preserved_and_suspension_survives_restart(self):
        await self.controller.async_start()
        self.states['switch.heater'].state = 'on'
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['device:heater']['state'], 'overridden')
        self.assertEqual(self.states['switch.heater'].state, 'on')
        other = ScheduledController(self.hass, self.coordinator, self.store, lambda: deepcopy(self.options))
        await other.async_start()
        self.assertEqual(other.status['device:heater']['state'], 'overridden')
        self.assertEqual(self.states['switch.heater'].state, 'on')

    async def test_expiry_and_schema_six_do_not_infer_commands_from_watts(self):
        await self.controller.async_start()
        self.coordinator.optimisation_plan['schema_version'] = 6
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.assertEqual(self.controller.status['device:heater']['state'], 'idle')

    async def test_conflicting_owners_never_capture_or_write(self):
        self.options['device_modes']['duplicate'] = 'controlling'
        self.options['device_control_mappings']['duplicate'] = deepcopy(self.options['device_control_mappings']['heater'])
        await self.controller.async_start()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.status['device:heater']['state'], 'fault')

    async def test_maximum_inhibit_restores(self):
        await self.controller.async_start()
        self.controller.records['device:heater']['inhibited_since'] = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.assertEqual(self.controller.status['device:heater']['state'], 'fault')

    async def test_setpoint_and_mapping_edit_restore_original_target(self):
        mapping = self.options['device_control_mappings']['heater']
        mapping.update(control_type='setpoint', setpoint_entity_id='number.target',
                       minimum_temperature_c=18, maximum_temperature_c=24)
        self.coordinator.optimisation_plan['device_models'][0]['control_type'] = 'setpoint'
        self.coordinator.async_cached_device_configuration.return_value[0]['control_type'] = 'setpoint'
        self.slot['device_commands']['heater'] = {'type': 'setpoint', 'target_c': 21.4, 'minimum_c': 20, 'maximum_c': 23}
        await self.controller.async_start()
        self.assertEqual(float(self.states['number.target'].state), 21.5)
        mapping['setpoint_entity_id'] = 'number.missing'
        await self.controller.async_tick()
        self.assertEqual(float(self.states['number.target'].state), 20)
        self.assertEqual(self.controller.status['device:heater']['state'], 'fault')

    async def test_failed_acknowledgement_is_fault_and_restores(self):
        async def refuse(*args, **kwargs):
            pass
        self.hass.services.async_call = refuse
        await self.controller.async_start()
        self.assertEqual(self.controller.status['device:heater']['state'], 'fault')
        self.assertEqual(self.states['switch.heater'].state, 'on')

    async def test_bad_or_missing_device_command_rejected(self):
        models = self.coordinator.optimisation_plan['device_models']
        for commands in ({}, {'heater': {'type': 'permit_inhibit', 'permitted': 1}},
                         {'heater': {'type': 'setpoint', 'target_c': 21}}):
            with self.assertRaises(ValueError):
                validate_commands(commands, models)

    async def test_switch_restore_waits_for_minimum_run_time(self):
        mapping = self.options['device_control_mappings']['heater']
        mapping.update(control_type='switch_schedule', minimum_on_seconds=60, minimum_off_seconds=60)
        self.states['switch.heater'].last_changed = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.coordinator.optimisation_plan['device_models'][0]['control_type'] = 'switch_schedule'
        self.coordinator.async_cached_device_configuration.return_value[0]['control_type'] = 'switch_schedule'
        self.slot['device_commands']['heater'] = {'type':'switch_schedule', 'on_seconds':0}
        await self.controller.async_start()
        self.assertEqual(self.states['switch.heater'].state, 'off')
        self.options['device_modes']['heater'] = 'planning'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'off')
        self.assertTrue(self.controller.records['device:heater']['restoration_pending'])
        self.controller.records['device:heater']['transition_times']['switch.heater'] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.assertFalse(self.controller.records)

    async def test_climate_target_acknowledgement_preserves_heating_mode(self):
        self.hass.config = SimpleNamespace(units=SimpleNamespace(temperature_unit='°C'))
        self.states['climate.heater'] = State('heat', temperature=20, min_temp=5, max_temp=35, target_temp_step=.5)
        mapping = self.options['device_control_mappings']['heater']
        mapping.update(control_type='setpoint', actuator_entity_ids=['climate.heater'],
                       minimum_temperature_c=18, maximum_temperature_c=24)
        self.coordinator.optimisation_plan['device_models'][0]['control_type'] = 'setpoint'
        self.coordinator.async_cached_device_configuration.return_value[0]['control_type'] = 'setpoint'
        self.slot['device_commands']['heater'] = {'type':'setpoint', 'target_c':21.5, 'minimum_c':20, 'maximum_c':23}
        async def climate_call(domain, service, data, blocking):
            self.assertIsNotNone(self.store.saved)
            self.assertEqual(service, 'set_temperature')
            self.states[data['entity_id']].attributes['temperature'] = data['temperature']
        self.hass.services.async_call = climate_call
        await self.controller.async_start()
        self.assertEqual(self.states['climate.heater'].attributes['temperature'],21.5)
        self.assertEqual(self.states['climate.heater'].state,'heat')
        self.options['device_modes']['heater'] = 'planning'
        await self.controller.async_tick()
        self.assertEqual(self.states['climate.heater'].attributes['temperature'],20)

    async def test_lost_ownership_inventory_hands_back_control(self):
        await self.controller.async_start()
        self.coordinator.async_cached_device_configuration.side_effect = ValueError('bad cache')
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state,'on')

    async def test_server_generated_schema_seven_fixture_validates_in_ha(self):
        import json
        from pathlib import Path
        from optimisation import validate_plan_contract
        fixture = json.loads((Path(__file__).parent / 'fixtures/schema-7-device-plan.json').read_text())
        now = datetime.fromisoformat(fixture['validation_time'].replace('Z','+00:00'))
        validate_plan_contract(fixture['plan'], now)
        fixture['plan']['plans']['cost']['slots'][-1]['device_commands']['thermostat']['target_c'] = float('nan')
        with self.assertRaises(ValueError):
            validate_plan_contract(fixture['plan'], now)

    async def test_website_method_change_hands_back_previous_mapping(self):
        await self.controller.async_start()
        self.coordinator.async_cached_device_configuration.return_value[0]['control_type'] = 'switch_schedule'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.assertFalse(self.controller.records)

    async def test_stopping_control_does_not_require_a_live_actuator(self):
        await self.controller.async_start()
        del self.states['switch.heater']
        self.options['device_modes']['heater'] = 'monitoring'
        await self.controller.async_tick()
        self.assertFalse(self.controller.eligible('device:heater', self.options))
        self.assertFalse(self.controller.records)
        self.assertIn('device:heater', self.controller.overrides)
