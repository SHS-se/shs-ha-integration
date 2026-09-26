# Device operating modes and control verification

## Replacement participation model — 15 September 2026

The four-mode selector and local planning override below are implementation history, not the target design. Included/Excluded belongs to HA Devices; Monitoring/Planned belongs to the website; only Planned equipment appears on HA Schedule, with Verification or Controlling. New Planned admission defaults to Verification. Verification and Controlling are planned identically: the mode decides only whether SHS writes the schedule ([authoritative plan contract](authoritative-plan-contract.md)). Remove the duplicate plan-inclusion/review/Website-link rows.

See the [agreed participation and battery supply specification](device-participation-and-battery-supply.md).
Documentation only; replacement implementation and coordinated rollout remain pending.


| Decision | Owner | Values |
|---|---|---|
| Individual participation | HA Devices | Included / Excluded |
| Scheduling intent | Website | Monitoring / Planned |
| Planned-device execution | HA Schedule | Verification / Controlling |

The remaining four-mode descriptions are a dated implementation record. They do
not define the replacement user choices. Proportional solar allocation and explicit
battery supply scope are defined in the linked specification.

Target-design update, 14 September: the [14 September architecture review and battery release gates](controller-architecture-review.md)
now proposes explicit real versus hypothetical execution scope, requested versus
effective authority, entry/release/rapid-change behaviour, isolated verification
and battery-first coexistence. These requirements are in scope for battery
deployment; the implementation record below still describes the current controller.

Status: current implemented UI/execution behaviour, checked 13 September 2026.
The [target household runtime](https://github.com/SHS-se/smart-home-solutions/blob/main/docs/energy-optimisation/reactive-controls.md) separately specifies curve-valued
allocation and routine-restart continuation; those features are not implemented here. The later revision removes target
minimum-runtime configuration and adds mode-owned full control with automatic
external-drift correction and bounded retries; current
behaviour below is retained as implementation evidence.

Each equipment card has one mode selector on Schedule. Devices contains setup fields only:

| Mode | Collect readings | Include in optimisation | Record proposed commands | Call actuators |
| --- | --- | --- | --- | --- |
| Monitoring | Yes | No | No | No |
| Planning | Yes | Yes | No | No |
| Control verification | Yes | Yes | Yes | No |
| Controlling | Yes | Yes | No | Yes |

The website still supplies each device's planning method and inclusion contract.
Local modes restrict that contract; they cannot activate a device excluded on the
website. New devices default to monitoring. Pool water heating and EV charging
have one physical system controller each; other meter cards retain their own
participation choices. A device whose command method is unsupported cannot be
controlled merely by selecting a mode.

The integration-wide planning selector is removed. Planning runs when at least
one device requests a planning, verification or controlling mode. Internal
`planning_mode` and system `*_control_enabled` status values are derived from
`device_modes`, not independently persisted permissions. Old permission flags
are imported once by config-entry migration 12 and then removed. A previously
disabled global planner never becomes live through migration; existing live
planning and authorised current controllers preserve their modes. Retired,
incompatible controller permissions remain disabled.

Changing participation recommends a manual replan; plans otherwise change only
on a new price release. Moving between Verification and Controlling changes
neither the plan nor its recommendations, and does not reload the integration or
reset unrelated controllers. Leaving controlling first restores settings still owned by SHS;
that handover can make real service calls and can remain pending on an error.
Verification begins only after the previous ownership has been released. Leaving
Controlling on the select is the only release: restarts, unavailable or stale
readings, missing plans and faults hold the last setting SHS sent (see
[control continuity](control-continuity.md)).

## Target authority and remaining mode design

The latest authority correction changes the target, not the current implementation
record above. Controlling (also called Control) owns the supported device controls;
external changes do not override SHS intent. Monitoring, Planning and Control
verification grant no optimisation writes. Their hypothetical actions cannot
count as physical delivery or released headroom for devices under live control.

Leaving Controlling fences new optimisation commands immediately. The existing
approved handover may still write while release is pending, and late effects from
already-issued commands remain accounted for. A mode selection must not falsely
report completed release. The review linked above proposes battery-first
replacement semantics for mixed-mode planning, shared-control admission, mode
entry/rapid changes, release failures and verification isolation. These still need
implementation and end-to-end tests; wider shared thermal control remains deferred. See the canonical
[authority and mode questions](https://github.com/SHS-se/smart-home-solutions/blob/main/docs/energy-optimisation/control-reconciliation.md#operating-modes-handover-and-restart).

## Warnings and correction

Status lists controller faults, unsupported commands, manual overrides, operating
limits and incomplete handover verification above the collapsed diagnostics.
The Status tab count uses this same list; Energy, Devices and Schedule do not
repeat general warning cards. Local input issues highlight the precise field
and explain the problem beside it. **Fix** links open its tab and card and focus
the input, including optional fields that were hidden. Website issues link to
the website and do not mark unrelated local fields. Sensor faults identify
fields mapped to that exact entity. Each status warning retains its controller
reason, affected plan quarter, corrective guidance and evidence download.

The panel polls status on all tabs. While a user is editing, it updates the
warning badge and existing field highlights without replacing the input or its
unsaved text. A renewed subscription clears on the next successful status
refresh; a price download failure is not treated as proof of missing supplier
configuration.
The integration status says **Needs attention** while warnings are present,
even when planning has a valid plan. Successful subsequent checks clear the
device warning without removing unrelated issues.

Stale or unavailable observations identify the source entity and offer a Home
Assistant inspection button. Freshness errors include the last report time and
the applicable maximum age (battery 120 seconds, EV/pool 900 seconds); sources must report regularly
even when its value stays unchanged. Verification retries on each scheduler
tick. These messages do not bypass freshness checks or enable live control.

## Controller diagnostics download

Use **Schedule → Controller diagnostics → Download controller diagnostics**.
The admin-only download is `shs-controller-diagnostics.json.gz`, a gzip-compressed
JSON file. It includes every configured device in Monitoring, Planning, Control
verification or Controlling, except explicit `excluded_device_readings` entries.
A device being outside optimisation does not exclude it from diagnostics.

The schema-4 export separates current state, decisions and sampled measurements:

- `current`: all non-excluded devices and their modes, configuration/mappings,
  observed entity state and report timestamps, the accepted plan and active slot,
  planning/readiness status, ownership/restoration state and faults. Devices with
  no retained controller evaluation have a null `last_evaluated_at`; passive
  devices are not evaluated or actuated merely to produce the download.
  `unassigned_mappings` lists local mapping keys absent from the current inventory;
  these are not counted as extra physical devices. Shared entities identify alias
  candidates, not proven identity. `mapping_readiness`, `planning_support`,
  `execution_eligibility` and `last_controller_result` distinguish separate facts.
  `ready_devices` counts mapping completeness only. Observation references come
  from declared entity fields and meter IDs, not dotted metadata keys.
- `evaluations`: actual controller evaluations, with mode, trigger, plan/slot,
  observations, outcome, ownership before/after, failure latch and exact real
  service attempts. Commands distinguish `called`, transport acceptance or
  ambiguity, and setting readback. Real mode-exit handover commands have
  `phase: handover`, even when the newly selected mode is passive; restarts send none.
- `attempts` and `coverage`: simulated Control verification command generation
  and its operation coverage. Hypothetical handover remains labelled separately;
  neither simulated commands nor successful register readback prove delivered
  energy. A runtime evaluation in Verification can record real release before
  simulation, but its simulated commands appear only in `attempts`. Each group has
  a stable `group_id`; new runtime verification evaluations reference their
  `verification_group_id`. `verification_link_status` distinguishes retained,
  evicted and older unlinked evidence.
- `samples` and `sample_contexts`: read-only observations about once a minute for
  every non-excluded inventory device, including passive devices. Contexts retain
  identity, mode and configuration; samples reference the active slot. These are
  independent of control evaluations. Available mapped instantaneous power is
  separate from cumulative energy-counter intervals. Unmapped devices still have
  their energy readings and interval evidence; suggested controls do not become
  authoritative power measurements.
- `current_session` and `historical_summary`: separate counts and verification
  coverage, including `current_configuration_coverage` for the current session.
  Historical coverage does not commission a new configuration.

The persistent local journal remains
`.storage/shs_energy.verification.<config_entry_id>` and is never uploaded to the
website. Existing verification history rolls forward to schema 4 without inventing
past live evaluations, measurements or links. The latest 2,000 groups of each kind,
720 observation samples (about 12 hours) and 500 lifecycle events are retained.
Repeated equivalent checks carry counts and first/latest observations;
plan slots and configuration scopes are shared. New groups persist immediately;
repeat counts checkpoint at most once a minute and on clean shutdown. Samples
persist at each sampling interval. Retention counters expose discarded history.

Household `average_w` values estimate energy use between sample boundaries from
configured energy meters, using their reported units and counter metadata. Source
reporting times and ages remain available: sample-boundary estimates are not exact
instantaneous power. Missing/stale sources, unsupported units, counter resets,
unchanged reports, new sessions/configurations and sampling gaps over two minutes
produce explicit gaps. A counter's own source interval and average remain visible;
if its duration differs from the sample interval by more than five seconds, it is
not used in the household interval sum. Sources from before the active slot cannot
be compared with that slot's forecast. This prevents delayed reports becoming
false one-minute power spikes. Every configured source in a household category must be
usable before summing it. A fixed configured power rating is not a measurement.
Planned comparisons require the same plan and slot at both interval boundaries.
Household forecasts include the planned requests of Verification devices, which
SHS does not send, so a measured difference alone does not establish a controller
fault. Actual power
attribution still depends on correct metering and reviewed device mappings.

This is bounded diagnostic evidence, not a complete sensor history or a durable
record of every in-flight command across an abrupt crash. Runtime evaluation
records are appended when the evaluation ends; the existing restoration journal
still owns persistence before control writes. A diagnostic recording error is
visible in `current.recording_error` and does not trigger live-device restoration.
Sampling failures separately expose `current.sampling_error` and
`failed_samples_this_session`. Sampling waits for the controller lock; timestamps
show any delay, and long gaps are not interpolated.

Current device rows and per-device retained evidence omit explicit exclusions.
The whole-home plan, current configuration and historical configuration scopes
retain shared context, including references to excluded equipment where relevant.
The download contains local entity IDs and configuration and is not redacted.
Download periodically to preserve a longer analysis history.

## Coverage and limits

The export gives covered/total operation counts and explicit observed/missing
operations for each configuration and integration version. The operation
catalogue contains all six battery intents plus handover; pool heat/defer plus
handover; EV charge/stop plus handover; and the supported per-device setpoint,
on/off and permit/inhibit operations plus handover.

Only successfully generated branches count. A blocked command does not increase
coverage. A deferred or failed hypothetical handover remains missing even when
the plan operation succeeds. Retention removes corresponding old coverage too.

This is command-generation coverage, not exhaustive code coverage. It does not
prove physical response, every numeric boundary, fault recovery, cross-slot
relay timing, or exclusive ownership against external automations. In particular:

- Battery verification cannot prove that the inverter accepts remote writes or
  actually charges/discharges within the requested ceilings.
- Pool verification cannot prove water flow, heat delivery, or the thermostat's
  behaviour after a shifted band. The temperature controls' lower limits can prevent full deferral.
- Freshness checks still apply, including battery direction observations. A
  derived binary sensor that only reports on changes can become stale.
- A release (leaving Controlling on the select) relies on a running HA process
  and a reachable device. Restarts, expiry and mapping changes release nothing,
  so while HA is down every device keeps the last setting SHS sent.

Review a representative file against its plan before selecting controlling,
then commission physical response and handover on the installation. Keep native
equipment protection active. Current generic relay setup includes minimum-on/off
fields and SHS delays; the target retires those settings/locks and observes native
availability instead. Current conflict handling is not uniform across device
paths. In the replacement, Controlling grants SHS complete operational authority
over the supported device surface. Unexpected/external settings are overwritable
drift: reconcile and automatically reassert current SHS intent, with bounded
adapter-supported retries for ambiguous delivery. Do not suspend control or
require explicit resume because of an external edit. Users act through SHS
controls or leave Controlling before operating elsewhere. Native equipment
protections still apply; no global action mutex is needed.

## Pool temperature setup

Select the Nibe start and stop temperature entities and the water-temperature
reading. There are no separate coldest/warmest band settings in SHS. The Nibe's
current start/stop values define the normal heating band. During deferral SHS
lowers that band, preserving its width and respecting both entities' reported
minimum, maximum and step. Both targets are validated before either is written.
Heating requests and handover restore the captured normal band; SHS never
invents a warmer target in this interim adapter. Future authorised pool preheat
comes from the trajectory and editable curve, not a new duplicate band setting.
Config-entry migration 13 removes the retired duplicate
bounds while retaining the selected controls and operating modes.
