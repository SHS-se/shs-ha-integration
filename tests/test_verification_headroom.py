"""Mode handover can reduce demand while battery charging is constrained."""
from unittest.mock import Mock
import unittest

import test_verification as verification
import test_device_gateway as gateway
from test_battery_runtime import Rig


class HandoverHeadroomTests(unittest.IsolatedAsyncioTestCase):
    setUp = verification.VerificationTests.setUp

    async def test_ev_off_handover_restores_current_without_reserving_demand(self):
        self.options['device_modes']['$ev'] = 'controlling'
        self.slot['ev_target_current_a'] = 0
        await self.controller.async_start()
        self.states['number.current'].state = '16'
        self.controller.before_external_command = Mock(return_value=False)
        self.options['device_modes']['$ev'] = 'control_verification'
        await self.controller.async_tick()
        self.assertEqual(self.states['number.current'].state, '8.0')
        self.assertEqual(self.states['switch.charge'].state, 'off')
        self.assertNotIn('ev', self.controller.ownership.records)
        self.assertEqual(self.controller.status['ev']['state'], 'verified')
        self.controller.before_external_command.assert_not_called()

    async def test_ev_stop_is_not_blocked_by_battery_charging(self):
        self.options['device_modes']['$ev'] = 'controlling'
        await self.controller.async_start()
        self.controller.before_external_command = Mock(return_value=False)
        self.slot['ev_target_current_a'] = 0
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.charge'].state, 'off')
        self.controller.before_external_command.assert_not_called()

    async def test_ev_handover_to_on_still_requires_headroom(self):
        self.states['switch.charge'].state = 'on'
        self.options['device_modes']['$ev'] = 'controlling'
        self.slot['ev_target_current_a'] = 0
        await self.controller.async_start()
        self.controller.before_external_command = Mock(return_value=False)
        self.options['device_modes']['$ev'] = 'control_verification'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.charge'].state, 'off')
        self.assertIn('ev', self.controller.ownership.records)
        self.assertEqual(self.controller.status['ev']['state'], 'pending')


class MeasuredChargeReliefTests(unittest.IsolatedAsyncioTestCase):
    async def test_measured_charge_above_register_bound_can_release_headroom(self):
        r = Rig()
        await r.start()
        try:
            for _ in range(4):
                await r.advance()
            ceiling = float(r.rows['number.charge']['state']) * 1000
            r.rows['sensor.battery']['state'] = str(ceiling + 1)
            self.assertFalse(r.runtime.before_external_command('ev'))
            for _ in range(10):
                await r.advance(1000)
                if float(r.rows['number.charge']['state']) == 0:
                    break
            self.assertEqual(float(r.rows['number.charge']['state']), 0, r.runtime.snapshot()['command_state'])
            # Acknowledged ceiling relief does not authorize an appliance until
            # the measured physical response has also stopped charging.
            self.assertFalse(r.runtime.before_external_command('ev'))
            r.rows['sensor.battery']['state'] = '0'
            for _ in range(4):
                await r.advance(1000)
            self.assertTrue(r.runtime.before_external_command('ev'))
        finally:
            await r.runtime.close()


class GatewayHandoverHeadroomTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = gateway.PhysicalGatewayTests.asyncSetUp

    def ev_request(self, operation, identity, **parameters):
        return dict(request_id=identity, device='ev', operation=operation,
                    configuration_revision=1, policy_revision=1,
                    headroom_reserved=False, parameters=parameters)

    async def test_gateway_restores_off_ev_without_app_reservation(self):
        self.options['device_modes']['$ev'] = 'controlling'
        await self.gateway.capture('ev', self.options, ['number.current', 'switch.charge'])
        self.states['number.current'].state = '16'
        self.options['device_modes']['$ev'] = 'control_verification'
        self.gateway.external_ready = Mock(return_value=False)
        result = await self.gateway.perform(self.ev_request('release', 'release'))
        self.assertIsNone(result['error'])
        self.assertFalse(result['reservation_required'])
        self.assertEqual(self.states['number.current'].state, '8.0')
        self.assertEqual(result['ownership']['records'], {})
        self.gateway.external_ready.assert_not_called()

    async def test_gateway_rechecks_switch_before_current_dispatch(self):
        self.options['device_modes']['$ev'] = 'controlling'
        await self.gateway.capture('ev', self.options, ['number.current', 'switch.charge'])
        self.states['number.current'].state = '16'
        self.options['device_modes']['$ev'] = 'control_verification'
        prepare = self.gateway.native_executor.transport.journal.prepare
        def charger_starts(command):
            result = prepare(command)
            self.states['switch.charge'].state = 'on'
            return result
        self.gateway.native_executor.transport.journal.prepare = charger_starts
        result = await self.gateway.perform(self.ev_request('release', 'release'))
        self.assertIsNotNone(result['error'])
        self.assertTrue(result['reservation_required'])
        self.assertEqual(self.states['number.current'].state, '16')
        self.assertIn('ev', result['ownership']['records'])
