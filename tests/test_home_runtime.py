"""Exercise the real pure reducer with synthetic finite adapter evidence and crash cuts."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import subprocess
import unittest

sys.path.append(str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))
from home_runtime import (
    Envelope, Guard, Request, Step, GroupSpec, Limits, Observation, Frame, create_home,
    Observed, FrameObserved, AuthorityChanged, Requested, Proposed, JournalDurable,
    JournalFailed, TransportResult, Tick, Persist, Send, NeedTransition, Observe,
    reduce_home, reservation, WriterIdentity, WriterGrant, GrantConfirmed, authorize_send,
)
from home_runtime_checkpoint import encode_checkpoint, decode_checkpoint, restore_checkpoint, decode_event, event_json


def controls(mode="hold", charge=0, discharge=0):
    return (("mode", mode), ("charge", charge), ("discharge", discharge))


def synthetic_steps(before, target, *, repeat=True, delay=100):
    """Declared fake adapter: isolate both directions before switching modes."""
    steps = []
    current = before
    def append(key, value):
        nonlocal current
        if dict(current)[key] == value:
            return
        after = tuple((name, value if name == key else old) for name, old in current)
        steps.append(Step(key, value, current, (Guard("ready", 1, 1),),
                          Envelope(max(dict(current)["charge"], dict(after)["charge"]),
                                   max(dict(current)["discharge"], dict(after)["discharge"])),
                          20, delay, repeat, "synthetic-absolute-v1"))
        current = after
    for key in ("charge", "discharge"):
        append(key, 0)
    append("mode", dict(target)["mode"])
    for key in ("charge", "discharge"):
        append(key, dict(target)[key])
    return tuple(steps)


class Harness:
    def __init__(self, groups=("battery",), limit=10000):
        self.now = 1000
        self.state = create_home(tuple(GroupSpec(key, "fake-v1", ("mode", "charge", "discharge"), Envelope(4000, 4000), writer=WriterIdentity("fake-runtime", "fake-config", "fake-surface")) for key in groups), Limits(10, 30, 120))
        self.effects = ()
        self.event(FrameObserved(Frame(1, self.now, 100000, Envelope(0, 0), Envelope(limit, limit))))
        for key in groups:
            self.observe(key, controls())
            self.authority(key, "controlling", 1)

    def group(self, key="battery"):
        return next(g for g in self.state.groups if g.spec.id == key)

    def event(self, event, now=None):
        if now is not None:
            self.now = now
        old = encode_checkpoint(self.state)
        self.state, self.effects = reduce_home(self.state, event, self.now)
        # Decoder/encoder exercises each checkpoint and proves prior state immutability.
        self.assert_old = decode_checkpoint(old)
        for effect in self.effects:
            if isinstance(effect, Persist):
                assert decode_checkpoint(encode_checkpoint(effect.state)) == effect.state
        return self.effects

    def observe(self, key="battery", target=None, *, envelope=None, ready=1):
        group = self.group(key)
        target = target if target is not None else group.observation.controls
        envelope = envelope or Envelope(dict(target)["charge"], dict(target)["discharge"])
        return self.event(Observed(key, Observation(group.observation_revision + 1, self.now, 100000, target, (("ready", ready),), envelope)))

    def authority(self, key, mode, revision):
        release = Request("baseline", 1, 100000, controls("baseline", 1000, 1000), ())
        effects = self.event(AuthorityChanged(key, mode, revision, release))
        group = self.group(key)
        self.event(GrantConfirmed(key, group.grant or WriterGrant("fake-runtime", max(1, group.grant_epoch + 1), "fake-config", "fake-surface", 100000)))
        return effects

    def request(self, key="battery", target=None, revision=1, expiry=90000):
        return self.event(Requested(key, self.group(key).mode_revision, Request("desired", revision, expiry, target or controls("charge", 1000, 0), ())))

    def propose(self, key="battery", steps=None):
        group = self.group(key)
        request = group.desired if group.mode == "controlling" and group.desired and self.now < group.desired.valid_until_ms else group.release
        steps = steps or synthetic_steps(group.observation.controls, request.target)
        return self.event(Proposed(key, group.generation, request.id, request.revision, group.observation.revision, "fake-v1", steps, getattr(group.transition_work, "token", group.next_transition)))

    def durable(self, key="battery"):
        prepared = next(a for a in self.group(key).attempts if a.stage == "prepared")
        self.event(JournalDurable(prepared.prepared_revision))
        sent = next((e for e in self.effects if isinstance(e, Send) and e.group_id == key), None)
        if sent:
            assert authorize_send(self.state, sent, self.now)
        return sent

    def settle(self, key="battery", target=None):
        group = self.group(key)
        self.now = max(a.latest_effect_ms for a in group.attempts)
        self.observe(key, target or group.attempts[-1].step.after)

    def finish(self, key="battery"):
        for _ in range(20):
            group = self.group(key)
            if group.status in ("adopted", "released"):
                return
            if any(a.stage == "prepared" for a in group.attempts):
                self.durable(key)
            elif group.attempts:
                self.settle(key)
            elif group.plan is None:
                self.propose(key)
            else:
                self.event(Tick(), max(self.now + 1, group.retry_not_before_ms))
        raise AssertionError("trace did not converge")


class HomeRuntimeTests(unittest.TestCase):
    def test_durability_precedes_every_send_and_accepted_is_not_confirmation(self):
        h = Harness()
        h.request(); h.propose()
        self.assertTrue(any(isinstance(e, Persist) for e in h.effects))
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))
        sent = h.durable()
        self.assertIsNotNone(sent)
        h.event(TransportResult("battery", sent.attempt_id, "accepted", "transport receipt"))
        self.assertEqual(h.group().attempts[0].stage, "accepted")
        self.assertNotEqual(h.group().status, "adopted")
        h.finish()
        self.assertEqual(h.group().status, "adopted")
        self.assertEqual(h.group().observation.controls, controls("charge", 1000, 0))

    def test_drift_between_steps_requests_a_fresh_whole_group_transition(self):
        h = Harness(); h.request(); h.propose(); h.durable(); h.settle()
        prepared = next(a for a in h.group().attempts if a.stage == "prepared")
        h.now += 1
        h.observe(target=controls("external"))
        self.assertIsNone(h.group().plan)
        self.assertTrue(any(isinstance(e, NeedTransition) for e in h.effects))
        h.event(JournalDurable(prepared.prepared_revision))
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))
        h.propose(); h.finish()
        self.assertEqual(h.group().status, "adopted")
        self.assertEqual(h.group().mode, "controlling")

    def test_same_target_external_drift_is_automatically_reasserted(self):
        h = Harness(); h.request(); h.propose(); h.finish()
        h.now += 1; h.observe(target=controls("external"))
        self.assertTrue(any(isinstance(e, NeedTransition) for e in h.effects))
        h.propose(); h.finish()
        self.assertEqual(h.group().status, "adopted")

    def test_stale_durability_ack_cannot_revive_a_superseded_request(self):
        h = Harness(); h.request(); h.propose()
        revision = h.group().attempts[0].prepared_revision
        h.request(target=controls(), revision=2)
        h.event(JournalDurable(revision))
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))
        self.assertEqual(h.group().status, "adopted")

    def test_safe_identical_ambiguous_repeat_is_paced_and_retains_each_attempt(self):
        h = Harness(); h.request(); h.propose(); sent = h.durable()
        original = h.group().attempts[0]
        h.event(TransportResult("battery", sent.attempt_id, "ambiguous", "timeout"))
        h.event(Tick(), h.group().retry_not_before_ms - 1)
        self.assertEqual(len(h.group().attempts), 1)
        h.event(Tick(), h.group().retry_not_before_ms)
        self.assertEqual(len(h.group().attempts), 2)
        second = h.durable()
        self.assertNotEqual(second.attempt_id, sent.attempt_id)
        h.event(TransportResult("battery", second.attempt_id, "not_sent", "not dispatched"))
        self.assertEqual([a.id for a in h.group().attempts], [original.id])
        self.assertGreater(h.group().retry_not_before_ms, h.now)

    def test_nonrepeatable_and_obsolete_effects_block_incompatible_writes(self):
        h = Harness(); h.request()
        steps = synthetic_steps(h.group().observation.controls, h.group().desired.target, repeat=False)
        h.propose(steps=steps); sent = h.durable()
        h.event(TransportResult("battery", sent.attempt_id, "ambiguous", "timeout"))
        h.request(target=controls("export", 0, 1000), revision=2)
        h.event(Tick(), h.group().retry_not_before_ms)
        h.propose()
        self.assertEqual(len(h.group().attempts), 1)
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))
        old_bound = h.group().attempts[0].latest_effect_ms
        h.event(Tick(), old_bound + 1)
        self.assertEqual(len(h.group().attempts), 1, "deadline alone is not evidence")
        h.observe(target=controls("charge"))
        self.assertTrue(any(isinstance(e, NeedTransition) for e in h.effects))
        h.propose(); h.finish()
        self.assertEqual(h.group().observation.controls, controls("export", 0, 1000))

    def test_matching_readback_before_latest_effect_does_not_release_uncertainty(self):
        h = Harness(); h.request(target=controls("charge")); h.propose(); h.durable()
        h.now += 1; h.observe(target=controls("charge"))
        self.assertEqual(h.group().status, "reconciling")
        self.assertEqual(len(h.group().attempts), 1)
        h.settle(target=controls("charge"))
        self.assertEqual(h.group().status, "adopted")
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))

    def test_mode_exit_fences_optimisation_and_completes_explicit_handover(self):
        h = Harness(); h.request(); h.propose(); h.durable()
        h.authority("battery", "monitoring", 2)
        self.assertIsNone(h.group().desired)
        self.assertEqual(len(h.group().attempts), 1)
        h.finish()
        self.assertEqual(h.group().status, "released")
        self.assertEqual(h.group().observation.controls, controls("baseline", 1000, 1000))
        self.assertFalse(h.group().owned)
        h.now += 1; h.observe(target=controls("external"))
        self.assertFalse(any(isinstance(e, (Send, NeedTransition)) for e in h.effects))

    def test_planning_and_verification_never_create_physical_writes(self):
        for mode in ("monitoring", "planning", "control_verification"):
            h = Harness(); h.authority("battery", mode, 2); h.request()
            self.assertEqual(h.group().attempts, ())
            self.assertFalse(any(isinstance(e, (Send, NeedTransition)) for e in h.effects))

    def test_expiry_does_not_renew_authority_and_old_requests_are_fenced_on_reentry(self):
        h = Harness(); h.request(expiry=1500); h.propose(); h.finish()
        h.event(Tick(), 1500); h.finish()
        self.assertEqual(h.group().status, "released")
        h.authority("battery", "monitoring", 2); h.authority("battery", "controlling", 3)
        h.event(Requested("battery", 1, Request("old", 99, 90000, controls("export", 0, 1000), ())))
        self.assertIsNone(h.group().desired)
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))

    def test_journal_failure_blocks_that_send_but_an_independent_group_progresses(self):
        h = Harness(("battery", "second"))
        h.request("battery"); h.propose("battery")
        first = h.group().attempts[0]
        h.event(JournalFailed(first.prepared_revision))
        self.assertEqual(h.group().status, "journal_fault")
        h.request("second"); h.propose("second")
        self.assertIsNotNone(h.durable("second"))
        self.assertEqual(h.group().attempts[0].stage, "prepared")
        self.assertIsNotNone(h.durable("battery"), "matching durable retry recovers without manual resume")

    def test_pending_group_effects_are_reserved_and_not_double_counted(self):
        h = Harness(("battery", "second"), limit=1500)
        for key in ("battery", "second"):
            h.request(key, target=controls("hold", 1000, 0))
        h.propose("battery"); h.durable("battery")
        h.propose("second")
        self.assertEqual(h.group("second").status, "physical_scope_blocked")
        self.assertEqual(reservation(h.group(), h.now), Envelope(1000, 0))
        h.event(TransportResult("battery", h.group().attempts[0].id, "ambiguous", "timeout"))
        h.event(Tick(), h.group().retry_not_before_ms)
        h.durable()
        self.assertEqual(reservation(h.group(), h.now), Envelope(1000, 0), "identical within-group possibilities are a union")
        self.assertEqual(h.group("second").attempts, ())

    def test_prepared_crash_is_ambiguous_and_needs_fresh_authority_and_postbound_observation(self):
        h = Harness(); h.request(target=controls("charge")); h.propose()
        checkpoint = encode_checkpoint(h.state)
        old = h.group().attempts[0]
        retry = h.group().retry_not_before_ms
        h.state, effects = restore_checkpoint(checkpoint, h.now + 1)
        h.now += 1
        self.assertFalse(any(isinstance(e, Send) for e in effects))
        self.assertEqual(h.group().attempts[0].stage, "ambiguous")
        self.assertEqual(h.group().retry_not_before_ms, retry)
        h.event(JournalDurable(old.prepared_revision))
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))
        h.event(FrameObserved(Frame(2, h.now, 100000, Envelope(0, 0), Envelope(10000, 10000))))
        h.authority("battery", "controlling", 1)
        h.now = old.latest_effect_ms
        h.observe(target=controls("charge"))
        self.assertEqual(h.group().status, "adopted")
        self.assertFalse(any(isinstance(e, (Send, NeedTransition)) for e in h.effects))

    def test_restart_preserves_unchanged_request_without_baseline_cycle(self):
        h = Harness(); h.request(); h.propose(); h.finish()
        original = h.group().desired
        h.now += 10
        h.state, _ = restore_checkpoint(encode_checkpoint(h.state), h.now)
        h.authority("battery", "controlling", 1)
        h.event(FrameObserved(Frame(2, h.now, 100000, Envelope(0, 0), Envelope(10000, 10000))))
        h.observe(target=original.target)
        self.assertEqual(h.group().desired, original)
        self.assertEqual(h.group().status, "adopted")
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))

    def test_clock_rollback_and_freshness_prevent_sends(self):
        h = Harness(); h.request(); h.propose()
        h.event(Tick(), 1005)
        state, effects = reduce_home(h.state, JournalDurable(h.group().attempts[0].prepared_revision), 1004)
        self.assertIsNone(state.groups[0].observation)
        self.assertEqual(state.groups[0].attempts, ())
        self.assertFalse(any(isinstance(e, Send) for e in effects))
        h.event(JournalDurable(h.group().attempts[0].prepared_revision), 1010)
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))

    def test_codec_is_closed_and_rejects_corrupt_or_unsupported_checkpoints(self):
        h = Harness(); h.request(); h.propose()
        encoded = encode_checkpoint(h.state)
        self.assertEqual(decode_checkpoint(encoded), h.state)
        for modify in (
            lambda v: v.update(schema_version=1),
            lambda v: v['state'].update(unknown=1),
            lambda v: v['state'].update(revision=True),
            lambda v: v['state']['groups'][0].update(mode='manual_hold'),
            lambda v: v['state']['groups'][0]['attempts'][0].update(prepared_revision=99999),
        ):
            value = json.loads(encoded); modify(value)
            with self.assertRaises(ValueError):
                decode_checkpoint(json.dumps(value).encode())
        with self.assertRaises(ValueError):
            decode_checkpoint(b'{"schema_version":1,"schema_version":1,"state":null}')
        with self.assertRaises(ValueError):
            decode_checkpoint(b'x' * 1000001)
        event = Observed("battery", h.group().observation)
        self.assertEqual(decode_event(event_json(event)), event)

    def test_request_guards_gate_preparation_and_durable_dispatch(self):
        h = Harness()
        request = Request("guarded", 1, 90000, controls("charge"), (Guard("ready", 1, 1),))
        h.observe(ready=0)
        h.event(Requested("battery", 1, request))
        steps = tuple(replace(s, native_guards=()) for s in synthetic_steps(controls(), request.target))
        h.propose(steps=steps)
        self.assertEqual(h.group().status, "native_guard_blocked")
        self.assertEqual(h.group().attempts, ())
        h.observe(ready=1); h.propose(steps=steps)
        revision = h.group().attempts[0].prepared_revision
        h.observe(ready=0)
        h.event(JournalDurable(revision))
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))
        self.assertEqual(h.group().attempts, ())

    def test_clock_rollback_retains_mode_exit_and_transport_evidence(self):
        h = Harness(); h.request(); h.propose(); sent = h.durable()
        h.event(Tick(), 1005)
        h.event(AuthorityChanged("battery", "monitoring", 2, None), 1004)
        self.assertEqual(h.group().mode, "monitoring")
        self.assertIsNone(h.group().desired)
        self.assertIsNone(h.group().observation)
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))
        h.event(TransportResult("battery", sent.attempt_id, "not_sent", "cancelled before dispatch"), 1003)
        self.assertEqual(h.group().attempts, ())
        h.event(Tick(), 1010)
        self.assertEqual(h.group().mode, "monitoring")
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))

    def test_stale_request_still_processes_other_group_expiry(self):
        h = Harness(("battery", "second"))
        h.request("second", target=controls(), expiry=1005)
        self.assertEqual(h.group("second").status, "adopted")
        h.event(Requested("battery", 0, Request("stale", 1, 90000, controls(), ())), 1006)
        self.assertTrue(any(isinstance(e, NeedTransition) and e.group_id == "second" and e.purpose == "release" for e in h.effects))

    def test_restart_at_each_transport_cut_never_replays_a_send(self):
        for stage in ("prepared", "sent", "accepted", "ambiguous"):
            with self.subTest(stage=stage):
                h = Harness(); h.request(); h.propose()
                if stage != "prepared":
                    sent = h.durable()
                    if stage != "sent":
                        h.event(TransportResult("battery", sent.attempt_id, stage, "synthetic transport evidence"))
                before = h.group()
                restored, effects = restore_checkpoint(encode_checkpoint(h.state), h.now + 1)
                group = restored.groups[0]
                self.assertFalse(any(isinstance(e, Send) for e in effects))
                self.assertEqual(group.attempts[0].stage, "ambiguous")
                self.assertEqual(group.attempts[0].latest_effect_ms, before.attempts[0].latest_effect_ms)
                self.assertEqual(group.retry_not_before_ms, before.retry_not_before_ms)
                self.assertEqual(group.next_attempt, before.next_attempt)
                self.assertIsNone(group.plan)
                self.assertFalse(group.grant_confirmed)

    def test_invalid_checkpoint_identity_and_plan_are_rejected(self):
        h = Harness(); h.request(); h.propose()
        encoded = encode_checkpoint(h.state)
        for modify in (
            lambda g: g.update(next_attempt=1),
            lambda g: g["attempts"][0].update(id="other:1"),
            lambda g: g["plan"].update(index=len(g["plan"]["steps"])),
            lambda g: g["plan"]["steps"][0].update(key="unknown"),
            lambda g: g["attempts"][0].update(latest_effect_ms=1200),
        ):
            value = json.loads(encoded); modify(value["state"]["groups"][0])
            with self.assertRaises(ValueError):
                decode_checkpoint(json.dumps(value).encode())
        with self.assertRaises(ValueError):
            Request("invalid", True, 1000, controls(), ())
        with self.assertRaises(ValueError):
            Limits(1.5, 10, 20)

    def test_reproducible_cli_trace_retains_uncertainty_then_adopts_without_replay(self):
        root = Path(__file__).parents[1]
        result = subprocess.run([sys.executable, str(root / "scripts/replay-home-runtime.py"),
                                 str(root / "tests/fixtures/home-runtime-crash-recovery.json")],
                                check=True, capture_output=True, text=True)
        trace = json.loads(result.stdout)
        sends = [(row["record"], e) for row in trace["rows"] for e in row["effects"] if e["type"] == "Send"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][0], 5)
        self.assertEqual(trace["rows"][6]["groups"][0]["attempts"][0]["stage"], "ambiguous")
        self.assertEqual(trace["rows"][10]["groups"][0]["status"], "reconciling")
        self.assertEqual(trace["rows"][-1]["groups"][0]["status"], "adopted")
        final = decode_checkpoint(json.dumps(trace["final_checkpoint"]).encode())
        self.assertEqual(final.groups[0].attempts, ())

    def test_meter_actuals_never_confirm_commands_or_release_reservations(self):
        from energy_ledger import create_ledger, MeterSpec, CounterSample, EnergyBounds
        from home_runtime import MeterObserved
        h = Harness(); h.request(target=controls("hold", 1000, 0)); h.propose(); h.durable()
        ledger = create_ledger("actuals", "v1", (MeterSpec("charge", "battery", "AC", "charge", None),))
        h.state = replace(h.state, ledger=ledger)
        original = h.group().attempts
        h.event(MeterObserved(CounterSample("charge", "physical:charge", 0, 0, h.now, 0, "initial")))
        h.event(MeterObserved(CounterSample("charge", "physical:charge", 0, 1, h.now + 1, 100)), h.now + 1)
        self.assertEqual(h.group().attempts, original)
        self.assertEqual(reservation(h.group(), h.now), Envelope(1000, 0))
        self.assertEqual(h.state.ledger.streams[0].lifetime, EnergyBounds(100, 100))
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))


if __name__ == "__main__":
    unittest.main()
