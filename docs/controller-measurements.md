# Controller baseline measurements

Keep devices in **Control verification** while collecting the baseline. Install
the beta and let it run across several battery and pool slots, then download
either Home Assistant integration diagnostics or the control verification file.
Both contain `controller_metrics`; HA diagnostics also contain the existing
`network_traffic` counters. Collection sends no additional requests to the website.

Counters start at integration load and reset on reload/restart. Download them
before upgrading. They are aggregated in memory, with no per-tick history or raw
sensor values in the metrics export. Generic devices have opaque identifiers;
battery, pool and EV keep their system names. Historical verification records can
span versions, but these counters describe only the current integration session.

Beta.71 is the polling baseline. From beta.72, entity events and one-shot deadlines
replace the five-second sweep and the 0.5-second confirmation polling. Keep the
same devices in verification mode for the comparison. Export beta.71 before
upgrading; collect beta.72 for a comparable period and compare per-hour rates,
device modes and slot/operation coverage, not just raw totals.

`triggers` separates startup, five-second timer (beta.71), slot boundary,
coordinator update, state changes, stale-source recovery reports, freshness
deadlines, plan expiry, device deadlines, restoration retries and manual calls.
An event batch with multiple causes is labelled `coalesced`.
Each bucket records requested runs, busy/inactive skips, completed
runs, total and maximum elapsed milliseconds, and runs longer than five seconds.
Average elapsed time is `elapsed_ms / completed` when `completed` is nonzero.
Completed means the run finished, including runs that reported a control fault.

`devices` measures each device's evaluation, including eligibility checks for
disabled devices. Its `modes` counts distinguish these from verification/control.
The first evaluation establishes a baseline. Later evaluations count as either
`changed_inputs` or `unchanged_inputs`; `unchanged_inputs_new_reports` is a subset
of unchanged evaluations where report timestamps advanced. The inputs include
shared configuration/current-plan context, local ownership and the first real
state/attribute reads during each evaluation. Simulated writes do not become
observations. Shared configuration changes can mark multiple devices as changed.

These are elapsed wall times, including asynchronous waits and measurement
overhead, not CPU measurements. The export does not capture every sensor event
between evaluations. Unchanged inputs do not prove an evaluation can be removed:
freshness deadlines, plan expiry and run-time protections can become due without
any value changing. Metrics neither change scheduling nor enable live control.

The event scheduler additionally exposes `scheduling`: relevant state-change,
unchanged-report and entity-registry event counts; notifications combined into
pending work; notifications queued while busy; and latency from the earliest
queued local notification to each affected device's evaluation. These additions
are not available retrospectively for beta.71. They measure local dispatch delay,
not the equipment's sampling or network latency. No full sensor event history is
retained. Live hardware write volume is not inferred from dry-run commands.

## Event-driven control

The scheduler subscribes to the actual entity dependencies read by each device,
including unavailable entities and raw sources of Filter sensors. Changed values,
attributes and registry entries schedule only affected devices. Unchanged reports
refresh freshness deadlines and wake active command confirmation; they only
request a full evaluation when a stale source recovers. Notifications arriving
during execution are queued and coalesced, not discarded because the lock is busy.
Plan/configuration coordinator updates and slot boundaries evaluate all devices.

One-shot deadlines cover slot boundaries, exact plan expiry, sensor freshness,
minimum relay run times and maximum continuous inhibition. Confirmation waits
for relevant reports/changes or the existing 15-second timeout. Dependencies and
timers are removed on remapping or unload. Local cloud-independent execution and
the existing control validation rules remain in place.

Only failed real handover keeps the existing five-second retry interval: service
availability may recover without a state event. That timer exists only while
restoration is pending. Verification's hypothetical handovers do not arm it.
Website API exchange intervals and device integrations' own polling are unchanged.
