"""The manual EV charge override (docs/ev-charge-override.md)."""
import unittest

from test_controller import State, Store
from test_control_continuity import Continuity, later
from test_ev_outside_stop import car_reports
from shs_core.ev_charge_override import (EvChargeOverride, capacity_kwh, charge_need_kwh,
                                         fastest_hours, override_current)


class Calculations(unittest.TestCase):
    def test_fastest_finish_is_whole_hours_at_the_charger_maximum(self):
        self.assertEqual(fastest_hours(0, 16), 0)
        self.assertEqual(fastest_hours(27, 16), 3)  # 27 kWh at 11.04 kW is 2.4 h
        self.assertEqual(fastest_hours(1, 16), 1)

    def test_a_long_window_never_goes_below_the_minimum_current(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 11, 20, tzinfo=timezone.utc)
        self.assertEqual(override_current(16.3, now + timedelta(hours=12), now, 5, 16), 5)
        self.assertEqual(override_current(48.9, now + timedelta(hours=6), now, 5, 16), 12)
        self.assertEqual(override_current(48.9, None, now, 5, 16), 16)
        self.assertEqual(override_current(48.9, now - timedelta(minutes=1), now, 5, 16), 16)

    def test_battery_size_comes_from_the_cars_own_readings(self):
        self.assertAlmostEqual(capacity_kwh(.49, 38.74), 79.06, places=2)
        self.assertIsNone(capacity_kwh(.01, 1))
        self.assertIsNone(capacity_kwh(.5, None))


class OverrideTests(Continuity):
    def setUp(self):
        super().setUp()
        self.options['device_modes']['$ev'] = 'controlling'
        self.options['ev_target_soc_entity'] = 'number.car_limit'
        self.options['ev_energy_remaining_entity'] = 'sensor.ev_energy'
        self.states['number.car_limit'] = State(80, min=50, max=100, step=1, unit_of_measurement='%')
        self.states['sensor.ev_energy'] = State(22.5, unit_of_measurement='kWh')  # 75 kWh at 30%
        self.slot['ev_target_current_a'] = 0  # The plan is not charging now.
        self.changes = 0
        self.override_store = Store()
        self.override = EvChargeOverride(self.override_store, self.changed)
        self.controller.ev_override = self.override

    def changed(self):
        self.changes += 1

    async def set(self, field, value):
        soc = float(self.states['sensor.ev_soc'].state) / 100
        size = capacity_kwh(soc, float(self.states['sensor.ev_energy'].state))
        await self.override.change(field, value, soc=soc, maximum_a=16, options=self.options,
                                   need_kwh=lambda target: charge_need_kwh(soc, target, size, .92))

    def status(self):
        return self.controller.status['ev']

    async def test_a_target_starts_charging_at_full_power_and_raises_the_limit(self):
        await self.controller.async_start()
        self.assertEqual(self.calls, [('switch.charge', 'off')] if self.calls else [])
        self.calls.clear()
        await self.set('target', 90)
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('number.car_limit', 90), ('number.current', 16), ('switch.charge', 'on')])
        self.assertEqual(self.override.active['saved_limit'], 80)
        self.assertEqual(self.override.active['raised_limit'], 90)
        self.assertIn('as fast as possible', self.status()['reason'])
        from shs_core.presentation import controller_explanation
        text = controller_explanation('ev', 'controlling', self.status(), self.slot)['explanation']
        self.assertIn('The plan requests no car charging now. The charge override takes precedence', text)

    async def test_a_stale_current_readback_is_not_rewritten_within_a_minute(self):
        # 20 November 2026: Tessie echoed 14 A, then polled the car's older
        # 16 A, and the same 14 A was sent again every ten seconds.
        await self.controller.async_start()
        await self.set('target', 54)
        await self.controller.async_tick()
        self.assertIn(('number.current', 16), self.calls)
        self.states['switch.charge'] = car_reports('on')
        self.states['number.current'] = State(12, min=5, max=16, step=1, unit_of_measurement='A')
        self.calls.clear()
        await self.controller.async_tick()
        await self.controller.async_tick()
        self.assertEqual(self.calls, [], 'the readback may still be from before the write')
        later(self, 1.1)
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('number.current', 16)], 'unanswered after a minute, it is asked again')

    async def test_a_car_limit_before_charging_starts_is_not_blamed_on_the_house(self):
        self.states['number.current'] = State(5, min=0, max=5, step=1, unit_of_measurement='A')
        await self.controller.async_start()
        await self.set('target', 90)
        await self.controller.async_tick()
        self.assertIn('The car reports a limit of 5 A', self.status()['reason'])
        self.assertNotIn('House import', self.status()['reason'])

    def test_the_car_current_is_raised_at_most_once_a_minute(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self.controller.ev_sends = {'number.current': (7.0, now - timedelta(seconds=10))}
        self.controller.verifying = False
        self.assertEqual(self.controller.ev_raise_held('number.current', 11.0), 7.0, 'no raise within a minute')
        self.assertEqual(self.controller.ev_raise_held('number.current', 5.0), 5.0, 'a reduction applies at once')
        self.controller.ev_sends = {'number.current': (7.0, now - timedelta(seconds=61))}
        self.assertEqual(self.controller.ev_raise_held('number.current', 11.0), 11.0)

    def test_a_raise_goes_out_once_the_cars_meter_confirms_the_last_current(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self.controller.verifying = False
        self.controller.ev_sends = {'number.current': (7.0, now - timedelta(seconds=10))}
        meter = State(1500, unit_of_measurement='W', state_class='measurement')
        meter.last_reported = now - timedelta(seconds=20)  # before the send
        self.states['sensor.ev_power'] = meter
        self.assertEqual(self.controller.ev_raise_held('number.current', 11.0, 'sensor.ev_power'), 7.0)
        meter = State(4900, unit_of_measurement='W', state_class='measurement')  # 7 A drawn
        self.states['sensor.ev_power'] = meter
        self.assertEqual(self.controller.ev_raise_held('number.current', 11.0, 'sensor.ev_power'), 11.0)
        self.states['sensor.ev_power'] = State(2000, unit_of_measurement='W', state_class='measurement')
        self.assertEqual(self.controller.ev_raise_held('number.current', 11.0, 'sensor.ev_power'), 7.0,
                         'still ramping: not yet confirmed')

    async def test_preset_hours_spread_the_charge_but_not_below_the_minimum_current(self):
        await self.controller.async_start()
        await self.set('hours', 12)
        await self.set('target', 50)
        self.calls.clear()
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('number.current', 5), ('switch.charge', 'on')])
        self.assertIsNone(self.override.value['hours'], 'preset hours apply to one override only')
        self.assertIsNotNone(self.override.active['deadline'])
        self.assertIn('Charge override to 50% within 11 h 59 min', self.status()['reason'])

    async def test_hours_set_during_an_override_set_its_deadline(self):
        await self.controller.async_start()
        await self.set('target', 90)
        await self.set('hours', 6)  # 48.9 kWh over 6 h
        self.calls.clear()
        await self.controller.async_tick()
        self.assertIn(('number.current', 12), self.calls)

    async def test_hours_within_the_fastest_finish_mean_full_power(self):
        await self.controller.async_start()
        await self.set('target', 90)
        await self.set('hours', 5)  # The fastest finish is 4.4 h, shown as 5.
        self.assertIsNone(self.override.active['deadline'])

    async def test_a_target_at_or_below_the_current_charge_does_nothing(self):
        await self.set('target', 30)
        self.assertIsNone(self.override.active)
        self.assertEqual(self.changes, 0)

    async def test_reaching_the_target_restores_the_limit_and_returns_to_the_plan(self):
        await self.controller.async_start()
        await self.set('target', 90)
        await self.controller.async_tick()
        self.calls.clear()
        self.states['sensor.ev_soc'] = State(90, unit_of_measurement='%')
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('number.car_limit', 80), ('switch.charge', 'off')])
        self.assertIsNone(self.override.active)

    async def test_the_car_stopping_one_point_short_of_the_target_is_reaching_it(self):
        await self.controller.async_start()
        await self.set('target', 90)
        await self.controller.async_tick()
        self.states['switch.charge'] = car_reports('on')
        await self.controller.async_tick()
        self.calls.clear()
        self.states['sensor.ev_soc'] = State(89, unit_of_measurement='%')
        self.states['switch.charge'] = car_reports('off')  # The car stopped itself.
        await self.controller.async_tick()
        self.assertIsNone(self.override.active)
        self.assertEqual(self.calls, [('number.car_limit', 80)])
        self.assertEqual(self.status()['decision']['reason'], 'charge override target reached')
        self.assertNotIn('outside_stop_until', self.controller.ownership.records['ev'], 'no pause')

    async def test_a_stop_in_the_car_app_ends_the_override_and_leaves_the_car_stopped(self):
        # 20 November 2026: SHS switches the Easee charger; a stop in the Tesla
        # app leaves that switch on and the car drawing nothing.
        self.options['device_control_mappings']['charger']['power'] = 'sensor.ev_power'
        self.states['sensor.ev_power'] = State(7000, unit_of_measurement='W', state_class='measurement')
        await self.controller.async_start()
        await self.set('target', 90)
        await self.controller.async_tick()
        self.states['switch.charge'] = car_reports('on')
        await self.controller.async_tick()
        self.calls.clear()
        self.states['sensor.ev_power'] = State(0, unit_of_measurement='W', state_class='measurement')
        await self.controller.async_tick()
        self.assertIsNotNone(self.override.active, 'a short dip is not a stop')
        later(self, 1.1)
        await self.controller.async_tick()
        self.assertIsNone(self.override.active)
        self.assertEqual(self.calls, [('number.car_limit', 80)], 'the charger switch is left alone')
        self.assertEqual(self.status()['decision']['reason'], 'charging stopped outside SHS')

    async def test_a_limit_changed_elsewhere_is_left_as_it_is(self):
        await self.controller.async_start()
        await self.set('target', 90)
        await self.controller.async_tick()
        self.states['number.car_limit'].state = '85'
        self.calls.clear()
        await self.set('end', None)
        await self.controller.async_tick()
        self.assertNotIn('number.car_limit', [entity for entity, _ in self.calls])
        self.assertIsNone(self.override.active)

    async def test_ending_restores_the_limit(self):
        await self.controller.async_start()
        await self.set('target', 90)
        await self.controller.async_tick()
        self.calls.clear()
        await self.set('end', None)
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('number.car_limit', 80), ('switch.charge', 'off')])

    async def test_unplugging_ends_the_override(self):
        await self.controller.async_start()
        await self.set('target', 90)
        await self.controller.async_tick()
        self.states['binary_sensor.connected'] = State('off')
        await self.controller.async_tick()
        self.assertIsNone(self.override.active)
        self.assertEqual(float(self.states['number.car_limit'].state), 80)

    async def test_a_stop_made_outside_shs_ends_the_override_and_pauses_the_plan(self):
        self.slot['ev_target_current_a'] = 10
        await self.controller.async_start()
        await self.set('target', 90)
        await self.controller.async_tick()
        self.states['switch.charge'] = car_reports('on')  # The car reports charging.
        await self.controller.async_tick()
        self.calls.clear()
        self.states['switch.charge'] = State('off')
        await self.controller.async_tick()
        self.assertIsNone(self.override.active)
        self.assertEqual(self.calls, [('number.car_limit', 80)], 'charging is not switched back on')
        self.assertEqual(self.status()['decision']['reason'], 'charging stopped outside SHS')
        later(self, 1.1)
        await self.controller.async_tick()
        self.assertIn(('switch.charge', 'on'), self.calls, 'the plan resumes after the minute')

    async def test_an_override_set_during_a_pause_charges_at_once(self):
        self.slot['ev_target_current_a'] = 10
        await self.controller.async_start()
        self.states['switch.charge'] = car_reports('on')  # The car reports charging.
        await self.controller.async_tick()
        self.states['switch.charge'] = State('off')
        await self.controller.async_tick()
        self.calls.clear()
        later(self, 0.2)
        await self.set('target', 90)
        await self.controller.async_tick()
        self.assertIn(('switch.charge', 'on'), self.calls)

    async def test_leaving_controlling_ends_the_override_without_a_write(self):
        await self.controller.async_start()
        await self.set('target', 90)
        await self.controller.async_tick()
        self.calls.clear()
        self.options['device_modes']['$ev'] = 'control_verification'
        await self.override.retire(self.options)
        await self.controller.async_tick()
        self.assertIsNone(self.override.active)
        self.assertEqual(self.calls, [])

    async def test_the_override_survives_a_restart(self):
        await self.set('target', 90)
        restored = EvChargeOverride(self.override_store, self.changed)
        await restored.load()
        self.assertEqual(restored.active['target_soc'], 90)

    async def test_editing_needs_controlling(self):
        self.options['device_modes']['$ev'] = 'monitoring'
        with self.assertRaises(ValueError):
            await self.set('target', 90)



class EntityTests(unittest.IsolatedAsyncioTestCase):
    """The two controls track the car while idle and show the override while active."""

    async def asyncSetUp(self):
        from math import ceil
        from types import SimpleNamespace
        from test_execution_mode_select import load_adapter, load_equipment
        from shs_core.gateway_service import GatewayService
        from shs_core.operating_modes import device_mode
        self.options = {'device_modes': {'$ev': 'controlling'}, 'ev_soc_entity': 'sensor.ev_soc',
                        'ev_energy_remaining_entity': 'sensor.ev_energy',
                        'device_control_mappings': {'charger': {'control_type': 'variable_power',
                            'control_entity_id': 'number.current', 'minimum_value': 5, 'maximum_value': 16}}}
        self.rows = {'sensor.ev_soc': {'state': '30', 'attributes': {'unit_of_measurement': '%'}},
                     'sensor.ev_energy': {'state': '22.5', 'attributes': {'unit_of_measurement': 'kWh'}}}
        source = SimpleNamespace(options=lambda: self.options, report=lambda entity: self.rows.get(entity))
        service = GatewayService(SimpleNamespace(listeners=set()), {}, source)
        service.active = True
        service.ev_override = EvChargeOverride(Store(), lambda: None)
        self.service = service
        equipment = load_equipment()
        entities = load_adapter('ev_override_entities.py', {'callback': lambda fn: fn,
            'equipment_device_info': equipment.equipment_device_info, 'device_mode': device_mode,
            'GatewayConflict': Exception, 'HomeAssistantError': Exception, 'ceil': ceil})
        number = load_adapter('number.py', {'HeatingEntity': type('HeatingEntity', (), {}), 'NumberEntity': object,
            'EvOverrideEntity': entities.EvOverrideEntity,
            'NumberDeviceClass': SimpleNamespace(TEMPERATURE='temperature', DURATION='duration', BATTERY='battery'),
            'NumberMode': SimpleNamespace(BOX='box')})
        entry = SimpleNamespace(entry_id='home', data={'device_token_id': 'token'},
            runtime_data=SimpleNamespace(service=service, online=True, catalogue={'devices': []}))
        self.target = number.EvOverrideNumber(entry, 'target')
        self.hours = number.EvOverrideNumber(entry, 'hours')

    async def test_idle_controls_show_the_cars_charge_and_no_hours(self):
        self.assertEqual(self.target.native_value, 30)
        self.assertEqual(self.hours.native_value, 0)

    async def test_preset_hours_are_shown_until_a_target_is_set(self):
        await self.hours.async_set_native_value(7.2)
        self.assertEqual(self.hours.native_value, 8, 'rounded up to a whole hour')

    async def test_an_active_override_shows_its_target_and_at_least_the_fastest_finish(self):
        await self.target.async_set_native_value(90)
        self.assertEqual(self.target.native_value, 90)
        self.assertEqual(self.hours.native_value, 5)  # 48.9 kWh at 11.04 kW
        await self.hours.async_set_native_value(12)
        self.assertEqual(self.hours.native_value, 12)
        await self.hours.async_set_native_value(2)
        self.assertEqual(self.hours.native_value, 5, 'never less than the fastest finish')
        self.assertIsNone(self.service.ev_override.active['deadline'])

    async def test_the_controls_are_unavailable_outside_controlling(self):
        self.options['device_modes']['$ev'] = 'control_verification'
        self.assertFalse(self.target.available)
        self.assertIsNone(self.target.native_value)



class PlannerLimitTests(unittest.TestCase):
    """While an override has raised the car's limit, the planner sees the saved one."""

    def payload(self, limit, override):
        from types import SimpleNamespace
        from shs_core.household import Household
        rows = {'number.car_limit': limit, 'sensor.ev_soc': '40'}
        host = SimpleNamespace(
            ev_override_limit=override,
            ports=SimpleNamespace(options=lambda: {'ev_target_soc_entity': 'number.car_limit'}),
            _entity_payload=lambda entity: {'entity_id': entity, 'state': rows[entity],
                                            'attributes': {'unit_of_measurement': '%'}})
        return lambda entity: Household._planning_entity_payload(host, entity)['state']

    def test_a_raised_limit_is_planned_as_the_saved_limit(self):
        read = self.payload('90', (80, 90))
        self.assertEqual(read('number.car_limit'), '80')
        self.assertEqual(read('sensor.ev_soc'), '40')

    def test_a_limit_changed_elsewhere_is_planned_as_it_is(self):
        self.assertEqual(self.payload('85', (80, 90))('number.car_limit'), '85')
        self.assertEqual(self.payload('90', None)('number.car_limit'), '90')


if __name__ == '__main__':
    unittest.main()
