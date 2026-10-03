"""Physical gateway executes finite semantic operations, never a policy loop."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import unittest

import test_controller as fixtures
from command_fixture import native_executor, controller_inputs
from shs_core.device_gateway import DeviceGateway
from shs_core.gateway_journal import GatewayConflict


class MemoryOperations:
    def __init__(self):
        self.values = {}

    async def begin(self, operation):
        key = operation['request_id']
        if key in self.values:
            original, result = self.values[key]
            if original != operation:
                raise GatewayConflict('Operation identity reused')
            return {'state':'completed', 'result':result} if result else {'state':'interrupted'}
        self.values[key] = deepcopy(operation), None
        return {'state':'new'}

    async def finish(self, operation, result):
        self.values[operation['request_id']] = deepcopy(operation), deepcopy(result)


class PhysicalGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixtures.ControllerTests.setUp(self)
        self.permitted = True
        def authorize(intent):
            if not self.permitted:
                raise GatewayConflict('Session revoked')
        self.gateway = DeviceGateway(controller_inputs(self.hass), self.controller.options, self.store,
            self.controller.native_executor, authorize=authorize, before_external=lambda device: True, operations=MemoryOperations())
        await self.gateway.load()
        self.options['device_modes']['$pool'] = 'controlling'
        self.models = self.coordinator.optimisation_plan['device_models']
        await self.gateway.synchronize(self.models)

    def request(self, operation='apply', **parameters):
        body = dict(start=(datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat(),
                    heating_w=2000, stop_temperature_c=30, models=self.models) if operation == 'apply' else {}
        return dict(request_id='one', device='pool', operation=operation, configuration_revision=1,
                    policy_revision=1, headroom_reserved=True, parameters={**body, **parameters})

    async def test_pool_capture_command_and_restore_without_household_or_scheduler(self):
        result = await self.gateway.perform(self.request())
        self.assertIsNone(result['error'])
        self.assertEqual(self.states['switch.pool'].state, 'on')
        self.assertEqual(result['ownership']['records']['pool']['originals'], {'switch.pool':'off'})
        self.assertFalse(hasattr(self.gateway, 'coordinator'))
        self.assertIsNone(self.gateway.scheduler)
        released = await self.gateway.perform({**self.request('release'), 'request_id':'release'})
        self.assertIsNone(released['error'])
        self.assertEqual(self.states['switch.pool'].state, 'off')
        self.assertEqual(released['ownership']['records'], {})

    async def test_permission_change_during_durable_prepare_prevents_service(self):
        transport = self.gateway.native_executor.transport
        prepare = transport.journal.prepare
        def revoke(command):
            result = prepare(command)
            self.permitted = False
            return result
        transport.journal.prepare = revoke
        result = await self.gateway.perform(self.request())
        self.assertEqual(result['error']['kind'], 'rejected')
        self.assertEqual(self.states['switch.pool'].state, 'off')

    async def test_verification_mode_hands_the_device_back_and_accepts_no_schedule(self):
        await self.gateway.perform(self.request())
        self.options['device_modes']['$pool'] = 'control_verification'
        ownership = await self.gateway.synchronize(self.models)
        self.assertEqual(ownership['records']['pool']['originals'], {'switch.pool':'off'})
        result = await self.gateway.perform({**self.request(), 'request_id':'after-verification'})
        self.assertIsNotNone(result['error'])
        self.assertEqual(self.states['switch.pool'].state, 'on')
        released = await self.gateway.perform({**self.request('release'), 'request_id':'release'})
        self.assertIsNone(released['error'])
        self.assertEqual(self.states['switch.pool'].state, 'off')
        self.assertEqual(released['ownership']['records'], {})

    async def test_verification_handover_is_finished_without_the_app(self):
        await self.gateway.perform(self.request())
        self.options['device_modes']['$pool'] = 'control_verification'
        await self.gateway.maintain_obligations()
        self.assertEqual(self.states['switch.pool'].state, 'off')
        self.assertEqual(self.gateway.ownership.records, {})

    async def test_minimum_run_rejects_early_stop_and_preserves_ownership(self):
        self.options['device_control_mappings']['pool']['minimum_on_seconds'] = 3600
        await self.gateway.synchronize(self.models)
        result = await self.gateway.perform(self.request())
        self.assertIsNone(result['error'])
        stopped = await self.gateway.perform({**self.request(heating_w=0), 'request_id':'stop'})
        self.assertEqual(stopped['error']['kind'], 'deadline')
        self.assertTrue(stopped['deadlines'])
        self.assertEqual(self.states['switch.pool'].state, 'on')

    async def test_raw_battery_or_service_payload_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.gateway.perform({**self.request(), 'parameters':{'service':'turn_on','entity':'switch.pool'}})
        with self.assertRaises(ValueError):
            await self.gateway.perform({**self.request(), 'device':'battery'})
