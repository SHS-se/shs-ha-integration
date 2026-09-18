"""Counter accounting, uncertain time cuts, epoch gaps and durable continuation."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import subprocess
import unittest

sys.path.append(str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))
from energy_ledger import (
    EnergyBounds, MeterSpec, CounterSample, create_ledger, record_sample,
    prune_ledger, mark_actuals, mark_retained_actuals, actuals_since,
)
from home_runtime import (
    create_home, GroupSpec, Envelope, Limits, MeterObserved, LedgerPruned,
    reduce_home, Persist, Send,
)
from home_runtime_checkpoint import encode_checkpoint, decode_checkpoint, restore_checkpoint

HOUR = 3600000


def spec(key="grid", direction="import", maximum=None, boundary="AC"):
    return MeterSpec(key, key, boundary, direction, maximum,
                     "synthetic-rated-bound" if maximum is not None else None)


def sample(revision, at, total, *, stream="grid", epoch=0, source=None, reason=None):
    return CounterSample(stream, source or stream + ":register", epoch, revision, at, total,
                         "initial binding" if revision == 0 and reason is None else reason)


class EnergyLedgerTests(unittest.TestCase):
    def ledger(self, maximum=None, capacity=128):
        return create_ledger("home-actuals", "mapping-v1", (spec(maximum=maximum),), max_intervals=capacity)

    def append(self, ledger, value):
        return record_sample(ledger, value, value.at_ms)[0]

    def test_delayed_policy_cut_uses_retained_counters_without_rebasing(self):
        ledger = self.append(self.ledger(maximum=1000), sample(0, 0, 0))
        ledger = self.append(ledger, sample(1, HOUR, 800000))
        ledger = self.append(ledger, sample(2, HOUR * 2, 1600000))
        with self.assertRaises(ValueError):
            mark_actuals(ledger, HOUR)
        watermark = mark_retained_actuals(ledger, HOUR, HOUR * 2)
        self.assertEqual(watermark.at_ms, HOUR)
        self.assertEqual(actuals_since(ledger, watermark, HOUR * 2)[0].energy, EnergyBounds(800000, 800000))
        partial = mark_retained_actuals(ledger, HOUR // 2, HOUR * 2)
        self.assertEqual(actuals_since(ledger, partial, HOUR * 2)[0].energy, EnergyBounds(1100000, 1300000))

    def test_historical_cut_requires_a_left_anchor_for_every_stream(self):
        ledger = self.append(self.ledger(), sample(0, HOUR, 0))
        for cut, now in [(0, HOUR), (HOUR, HOUR - 1), (HOUR * 2, HOUR)]:
            with self.assertRaises(ValueError):
                mark_retained_actuals(ledger, cut, now)
        with self.assertRaises(ValueError):
            mark_retained_actuals(self.ledger(), 0, HOUR)
        ledger = self.append(ledger, sample(1, HOUR * 2, 2000))
        ledger = prune_ledger(ledger, HOUR * 2)
        with self.assertRaises(ValueError):
            mark_retained_actuals(ledger, 0, HOUR * 2)

    def test_same_epoch_recovers_whole_downtime_and_duplicates_are_idempotent(self):
        ledger = self.append(self.ledger(), sample(0, 0, 10000000))
        mark = mark_actuals(ledger, 0)
        ledger = self.append(ledger, sample(7, HOUR, 11000000))
        self.assertEqual(ledger.streams[0].lifetime, EnergyBounds(1000000, 1000000))
        result = actuals_since(ledger, mark, HOUR)[0]
        self.assertEqual(result.energy, EnergyBounds(1000000, 1000000))
        self.assertEqual((result.counter_measured_ms, result.uncertain_ms, result.reasons), (HOUR, 0, ()))
        self.assertEqual(record_sample(ledger, sample(7, HOUR, 11000000), HOUR), (ledger, "duplicate_meter_sample"))
        self.assertEqual(record_sample(ledger, sample(0, 0, 10000000), HOUR), (ledger, "stale_meter_sample"))
        with self.assertRaises(ValueError):
            self.append(ledger, sample(7, HOUR, 11000001))

    def test_separate_gross_directions_and_boundaries_never_net_or_convert(self):
        specs = (spec("charge", "charge", boundary="battery-AC"),
                 spec("discharge", "discharge", boundary="battery-AC"),
                 spec("dc", "charge", boundary="battery-DC"))
        ledger = create_ledger("home-actuals", "mapping-v1", specs)
        for key in ("charge", "discharge", "dc"):
            ledger = self.append(ledger, sample(0, 0, 0, stream=key))
        mark = mark_actuals(ledger, 0)
        for key, total in (("charge", 1000000), ("discharge", 1000000), ("dc", 900000)):
            ledger = self.append(ledger, sample(1, HOUR, total, stream=key))
        result = actuals_since(ledger, mark, HOUR)
        self.assertEqual([s.energy.lower_mwh for s in result], [1000000, 1000000, 900000])
        self.assertEqual([s.spec.boundary_id for s in result], ["battery-AC", "battery-AC", "battery-DC"])

    def test_partial_interval_is_bounded_not_prorated(self):
        ledger = self.append(self.ledger(), sample(0, 0, 0))
        mark = mark_actuals(ledger, HOUR // 4)
        ledger = self.append(ledger, sample(1, HOUR, 1000000))
        result = actuals_since(ledger, mark, HOUR * 3 // 4)[0]
        self.assertEqual(result.energy, EnergyBounds(0, 1000000))
        self.assertEqual(result.uncertain_ms, HOUR // 2)
        self.assertIn("partial_counter_interval", result.reasons)
        ledger2 = self.append(self.ledger(maximum=1000), sample(0, 0, 0))
        mark2 = mark_actuals(ledger2, HOUR // 4)
        ledger2 = self.append(ledger2, sample(1, HOUR, 800000))
        self.assertEqual(actuals_since(ledger2, mark2, HOUR * 3 // 4)[0].energy, EnergyBounds(300000, 500000))

    def test_partial_bounds_contain_every_small_feasible_subinterval_trajectory(self):
        # Exhaustive small independent oracle: three 1-hour cells, each 0..3 Wh.
        import itertools
        for trajectory in itertools.product(range(4), repeat=3):
            ledger = self.append(self.ledger(maximum=3), sample(0, 0, 0))
            mark = mark_actuals(ledger, HOUR)
            ledger = self.append(ledger, sample(1, HOUR * 3, sum(trajectory) * 1000))
            bounds = actuals_since(ledger, mark, HOUR * 2)[0].energy
            actual = trajectory[1] * 1000
            self.assertLessEqual(bounds.lower_mwh, actual)
            self.assertGreaterEqual(bounds.upper_mwh, actual)

    def test_missing_then_recovered_reading_replaces_query_uncertainty(self):
        ledger = self.append(self.ledger(maximum=2000), sample(0, 0, 0))
        mark = mark_actuals(ledger, 0)
        pending = actuals_since(ledger, mark, HOUR)[0]
        self.assertEqual(pending.energy, EnergyBounds(0, 2000000))
        self.assertIn("awaiting_meter_sample", pending.reasons)
        ledger = self.append(ledger, sample(1, HOUR, 600000))
        self.assertEqual(actuals_since(ledger, mark, HOUR)[0].energy, EnergyBounds(600000, 600000))
        self.assertEqual(ledger.streams[0].lifetime, EnergyBounds(600000, 600000))

    def test_epoch_reset_preserves_unknown_gap_and_reanchors_without_credit(self):
        ledger = self.append(self.ledger(maximum=2000), sample(0, 0, 5000000))
        mark = mark_actuals(ledger, 0)
        ledger = self.append(ledger, sample(1, HOUR, 6000000))
        ledger = self.append(ledger, sample(2, HOUR * 2, 700000, epoch=1, reason="meter reset"))
        ledger = self.append(ledger, sample(3, HOUR * 3, 800000, epoch=1))
        result = actuals_since(ledger, mark, HOUR * 3)[0]
        self.assertEqual(result.energy, EnergyBounds(1100000, 3100000))
        self.assertEqual(result.uncertain_ms, HOUR)
        self.assertEqual(result.reasons, ("epoch_gap",))
        self.assertEqual(ledger.streams[0].lifetime, result.energy)

    def test_rebinding_requires_new_epoch_and_carries_unbounded_gap(self):
        ledger = self.append(self.ledger(), sample(0, 0, 1000))
        mark = mark_actuals(ledger, 0)
        with self.assertRaises(ValueError):
            self.append(ledger, sample(1, HOUR, 100000, source="new-register"))
        ledger = self.append(ledger, sample(1, HOUR, 100000, source="new-register", epoch=1, reason="meter replacement"))
        self.assertEqual(actuals_since(ledger, mark, HOUR)[0].energy, EnergyBounds(0, None))
        ledger = self.append(ledger, sample(2, HOUR * 2, 100500, source="new-register", epoch=1))
        self.assertEqual(ledger.streams[0].lifetime, EnergyBounds(500, None))

    def test_first_sample_and_unknown_direction_are_never_assumed_zero(self):
        ledger = self.ledger()
        mark = mark_actuals(ledger, 0)
        self.assertEqual(actuals_since(ledger, mark, HOUR)[0].energy, EnergyBounds(0, None))
        ledger = self.append(ledger, sample(0, HOUR, 1000000))
        result = actuals_since(ledger, mark, HOUR)[0]
        self.assertEqual(result.energy, EnergyBounds(0, None))
        self.assertEqual(result.reasons, ("before_first_anchor",))
        self.assertEqual(ledger.streams[0].lifetime, EnergyBounds(0, 0))

    def test_unchanged_cumulative_reading_is_measured_zero_without_power_integration(self):
        ledger = self.append(self.ledger(), sample(0, 0, 2000))
        mark = mark_actuals(ledger, 0)
        ledger = self.append(ledger, sample(1, HOUR, 2000))
        result = actuals_since(ledger, mark, HOUR)[0]
        self.assertEqual(result.energy, EnergyBounds(0, 0))
        self.assertEqual(result.counter_measured_ms, HOUR)

    def test_bad_source_evidence_is_rejected_atomically(self):
        ledger = self.append(self.ledger(maximum=1000), sample(0, 100, 1000))
        for bad in (
            sample(1, 200, 999),
            sample(1, 99, 1000),
            sample(1, 100, 1000),
            sample(1, 200, 1000000),
            sample(1, 200, 1000, epoch=1),
            sample(1, 200, 1000, reason="reset without epoch"),
        ):
            with self.subTest(sample=bad), self.assertRaises(ValueError):
                self.append(ledger, bad)
        with self.assertRaises(ValueError):
            record_sample(ledger, sample(1, 200, 1000), 199)
        with self.assertRaises(ValueError):
            sample(True, 200, 1000)
        self.assertEqual(ledger.streams[0].samples[-1].revision, 0)

    def test_duplicate_physical_counter_or_boundary_registration_is_rejected(self):
        with self.assertRaises(ValueError):
            create_ledger("home", "v1", (spec(), replace(spec(), stream_id="alias")))
        ledger = create_ledger("home", "v1", (spec(), spec("second")))
        ledger = self.append(ledger, sample(0, 0, 0))
        with self.assertRaises(ValueError):
            self.append(ledger, sample(0, 0, 0, stream="second", source="grid:register"))
        with self.assertRaises(ValueError):
            MeterSpec("x", "x", "AC", "import", 1000)

    def test_retention_preserves_lifetime_and_rejects_pruned_watermark(self):
        ledger = self.append(self.ledger(capacity=2), sample(0, 0, 0))
        old = mark_actuals(ledger, 0)
        ledger = self.append(ledger, sample(1, HOUR, 1000))
        retained = mark_actuals(ledger, HOUR)
        ledger = self.append(ledger, sample(2, HOUR * 2, 3000))
        next_sample = sample(3, HOUR * 3, 6000)
        with self.assertRaises(ValueError):
            self.append(ledger, next_sample)
        ledger = prune_ledger(ledger, HOUR)
        self.assertEqual(ledger.streams[0].lifetime, EnergyBounds(3000, 3000))
        with self.assertRaises(ValueError):
            actuals_since(ledger, old, HOUR * 2)
        ledger = self.append(ledger, next_sample)
        self.assertEqual(actuals_since(ledger, retained, HOUR * 3)[0].energy, EnergyBounds(5000, 5000))
        self.assertEqual(ledger.streams[0].lifetime, EnergyBounds(6000, 6000))
        self.assertEqual(record_sample(ledger, sample(0, 0, 0), HOUR * 3)[0], ledger)

    def test_queries_and_plan_watermarks_never_consume_or_reset_actuals(self):
        ledger = self.append(self.ledger(), sample(0, 0, 0))
        early = mark_actuals(ledger, 0)
        middle = mark_actuals(ledger, HOUR // 2)
        ledger = self.append(ledger, sample(1, HOUR, 1000))
        first = actuals_since(ledger, early, HOUR)
        actuals_since(ledger, middle, HOUR)
        self.assertEqual(actuals_since(ledger, early, HOUR), first)
        self.assertEqual(ledger.streams[0].lifetime, EnergyBounds(1000, 1000))
        for invalid in (replace(early, ledger_id="other"), replace(early, mapping_revision="v2"), replace(early, ledger_revision=100)):
            with self.assertRaises(ValueError):
                actuals_since(ledger, invalid, HOUR)

    def home(self):
        return create_home((GroupSpec("battery", "fake", ("mode",), Envelope(1000, 1000)),),
                           Limits(10, 30, 120), ledger=self.ledger())

    def test_crash_before_and_after_meter_checkpoint_has_same_actuals(self):
        home = self.home()
        home, _ = reduce_home(home, MeterObserved(sample(0, 0, 1000000)), 0)
        before = encode_checkpoint(home)
        event = MeterObserved(sample(1, HOUR, 1100000))
        updated, effects = reduce_home(home, event, HOUR)
        self.assertTrue(any(isinstance(e, Persist) and e.state.ledger == updated.ledger for e in effects))
        after = encode_checkpoint(updated)
        for checkpoint in (before, after):
            resumed, effects = restore_checkpoint(checkpoint, HOUR)
            self.assertFalse(any(isinstance(e, Send) for e in effects))
            resumed, effects = reduce_home(resumed, event, HOUR)
            self.assertEqual(resumed.ledger.streams[0].lifetime, EnergyBounds(100000, 100000))
            self.assertFalse(any(isinstance(e, Send) for e in effects))

    def test_pruning_and_unknown_gaps_survive_whole_home_checkpoint(self):
        home = self.home()
        for value in (sample(0, 0, 1000), sample(1, HOUR, 2000),
                      sample(2, HOUR * 2, 100, epoch=1, reason="reset")):
            home, _ = reduce_home(home, MeterObserved(value), value.at_ms)
        home, _ = reduce_home(home, LedgerPruned(HOUR * 2), HOUR * 2)
        resumed, _ = restore_checkpoint(encode_checkpoint(home), HOUR * 3)
        self.assertEqual(resumed.ledger, home.ledger)
        self.assertEqual(resumed.ledger.streams[0].lifetime, EnergyBounds(1000, None))
        self.assertEqual(resumed.ledger.streams[0].samples[0].at_ms, HOUR * 2)

    def test_closed_checkpoint_rejects_corrupt_ledger_and_legacy_schema(self):
        home, _ = reduce_home(self.home(), MeterObserved(sample(0, 0, 0)), 0)
        home, _ = reduce_home(home, MeterObserved(sample(1, HOUR, 1000)), HOUR)
        encoded = encode_checkpoint(home)
        for corrupt in (
            lambda v: v.update(schema_version=1),
            lambda v: v["state"].pop("ledger"),
            lambda v: v["state"]["ledger"].update(revision=0),
            lambda v: v["state"]["ledger"]["streams"][0]["samples"][0].update(total_mwh=True),
            lambda v: v["state"]["ledger"]["streams"][0]["samples"][1].update(epoch=1),
            lambda v: v["state"]["ledger"]["streams"][0]["samples"][1].update(at_ms=HOUR * 2),
            lambda v: v["state"]["ledger"]["streams"][0].update(started_at_ms=100),
        ):
            data = json.loads(encoded); corrupt(data)
            with self.assertRaises(ValueError):
                decode_checkpoint(json.dumps(data).encode())

    def test_archived_bounds_validate_rounding_and_impossible_history(self):
        ledger = self.append(self.ledger(maximum=1), sample(0, 0, 0))
        # Each 1 ms reset gap rounds outward to 1 mWh; the sum is deliberately
        # wider than one rounded bound for the complete interval.
        for revision in range(1, 5):
            ledger = self.append(ledger, sample(revision, revision, 0, epoch=revision, reason="reset"))
        ledger = prune_ledger(ledger, 4)
        stream = ledger.streams[0]
        self.assertEqual(stream.archived, EnergyBounds(0, 4))
        self.assertEqual(stream.archived_intervals, 4)
        with self.assertRaises(ValueError):
            replace(stream, archived=EnergyBounds(1000000, 1000000))
        with self.assertRaises(ValueError):
            replace(stream, archived_intervals=0)

    def test_committed_replay_accounts_gross_flows_once_and_preserves_reset_gap(self):
        root = Path(__file__).parents[1]
        result = subprocess.run([sys.executable, str(root / "scripts/replay-home-runtime.py"),
                                 str(root / "tests/fixtures/home-runtime-energy-recovery.json")],
                                check=True, capture_output=True, text=True)
        trace = json.loads(result.stdout)
        home = decode_checkpoint(json.dumps(trace["final_checkpoint"]).encode())
        self.assertEqual([s.lifetime for s in home.ledger.streams],
                         [EnergyBounds(700000, 700000), EnergyBounds(400000, 2400000)])
        self.assertFalse(any(e["type"] == "Send" for row in trace["rows"] for e in row["effects"]))
        self.assertEqual(trace["rows"][5]["actuals"][0]["since_first_anchor_mwh"]["lower"], 700000)
        self.assertEqual(trace["rows"][7]["actuals"][0]["since_first_anchor_mwh"]["lower"], 700000)


if __name__ == "__main__":
    unittest.main()
