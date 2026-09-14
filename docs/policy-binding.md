# Exact-condition battery policy binding

Review update: backend time/state coverage is now implemented for diagnostics,
but this reader still accepts exact anchors only. The [14 September architecture review and battery release gates](controller-architecture-review.md)
records the production profile, mixed-mode and live-port requirements, plus the
expired-policy retention defect, now fixed by the
[recovery follow-up](runtime-recovery-fixes.md). Do not lengthen this prototype's lease.

The offline battery compiler now has a real HA-side reader and a tested path from
its economic decision to the household runtime's request/journal protocol. The
reader consumes output from the existing `compileBatteryPolicy` implementation;
the two provider fixtures are generated, not handwritten substitutes.

**This is an exact-anchor prototype, not production live policy execution.** The
compiler covers one complete input problem at its initial timestamp. It cannot
price a shorter remaining interval after time passes, interpolate changed state,
or predict how native inverter ceilings produce physical flow. The implementation
preserves those limits. Thermal models remain a significant deferred workstream.

## Caller and ownership

```python
from battery_policy import read_battery_policy
from home_runtime import PolicyContextChanged, PolicyOffered, reduce_home

compiled = read_battery_policy(provider_bytes)
# A local observation/model port constructs a validated PolicyContext and
# explicit synthetic response bindings. These are not taken from provider claims.
home, effects = reduce_home(home, PolicyContextChanged(context), now_ms)
home, effects = reduce_home(home, PolicyOffered(
    policy_revision, compiled, context.watermark, bindings, deadband_sek,
), now_ms)
```

`battery_policy.py` owns closed parsing, the immutable compiled summary, current
response checks, current cost evaluation and C/F ranking. `home_runtime.py` owns
the current context, accepted policy revision, selected request and reconciliation
record together with the ledger and pending device effects. The shared
`runtime_json.py` owns bounded JSON/tag decoding; it replaces duplicated codec
logic rather than adding a second checkpoint format.

`PolicyContext` supplies the complete canonical resolved problem, current mode,
observation and physical-frame revisions, and the real `ActualsWatermark` captured
with the model. Current battery energy, PV/load, prices, availability and source
permissions must also match fresh numeric observation values. Grid limits and
external demand must match the physical frame. Future forecasts and model data
remain inputs of the local model/observation port, compared without extrapolation.
The live port that constructs this context is still to be implemented.

`PolicyBinding` names an alternative, adapter revision, response evidence, exact
current response path and complete target. This version only supports adapter
revision `synthetic-imposed-power-v1`: explicit charge, discharge, solar-charge,
export and zero PV-curtailment values. A native mode or ceiling is not accepted
as equivalent. Every alternative, including the reference, needs a matching local
binding and conservative current headroom. Pending effects are outside this
compiler's coverage and block acceptance of another policy.

## Acceptance and economic checks

The initial reader supports these explicit bounds and exclusions:

- `offline-battery-policy-v1` and `offline-household-v1`, with exact-problem coverage,
  imposed physical response and no interpolation.
- One battery, one current interval ending at the next UTC quarter, at most 288
  horizon intervals, 12 alternatives and 32 residual electrical-load series.
- Energy-only tariffs, including negative prices, battery efficiencies/gross wear,
  shaping and ramp costs. Thermal/EV models, service terms and terminal utility are
  rejected by this initial reader even where the offline scorer can support them.
- Exhaustive results within the declared finite search graph, with no omitted
  alternatives. Pruned search results require a later supported acceptance profile.
- 128 KB per compiler result, plus the existing 1 MB whole-checkpoint limit.
  Unknown fields, unsupported tags, duplicate IDs, nonfinite values, mismatched
  series, malformed intervals and inconsistent work evidence are rejected.

The reader independently validates the **current** imposed response against battery
ratings, availability, source/export permissions, gross attribution, grid limits
and the energy endpoint including charge/discharge efficiencies. Simultaneous
imposed charging/discharging is rejected under this source model. It recomputes
current import/export cost, gross wear, shaping and initial ramp cost and compares
every component with the compiler's C. The tolerance is 1e-7 SEK per component.

For every alternative it checks component totals and:

```text
billable = import − export
total = billable + wear + starts + shaping + ramp − service − terminal
C_a − C_reference + F_a = J_a − J_reference
```

The common reference must exist and have zero future delta. Ranking is rebuilt
from reconciled C/F values; a contradictory published ranking is rejected. The
current represented operation wins within an explicit local deadband (at least the
1e-7 SEK numerical tolerance); otherwise
the lowest delta wins, with stable identity ordering. This adds no separate battery
opportunity price, headroom reward or fixed grid-energy entitlement.

Future trajectory feasibility and J/F values remain the compiler's responsibility.
The consumer checks their structure and reconciliation, not a second future solve,
continuous optimality or a regional error certificate. The generated-fixture check
detects provider drift for the included examples. Sustained-advantage filtering,
broader coverage and production error acceptance are still work to do.

## Time, revisions and meter history

Selection is allowed only at the exact source anchor millisecond with matching
context and ledger revision. A synthetic selected request expires at
`anchor_ms + 1`, not at the quarter boundary. Preparation and durable acknowledgement
must occur at that same virtual timestamp to emit a Send in a replay. This short
lease exists to test the boundary; it is unusable as a real battery schedule.

Before every runtime drive, the accepted context, source time, mode authority,
observation/frame revisions and ledger revision are checked again. Any mismatch
retires the policy's unsent work and requests recompilation. An old journal
acknowledgement cannot resurrect it. Already issued commands retain their possible
effects and reservations under normal reconciliation. A genuine expiry can require
the explicitly approved handover; this does not invent a default inverter setting.

Monitoring, Planning and Control Verification produce economic diagnostics only.
Controlling can publish the synthetic request. Direct `Requested` events cannot
overwrite an active policy decision. Rejected replacements preserve the previously
accepted revision, subject to its existing freshness and expiry checks.

The source compiler watermark is only a timestamp. Acceptance also requires the
actual ledger identity, mapping and revision; none is inferred from that timestamp.
On replacement, the previous policy's settled per-stream prefixes and retained
tails are reconciled from its original watermark before mutation.
The source watermark, gross bounds and coverage of this reconciliation are saved
with the new policy. The ledger itself is never reset. Those mWh are not subtracted
from SEK costs or added to a new allowance. Pruning and automatic capacity
maintenance settle each stream only through a retained physical counter anchor,
atomically with the ledger. Policy expiry no longer pins the history buffer; the
original watermark and measured/uncertain actuals remain available for replacement.

Checkpoint schema **4** includes policy, context and ledger atomically. Older
prototype schemas are rejected without a compatibility path. Active checkpoint
validation ties the selection, target, native guards, request expiry, context and
watermark together. Restore keeps actuals and ambiguous issued effects, invalidates
context and retires the policy-owned request. A selection cannot simply replay
after restart; a new exact compilation/context is required for this prototype.

## Reproduction and verification

From this repository, with cached backend Deno dependencies:

```bash
python3.13 scripts/generate-policy-fixtures.py ../smart-home-solutions-t-by --check
python3.13 scripts/replay-home-runtime.py tests/fixtures/home-runtime-policy-binding.json
python3.13 -m unittest discover -s tests -p test_battery_policy.py -v
```

Omit `--check` to regenerate the two provider fixtures. This invokes the existing
backend compiler CLI and does not alter the backend planner or shipping API output.

The fixed-tail fixture selects hold: discharge saves 0.50 SEK in C but loses 2.50
SEK in F, leaving it 2.00 SEK worse. The negative-price fixture selects charge, whose
full objective is 0.0413125 SEK. The committed runtime trace selects charge, persists
its preparation, emits one synthetic send at the anchor, then expires the decision
one millisecond later while retaining the issued effect's reservation.

Validation: 565 Python tests, including 17 policy tests, and 58 frontend tests passed;
integration compilation and regenerated-provider fixture checks passed. Tests cover
forged costs and physical responses, changed conditions, all passive modes, stale
journal acknowledgement, invalid bindings, replacement actuals, retention,
restart and corrupt checkpoints. Independent Codex review identified the initial
current-response validation gap and dynamic-ID bug; both are fixed and tested.
Claude review was deferred at implementation time; the subsequent Opus Max/Codex
review linked above is complete. Its four runtime defects are fixed; follow-up
validation passes 583 Python and 58 frontend tests. Integration and native
commissioning remain outstanding.

A local Python 3.13 probe ran 200 iterations on the 6,741-byte negative-price fixture.
Reader p99 was 0.25 ms and acceptance p99 0.12 ms; the accepted checkpoint was 9,897
bytes. These are small synthetic local measurements, not maximum-policy benchmarks
on the slowest supported HA host or production latency guarantees.

## Next required work

The backend now has diagnostic remaining-time/state coverage. Define its
deployable C/F acceptance profile, native response and validated error evidence,
and implement the corresponding HA consumer. This is now a prerequisite to meaningful
live execution, not something that adapter wiring can bypass. Commission native
battery response and transition contracts, then connect the journal, observation,
timer and transport ports through verification and rollout gates. Device order
remains battery, pool, then car; thermal modelling and notifications remain deferred.
