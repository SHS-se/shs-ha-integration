"""Exercise real controller sequencing with an in-memory HA service boundary."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from controller import ScheduledController, pool_band


class State:
    def __init__(self, value, **attributes):
        self.state = str(value)
        self.attributes = attributes
        self.last_updated = self.last_reported = datetime.now(timezone.utc)


class Store:
    def __init__(self):
        self.saved = None
    async def async_load(self):
        return deepcopy(self.saved)
    async def async_save(self, value):
        self.saved = deepcopy(value)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.states = {
            'number.current': State(8, min=5, max=16, step=1, unit_of_measurement='A'),
            'switch.charge': State('off'),
            'binary_sensor.connected': State('on'),
            'sensor.ev_soc': State(30, unit_of_measurement='%'),
            'sensor.ev_target': State(80, unit_of_measurement='%'),
            'number.start': State(29.5, min=5, max=40, step=.1, unit_of_measurement='°C'),
            'number.stop': State(30, min=5, max=40, step=.1, unit_of_measurement='°C'),
            'sensor.water': State(29, unit_of_measurement='°C'),
            'switch.pool': State('off'),
            'number.battery': State(0, min=-100, max=100, step=.001, unit_of_measurement='kW'),
            'select.mode': State('Baseline', options=['Charge', 'Discharge', 'Hold', 'Baseline']),
            'sensor.battery_power': State(0, unit_of_measurement='kW'),
            'sensor.battery_soc': State(50, unit_of_measurement='%'),
            'switch.authority': State('on'),
            'sensor.authority': State('Remote'),
        }
        self.options = {
            **{f'{d}_control_enabled': False for d in ('battery', 'ev', 'pool')},
            **{f'{d}_enabled': True for d in ('battery', 'ev', 'pool')},
            'ev_charge_switch_entity': 'switch.charge',
            'ev_connected_entity': 'binary_sensor.connected',
            'ev_soc_entity': 'sensor.ev_soc', 'ev_target_soc_entity': 'sensor.ev_target',
            'device_control_mappings': {'charger': {'control_entity_id': 'number.current', 'control_type': 'variable_power', 'minimum_value': 5, 'maximum_value': 16}},
            'pool_start_temperature_entity': 'number.start', 'pool_stop_temperature_entity': 'number.stop',
            'pool_water_temperature_entity': 'sensor.water', 'pool_permission_entity': 'switch.pool',
            'pool_temperature_minimum': 24, 'pool_temperature_maximum': 32,
            'battery_mode_entity': 'select.mode', 'battery_mode_charge': 'Charge',
            'battery_mode_discharge': 'Discharge', 'battery_mode_idle': 'Hold', 'battery_mode_baseline': 'Baseline',
            'battery_power_entity': 'number.battery', 'battery_power_unit': 'kW',
            'battery_discharge_is_negative': True, 'battery_measurement_charge_positive': True,
            'battery_power_measurement_entity': 'sensor.battery_power', 'battery_soc_entity': 'sensor.battery_soc',
            'battery_min_soc': .05, 'battery_max_soc': 1, 'battery_charge_max_w': 8800, 'battery_discharge_max_w': 9600,
            'battery_authority_entity': 'switch.authority', 'battery_authority_confirm_entity': 'sensor.authority', 'battery_authority_confirm_state': 'Remote',
        }
        self.slot = {'start': '2026-09-08T12:00:00+00:00', 'ev_target_current_a': 10,
                     'ev_min_current_a': 0, 'ev_max_current_a': 16, 'pool_w': 3300,
                     'battery_charge_w': 2000, 'battery_discharge_w': 0,
                     'device_loads_w': {'charger': 6900}}
        self.coordinator = SimpleNamespace(current_plan_slot=self.slot, optimisation_plan={
            'plan_id': 'test', 'capabilities': dict.fromkeys(('battery', 'ev', 'pool'), True),
            'device_models': [{'key': 'charger', 'category': 'ev_charging', 'control_type': 'variable_power'}]})
        self.calls = []
        self.store = Store()
        async def call(domain, service, data, blocking):
            self.assertTrue(blocking)
            self.assertIsNotNone(self.store.saved, 'journal must precede any write')
            entity = data['entity_id']
            value = data.get('value', data.get('option', service.removeprefix('turn_')))
            self.calls.append((entity, value))
            self.states[entity].state = str(value)
            if entity in ('number.battery', 'select.mode'):
                self.states['sensor.battery_power'].last_reported = datetime.now(timezone.utc)
            if entity == 'number.battery':
                self.states['sensor.battery_power'].state = str(value)
        self.hass = SimpleNamespace(states=SimpleNamespace(get=self.states.get), services=SimpleNamespace(async_call=call))
        self.controller = ScheduledController(self.hass, self.coordinator, self.store, lambda: deepcopy(self.options))
        # Timeouts are exercised as refusal rather than sleeping in a test.
        async def confirm(predicate, error):
            self.controller.check_authority()
            if not predicate():
                raise ValueError(error)
        self.controller.confirm = confirm

    async def test_all_off_never_writes(self):
        await self.controller.async_start()
        self.assertEqual(self.calls, [])

    async def test_ev_current_precedes_start_and_zero_stops_without_invalid_current(self):
        self.options['ev_control_enabled'] = True
        await self.controller.async_start()
        self.assertEqual(self.calls, [('number.current', 10), ('switch.charge', 'on')])
        self.slot['ev_target_current_a'] = 0
        await self.controller.async_tick()
        self.assertEqual(self.calls[-1], ('switch.charge', 'off'))
        self.assertNotIn(('number.current', 0), self.calls)

    async def test_disable_restores_and_other_devices_remain_disabled(self):
        self.options['ev_control_enabled'] = True
        await self.controller.async_start()
        self.options['ev_control_enabled'] = False
        await self.controller.async_tick()
        self.assertEqual(self.states['number.current'].state, '8.0')
        self.assertEqual(self.states['switch.charge'].state, 'off')
        self.assertFalse(self.controller.records)
        self.assertFalse(any('battery' in e or e == 'number.start' for e, _ in self.calls))

    async def test_disconnection_and_soc_completion_stop_charging(self):
        self.options['ev_control_enabled'] = True
        await self.controller.async_start()
        self.states['binary_sensor.connected'].state = 'off'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.charge'].state, 'off')
        self.states['binary_sensor.connected'].state = 'on'
        self.states['sensor.ev_soc'].state = '80'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.charge'].state, 'off')

    async def test_plan_expiry_restores_pool_without_recapturing_shifted_band(self):
        self.options['pool_control_enabled'] = True
        self.slot['pool_w'] = 0
        await self.controller.async_start()
        self.assertAlmostEqual(float(self.states['number.stop'].state), 28.9)
        self.assertAlmostEqual(float(self.states['number.start'].state), 28.4)
        self.slot['pool_w'] = 3300
        await self.controller.async_tick()
        self.assertEqual(float(self.states['number.start'].state), 29.5)
        self.coordinator.current_plan_slot = None
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.pool'].state, 'off')
        self.assertFalse(self.controller.records)

    async def test_restart_recovers_journal_before_using_new_mapping(self):
        self.options['pool_control_enabled'] = True
        self.slot['pool_w'] = 0
        await self.controller.async_start()
        self.options['pool_control_enabled'] = False
        other = ScheduledController(self.hass, self.coordinator, self.store, lambda: deepcopy(self.options))
        await other.async_start()
        self.assertEqual(float(self.states['number.start'].state), 29.5)
        self.assertEqual(float(self.states['number.stop'].state), 30)
        self.assertFalse(other.records)

    async def test_battery_reversal_mode_before_power_without_zero_staging(self):
        self.options['battery_control_enabled'] = True
        await self.controller.async_start()
        self.calls.clear()
        self.slot.update(battery_charge_w=0, battery_discharge_w=3000)
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('select.mode', 'Discharge'), ('number.battery', -3)])
        self.assertEqual(self.controller.status['battery']['state'], 'confirmed')

    async def test_battery_expiry_zero_before_baseline(self):
        self.options['battery_control_enabled'] = True
        await self.controller.async_start()
        self.calls.clear()
        self.coordinator.current_plan_slot = None
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('number.battery', 0), ('select.mode', 'Baseline')])

    async def test_failed_mode_confirmation_never_writes_new_power(self):
        self.options['battery_control_enabled'] = True
        await self.controller.async_start()
        self.calls.clear()
        original = self.hass.services.async_call
        async def refuse(domain, service, data, blocking):
            if data.get('option') == 'Discharge':
                return
            await original(domain, service, data, blocking)
        self.hass.services.async_call = refuse
        self.slot.update(battery_charge_w=0, battery_discharge_w=3000)
        await self.controller.async_tick()
        self.assertNotIn(('number.battery', -3), self.calls)
        self.assertEqual(self.controller.status['battery']['state'], 'fault')
        self.assertEqual(self.states['select.mode'].state, 'Baseline')

    async def test_battery_soc_floor_and_authority_loss_restore(self):
        self.options['battery_control_enabled'] = True
        await self.controller.async_start()
        self.states['sensor.authority'].state = 'Local'
        await self.controller.async_tick()
        self.assertEqual(self.states['select.mode'].state, 'Baseline')
        self.assertIn('authority was lost', self.controller.status['battery']['reason'])
        self.slot.update(battery_charge_w=0, battery_discharge_w=3000)
        self.states['sensor.battery_soc'].state = '5'
        await self.controller.async_tick()
        self.assertIn('SOC protection', self.controller.status['battery']['reason'])

    async def test_invalid_current_fault_is_isolated_from_pool(self):
        self.options.update(ev_control_enabled=True, pool_control_enabled=True)
        self.slot['ev_target_current_a'] = 19
        await self.controller.async_start()
        self.assertEqual(self.controller.status['ev']['state'], 'fault')
        self.assertEqual(self.controller.status['pool']['state'], 'scheduled')

    async def test_manual_override_releases_and_does_not_reapply(self):
        self.options['ev_control_enabled'] = True
        self.options['ev_control_override_entity'] = 'switch.override'
        self.states['switch.override'] = State('off')
        await self.controller.async_start()
        self.states['switch.override'].state = 'on'
        await self.controller.async_tick()
        count = len(self.calls)
        await self.controller.async_tick()
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.states['switch.charge'].state, 'off')

    async def test_stop_restores_and_blocks_future_ticks(self):
        self.options['ev_control_enabled'] = True
        await self.controller.async_start()
        await self.controller.async_stop()
        count = len(self.calls)
        await self.controller.async_tick()
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.states['number.current'].state, '8.0')


    async def test_configuration_change_during_command_prevents_start(self):
        self.options['ev_control_enabled'] = True
        original = self.hass.services.async_call
        async def disable(domain, service, data, blocking):
            await original(domain, service, data, blocking)
            if data['entity_id'] == 'number.current':
                self.options['ev_control_enabled'] = False
        self.hass.services.async_call = disable
        await self.controller.async_start()
        self.assertNotIn(('switch.charge', 'on'), self.calls)
        self.assertEqual(self.states['number.current'].state, '8.0')

    async def test_failed_restoration_persists_and_retries_before_new_commands(self):
        self.options['ev_control_enabled'] = True
        await self.controller.async_start()
        self.states['number.current'].state = 'unavailable'
        self.coordinator.current_plan_slot = None
        await self.controller.async_tick()
        self.assertTrue(self.store.saved['records']['ev']['restoration_pending'])
        self.states['number.current'].state = '10'
        await self.controller.async_tick()
        self.assertFalse(self.store.saved['records'])
        self.assertEqual(self.states['number.current'].state, '8.0')

    async def test_stale_water_restores_without_affecting_ev(self):
        self.options.update(pool_control_enabled=True, ev_control_enabled=True)
        await self.controller.async_start()
        self.states['sensor.water'].last_reported -= timedelta(minutes=3)
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'fault')
        self.assertIn('stale', self.controller.status['pool']['reason'])
        self.assertEqual(self.controller.status['ev']['state'], 'commanded')

    async def test_old_matching_power_is_not_confirmation_of_a_new_command(self):
        self.options['battery_control_enabled'] = True
        await self.controller.async_start()
        original = self.hass.services.async_call
        old_report = self.states['sensor.battery_power'].last_reported
        async def no_new_measurement(domain, service, data, blocking):
            await original(domain, service, data, blocking)
            self.states['sensor.battery_power'].last_reported = old_report
        self.hass.services.async_call = no_new_measurement
        self.slot.update(battery_charge_w=3000)
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['battery']['state'], 'fault')
        self.assertIn('did not achieve', self.controller.status['battery']['reason'])

    async def test_disable_reenable_clears_fault_latch(self):
        self.options['ev_control_enabled'] = True
        self.states['binary_sensor.connected'].state = 'unavailable'
        await self.controller.async_start()
        self.assertEqual(self.controller.status['ev']['state'], 'fault')
        self.options['ev_control_enabled'] = False
        await self.controller.async_tick()
        self.states['binary_sensor.connected'].state = 'on'
        self.options['ev_control_enabled'] = True
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['ev']['state'], 'commanded')

    def test_pool_clamp_preserves_width_and_never_invents_maximum_target(self):
        self.assertEqual(pool_band((29.5, 30), 29, True, 24, 32, .1), (29.5, 30))
        self.assertEqual(pool_band((29.5, 30), 20, False, 24, 32, .1), (24, 24.5))
        with self.assertRaises(ValueError):
            pool_band((20, 30), 29, False, 24, 32, .1)

if __name__ == '__main__':
    unittest.main()
