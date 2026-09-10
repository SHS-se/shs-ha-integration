# A shared control definition for HA and the website

Revised proposal, 10 September 2026. Pool responsibility boundary updated with
Phil: all Nibe/pump actions and trigger/sequencing logic belong to the customer's
automation. SHS sends heat/defer/release requests through its declared interface.
See the tested [control contract v1](../contracts/control/v1/README.md). Supersedes the earlier equipment-first
catalogue proposal. Design only: no runtime, website or live settings changed.

## The idea in plain language

For each thing SHS controls, keep one agreed description of the job:

- Home Assistant says which controls exist and what values they accept.
- The website says what the plan should achieve and describes energy use.
- Our integration checks that those two fit together, then carries out the plan.

Both sides refer to the same version of that description. Editing a value cannot
silently make yesterday's plan mean something different today.

The controllable device is the interface we operate. For a Shelly channel, that
is its relay. We do not require the make/model of whatever is wired after it.
For an ESPHome switch, generic HA switch support is sufficient. An ESPHome number
also has a standard write operation, but its meaning must be declared before a
planner can use it: 10 A, 10 degrees and 10% are different instructions.

Make/model helps discovery and provides presets for complex controls. It is
optional metadata for generic controls, not a prerequisite or permission gate.

## Four facts that must stay separate

| Fact | Meaning | Example |
| --- | --- | --- |
| Control connection | What SHS can actually change | Relay on/off; temperature target; charger current |
| Planning objective | What the schedule should achieve | Run for two hours before 07:00; maintain room comfort; reach a charge target |
| Energy behaviour | How to estimate the resulting electricity use | Fixed running watts; changing power; thermostat cycling |
| Permission/status | Whether SHS may operate it now | Disabled; awaiting setup; waiting for plan; operating; overridden |

The existing website control-method and load-characteristic fields are useful,
with these boundaries. Selecting "Inverter load" describes energy use; it never
creates variable-power control. A relay can switch an inverter load. A smart
socket can switch a thermostat-controlled load. Neither requires SHS to identify
the appliance downstream of the switch.

Category is for grouping/reporting and initial suggestions. It must not secretly
select a different executor or planning objective. Room identity and pool/EV
association belong in explicit planning setup, separate from a category label.

An on/off switch plus estimated watts is enough to execute a schedule, but not
always enough to choose a useful schedule. The objective supplies the missing
reason to run it; otherwise a cost optimiser can simply leave it off forever.

## One control definition, with clear ownership

Create a stable home-scoped control ID independent of names and energy-statistic
IDs. Link existing meters to it without replacing their history. Support
multiple channels per HA device and composed controls involving several HA
entities. Give each controllable actuator one SHS owner; overlapping groups,
companions and plant controls must be resolved explicitly.

| Information | Authority | How the other side uses it |
| --- | --- | --- |
| Entity identity, platform, supported operations, units, physical bounds/steps | HA | Website shows available choices and constraints |
| Entity bindings, number meaning, direction, supported mode mapping, handover behavior, local operating limits | Integration setup in HA | Website receives a validated abstract capability, not raw service calls |
| Inclusion, objective, schedule/comfort preferences, load characteristic, planning estimates | Website | HA receives versioned desired planning settings |
| Actual states, meter readings and live availability | HA | Inputs and operational feedback, never configuration overwrites |
| Learned energy estimates | Integration/model | Used when no explicit planning override exists; show source/sample count |
| Permission to write and manual override | HA | Website displays acknowledged operational status |
| Accepted configuration and execution readiness | Computed by validation | Neither UI may set these with an editable boolean |

Allow the website to supply information absent from HA, such as running watts,
energy required, deadlines, a load characteristic or desired temperature. Each
field has one owner and a visible source: measured, learned or user supplied.
An override remains until explicitly cleared; subsequent discovery must not
silently replace it. A user estimate must not rewrite a measured sensor value.

Physical actuator limits come from HA. User operating limits can narrow those
limits, never widen them. Hardware units and declared semantic roles must agree.
Unsupported steps, empty limit intersections and unknown number meanings produce
a specific setup error, not a guessed conversion. A percentage-to-watts relation
requires an explicit model; amperes-to-watts requires its electrical parameters.

Keep frequently changing state separate from the definition: changing a relay
state, measured power or SOC does not create a new configuration version.

## Standard building blocks and composed controls

Implement reusable primitives for switch operations, bounded number writes,
select choices and the supported climate operations. Validate capabilities at
setup and again at execution. The domain alone does not mean every optional
operation is available, and a number's unit alone may not establish its purpose.

Build a small set of explicit control contracts from those primitives:

- Relay schedule: on/off and optional reviewed minimum on/off durations.
- Permission: permit/inhibit, with declared polarity and maximum inhibit time.
- Temperature target: measured temperature plus Celsius target and limits.
- Adjustable output: current, power or percentage with declared conversion and
  any separate start/stop control.
- Pool service: heat/defer/release and a temperature objective through a customer
  automation interface; no SHS Nibe/pump sequencing.
- Battery dispatch: intent, modes, two ceilings, authority and measured feedback.

A Shelly or ESPHome relay uses the same relay contract; no custom brand adapter
or appliance identification is required. Standard controls need interface
validation and a normal setup check, not a certification exercise per model.

Sigenergy battery dispatch needs a composed contract because several ordinary
fields have coupled meanings and write ordering. Keep its semantics in tested
local code with declarative mapping presets. For the pool, the customer owns
Nibe bands, pump sequencing and triggers; SHS validates the request/feedback
interface and does not bind or reserve those internal actuators. Never infer that any signed number is a battery command. Never
silently substitute a generic contract for an incomplete composed one.

The catalogue therefore lists supported contracts, optional make/model presets,
observed interfaces and gaps. It can say "Shelly Pro 2PM: relay channels supported"
and "Sigenergy battery dispatch: implementation required". A new generic ESPHome
switch should work without adding its board model to a whitelist.

## How saving and planning work

Use explicit versions, not timestamps or equality of a control-method label:

1. A website save validates its fields and records a desired-settings revision.
   It uses the version the editor loaded; a stale editor gets a conflict instead
   of overwriting a newer edit. A multi-field method change is saved together.
2. HA receives that request and validates it against the current local mapping,
   capabilities, limits and permission. HA setup edits are persisted locally
   before they can be acknowledged remotely. Interrupted exchanges retry the
   same revision idempotently; old responses cannot supersede newer ones.
3. The accepted record identifies the exact website revision, local control
   revision and contract version. HA reports either agreement or specific
   missing/invalid fields. Readiness belongs to that exact combination.
4. The server builds a plan against a snapshot of those accepted definitions and
   planning-model revisions. It checks that they are still current before
   publishing the plan. If settings changed during calculation, that result is
   discarded and recomputed.
5. Before every write, HA checks plan validity, local permission, live bounds,
   overrides and the referenced control revision. A plan targeting an old relay
   mapping or old number meaning cannot execute against the new mapping.

Keep a last acknowledged state and a separate pending edit. That is a visible
change transaction, not a legacy execution fallback. Configuration affecting
what may be written requires a new accepted definition and matching plan. Pure
planning changes can take effect at an explicit plan boundary while the existing
bounded plan remains valid; the UI must show which settings are active.

Plans combine household loads and grid headroom. A mapping failure is reported
against the affected control, but may require replacing the whole electrical
plan. Other controls may continue only where their existing authorization and
shared resource envelope remain valid. Do not promise isolation that violates
whole-home constraints; never execute a mixture of incompatible plan versions.

## What happens when someone changes something

| Change | Defined behavior |
| --- | --- |
| Friendly name or reporting category | Update presentation; keep stable identity, history, bindings and control semantics |
| Live SOC, temperature or power | Update runtime inputs; guards may stop a command and planning may refresh; no setup reset |
| Website load characteristic/running-watts estimate | Preserve control mapping; validate and rebuild energy model/plan; show pending until active |
| Website comfort target, deadline or runtime | Validate against accepted limits; replan; no hardware remapping |
| Control method, entity binding, numeric meaning or direction | Block old commands for that control; finish defined handover with old bindings; validate new definition; require matching plan |
| Local limit tightened | Apply new command guard immediately; interrupt a now-invalid request using the contract's handover behavior and replan |
| Hardware unit, supported modes or usable bounds change | Revalidate affected contract; pause with a precise reason if incompatible |
| Entity renamed | Resolve same registry identity to its new name; no user remapping |
| Entity removed/replaced | Mark missing; never select a similarly named replacement automatically |
| Website inclusion turned off | Request removal and local handover; show pending until HA acknowledges |
| Local permission turned off | Stop local plan execution and perform defined handover immediately, independent of website connectivity |
| Actuator changed outside SHS | Use explicit manual-override policy; yield the affected control rather than repeatedly fighting the change |

Track expected SHS writes separately from external state changes, including
confirmation/readback, so our own commands do not trigger manual override. For
compound controls use adapter-specific detection; natural measured battery flow
is not an external edit to its requested operating mode.

Remote changes cannot take effect while HA is disconnected. Deliver changes
promptly through a lightweight configuration synchronization path, separate
from expensive statistics/plan building. Use an explicit periodic revision check
with bounded authorization expiry locally. The accepted implementation plan uses
a 15-second authenticated revision poll and a 120-second renewable lease; no
push transport is required initially. Until HA confirms,
the website says "Waiting for Home Assistant" rather than claiming control has
stopped. HA expires unrenewed authority locally and performs its defined handover.
This still cannot run software restoration while the HA machine itself is down.

Handover is contract-specific: release to local control, restore a saved setting,
or restore a reviewed normal profile. It is not universally "turn off". Persist
ownership and handover details before writes; failed handover remains visible
and blocks reassignment of the actuator. Group changes must not silently take
ownership of new members. Physical constraints and independent controllers
remain outside the planner's ability to guarantee delivered energy.

## Changes to the website shown in the screenshot

Keep the table, but give its columns distinct jobs:

- Device: a user-friendly control name; make/model/HA connection in details.
- Category: grouping only.
- Include in plan: desired participation, with pending/active acknowledgement.
- Control method: choices supported by the discovered or configured connection;
  an unbound control can request a method, visibly requiring HA setup. Do not
  show every method as usable for an already bound switch.
- Load characteristic: energy-model setting, with source and user override.
- Running power estimate: distinguish this from "Power now" and show whether
  it is measured during operation, learned, or explicitly supplied.
- Status: explain setup, synchronization, planning and operation separately.

Put "What should the plan achieve?" in device details: timed run, room comfort,
charge target, pool temperature objective, etc. Reuse the same editor schema in
HA and the website for rendering relevant summaries, validation requirements and
field labels, while preserving field ownership. Avoid two separate collections
of guessed required fields.

Examples of honest statuses: "Choose controls in HA", "Set a running-power
estimate", "Settings syncing", "Ready to plan · Control off", "Following plan",
"Paused: manual change", "Paused: selected number now uses different units".
A single "Ready in Home Assistant" check is insufficient to express all of these.

## Fit with the existing code

The current code already has website-owned planning choices, HA-owned mappings,
shared mapping validation, per-device setup saves and ownership restoration.
Build on those rather than introduce a competing inventory/configuration system.

Concrete weaknesses visible in the current code:

- `EmpiricalDeviceModelsCard.tsx` checks mapping status and matching control type
  for readiness; it does not bind that badge to the exact settings revision.
- `device_controls.planning_path()` uses category and control type to choose
  room/pool/boiler/EV planning. Replace implicit category routing with an explicit
  objective and validated contract association.
- Configuration refresh clears the plan when returned choices change; the
  controller compares whole option dictionaries for restoration. Use typed,
  scoped change classification rather than treating every field alike.
- Mapping save reports the proposed mapping to the server before persisting HA
  options. The revised acknowledgement protocol must not report that proposed
  state as locally active before it is durably saved.
- Supported primitive writes already exist in `ScheduledController.command()`.
  Extract/reuse their contracts rather than implement a controller per brand.

## Initial catalogue and delivery

The 10 September read-only survey covered all 378 registry entries, including
software devices and duplicate observations. Relevant interfaces include Shelly
relay/meter families, Moes MQTT/Zigbee2MQTT switches and thermostats, Daikin climate
entities, an ESPHome pool-pump switch, Easee Home, Tesla Model Y via Tessie, Nibe
S1256 and Sigenergy SigenStor EC 12.0 TP. Some models/versions are missing; that
must not prevent generic interface support. No Bosch-labelled registry entry
was found; this establishes no conclusion about appliances behind relays.

Deliver in this order:

1. Define and test the shared contracts and field ownership; introduce stable
   control IDs linked to existing meters and local registry identities.
2. Discover available primitives, show supported methods, add missing semantic
   setup and explicit planning objectives. Seed the catalogue from this home,
   preserving generic support for other homes without brand entries.
3. Implement revisioned saves/acknowledgement, plan binding, conflict handling,
   prompt configuration sync and clear pending/active status on both sides.
4. Test edits while running, delayed/reordered responses, offline saves, entity
   rename/removal, group membership changes, changing bounds, external edits,
   restart and failed restoration. Preserve energy accounting exactly once.
5. Add the accepted Sigenergy composed contract and connect the customer pool
   request/feedback interface using these shared foundations. Commission requests,
   observations and handover without moving Nibe/pump automation into SHS.

Use the project's explicit configuration/plan-schema migrations. Preserve history
and explicit user choices; carry forward mappings only where their meaning is
unambiguous, otherwise show the exact setup gap. Do not change live permissions
or introduce compatibility execution paths as part of this design work.
