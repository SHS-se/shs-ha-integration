"""Actual compiler fixtures through the reader, ledger and command journal boundary."""
from dataclasses import replace
import json
from datetime import datetime, timedelta
from pathlib import Path
import sys
import subprocess
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))
from battery_policy import BatteryPolicy, read_battery_policy, canonical_json, rank_policy
from energy_ledger import MeterSpec, CounterSample, EnergyBounds, create_ledger, mark_actuals
from home_runtime import (
    create_home, GroupSpec, Envelope, Limits, FrameObserved, Frame, Observed, Observation,
    AuthorityChanged, MeterObserved, PolicyContext, PolicyContextChanged, PolicyBinding,
    PolicyOffered, Proposed, Step, JournalDurable, Send, Tick, reduce_home, reservation,
    LedgerPruned,
)
from home_runtime_checkpoint import encode_checkpoint, decode_checkpoint, restore_checkpoint

FIXTURES = Path(__file__).parent / "fixtures"
KEYS = ("charge_w", "discharge_w", "solar_charge_w", "export_w", "pv_curtail_w")


def load(name="fixed-tail"):
    return read_battery_policy((FIXTURES / f"battery-policy-{name}.json").read_bytes())


def controls(**values):
    return tuple((key, values.get(key, 0)) for key in KEYS)


class Harness:
    def __init__(self, name="fixed-tail", mode="controlling", initial=None):
        self.compiled = load(name)
        summary = self.compiled.summary
        self.now = summary.anchor_ms
        self.state = create_home((GroupSpec("battery", "synthetic-imposed-power-v1", KEYS, Envelope(4000, 4000)),),
                                 Limits(10, 30, 120), ledger=create_ledger("house", "mapping-1", (MeterSpec("charge", "battery", "AC", "charge", None),)))
        self.effects = ()
        self.event(MeterObserved(CounterSample("charge", "physical-register", 0, 0, self.now, 1000000, "initial binding")))
        self.event(FrameObserved(Frame(0, self.now, self.now + 10000,
                                      Envelope(summary.external_import_w, summary.external_export_w),
                                      Envelope(summary.grid_import_limit_w, summary.grid_export_limit_w))))
        self.event(Observed("battery", Observation(0, self.now, self.now + 10000, initial or controls(),
                                                    summary.expected_measurements, Envelope(0, 0))))
        self.event(AuthorityChanged("battery", mode, 1, None))
        self.bindings = tuple(PolicyBinding(a.id, "synthetic-imposed-power-v1", "synthetic-fixture-response",
                                           a.current_path_json, tuple((key, value) for key, value in {
                                               **{k: v for k, v in json.loads(a.current_path_json)["actions"][0].items() if k != "kind"},
                                               "pv_curtail_w": 0}.items())) for a in summary.alternatives)
        self.context()

    @property
    def group(self):
        return self.state.groups[0]

    def event(self, value, now=None):
        if now is not None:
            self.now = now
        self.state, self.effects = reduce_home(self.state, value, self.now)
        self.state = decode_checkpoint(encode_checkpoint(self.state))
        return self.effects

    def context(self, revision=0, problem_json=None):
        context = PolicyContext(revision, problem_json or self.compiled.summary.problem_json, self.group.mode_revision,
                                self.group.observation_revision, self.state.frame.revision, mark_actuals(self.state.ledger, self.now))
        self.event(PolicyContextChanged(context))

    def offer(self, revision=1, **changes):
        event = PolicyOffered(revision, self.compiled, self.state.policy_context.watermark, self.bindings)
        return self.event(replace(event, **changes))

    def prepare(self):
        target = self.group.desired.target
        differences = [(k, v) for k, v in target if dict(self.group.observation.controls)[k] != v]
        assert len(differences) == 1
        key, value = differences[0]
        step = Step(key, value, self.group.observation.controls, (), Envelope(dict(target)["charge_w"], dict(target)["discharge_w"]),
                    10, 20, True, "synthetic-fixture-response")
        self.event(Proposed("battery", self.group.generation, self.group.desired.id, self.group.desired.revision,
                            self.group.observation_revision, self.group.spec.adapter_revision, (step,), self.group.transition_work.token))
        return self.group.attempts[0].prepared_revision


class BatteryPolicyTests(unittest.TestCase):
    def test_real_compiler_fixed_tail_reversal_reconciles_and_selects_hold(self):
        policy = load()
        summary = policy.summary
        alternatives = {a.id: a for a in summary.alternatives}
        self.assertEqual(alternatives["hold"].current[-1], .5)
        self.assertEqual(alternatives["discharge"].current[-1], 0)
        self.assertEqual(alternatives["discharge"].future_delta[-1], 2.5)
        self.assertEqual(rank_policy(policy)[0], "hold")
        h = Harness(initial=controls(discharge_w=1000)); h.offer()
        self.assertEqual(h.state.policy.selected_id, "hold")
        self.assertEqual(dict(h.group.desired.target)["discharge_w"], 0)
        revision = h.prepare()
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))
        h.event(JournalDurable(revision))
        self.assertTrue(any(isinstance(e, Send) and e.value == 0 for e in h.effects))

    def test_real_negative_price_case_selects_charge_and_uses_one_ms_lease(self):
        h = Harness("negative-price"); h.offer()
        self.assertEqual(h.state.policy.selected_id, "charge")
        selected = next(a for a in h.compiled.summary.alternatives if a.id == "charge")
        self.assertAlmostEqual(selected.full[-1], .0413125)
        self.assertEqual(h.group.desired.valid_until_ms, h.now + 1)
        revision = h.prepare()
        h.event(JournalDurable(revision), h.now + 1)
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))
        self.assertEqual(h.state.policy.status, "outside_coverage")
        self.assertEqual(h.group.attempts, ())

    def test_changed_full_problem_is_outside_coverage(self):
        for mutate in (
            lambda p: p["economics"]["import_sek_per_kwh"].__setitem__(1, 100),
            lambda p: p["plant"]["pv_w"].__setitem__(1, 10),
            lambda p: p["plant"]["equipment"][0]["export_allowed"].__setitem__(1, True),
            lambda p: p["plant"]["equipment"][0]["state_kwh"].update(initial=.2),
            lambda p: p["identity"].update(intent_revision="changed"),
        ):
            h = Harness(); problem = json.loads(h.compiled.summary.problem_json); mutate(problem)
            h.context(1, canonical_json(problem)); h.offer()
            self.assertIsNone(h.state.policy)
            self.assertIsNone(h.group.desired)

    def test_closed_reader_rejects_cost_reference_ranking_and_scope_corruption(self):
        source = json.loads(load().source_json)
        for mutate in (
            lambda v: v.update(unknown=True),
            lambda v: v.update(reference_id="missing"),
            lambda v: v.update(ranking=list(reversed(v["ranking"]))),
            lambda v: v["alternatives"][0]["current"].update(total_sek=99),
            lambda v: v["alternatives"][1]["future_delta"].update(wear_sek=1, total_sek=3.5),
            lambda v: v["coverage"].update(current_interval_count=True),
            lambda v: v["coverage"].update(interpolation="linear"),
            lambda v: v["identity"].update(intent_revision="foreign"),
            lambda v: v["coverage"]["problem"]["plant"]["equipment"][0].update(kind="ev"),
            lambda v: v["coverage"]["problem"]["economics"].update(services=[{}]),
            lambda v: v["alternatives"][0]["candidate"]["actions"]["battery"][0].update(unknown=1),
            lambda v: v["alternatives"][1].update(id=v["alternatives"][0]["id"]),
            lambda v: v["coverage"].update(optimality="unproven_after_pruning"),
        ):
            value = json.loads(json.dumps(source)); mutate(value)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                read_battery_policy(json.dumps(value).encode())
        with self.assertRaises(ValueError):
            read_battery_policy(b'{"status":"compiled","status":"compiled"}')

    def test_current_operation_wins_only_inside_explicit_deadband(self):
        policy = load()
        self.assertEqual(rank_policy(policy, "discharge", 2.0)[0], "discharge")
        self.assertEqual(rank_policy(policy, "discharge", 1.99)[0], "hold")
        with self.assertRaises(ValueError):
            rank_policy(policy, deadband_sek=float("nan"))

    def test_missing_forged_or_native_binding_cannot_create_request(self):
        for mutation in (
            lambda h: h.bindings[:1],
            lambda h: (replace(h.bindings[0], current_path_json="{}"), *h.bindings[1:]),
            lambda h: (replace(h.bindings[0], adapter_revision="sigenergy"), *h.bindings[1:]),
            lambda h: (replace(h.bindings[0], target=controls(charge_w=999)), *h.bindings[1:]),
        ):
            h = Harness(); h.offer(bindings=mutation(h))
            self.assertIsNone(h.state.policy)
            self.assertIsNone(h.group.desired)
        h = Harness()
        h.state = replace(h.state, groups=(replace(h.group, spec=replace(h.group.spec, adapter_revision="real-inverter")),))
        h.offer()
        self.assertIsNone(h.state.policy)

    def test_context_observation_frame_or_meter_change_fences_prepared_dispatch(self):
        for change in ("context", "observation", "frame", "meter", "mode"):
            h = Harness("negative-price"); h.offer(); revision = h.prepare()
            if change == "context":
                h.context(1)
            elif change == "observation":
                h.event(Observed("battery", replace(h.group.observation, revision=1)))
            elif change == "frame":
                h.event(FrameObserved(replace(h.state.frame, revision=1)))
            elif change == "mode":
                h.event(AuthorityChanged("battery", "monitoring", 2, None))
            else:
                h.event(MeterObserved(CounterSample("charge", "physical-register", 0, 1, h.now + 1, 1000001)), h.now + 1)
            h.event(JournalDurable(revision))
            self.assertFalse(any(isinstance(e, Send) for e in h.effects), change)
            self.assertIsNone(h.group.desired)
            self.assertFalse(any(a.stage == "prepared" for a in h.group.attempts))

    def test_policy_replacement_preserves_issued_uncertainty_and_actuals(self):
        h = Harness("negative-price"); h.offer(); revision = h.prepare()
        h.event(JournalDurable(revision))
        attempts, ledger = h.group.attempts, h.state.ledger
        h.offer(2)
        self.assertEqual(h.state.policy.revision, 1, "pending effects reject replacement")
        self.assertEqual(h.group.attempts, attempts)
        self.assertEqual(h.state.ledger, ledger)
        h.context(1)
        self.assertEqual(h.group.attempts, attempts)
        self.assertEqual(reservation(h.group, h.now), Envelope(1000, 0))

    def test_foreign_future_or_changed_meter_watermark_rejects_atomically(self):
        for field, value in (("ledger_id", "foreign"), ("mapping_revision", "foreign"), ("ledger_revision", 999), ("at_ms", 0)):
            h = Harness(); h.offer(watermark=replace(h.state.policy_context.watermark, **{field: value}))
            self.assertIsNone(h.state.policy)
        h = Harness(); h.offer()
        original = h.state.policy
        h.offer(1)
        self.assertEqual(h.state.policy, original)

    def test_passive_modes_produce_diagnostics_without_requests(self):
        for mode in ("monitoring", "planning", "control_verification"):
            h = Harness("negative-price", mode); h.offer()
            self.assertEqual(h.state.policy.selected_id, "charge")
            self.assertEqual(h.state.policy.status, "diagnostic_only")
            self.assertIsNone(h.group.desired)
            self.assertFalse(any(isinstance(e, Send) for e in h.effects))

    def test_restore_retains_actuals_and_possible_effects_but_cannot_replay_selection(self):
        h = Harness("negative-price"); h.offer(); revision = h.prepare()
        before = h.state
        restored, effects = restore_checkpoint(encode_checkpoint(before), h.now)
        self.assertEqual(restored.ledger, before.ledger)
        self.assertEqual(restored.groups[0].attempts[0].stage, "ambiguous")
        self.assertEqual(restored.policy.status, "awaiting_context")
        self.assertIsNone(restored.policy_context)
        self.assertIsNone(restored.groups[0].desired)
        restored, effects = reduce_home(restored, JournalDurable(revision), h.now)
        self.assertFalse(any(isinstance(e, Send) for e in effects))

    def test_raw_evidence_must_match_problem_even_when_context_claims_match(self):
        h = Harness()
        h.event(Observed("battery", replace(h.group.observation, revision=1, measurements=(("energy_kwh", .1),))))
        h.context(1); h.offer()
        self.assertIsNone(h.state.policy)

    def test_impossible_current_responses_or_consistently_forged_current_costs_fail(self):
        for change in (
            {"charge_w": 500000, "discharge_w": 500000},
            {"solar_charge_w": 100},
            {"export_w": 100},
            {"charge_w": 100},  # Grid charging is forbidden in this fixture.
            {"discharge_w": 2000},
        ):
            value = json.loads(load().source_json)
            value["alternatives"][0]["candidate"]["actions"]["battery"][0].update(change)
            with self.assertRaises(ValueError):
                read_battery_policy(json.dumps(value).encode())
        value = json.loads(load().source_json)
        # Preserve all C/F algebra while inventing 0.1 SEK of extra current wear
        # for both alternatives. Independent current-cost evaluation must catch it.
        for alternative in value["alternatives"]:
            for component in ("current", "full"):
                alternative[component]["wear_sek"] += .1
                alternative[component]["total_sek"] += .1
        with self.assertRaises(ValueError):
            read_battery_policy(json.dumps(value).encode())

    def test_battery_identity_with_spaces_is_an_exact_dictionary_key(self):
        value = json.loads(load().source_json)
        value["coverage"]["problem"]["plant"]["equipment"][0]["id"] = "home battery"
        for alternative in value["alternatives"]:
            actions = alternative["candidate"]["actions"]
            actions["home battery"] = actions.pop("battery")
        self.assertEqual(read_battery_policy(json.dumps(value).encode()).summary.group_id, "home battery")

    def test_replacement_reconciles_previous_watermark_without_replenishing_actuals(self):
        h = Harness(); h.offer()
        old_watermark = h.state.policy.watermark
        value = json.loads(h.compiled.source_json)
        def later(timestamp):
            return (datetime.fromisoformat(timestamp.replace("Z", "+00:00")) + timedelta(minutes=15)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        value["identity"]["actuals_watermark"] = later(value["identity"]["actuals_watermark"])
        value["coverage"]["problem"]["identity"]["actuals_watermark"] = value["identity"]["actuals_watermark"]
        value["coverage"]["segment_end"] = later(value["coverage"]["segment_end"])
        for interval in value["coverage"]["problem"]["intervals"]:
            interval.update(start=later(interval["start"]), end=later(interval["end"]))
        h.compiled = read_battery_policy(json.dumps(value).encode())
        h.now = h.compiled.summary.anchor_ms
        h.event(MeterObserved(CounterSample("charge", "physical-register", 0, 1, h.now, 1200000)))
        h.event(LedgerPruned(h.now))
        self.assertEqual(h.state.policy.watermark, old_watermark)
        self.assertEqual(h.state.policy.settled_actuals[0].actuals.energy, EnergyBounds(200000, 200000))
        h.event(FrameObserved(replace(h.state.frame, revision=1, at_ms=h.now, valid_until_ms=h.now + 10000)))
        h.event(Observed("battery", replace(h.group.observation, revision=1, at_ms=h.now, valid_until_ms=h.now + 10000)))
        h.context(1); h.offer(2)
        self.assertNotEqual(h.state.policy.watermark, old_watermark)
        self.assertEqual(h.state.policy.reconciled_actuals[0].energy, EnergyBounds(200000, 200000))
        h.offer(3)
        self.assertEqual(h.state.policy.reconciled_actuals[0].energy, EnergyBounds(0, 0))
        self.assertEqual(h.state.ledger.streams[0].lifetime, EnergyBounds(200000, 200000))
        h.event(LedgerPruned(h.now))
        self.assertEqual(h.state.ledger.streams[0].lifetime, EnergyBounds(200000, 200000))
        self.assertFalse(any(isinstance(e, Send) for e in h.effects))

    def test_corrupt_policy_checkpoint_cannot_bypass_dispatch_fence(self):
        h = Harness("negative-price"); h.offer(); h.prepare()
        checkpoint = json.loads(encode_checkpoint(h.state))
        for corrupt in (
            lambda v: v.update(schema_version=2),
            lambda v: v["state"]["policy"].update(status="unrecognised"),
            lambda v: v["state"]["policy"].update(status="outside_coverage"),
            lambda v: v["state"].update(policy_context=None),
            lambda v: v["state"]["policy"]["watermark"].update(ledger_revision=999),
            lambda v: v["state"]["groups"][0]["desired"].update(valid_until_ms=h.now + 1000),
        ):
            value = json.loads(json.dumps(checkpoint)); corrupt(value)
            with self.assertRaises(ValueError):
                decode_checkpoint(json.dumps(value).encode())

    def test_exact_anchor_trace_dispatches_once_and_retains_late_effect_after_expiry(self):
        root = Path(__file__).parents[1]
        result = subprocess.run([sys.executable, str(root / "scripts/replay-home-runtime.py"),
                                 str(FIXTURES / "home-runtime-policy-binding.json")],
                                check=True, capture_output=True, text=True)
        trace = json.loads(result.stdout)
        sends = [e for row in trace["rows"] for e in row["effects"] if e["type"] == "Send"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["value"], 1000)
        self.assertEqual(trace["rows"][-1]["policy"]["status"], "outside_coverage")
        self.assertEqual(trace["rows"][-1]["groups"][0]["reservation_w"]["import"], 1000)
        self.assertEqual(len(trace["rows"][-1]["groups"][0]["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()
