"""Actual reducer/checkpoint/async-host integration with synthetic transport."""
import asyncio
from pathlib import Path
import sys
import unittest
sys.path.append(str(Path(__file__).parents[1]/'custom_components/shs_energy'))
from home_host import HomeHost, HostPorts
from home_runtime import ExecutionPlanOffered, Proposed, Step, Guard, Observed, Observation, TransportResult, WriteConfirmed
from home_runtime_checkpoint import decode_checkpoint, encode_checkpoint
from test_home_runtime_execution import Harness


class HomeHostTests(unittest.IsolatedAsyncioTestCase):
    def test_only_explicit_matching_readback_can_close_the_effect_window_early(self):
        from dataclasses import replace
        h=Harness();h.offer();h.prepare();h.durable()
        attempt=h.group.attempts[0]
        h.event(TransportResult(h.group.spec.id,attempt.id,'accepted','synthetic-ack'))
        h.observe(attempt.step.after)
        self.assertTrue(any(a.id==attempt.id for a in h.group.attempts))
        physical=replace(h.group.observation,revision=h.group.observation_revision+1)
        with self.assertRaisesRegex(ValueError,'matching physical registers'):
            h.event(WriteConfirmed(h.group.spec.id,attempt.id,replace(physical,controls=attempt.step.before)))
        self.assertTrue(any(a.id==attempt.id for a in h.group.attempts))
        self.assertLess(h.now,attempt.latest_effect_ms)
        h.event(WriteConfirmed(h.group.spec.id,attempt.id,physical))
        self.assertFalse(any(a.id==attempt.id for a in h.group.attempts))

    async def make_host(self, *, fail_persist=False, grant=True, fail_send=False):
        h=Harness(); writes=[]; durable=[]; reports=[]
        async def persist(data):
            if fail_persist: raise OSError('disk unavailable')
            durable.append(data)
        async def dispatch(send):
            saved=decode_checkpoint(durable[-1])
            self.assertTrue(any(a.id==send.attempt_id and a.stage=='sent' for g in saved.groups for a in g.attempts))
            writes.append(send)
            if fail_send: raise TimeoutError('unknown delivery')
            group=host.state.groups[0]
            h.now=max(h.now, max(a.latest_effect_ms for a in group.attempts))
            controls=tuple((k,send.value if k==send.key else v) for k,v in group.observation.controls)
            observation=Observation(group.observation_revision+1,h.now,h.now+100000,controls,
                                    (('ready',1),),group.observation.envelope)
            await host.accept(Observed(group.spec.id,observation))
        async def no_events(_):return ()
        async def renew(_,reason):return ()
        async def transition(effect):
            controls=effect.observation.controls; steps=[]
            for key,value in effect.request.target:
                if dict(controls)[key]!=value:
                    step=Step(key,value,controls,(Guard('ready',1,1),),h.group.spec.maximum,100,200,True,'synthetic-transient')
                    steps.append(step);controls=step.after
            return Proposed(effect.group_id,effect.generation,effect.request.id,effect.request.revision,
                effect.observation.revision,effect.adapter_revision,tuple(steps),effect.token)
        ports=HostPorts(persist,dispatch,lambda _:grant,no_events,no_events,transition,renew,
                        lambda *row:reports.append(row),lambda:h.now)
        host=HomeHost(h.state,ports);await host.start()
        self.addAsyncCleanup(host.close)
        return host,h,writes,durable,reports

    async def test_real_reducer_persists_before_every_native_assignment(self):
        host,h,writes,durable,_=await self.make_host()
        await host.accept(ExecutionPlanOffered(h.contract));await host.idle()
        self.assertTrue(writes)
        self.assertTrue(durable)
        self.assertEqual(host.state.groups[0].observation.controls,host.state.groups[0].desired.target)
        self.assertFalse(host.state.groups[0].attempts)

    async def test_disk_failure_prevents_dispatch(self):
        host,h,writes,_,reports=await self.make_host(fail_persist=True)
        with self.assertRaises(RuntimeError):
            await host.accept(ExecutionPlanOffered(h.contract))
        await host.idle()
        self.assertEqual(writes,[])
        self.assertTrue(reports)
        self.assertIsInstance(host._fault, OSError)
        with self.assertRaises(RuntimeError):
            await host.accept(ExecutionPlanOffered(h.contract))

    async def test_adapter_fault_keeps_explanation_in_durable_checkpoint(self):
        from dataclasses import replace
        host,h,_,durable,reports=await self.make_host()
        async def failed(effect):raise ValueError('Unsupported inverter register combination: charging mode has no writable ceiling')
        host.ports=replace(host.ports,transition=failed)
        await host.accept(ExecutionPlanOffered(h.contract));await host.idle()
        saved=decode_checkpoint(durable[-1])
        self.assertIn('no writable ceiling',saved.groups[0].transition_work.reason)
        self.assertTrue(any('no writable ceiling' in row[1] for row in reports))

    async def test_live_arbiter_is_checked_even_when_reducer_grant_was_valid(self):
        host,h,writes,_,_=await self.make_host(grant=False)
        await host.accept(ExecutionPlanOffered(h.contract));await host.idle()
        self.assertEqual(writes,[])

    async def test_timeout_and_restart_preserve_possible_physical_effect(self):
        host,h,writes,durable,_=await self.make_host(fail_send=True)
        await host.accept(ExecutionPlanOffered(h.contract));await host.idle()
        self.assertEqual(len(writes),1)
        self.assertEqual(host.state.groups[0].attempts[0].stage,'ambiguous')
        restored=HomeHost(h.state,host.ports)
        await restored.start(encode_checkpoint(host.state));await restored.idle()
        self.addAsyncCleanup(restored.close)
        self.assertFalse(restored.state.groups[0].grant_confirmed)
        self.assertEqual(restored.state.groups[0].attempts[0].stage,'ambiguous')

    async def test_slow_journal_does_not_spend_the_adapter_execution_budget(self):
        from dataclasses import replace
        host,h,writes,_,reports=await self.make_host()
        host.state=replace(host.state,limits=replace(host.state.limits,dispatch_window_ms=30000))
        persist=host.ports.persist
        async def slow_disk(data):
            h.now+=2000  # More than the old one-second transition deadline.
            await asyncio.sleep(0)
            await persist(data)
        transition=host.ports.transition
        async def native_timing(effect):
            proposal=await transition(effect)
            return replace(proposal,steps=tuple(replace(step,confirmation_timeout_ms=75000,
                latest_effect_delay_ms=75000) for step in proposal.steps))
        host.ports=replace(host.ports,persist=slow_disk,transition=native_timing)
        await host.accept(ExecutionPlanOffered(h.contract))
        await asyncio.wait_for(host.idle(),2)
        self.assertTrue(writes)
        self.assertEqual(host.state.groups[0].observation.controls,host.state.groups[0].desired.target)
        self.assertFalse(any('timeout' in reason for _,reason in reports),reports)

    async def test_hung_adapter_is_cancelled_and_retried_without_writing(self):
        from dataclasses import replace
        from home_runtime import Tick
        host,h,writes,_,reports=await self.make_host()
        host.state=replace(host.state,limits=replace(host.state.limits,transition_timeout_ms=10))
        original=host.ports.transition
        cancelled=asyncio.Event()
        async def hung(effect):
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        host.ports=replace(host.ports,transition=hung)
        await host.accept(ExecutionPlanOffered(h.contract));await asyncio.wait_for(host.idle(),2)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(writes)
        failure=host.state.groups[0].transition_work
        self.assertIn('timed out',failure.reason)
        self.assertIsNotNone(failure.retry_at_ms)
        host.ports=replace(host.ports,transition=original)
        h.now=failure.retry_at_ms
        await host.accept(Tick());await host.idle()
        self.assertTrue(writes)

    async def test_proposal_waiting_in_queue_cannot_outlive_its_observation(self):
        from dataclasses import replace
        host,h,writes,_,_=await self.make_host()
        original=host.ports.transition
        async def expired(effect):
            result=await original(effect)
            h.now=effect.observation.valid_until_ms
            return result
        host.ports=replace(host.ports,transition=expired)
        await host.accept(ExecutionPlanOffered(h.contract));await host.idle()
        self.assertFalse(writes)
        self.assertIsNone(host.state.groups[0].plan)

    async def test_delayed_proposal_cannot_restore_revoked_authority(self):
        from dataclasses import replace
        from home_runtime import GrantRevoked
        host,h,writes,_,_=await self.make_host()
        original=host.ports.transition
        entered=asyncio.Event();finish=asyncio.Event()
        async def delayed(effect):
            entered.set();await finish.wait()
            return await original(effect)
        host.ports=replace(host.ports,transition=delayed)
        await host.accept(ExecutionPlanOffered(h.contract));await entered.wait()
        await host.accept(GrantRevoked(h.group.spec.id,h.group.grant_epoch+1))
        finish.set();await host.idle()
        self.assertFalse(writes)
