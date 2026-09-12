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

`triggers` separates startup, five-second timer, slot boundary, coordinator update
and manual calls. Each bucket records requested runs, busy/inactive skips, completed
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
