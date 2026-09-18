"""Regress the four reviewed recovery defects using the real reducer and codec."""
from dataclasses import replace
import json
import unittest

from test_home_runtime import Harness, controls, synthetic_steps
from test_home_runtime_execution import Harness as PolicyHarness
from test_energy_ledger import spec, sample
from energy_ledger import (
    EnergyBounds, CounterSample, create_ledger, record_sample, mark_actuals,
    actuals_since, start_settlement, record_actuals, settle_and_prune,
    reconciled_actuals,
)
from home_runtime import (
    Envelope, Guard, Step, ReliefRule, FrameObserved,
    Proposed, TransitionFailed, TransitionJob, TransitionFailure, NeedTransition,
    JournalDurable, TransportResult, Tick, Send, WakeAt, MeterObserved,
    LedgerPruned, reservation, reduce_home,
)
from home_runtime_checkpoint import encode_checkpoint, decode_checkpoint, restore_checkpoint


class RetryPreparationTests(unittest.TestCase):
    def test_post_bound_readback_cancels_unsent_retry_with_invalidated_or_advanced_plan(self):
        for matching in (False, True):
            with self.subTest(matching=matching):
                h = Harness(); h.request(); h.propose(); sent = h.durable()
                original = h.group().attempts[0]
                h.event(TransportResult("battery", sent.attempt_id, "ambiguous", "timeout"))
                h.event(Tick(), original.latest_effect_ms + 1)
                retry = next(a for a in h.group().attempts if a.stage == "prepared")
                h.now += 1
                h.observe(target=original.step.after if matching else original.step.before)
                # Harness encodes every Persist: this used to raise here.
                self.assertNotIn(retry.id, [a.id for a in h.group().attempts])
                self.assertNotIn(original.id, [a.id for a in h.group().attempts])
                h.event(JournalDurable(retry.prepared_revision))
                self.assertFalse(any(isinstance(e, Send) for e in h.effects))
                if matching:
                    self.assertEqual(h.group().plan.index, 1)
                else:
                    self.assertIsNone(h.group().plan)
                h.event(Tick(), max(h.now, h.group().retry_not_before_ms))
                h.finish()
                self.assertEqual(h.group().status, "adopted")

    def test_settlement_does_not_discard_a_later_issued_retry(self):
        h = Harness(); h.request(); h.propose(); first = h.durable()
        bound = h.group().attempts[0].latest_effect_ms
        h.event(TransportResult("battery", first.attempt_id, "ambiguous", "timeout"))
        h.event(Tick(), h.group().retry_not_before_ms)
        second = h.durable()
        h.now = bound
        h.observe(target=controls("external"))
        self.assertEqual([a.id for a in h.group().attempts], [second.attempt_id])
        self.assertIsNone(h.group().plan)
        h.request(target=controls("export", 0, 1000), revision=2)
        h.propose()
        self.assertFalse(any(a.stage == "prepared" for a in h.group().attempts))


class TransitionWorkerTests(unittest.TestCase):
    def proposal(self, h, token):
        g = h.group()
        return Proposed(g.spec.id, g.generation, g.desired.id, g.desired.revision,
                        g.observation_revision, g.spec.adapter_revision,
                        synthetic_steps(g.observation.controls, g.desired.target), token)

    def test_host_timeout_retries_and_fences_late_success_and_failure(self):
        h = Harness(); h.request()
        first = h.group().transition_work
        old = self.proposal(h, first.token)
        self.assertIn(WakeAt(first.deadline_ms), h.effects)
        h.event(Tick(), h.now + h.state.limits.transition_timeout_ms)
        self.assertFalse(any(isinstance(e, NeedTransition) for e in h.effects))
        self.assertEqual(h.group().transition_work, first)
        # Only the host can measure the adapter's execution time; queue time
        # alone must not invalidate this still-current proposal job.
        h.event(TransitionFailed("battery", first.token, "retryable", "preparation timed out"))
        failure = h.group().transition_work
        self.assertIsInstance(failure, TransitionFailure)
        self.assertEqual(failure.retry_at_ms, h.now + h.state.limits.retry_base_ms)
        h.event(old)
        self.assertIsNone(h.group().plan)
        h.event(Tick(), failure.retry_at_ms)
        second = h.group().transition_work
        self.assertEqual(second.attempt, 2)
        self.assertGreater(second.token, first.token)
        h.event(old)
        h.event(TransitionFailed("battery", first.token, "unsupported", "late failure"))
        self.assertEqual(h.group().transition_work, second)
        h.propose(); h.finish()
        self.assertEqual(h.group().status, "adopted")

    def test_retryable_failure_is_capped_and_observation_churn_keeps_pacing(self):
        h = Harness(); h.request()
        delays = []
        for _ in range(8):
            job = h.group().transition_work
            h.event(TransitionFailed("battery", job.token, "retryable", "worker unavailable"))
            retry = h.group().transition_work.retry_at_ms
            delays.append(retry - h.now)
            h.observe()
            self.assertEqual(h.group().transition_work.retry_at_ms, retry)
            h.event(Tick(), retry - 1)
            self.assertFalse(any(isinstance(e, NeedTransition) for e in h.effects))
            h.event(Tick(), retry)
            self.assertIsInstance(h.group().transition_work, TransitionJob)
        self.assertEqual(delays, [30, 60, 120, 120, 120, 120, 120, 120])

    def test_unsupported_result_stays_visible_until_inputs_change(self):
        h = Harness(); h.request()
        token = h.group().transition_work.token
        h.event(TransitionFailed("battery", token, "unsupported", "unsupported profile"))
        for now in (1100, 2000, 5000):
            h.event(Tick(), now)
            self.assertEqual(h.group().status, "transition_unsupported")
            self.assertFalse(any(isinstance(e, NeedTransition) for e in h.effects))
        h.observe()
        self.assertGreater(h.group().transition_work.token, token)

    def test_failed_group_does_not_block_an_independent_group(self):
        h = Harness(("battery", "second")); h.request()
        h.event(TransitionFailed("battery", h.group().transition_work.token, "unsupported", "unsupported"))
        h.request("second"); h.propose("second"); h.finish("second")
        self.assertEqual(h.group("second").status, "adopted")
        self.assertEqual(h.group().status, "transition_unsupported")

    def test_restart_preserves_tokens_and_retry_pacing(self):
        for pending in (True, False):
            with self.subTest(pending=pending):
                h = Harness(); h.request()
                job = h.group().transition_work
                if not pending:
                    h.event(TransitionFailed("battery", job.token, "retryable", "temporary"))
                h.state, _ = restore_checkpoint(encode_checkpoint(h.state), h.now)
                retry = h.group().transition_work.retry_at_ms
                self.assertEqual(retry, h.now + h.state.limits.retry_base_ms)
                h.authority("battery", "controlling", 1)
                h.event(FrameObserved(replace(Harness().state.frame, revision=2)))
                h.observe(target=controls())
                self.assertIsInstance(h.group().transition_work, TransitionFailure)
                h.event(Tick(), retry)
                self.assertGreater(h.group().transition_work.token, job.token)
                h.propose(); h.finish()
                self.assertEqual(h.group().status, "adopted")

    def test_checkpoint_rejects_legacy_schema_and_impossible_job_identity(self):
        h = Harness(); h.request()
        for mutate in (
            lambda v: v.update(schema_version=3),
            lambda v: v["state"]["groups"][0].update(next_transition=1),
            lambda v: v["state"]["groups"][0]["transition_work"].update(token=True),
            lambda v: v["state"]["groups"][0]["transition_work"]["key"].update(generation=999),
        ):
            value = json.loads(encode_checkpoint(h.state)); mutate(value)
            with self.assertRaises(ValueError):
                decode_checkpoint(json.dumps(value).encode())


class ReliefTests(unittest.TestCase):
    def relief(self, groups=("battery",), *, external=8000):
        h = Harness(groups)
        before, after = controls("charge", 4000), controls("charge")
        rule = ReliefRule("stop-charge", before, after, Envelope(4000, 0),
                          Envelope(4000, 0), Envelope(0, 0), (Guard("ready", 1, 1),),
                          "synthetic-commissioned-stop")
        group = replace(h.group(), spec=replace(h.group().spec, relief_rules=(rule,)))
        h.state = replace(h.state, groups=(group, *h.state.groups[1:]))
        h.observe(target=before)
        h.event(FrameObserved(replace(h.state.frame, revision=2, external=Envelope(external, 0))))
        h.request(target=after)
        step = Step("charge", 0, before, rule.native_guards, rule.during, 20, 100,
                    True, rule.evidence, rule.id)
        return h, step

    def test_commissioned_relief_dispatches_under_overload_without_releasing_reservation(self):
        h, step = self.relief(("battery", "second"))
        h.propose(steps=(step,))
        self.assertEqual(h.group().status, "awaiting_durability")
        self.assertEqual(reservation(h.group(), h.now), Envelope(4000, 0))
        sent = h.durable()
        self.assertEqual((sent.key, sent.value), ("charge", 0))
        h.observe("second", target=controls("charge"))
        h.request("second", target=controls("charge", 3000)); h.propose("second")
        self.assertEqual(h.group("second").status, "physical_scope_blocked")
        self.assertEqual(reservation(h.group(), h.now), Envelope(4000, 0))
        h.settle(target=step.after)
        self.assertEqual(reservation(h.group(), h.now), Envelope(0, 0))
        # The remaining 3 kW request still exceeds the 2 kW actual headroom.
        self.assertEqual(h.group("second").status, "physical_scope_blocked")

    def test_numeric_reduction_without_rule_remains_blocked(self):
        h, step = self.relief()
        h.propose(steps=(replace(step, relief_id=None),))
        self.assertEqual(h.group().status, "physical_scope_blocked")
        self.assertEqual(h.group().attempts, ())

    def test_relief_is_revalidated_at_durable_dispatch(self):
        for change in ("stale_frame", "guard", "controls", "envelope"):
            with self.subTest(change=change):
                h, step = self.relief(); h.propose(steps=(step,))
                prepared = h.group().attempts[0]
                if change == "stale_frame":
                    h.event(FrameObserved(replace(h.state.frame, revision=3, valid_until_ms=h.now + 1)))
                    h.now += 1
                elif change == "guard":
                    h.observe(ready=0)
                elif change == "controls":
                    h.observe(target=controls("export", 4000))
                else:
                    h.observe(envelope=Envelope(3500, 0))
                h.event(JournalDurable(prepared.prepared_revision))
                self.assertFalse(any(isinstance(e, Send) for e in h.effects))

    def test_unknown_mismatched_and_worsening_relief_proofs_are_rejected(self):
        for mutate in (
            lambda s: replace(s, relief_id="unknown"),
            lambda s: replace(s, evidence="invented"),
            lambda s: replace(s, native_guards=()),
            lambda s: replace(s, possible=Envelope(0, 0)),
        ):
            h, step = self.relief()
            with self.assertRaises(ValueError):
                h.propose(steps=(mutate(step),))
        h, _ = self.relief(); rule = h.group().spec.relief_rules[0]
        for changes in ({"during": Envelope(4001, 0)}, {"settled": Envelope(0, 1)},
                        {"settled": rule.observed}, {"after": controls("export")}):
            with self.assertRaises(ValueError):
                replace(rule, **changes)

    def test_only_improvement_in_the_violated_direction_qualifies(self):
        h, step = self.relief(external=0)
        h.event(FrameObserved(replace(h.state.frame, revision=3, external=Envelope(0, 11000))))
        h.propose(steps=(step,))
        self.assertEqual(h.group().status, "physical_scope_blocked")

    def test_ambiguous_relief_cannot_repeat_using_unconfirmed_reduction(self):
        h, step = self.relief(); h.propose(steps=(step,)); sent = h.durable()
        h.event(TransportResult("battery", sent.attempt_id, "ambiguous", "timeout"))
        h.event(Tick(), h.group().retry_not_before_ms)
        self.assertEqual(len(h.group().attempts), 1)
        self.assertEqual(h.group().status, "physical_scope_blocked")


class SettlementTests(unittest.TestCase):
    def test_execution_actuals_survive_expiry_and_restart_without_replan(self):
        from home_runtime import CounterReceived
        from plan_execution import MeterReceipt, measured
        h=PolicyHarness();h.offer();start=h.now
        for revision in range(261):
            h.event(CounterReceived(MeterReceipt(str(revision),'charge','charge','battery_dc','0',
                start+revision*10000,1000000+revision*10,0)),start+revision*10000)
        restored,_=restore_checkpoint(encode_checkpoint(h.state),h.now)
        self.assertEqual(restored.execution.account,h.state.execution.account)
        total=measured(restored.execution.account.meters,'charge',start,h.now)
        self.assertEqual((total.low,total.high),(2600,2600))
        self.assertFalse(any(isinstance(e,Send) for e in h.effects))

    def test_async_stream_settlement_matches_unpruned_oracle_with_partial_cuts_and_reset(self):
        specs = (spec("fast", maximum=3600), spec("slow"), spec("missing"))
        ledger = create_ledger("home", "v1", specs, max_intervals=2)
        ledger = record_sample(ledger, sample(0, 0, 0, stream="fast"), 0)[0]
        ledger = record_sample(ledger, sample(0, 0, 0, stream="slow"), 0)[0]
        origin = mark_actuals(ledger, 5)  # Strictly inside a later counter interval.
        prefix = start_settlement(ledger, origin)
        oracle = replace(ledger, max_intervals=256)
        events = [sample(i, i * 10, i * 2 if i < 4 else (i - 4) * 3,
                         stream="fast", epoch=int(i >= 4), reason="reset" if i == 4 else None)
                  for i in range(1, 21)]
        events += [sample(1, 95, 200, stream="slow"), sample(2, 195, 300, stream="slow"),
                   sample(0, 170, 1000, stream="missing"), sample(1, 190, 1005, stream="missing")]
        for value in sorted(events, key=lambda s: s.at_ms):
            ledger, prefix, _ = record_actuals(ledger, value, value.at_ms, origin=origin, settled=prefix)
            oracle = record_sample(oracle, value, value.at_ms)[0]
            self.assertEqual(reconciled_actuals(ledger, origin, prefix, value.at_ms + 3),
                             actuals_since(oracle, origin, value.at_ms + 3))
            self.assertEqual([s.lifetime for s in ledger.streams], [s.lifetime for s in oracle.streams])
        ledger, prefix = settle_and_prune(ledger, origin, prefix, 197)
        self.assertEqual([p.through_ms for p in prefix], [190, 195, 190])
        self.assertEqual(reconciled_actuals(ledger, origin, prefix, 205), actuals_since(oracle, origin, 205))

    def test_capacity_maintenance_without_policy_and_invalid_sample_atomicity(self):
        ledger = create_ledger("home", "v1", (spec(),), max_intervals=1)
        for value in (sample(0, 0, 0), sample(1, 10, 10)):
            ledger, _, _ = record_actuals(ledger, value, value.at_ms)
        origin = mark_actuals(ledger, 10)
        prefix = start_settlement(ledger, origin)
        before = ledger
        with self.assertRaises(ValueError):
            record_actuals(ledger, sample(2, 20, 9), 20, origin=origin, settled=prefix)
        self.assertEqual(ledger, before)
        self.assertEqual(record_actuals(ledger, sample(1, 10, 10), 20, origin=origin, settled=prefix),
                         (ledger, prefix, "duplicate_meter_sample"))
        ledger, _, _ = record_actuals(ledger, sample(2, 20, 30), 20)
        self.assertEqual(ledger.streams[0].lifetime, EnergyBounds(30, 30))
        self.assertEqual(len(ledger.streams[0].samples), 2)

    def test_corrupt_execution_account_checkpoint_is_rejected(self):
        h=PolicyHarness();h.offer()
        for mutate in (
            lambda a:a.update(receipt=-1),
            lambda a:a['admissions'][0].update(receipt=a['receipt']+1),
            lambda a:a['admissions'][0]['contract'].update(generation=99),
        ):
            value=json.loads(encode_checkpoint(h.state))
            mutate(value['state']['execution']['account'])
            with self.assertRaises(ValueError):decode_checkpoint(json.dumps(value))


if __name__ == "__main__":
    unittest.main()
