# Actual energy ledger

## Scope is not source measurement — 15 September 2026

Record the supply-scope and participation identity with decision evidence without resetting physical energy accounting. Scope controls the amount permitted to offset eligible demand; it does not prove which appliance received battery electrons. Excluded consumption remains in aggregate meters. Keep gross consumption, solar and battery flows separate, subtract PV once, and label any per-device source/cost attribution as accounting rather than direct measurement.

See the [agreed participation and battery supply specification](device-participation-and-battery-supply.md).
Documentation only; replacement implementation and coordinated rollout remain pending.

Review update: the expired-policy retention defect from the
[architecture review](controller-architecture-review.md) is fixed. The
[recovery follow-up](runtime-recovery-fixes.md) adds per-stream settlement and
atomic capacity maintenance independent of policy execution lifetime.

The household runtime now records measured gross electrical energy in its own
immutable ledger, saved atomically with command and reservation state. This
completes the ledger implementation following the command/restart prototype.
It is still offline: no live meter subscriptions or journal worker have been connected.
[Exact-condition policy acceptance](policy-binding.md) now consumes its watermarks.

## Usage and ownership

```python
from energy_ledger import (
    MeterSpec, CounterSample, create_ledger, mark_actuals, actuals_since,
)
from home_runtime import create_home, reduce_home, MeterObserved

ledger = create_ledger("household-actuals", "mapping-1", (
    MeterSpec("battery-charge", "battery", "battery-AC", "charge", None),
    MeterSpec("battery-discharge", "battery", "battery-AC", "discharge", None),
))
home = create_home(group_specs, runtime_limits, ledger=ledger)
home, effects = reduce_home(home, MeterObserved(CounterSample(
    "battery-charge", "physical-meter:charge-register", 0, 0,
    1000, 5_000_000, "initial binding",
)), 1000)
watermark = mark_actuals(home.ledger, 1000)
home, effects = reduce_home(home, MeterObserved(CounterSample(
    "battery-charge", "physical-meter:charge-register", 0, 1,
    3_601_000, 5_600_000,
)), 3_601_000)
actuals = actuals_since(home.ledger, watermark, 3_601_000)
# Charge: 600,000 mWh = 600 Wh. Discharge: unknown, because it has no anchor.
```

`energy_ledger.py` owns counter continuity, interval bounds, watermark queries and
retention. The household reducer is the sole state writer. A meter event updates
the ledger and emits the existing whole-home `Persist`; it does not acknowledge a
command, change a reservation or release physical headroom. Native state remains
in the observation model. Queries do not mutate, consume or reset actuals, so a
new plan or repeated query cannot replenish an energy allowance.

Checkpoint schema **4** includes the ledger and its source high-water marks.
Earlier prototype schemas are rejected; this offline prototype has no compatibility migration.
`ledger: null` explicitly means that accounting is not configured, and meter
events are then rejected. Restore preserves the ledger unchanged while requiring
fresh control observations and authority under the existing restart protocol.

## Measurement contract

Energy is integer **mWh** (milli-watt-hours): 1 kWh = 1,000,000 mWh. Timestamps are
absolute integer milliseconds. These units avoid accumulating floating-point
rounding in the ledger; the future source port must convert the sensor's declared
unit and precision explicitly.

Each registered stream has a stable logical identity, device, electrical boundary
and direction. Each sample also identifies the actual hardware counter/register.
Register directions independently: no synchronisation of charge/discharge readings
is assumed, and an absent direction is never filled with zero. Duplicate stream,
physical boundary/direction and retained hardware counter mappings are rejected.
Actual installation bindings still have to establish that different source IDs
really identify different counters.

A source revision must increase across all epochs and process restarts. Its
original observation timestamp must also increase. Identical latest samples are
duplicates; older revisions are stale and do not count again. Conflicting payloads
at the latest revision, counter decreases, time regression and future samples are
rejected without changing the ledger. The port must preserve/recover source
revisions, coalesce reports with the same timestamp, and never assign receipt time
as a replacement for an unknown measurement time.

The first reading establishes a baseline; its accumulated counter value is not
newly delivered service. A reset or replacement requires an increased epoch and
explicit provenance. Hardware identity changes inside an epoch are rejected.
There is no inferred reset and no zero-filled recovery interval.

## What is known about an interval

| Evidence | Ledger result |
| --- | --- |
| Two accepted readings in the same epoch | Exact subtraction of those counters for the complete interval, even after missed reports or a restart |
| Unchanged cumulative counter at a later time | Measured zero for that complete interval |
| Query cuts through a counter interval | Bounds on the partial energy; no time-proportional allocation |
| Reset/replacement between readings | An uncertain gap, followed by the new baseline; no subtraction across epochs |
| Before the first anchor or after the last sample | Unknown consumption, bounded only if supported physical evidence exists |
| Watermark older than retained history | Explicit rejection; archived totals cannot reconstruct the lost time distribution |

Without a physical maximum, a partial interval is bounded by zero and the whole
measured delta; a missing/reset interval has no known upper bound (`None`). A
declared `maximum_power_w` requires `power_bound_evidence`, identifying a validated
limit at that exact electrical boundary. A forecast, requested ceiling or assumed
average power is not such evidence. The runtime cannot commission that evidence.

For a measured delta D, the partial upper bound is the smaller of D and the maximum
energy possible inside the query. The lower bound is D minus the maximum possible
outside, clamped to zero. Integer bounds round outward. A complete measured delta
inconsistent with the declared maximum is rejected, never clipped into plausibility.
Coverage reports distinguish full counter intervals, partial intervals, reset gaps
and missing anchors/tails. Bounds can become exact while the time-coverage reason
still records that a query cut a counter interval.

“Exact” here means exact arithmetic on accepted counters, not zero physical meter
error. The port must reject unusable evidence and specify meter precision and
calibration. This stage neither integrates instantaneous power nor derives flow
from SOC. Measurements at battery AC and DC boundaries remain separate; conversion
losses must not be applied to already metered energy again. No aggregate silently
adds overlapping grid, battery and appliance streams or nets charge against discharge.
Thermal service, SOC change and billed SEK are not electrical ledger quantities.

## Watermarks, retention and crash recovery

A watermark names ledger identity, mapping revision, ledger revision and an
absolute time. Its time must not precede the evidence present when it is captured.
Queries reconcile subsequent evidence over that time range. A foreign mapping,
future ledger revision or pruned start is rejected. The caller supplies the actual
as-of time; the pure query has no clock. Multiple overlapping queries do not create
new accounting entries, and later cumulative evidence can narrow earlier uncertainty.

Each stream retains up to the configured number of intervals (default 128, maximum
256), plus an anchor; there are at most 64 streams. The whole checkpoint's 1 MB
limit also applies. `LedgerPruned(before_ms)` archives complete intervals only,
preserving lifetime lower/upper totals, first-anchor time, archived interval count
and the latest source revision/epoch. Runtime `MeterObserved` validates the new
sample first and, at capacity, archives one oldest complete interval before
appending it. Policy settlement and ledger maintenance are one immutable update
and checkpoint. The low-level `record_sample` API still requires explicit pruning.

Each accepted policy retains its original watermark plus a settled prefix and
cursor per stream. Before pruning, each prefix absorbs only the interval through
that stream's new retained physical counter anchor. Replacement combines the
prefix with its retained tail exactly once. An absent or slow stream keeps its own
cursor; its unobserved tail is never permanently settled as zero or uncertainty.
Partial-origin intervals and epoch gaps retain their conservative energy bounds,
time coverage and provenance. No successful recompile is needed to keep metering.
Queries without a settled prefix still reject starts before retained history.

An event lost before its checkpoint can be replayed against the old durable anchor;
an event already checkpointed is a duplicate. Both paths yield the same total.
The ordered durable writer and a recoverable source/replay path are still required
for deployment. A later same-epoch cumulative sample can recover a missed aggregate;
an unobserved reset cannot recover unknown delivery. No power estimate is inserted
as a substitute for that evidence.

## Verification and next work

Run the deterministic recovery trace and focused tests:

```bash
python3.13 scripts/replay-home-runtime.py tests/fixtures/home-runtime-energy-recovery.json
python3.13 -m unittest discover -s tests -p test_energy_ledger.py -v
```

The fixture combines gross battery charge/discharge, a crash before an updated
sample is durable, replay of that sample, and a counter reset. Final charge is
700 Wh counted once. Discharge is bounded by 400–2,400 Wh because the reset gap is
unknown under the synthetic 2 kW maximum. It emits no device commands.

Validation passed with 548 Python tests (including 18 ledger tests and a runtime
reservation-independence test), 58 frontend tests and integration compilation.
Tests cover duplicate/conflicting samples, missed reports, epochs, partial cuts,
unknown coverage, pruning, corrupt checkpoints and both persistence crash cuts.
An exhaustive small trajectory test checks partial bounds against independent
actual subinterval flows. Independent Codex review also checked those bounds and
identified revision/archive validation improvements that are now included. Claude
review was deferred at implementation time; the subsequent Opus Max/Codex review
linked above has completed. Its settlement/retention finding is fixed; follow-up
regressions cover 260 intervals through policy expiry, crash replay at compaction,
asynchronous streams, missing anchors, reset gaps and partial cuts against an
unpruned oracle. The complete integration suite now passes 583 Python tests.

A local Python 3.13 probe with 64 streams and 32 retained samples per stream used a
336,053-byte checkpoint. Across 200 reducer calls after 20 warmups, meter-event
processing measured p95 1.89 ms, p99 2.07 ms, maximum 3.07 ms. This is not the required
slowest-HA-host benchmark, and excludes storage, queues and physical response.

[Exact-condition policy acceptance and request binding](policy-binding.md) now
validate and reconcile these watermarks while keeping actuals separate from expected
costs. Backend diagnostic time/state coverage now exists; a production acceptance
profile and HA consumer are still needed for deployable execution.
Live source/journal/transport ports and battery adapter commissioning follow.
Thermal models remain deferred as significant work; device order stays battery,
pool, then car. Household-specific configuration remains separate from this model.
