"""The real native adapter, FIFO journal and final HA permission boundary."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from gateway_fixture import IDENTITY, seed
from test_battery_runtime import Rig
from shs_core import home_runtime as rt
from shs_core.battery_gateway import BatteryGateway, OWNER
from shs_core.command_transport import CommandTransport
from shs_core.gateway_journal import GatewayConflict
from shs_core.gateway_stream import GatewayStream, GatewayCommands
from shs_core.native_commands import NativeExecutor
from shs_core.runtime_json import encode_value, decode_value, runtime_digest


class BatteryGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rig = Rig()
        self.effects = []
        transition = self.rig.runtime._transition
        async def capture(effect):
            self.effects.append(effect)
            return await transition(effect)
        self.rig.runtime._transition = capture
        await self.rig.start()
        self.assertTrue(self.effects)
        self.effect = self.effects[0]
        self.catalog = self.rig.runtime.adapter.catalog
        self.conversion = self.rig.runtime.adapter.conversion
        self.options = deepcopy(self.rig.options)
        self.rows = deepcopy(self.rig.rows)
        await self.rig.runtime.close(release=False)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        _, self.journal = seed(self.temp.name)
        self.journal.open()
        self.addCleanup(self.journal.close)
        self.session = self.journal.begin(IDENTITY, 'app')['session']
        revision = self.journal.record('configuration', self.options)
        page = self.journal.read(self.session, 0)
        self.journal.acknowledge_delivery(self.session, page['through'])
        self.journal.activate(self.session, 'a', dict(identity=IDENTITY, configuration_revision=revision,
            through=page['through'], app_checkpoint_sha256='a', physical_reconciliation_sha256='b'))
        self.journal.save_record('battery_writer', dict(schema='battery-writer-v1', owner='fenced', epoch=1, surfaces=[]))
        self.stream = GatewayStream(self.journal)
        self.stream.start()
        self.addAsyncCleanup(self.stream.close)
        self.calls = []
        async def send(*args):
            self.calls.append(args)
        def state(entity):
            row = self.rows[entity]
            return SimpleNamespace(state=row['state'], attributes=row['attributes'])
        commands = GatewayCommands(self.stream)
        native = NativeExecutor(CommandTransport(commands, commands.execute), state, lambda:'°C', send)
        self.physical = SimpleNamespace(lock=asyncio.Lock(), native_executor=native,
            ownership=SimpleNamespace(records={}))
        self.allowed = True
        self.policy = 1
        def authorize(operation):
            if not self.allowed:
                raise GatewayConflict('revoked')
        self.gateway = BatteryGateway(self.stream, self.physical, lambda:self.options,
            lambda entity:self.rows.get(entity), lambda:self.rig.now, authorize=authorize,
            session=lambda:self.session, context=lambda:dict(session=self.session, policy=self.policy))
        await self.gateway.open()
        identity = rt.WriterIdentity(OWNER, runtime_digest(self.options), self.catalog.control_surface_revision)
        self.grant = decode_value(await self.gateway.grant(encode_value(identity), encode_value(self.catalog),
            self.conversion.wire(), self.rig.now+900000), rt.WriterGrant)
        self.route = await self.gateway.propose(encode_value(self.effect), encode_value(self.grant))
        self.proposal = decode_value(self.route['proposal'], rt.Proposed)

    def send(self, index=0):
        p = self.proposal
        step = p.steps[index]
        return encode_value(rt.Send(p.group_id, 'attempt-'+str(index), step.key, step.value,
            self.rig.now+10000, self.grant, p.generation, p.request_id, p.request_revision))

    async def step(self, index=0):
        return await self.gateway.step(self.route['route_id'], index, self.send(index))

    async def test_exact_route_and_repeated_step_never_repeat_native_dispatch(self):
        self.assertEqual((await self.step())['status'], 'service_returned')
        self.assertEqual((await self.step())['status'], 'service_returned')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((await self.stream.call('read_route', self.session, self.route['route_id']))['next_step'], 1)

    async def test_out_of_order_or_tampered_step_cannot_dispatch(self):
        self.assertGreater(len(self.proposal.steps), 1)
        self.assertEqual((await self.step(1))['status'], 'not_sent')
        changed = self.send()
        changed['value'] = 'arbitrary'
        with self.assertRaises(GatewayConflict):
            await self.gateway.step(self.route['route_id'], 0, changed)
        self.assertFalse(self.calls)

    async def test_policy_change_during_prepare_is_not_sent(self):
        original = self.journal.prepare_command
        def prepare(command):
            result = original(command)
            self.policy += 1
            return result
        self.journal.prepare_command = prepare
        self.assertEqual((await self.step())['status'], 'not_sent')
        self.assertFalse(self.calls)

    async def test_socket_revocation_during_prepare_is_not_sent(self):
        original = self.journal.prepare_command
        def prepare(command):
            result = original(command)
            self.allowed = False
            self.gateway.revoke()
            return result
        self.journal.prepare_command = prepare
        self.assertEqual((await self.step())['status'], 'not_sent')
        self.assertFalse(self.calls)

    async def test_restart_retains_installation_but_no_writer_grant(self):
        replacement = BatteryGateway(self.stream, self.physical, lambda:self.options,
            lambda entity:self.rows.get(entity), lambda:self.rig.now, authorize=lambda op:None,
            session=lambda:self.session, context=lambda:{})
        await replacement.open()
        self.assertEqual(replacement.installation, self.gateway.installation)
        self.assertFalse(replacement.fence.is_current(self.grant, replacement.identity()))

    async def test_revoked_writer_returns_native_controls_to_baseline(self):
        self.gateway.revoke()
        await self.gateway.maintain_obligation()
        self.assertTrue(self.calls)
        settings = [(data['entity_id'],data.get('option',data.get('value'))) for _,_,data in self.calls]
        self.assertIn(('select.mode','Maximum Self Consumption'),settings)
        self.assertFalse(self.gateway.obligation['pending'])
        before = len(self.calls)
        await self.gateway.maintain_obligation()
        self.assertEqual(len(self.calls),before)

    async def test_verification_relinquishes_without_a_baseline_write(self):
        self.options['device_modes']['$battery'] = 'control_verification'
        self.gateway.revoke()
        await self.gateway.maintain_obligation()
        self.assertFalse(self.calls)
        self.assertFalse(self.gateway.obligation['pending'])

    async def test_uncertain_handback_is_fenced_and_never_replayed(self):
        async def ambiguous(*args):
            self.calls.append(args)
            raise TimeoutError('native response lost')
        self.physical.native_executor.send = ambiguous
        self.gateway.revoke()
        with self.assertRaises(TimeoutError):
            await self.gateway.maintain_obligation()
        self.assertIsNotNone(self.gateway.obligation['fault'])
        before = len(self.calls)
        with self.assertRaisesRegex(GatewayConflict,'uncertain'):
            await self.gateway.maintain_obligation()
        self.assertEqual(len(self.calls),before)
