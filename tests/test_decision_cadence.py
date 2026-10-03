"""Economic selection and physical command progress have distinct inputs."""
from dataclasses import replace
import unittest
from unittest.mock import patch
from test_home_runtime_execution import Harness
from shs_core import home_runtime as rt


class DecisionCadenceTests(unittest.TestCase):
    def capture(self, h, *, controls=None, load=1000, ready=1):
        revision=h.state.conditions_revision+1
        at=h.now+1
        observation=replace(h.group.observation, revision=revision,at_ms=at,
            controls=controls or h.group.observation.controls,measurements=(('ready',ready),))
        return rt.MeasurementsReceived(rt.Observed(h.group.spec.id,observation),
            replace(h.state.frame,revision=revision,at_ms=at),
            replace(h.state.conditions,revision=revision,at_ms=at,residual_load_w=load,eligible_load_w=load))

    def test_physical_capture_and_deadline_do_not_retarget(self):
        h=Harness();h.offer()
        request,assessment=h.group.desired,h.state.execution.assessment
        capture=self.capture(h,load=1200)
        # The send guard still assesses current evidence; selection alone is
        # prohibited. A completed target lets this check isolate that boundary.
        h.state=replace(h.state,groups=(replace(h.group,observation=replace(h.group.observation,
            controls=request.target),plan=None,transition_work=None),))
        capture=replace(capture,observed=replace(capture.observed,
            observation=replace(capture.observed.observation,controls=request.target)))
        with patch.object(rt.execution,'assess_execution',side_effect=AssertionError('physical event selected')):
            h.event(capture,capture.conditions.at_ms)
            h.event(rt.ExecutionWake())
            h.event(rt.AuthorityChanged(h.group.spec.id,h.group.mode,h.group.mode_revision,h.group.release))
        self.assertEqual(h.group.desired,request)
        self.assertEqual(h.state.execution.assessment,assessment)
        self.assertEqual(h.state.conditions.residual_load_w,1200)
        with patch.object(rt.execution,'assess_execution',wraps=rt.execution.assess_execution) as assess:
            h.event(rt.Tick())
            self.assertTrue(assess.called)

    def test_proposal_survives_new_capture_with_same_start_controls(self):
        h=Harness();h.offer()
        job=h.group.transition_work
        proposal=[];current=h.group.observation.controls
        for key,value in h.group.desired.target:
            if dict(current)[key]!=value:
                step=rt.Step(key,value,current,(rt.Guard('ready',1,1),),h.group.spec.maximum,100,200,True,'synthetic')
                proposal.append(step);current=step.after
        event=rt.Proposed(h.group.spec.id,h.group.generation,h.group.desired.id,h.group.desired.revision,
            job.key.observed_revision,h.group.spec.adapter_revision,tuple(proposal),job.token)
        capture=self.capture(h,load=1001)
        h.event(capture,capture.conditions.at_ms)
        self.assertEqual(h.group.transition_work.token,job.token)
        h.event(event)
        self.assertIsNotNone(h.group.plan)
        self.assertTrue(any(a.stage=='prepared' for a in h.group.attempts))

    def test_changed_start_controls_do_not_admit_old_proposal(self):
        h=Harness();h.offer()
        job=h.group.transition_work;request=h.group.desired
        step=rt.Step('mode',dict(request.target)['mode'],h.group.observation.controls,(),h.group.spec.maximum,100,200,True,'synthetic')
        capture=self.capture(h,controls=(('mode','Maximum Self Consumption'),('charge',0),('discharge',0)))
        h.event(capture,capture.conditions.at_ms)
        self.assertGreater(h.group.transition_work.token,job.token)
        h.event(rt.Proposed(h.group.spec.id,h.group.generation,request.id,request.revision,
            job.key.observed_revision,h.group.spec.adapter_revision,(step,),job.token))
        self.assertIsNone(h.group.plan)

    def test_accepted_state_report_settles_promptly_and_keeps_one_soc_fact(self):
        h=Harness();h.offer();h.prepare();send=h.durable()
        h.event(rt.TransportResult(h.group.spec.id,send.attempt_id,'accepted','service complete'))
        self.assertTrue(h.group.attempts)
        before=len(h.state.execution.account.observations)
        capture=self.capture(h,controls=rt.command_controls(h.group))
        state,effects=rt.archive_evidence(h.state,capture,capture.conditions.at_ms)
        self.assertNotIn(send.attempt_id,[a.id for a in state.groups[0].attempts])
        self.assertEqual(len(state.execution.account.observations),before+1)
        self.assertTrue(any(isinstance(e,rt.Persist) for e in effects))

    def test_invalid_held_target_waits_for_tick_without_repeated_proposals(self):
        h=Harness();h.offer()
        request=h.group.desired
        # Conditions for a discharge contract are tested with the same native
        # final guard; force its rejection here to isolate physical progression.
        with patch.object(rt,'_execution_send_valid',return_value=False):
            for now in (1,2,3,4):
                h.event(rt.ExecutionWake(),now)
                self.assertEqual(h.group.status,'awaiting_decision')
                self.assertIsNone(h.group.transition_work)
                self.assertFalse(any(isinstance(e,rt.NeedTransition) for e in h.effects))
                self.assertEqual(h.group.desired,request)

    def test_archival_guard_loss_cancels_unsent_work(self):
        h=Harness();h.offer();h.prepare()
        capture=self.capture(h,ready=0)
        state,effects=rt.archive_evidence(h.state,capture,capture.conditions.at_ms)
        self.assertFalse(state.groups[0].attempts)
        self.assertTrue(any(isinstance(e,rt.Persist) for e in effects))

    def test_physical_binding_keeps_all_power_checks_without_historical_diagnostics(self):
        h=Harness();h.offer()
        live=rt.execution_live(h.state,h.now)
        conversion=h.state.authority.plant.conversion
        account=h.state.execution.account
        full=rt.execution.assess_execution(account,live,conversion)
        with patch.object(rt.execution,'_live_objectives',side_effect=AssertionError('physical guard read history')):
            physical=rt.execution.assess_execution(account,live,conversion,responsibilities=False)
        self.assertEqual(physical,full)
        self.assertEqual(rt.execution_binding(h.state,physical),rt.execution_binding(h.state,full))
        with patch.object(rt.execution,'_live_objectives',return_value=([
            {'outcome':'missed','responsibility':'outstanding','objective':{'id':'older-objective'}}],{})):
            diagnostic=rt.execution.assess_execution(account,live,conversion)
        self.assertEqual(diagnostic.replan_reason,'objective_missed')
        self.assertEqual(rt.execution_binding(h.state,physical),rt.execution_binding(h.state,diagnostic))
        self.assertEqual(physical.valid_until_ms,diagnostic.valid_until_ms)
