"""A counter that goes backwards must not become energy that was never used.

``recorder_rows`` is Home Assistant's own sum compilation for a
``total_increasing`` sensor (``sensor/recorder.py``, 2026.10): a fall of more
than a tenth starts a new cycle at zero, so the next reading counts in full.
The readings are synthetic.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / "custom_components/shs_energy"))
from shs_core.counter_changes import judge_counter_rows, retract_catch_ups, settled_energy
from shs_core.household_ports import HouseholdReadError
from shs_core.optimisation import aggregate_device_changes
from household_fixture import Rig

T0 = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)
PERIOD = timedelta(minutes=5)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def recorder_rows(readings, periods, *, start=T0, scale=1.0, skipped=()):
    """Five-minute rows Recorder compiles from ``(minute, reading)`` pairs.

    ``readings[0]`` is the reading before the first period. ``scale`` is the
    factor from the entity's unit to kWh; ``skipped`` are periods Recorder
    wrote no row for (the entity was unavailable throughout).
    """
    old_state = new_state = readings[0][1]
    total, previous_total, rows = 0.0, 0.0, []
    for index in range(periods):
        begin, finish = start + index * PERIOD, start + (index + 1) * PERIOD
        inside = [value for minute, value in readings[1:] if begin < at(minute) <= finish]
        if index in skipped:
            continue
        for value in inside:
            if value < 0.9 * new_state:
                total += new_state - old_state
                new_state, old_state = value, 0.0
            else:
                new_state = value
        total += new_state - old_state
        old_state = new_state
        rows.append({"start": begin.timestamp(), "end": finish.timestamp(),
                     "state": new_state * scale, "sum": total * scale,
                     "change": (total - previous_total) * scale})
        previous_total = total
    return rows


def energy(readings, periods, **kwargs):
    """What the app keeps of each period: kWh, or None when unknown.

    Read as the app reads it: with the period before the window, which is then
    dropped, so the window's first change has a neighbour to be judged against.
    """
    skipped = {index + 1 for index in kwargs.pop("skipped", ())}
    rows = judge_counter_rows(
        recorder_rows(readings, periods + 1, start=T0 - PERIOD, skipped=skipped, **kwargs), T0)
    states = [(at(minute), value) for minute, value in readings]
    kept = {}
    for row in rows:
        if row.start < T0:
            continue
        if row.verdict == "unknown":
            value = None
        elif row.verdict == "suspect":
            value = settled_energy(row, states)
        else:
            value = row.change
        kept[round((row.start - T0) / PERIOD)] = None if value is None else round(value, 6)
    return kept


class RecorderHandlingTests(unittest.TestCase):
    """What Recorder alone reports, so the correction is not a second one."""

    def test_recorder_keeps_a_restart_from_zero_but_books_a_zero_reading_in_full(self):
        restart = recorder_rows([(0, 500.0), (2, 500.2), (3, 0.0), (4, 0.1)], 1)
        self.assertAlmostEqual(restart[0]["change"], 0.3)
        glitch = recorder_rows([(0, 500.0), (2, 0.0), (3, 500.1)], 1)
        self.assertAlmostEqual(glitch[0]["change"], 500.1)
        replaced = recorder_rows([(0, 500.0), (2, 120.0)], 1)
        self.assertAlmostEqual(replaced[0]["change"], 120.0)


class CounterJudgementTests(unittest.TestCase):
    def test_a_steady_counter_is_kept_as_recorded(self):
        self.assertEqual(energy([(0, 100.0), (3, 100.2), (8, 100.5), (13, 100.5)], 3),
                         {0: .2, 1: .3, 2: 0.0})

    def test_a_restart_from_zero_keeps_the_energy_on_both_sides(self):
        """Used before the restart and after it; the restart itself adds nothing."""
        self.assertEqual(energy([(0, 500.0), (2, 500.2), (3, 0.0), (4, 0.1), (8, 0.4)], 2),
                         {0: .3, 1: .3})

    def test_a_zero_reading_between_two_good_ones_is_their_difference(self):
        self.assertEqual(energy([(0, 500.0), (2, 0.0), (3, 500.1), (8, 500.4)], 2), {0: .1, 1: .3})

    def test_a_zero_reading_across_two_periods_leaves_the_return_unknown(self):
        """The period that climbs back would otherwise carry the whole counter."""
        self.assertEqual(energy([(0, 500.0), (4, 0.0), (7, 500.2), (13, 500.5)], 3),
                         {0: 0.0, 1: None, 2: .3})

    def test_a_replaced_meter_is_unknown_and_what_follows_is_kept(self):
        """A new meter starting at 120 kWh did not use 120 kWh in five minutes."""
        self.assertEqual(energy([(0, 500.0), (2, 120.0), (8, 120.3), (13, 120.5)], 3),
                         {0: None, 1: .3, 2: .2})

    def test_one_high_reading_is_unknown_in_one_period_or_across_two(self):
        self.assertEqual(energy([(0, 500.0), (2, 9000.0), (3, 500.1), (8, 500.4)], 2), {0: None, 1: .3})
        self.assertEqual(energy([(0, 500.0), (3, 500.2), (7, 9000.0), (12, 500.4), (18, 500.7)], 4),
                         {0: .2, 1: None, 2: None, 3: .3})

    def test_a_meter_that_reports_in_another_unit_is_settled_in_kwh(self):
        """Readings in MWh, statistics in kWh, as for a plant meter."""
        self.assertEqual(energy([(0, .5), (2, .5002), (3, 0.0), (4, .0001)], 1, scale=1000.0), {0: .3})

    def test_a_period_recorder_skipped_makes_the_next_change_unknown(self):
        """An unavailable counter: the row after the gap is two periods' sums apart."""
        self.assertEqual(energy([(0, 100.0), (3, 100.2), (13, 101.0), (18, 101.1)], 4, skipped={1}),
                         {0: .2, 2: None, 3: .1})

    def test_a_small_dip_is_left_to_the_rounding_rule(self):
        """A meter back from a power cut a hair lower: consistent, so not a reset."""
        rows = judge_counter_rows(recorder_rows([(0, 7862.724003), (3, 7862.724), (8, 7862.9)], 2))
        self.assertEqual([row.verdict for row in rows], ["trusted", "trusted"])
        self.assertLess(rows[0].change, 0)

    def test_rows_without_a_reading_are_trusted_as_before(self):
        rows = judge_counter_rows([{"start": T0.timestamp(), "change": .2},
                                   {"start": (T0 + PERIOD).timestamp(), "change": .3}])
        self.assertEqual([(row.verdict, row.change) for row in rows], [("trusted", .2), ("trusted", .3)])

    def test_a_window_that_starts_after_a_gap_does_not_trust_its_first_change(self):
        rows = recorder_rows([(0, 100.0), (3, 100.2), (8, 100.5)], 2)
        self.assertEqual(judge_counter_rows(rows, T0)[0].verdict, "unknown")
        self.assertEqual(judge_counter_rows(rows, T0 + PERIOD)[1].verdict, "trusted")


class CatchUpTests(unittest.TestCase):
    """A counter that stalls keeps its last reading, then books the whole stall at once."""

    def samples(self, *energies):
        return [(at(5 * index), energy) for index, energy in enumerate(energies)]

    def test_the_catch_up_and_the_zeros_it_caught_up_for_are_left_out(self):
        """A 13.2 kW plant uses at most 1.1 kWh in five minutes; 9.2 kWh is a stall catching up."""
        samples = self.samples(.4, .5, 0, 0, 0, 9.2, .6)
        kept, stalls, open_ended = retract_catch_ups(samples, 1.1, 300)
        self.assertEqual(kept, [samples[0], samples[1], samples[6]])
        self.assertEqual(stalls, [(at(10), at(25), 9.2)])
        self.assertIsNone(open_ended)

    def test_a_stall_that_reaches_the_first_sample_may_have_begun_earlier(self):
        samples = self.samples(0, 0, 9.2, .6)
        kept, stalls, open_ended = retract_catch_ups(samples, 1.1, 300)
        self.assertEqual((kept, open_ended), ([samples[3]], at(10)))

    def test_only_zeros_right_before_the_catch_up_belong_to_it(self):
        """Zeros before the counter last moved were measured; so was a sample across a gap."""
        samples = self.samples(0, 0, .3, 0, 9.2)
        self.assertEqual(retract_catch_ups(samples, 1.1, 300)[0], samples[:3])
        gapped = [(at(0), 0.0), (at(5), 0.0), (at(15), 9.2)]
        self.assertEqual(retract_catch_ups(gapped, 1.1, 300)[0], gapped[:2])

    def test_energy_the_equipment_can_use_is_kept(self):
        samples = self.samples(0, 0, 1.1, 1.0)
        self.assertEqual(retract_catch_ups(samples, 1.1, 300), (samples, [], None))
        # No evidence of what the equipment can draw: nothing is judged.
        lump = self.samples(0, 0, 50.0)
        self.assertEqual(retract_catch_ups(lump, float("nan"), 300)[0], lump)
        self.assertEqual(retract_catch_ups(lump, 0, 300)[0], lump)

    def test_two_stalls_are_each_retracted(self):
        samples = self.samples(0, 5.0, .2, 0, 0, 7.0, .1)
        kept, stalls, open_ended = retract_catch_ups(samples, 1.1, 300)
        self.assertEqual(kept, [samples[2], samples[6]])
        self.assertEqual([stall[:2] for stall in stalls], [(at(0), at(5)), (at(15), at(25))])
        self.assertEqual(open_ended, at(5))


class HouseholdReadTests(unittest.IsolatedAsyncioTestCase):
    """The whole read: Recorder rows in, quarters out, one device's fault kept to itself."""

    def rig(self, rows, states):
        rig = Rig(now=at(30))
        async def statistics(start, end, entities, period, units, kinds):
            self.assertEqual((start, kinds), (at(-5), {"change", "state"}))
            return {entity: [row for row in rows[entity] if row["start"] >= start.timestamp()]
                    for entity in entities}
        rig.history.statistics.side_effect = statistics
        rig.history.states.return_value = states
        return rig

    def series(self, readings):
        return recorder_rows(readings, 7, start=at(-5))

    async def test_a_bad_reading_costs_one_quarter_of_one_device(self):
        steady = [(-5, 10.0)] + [(minute, 10.0 + minute * .01) for minute in range(0, 31, 5)]
        glitch = [(-5, 500.0), (4, 500.2), (7, 0.0), (8, 500.3), (14, 500.5),
                  (19, 500.8), (24, 501.0), (29, 501.2)]
        rig = self.rig({"sensor.steady": self.series(steady), "sensor.glitch": self.series(glitch)},
                       {"sensor.glitch": [(at(minute), str(value), None) for minute, value in glitch]})
        changes = await rig.household._statistics_changes(
            ["sensor.steady", "sensor.glitch"], at(0), at(30), "5minute")
        quarters = aggregate_device_changes({"steady": changes["sensor.steady"], "glitch": changes["sensor.glitch"]})
        self.assertEqual([row["device_energy_kwh"] for row in quarters],
                         [{"steady": .15, "glitch": .5}, {"steady": .15, "glitch": .7}])
        # Only the counter that went backwards had its readings fetched.
        self.assertEqual(rig.history.states.await_args.args[2], ["sensor.glitch"])

    async def test_a_replaced_meter_leaves_its_quarter_incomplete(self):
        replaced = [(-5, 500.0), (4, 500.2), (7, 120.0), (14, 120.3), (19, 120.6), (24, 120.8), (29, 121.0)]
        rig = self.rig({"sensor.meter": self.series(replaced)},
                       {"sensor.meter": [(at(minute), str(value), None) for minute, value in replaced]})
        changes = await rig.household._statistics_changes(["sensor.meter"], at(0), at(30), "5minute")
        quarters = aggregate_device_changes({"meter": changes["sensor.meter"]})
        self.assertEqual([(row["start"], row["device_energy_kwh"]) for row in quarters],
                         [(at(15).isoformat(), {"meter": .7})])

    async def test_unreadable_readings_leave_the_period_unknown_and_keep_the_rest(self):
        glitch = [(-5, 500.0), (4, 500.2), (7, 0.0), (8, 500.3), (14, 500.5),
                  (19, 500.8), (24, 501.0), (29, 501.2)]
        rig = self.rig({"sensor.glitch": self.series(glitch)}, {})
        rig.history.states.side_effect = HouseholdReadError("recorder busy")
        changes = await rig.household._statistics_changes(["sensor.glitch"], at(0), at(30), "5minute")
        quarters = aggregate_device_changes({"glitch": changes["sensor.glitch"]})
        self.assertEqual([row["device_energy_kwh"] for row in quarters], [{"glitch": .7}])


class StalledPlantTests(unittest.IsolatedAsyncioTestCase):
    """Synthetic plant: the link drops, the counters hold, then catch up in one step."""

    def rig(self):
        options = {"planning_mode": "live", "solar_production_power_entity": "sensor.pv_power",
                   "grid_power_entity": "sensor.grid_power", "battery_charge_max_w": 8800,
                   "device_control_mappings": {"heater": {"power": "sensor.heater_power"}}}
        rig = Rig(now=at(60), options=options)
        rig.household.resolved_options = lambda: options
        self.peak_reads = 0
        async def statistics(start, end, entities, period, units, kinds):
            if period == "month":
                self.peak_reads += 1
                self.assertEqual((units, kinds), ({"power": "W"}, {"max", "min"}))
                recorded = {"sensor.pv_power": [{"max": 9100.0, "min": 0.0}, {"max": 13200.0, "min": 0.0}],
                            "sensor.grid_power": [{"max": 15000.0, "min": -7000.0}],
                            "sensor.heater_power": [{"max": 3400.0, "min": 0.0}]}
                # Actual native month boundaries are part of retained peak provenance.
                for values in recorded.values():
                    for index, row in enumerate(values):
                        row.update(start=(T0-timedelta(days=60-30*index)).timestamp(),
                                   end=(T0-timedelta(days=30-30*index)).timestamp())
                return {entity: recorded[entity] for entity in entities if entity in recorded}
            return {entity: [row for row in self.rows[entity] if row["start"] >= start.timestamp()]
                    for entity in entities}
        rig.history.statistics.side_effect = statistics
        return rig

    def counter(self, per_period):
        """Readings every five minutes from 08:55, as ``(minute, reading)``."""
        reading, readings = 1000.0, [(-5, 1000.0)]
        for index, energy in enumerate(per_period):
            reading += energy
            readings.append((5 * index + 4, reading))
        return recorder_rows(readings, len(per_period) + 1, start=at(-5))

    async def test_a_stalled_solar_counter_is_left_unmeasured_and_the_rest_is_kept(self):
        rig = self.rig()
        # Solar: steady, then nothing for 25 minutes, then 5.5 kWh at once.
        self.rows = {"sensor.pv": self.counter([.9, .9, .9, 0, 0, 0, 0, 0, 5.5, .9, .9, .9]),
                     "sensor.grid": self.counter([.1] * 12),
                     "sensor.heater_energy": self.counter([.25] * 12),
                     "sensor.charge": self.counter([.5] * 12)}
        categories = {"solar_production": ["sensor.pv"], "grid_import": ["sensor.grid"],
                      "battery_charge": ["sensor.charge"]}
        devices = [{"key": "heater", "statistic_id": "sensor.heater_energy"}]
        await rig.household._refresh_counter_bounds(categories, devices)
        self.assertEqual(rig.household._counter_bounds_w, {
            "sensor.pv": 13200.0, "sensor.grid": 15000.0, "sensor.heater_energy": 3400.0, "sensor.charge": 8800.0})
        rows = await rig.household._actual_quarters(categories, at(0), at(60))
        # The stall covers 09:15-09:45; its quarters keep the grid and battery, without solar.
        self.assertEqual([row.get("solar_production_kwh") for row in rows], [2.7, None, None, 2.7])
        self.assertEqual([row["grid_import_kwh"] for row in rows], [.3] * 4)
        self.assertEqual([row["battery_charge_kwh"] for row in rows], [1.5] * 4)
        # The stall lies inside these quarters, so nothing older needs sending again.
        self.assertIsNone(rig.household._open_catch_up)
        # The bounds are evidence that rarely changes: read once an hour.
        await rig.household._refresh_counter_bounds(categories, devices)
        self.assertEqual(self.peak_reads, 1)

    async def test_a_counter_with_no_power_evidence_is_read_as_before(self):
        rig = self.rig()
        self.rows = {"sensor.unbounded": self.counter([.1, 0, 0, 40.0, .1, .1])}
        await rig.household._refresh_counter_bounds({"total_consumption": ["sensor.unbounded"]}, [])
        changes = await rig.household._statistics_changes(["sensor.unbounded"], at(0), at(30), "5minute")
        self.assertEqual([round(energy, 6) for _, energy in changes["sensor.unbounded"]], [.1, 0, 0, 40.0, .1, .1])

    async def test_unreadable_peaks_change_nothing(self):
        rig = self.rig()
        rig.history.statistics.side_effect = HouseholdReadError("recorder busy")
        await rig.household._refresh_counter_bounds({"solar_production": ["sensor.pv"]}, [])
        self.assertEqual(rig.household._counter_bounds_w, {})


if __name__ == "__main__":
    unittest.main()
