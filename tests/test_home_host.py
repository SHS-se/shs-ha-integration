"""Actual reducer/checkpoint/async-host integration with synthetic transport."""
import asyncio
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).parents[1]/'custom_components/shs_energy'))
from home_host import HomeHost, HostPorts
from home_runtime import PolicyOffered, Proposed, Step, Guard, Observed, Observation
from home_runtime_checkpoint import decode_checkpoint, encode_checkpoint
from test_home_runtime_policy import Harness


class HomeHostTests(unittest.IsolatedAsyncioTestCase):
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
        await host.accept(PolicyOffered(h.compiled,h.watermark));await host.idle()
        self.assertTrue(writes)
        self.assertTrue(durable)
        self.assertEqual(host.state.groups[0].observation.controls,host.state.groups[0].desired.target)
        self.assertFalse(host.state.groups[0].attempts)

    async def test_disk_failure_prevents_dispatch(self):
        host,h,writes,_,reports=await self.make_host(fail_persist=True)
        await host.accept(PolicyOffered(h.compiled,h.watermark));await host.idle()
        self.assertEqual(writes,[])
        self.assertTrue(reports)
        self.assertIsInstance(host._fault, OSError)
        with self.assertRaises(RuntimeError):
            await host.accept(PolicyOffered(h.compiled,h.watermark))

    async def test_live_arbiter_is_checked_even_when_reducer_grant_was_valid(self):
        host,h,writes,_,_=await self.make_host(grant=False)
        await host.accept(PolicyOffered(h.compiled,h.watermark));await host.idle()
        self.assertEqual(writes,[])

    async def test_timeout_and_restart_preserve_possible_physical_effect(self):
        host,h,writes,durable,_=await self.make_host(fail_send=True)
        await host.accept(PolicyOffered(h.compiled,h.watermark));await host.idle()
        self.assertEqual(len(writes),1)
        self.assertEqual(host.state.groups[0].attempts[0].stage,'ambiguous')
        restored=HomeHost(h.state,host.ports)
        await restored.start(encode_checkpoint(host.state));await restored.idle()
        self.addAsyncCleanup(restored.close)
        self.assertFalse(restored.state.groups[0].grant_confirmed)
        self.assertEqual(restored.state.groups[0].attempts[0].stage,'ambiguous')
