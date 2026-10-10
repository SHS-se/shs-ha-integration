# SHS Energy

The app owns SHS configuration, planning exchange, controller decisions, operational
accounting and diagnostic downloads. The companion integration supplies Home
Assistant readings, executes acknowledged native commands and exposes SHS entities.

## Installation and access

1. Add `https://github.com/SHS-se/shs-ha-integration` under **Settings → Apps →
   App store → Repositories**, then install **SHS Energy**.
2. Use **Open web UI**. Enable **Show in sidebar** for the **SHS** entry. The SHS
   shield appears in the app store, app details, web header and browser icon.
3. Install the bundled companion when requested by the release instructions. The
   **Install companion** option runs the checked installer at the next app start.
   Check its result before restarting **Home Assistant Core**. Turn the option off
   after installation.
4. For a new home, add **Smart Home Solutions Energy** in **Settings → Devices &
   services** to pair the account. Configure energy sources, devices and control
   mappings in the app's **Settings** screen.

An existing integration-only installation needs the coordinated one-off migration
before app control can start. Installing an app is not permission to rerun a completed
migration. Preserve its source fence, activation identity and migration backups.

## Updates and compatibility

Routine app updates restart only the SHS app. Configuration changes apply through
acknowledged revisions without a Core restart. HA mode entities use the same app
configuration writer. During an app outage, SHS entities become unavailable and
native commands are fenced until the connection has reconciled.

A companion code update still requires a Core restart to load new Python modules.
Use the companion bundled with the app for a coordinated protocol upgrade. Live
compatibility is checked by protocol and capabilities, separately from the immutable
version pair recorded by the original migration.

The installer checks file hashes, stages replacements and journals the swap. It
preserves previous files outside `custom_components`. Unknown modifications stop
installation with an explicit error. Inspect old installation journals and backups
before replacing them. HACS can install a companion release, but does not coordinate
an app protocol upgrade.

## Screens and history

- **Overview:** plan readiness, current operation and measurements.
- **Schedule:** linked price, power, consumption, storage and temperature charts, with
  interval inspection and a table view.
- **Controller:** requested actions, measured outcomes and explanations.
- **System:** container resources, active database tables and sizes, receipt
  processing, query metrics and downloadable controller diagnostics.
- **Settings:** energy sources, device configuration and native control mappings.

Detailed diagnostics have a maximum three-day retention window, with additional
sample/count limits. Exact operational meter and objective facts needed for
accounting and late corrections have a separate lifetime. Migration archives and
upgrade backups are shown separately from working databases. Free SQLite pages
are reusable space; retention does not necessarily shrink a database file.

## Troubleshooting

- **Reconnecting:** check that Core and the companion are running; inspect app logs.
- **Protocol mismatch:** install the companion required by the app release.
- **Configuration not applied:** use the highlighted field and its linked editor;
  persisted and acknowledged settings revisions are reported separately.
- **Unavailable readings:** inspect the actual measurement sources in Settings.
- **Install failed:** read the precise installer result; do not remove journals or
  backups without inspecting them.

Access uses Home Assistant Ingress, without a host port or external browser token.
The HA API carries readings, entity projections and native commands. The writable
configuration mount supports explicit companion installation and storage inspection.
Keep **Install companion** off during routine app updates. Container resource counters
reset on restart; filesystem free space is shared storage, not a private allocation.

## Logs and profiling

In the app's Home Assistant **Configuration** tab, set **Log level**, save and
restart the app. The default **Info** logs startup/recovery progress and a minute
summary of process CPU (percent of one core), current resident memory, processed
receipts, receipt backlog, checkpoint saves and evidence-query groups. The first
sample establishes a baseline; rates start with the following sample. Each line
states the actual interval. A runtime reconnect resets the profiler's counters.

Connection failures include their traceback at Info, including the companion's
original receipt-persistence cause. A changed failure is logged immediately;
unchanged failures are summarized every twelve retries, and successful recovery
is logged once. App logs persist under `/data/logs/shs-energy.log` across app
restarts, with three rotated files and a total limit of 20 MiB. Download all
retained files through the app's Ingress URL followed by
`api/diagnostics/app-logs.zip`, even while the runtime is disconnected.

Info also reports ordered and replaceable source rates, native fact transactions,
and the app process's disk-write bytes and write calls for each interval. These
show whether irrelevant events are filtered and checkpoint work is reduced.
Disk bytes come from Linux process I/O counters; write calls include sockets too.
The companion's HA process I/O must be measured separately. See
[event processing and measurements](../../docs/event-processing.md) for the
processing cadence, reproducible replay and live comparison limits.

**Debug** adds per-operation wall time, synchronous CPU time, counter deltas,
projection sizes, dashboard access requests and exception traces. **Warning**,
**Error** and **Critical** progressively reduce output. Profiling messages contain operation names and counters; they do not dump
credentials, sensor readings or command payloads. Debug also enables the existing
detailed controller messages.

Use **System → Download controller diagnostics** for the full existing profiler,
its latest interval deltas and 120 minute samples. App timings now include receipt
processing, projection building, dashboard serialization, HA history/statistics
queries, cloud refreshes, planning exchange and diagnostic generation. Evidence
queries are broken down by purpose, including meter neighbours and energy ranges.
Those counts are logical query groups; a group can execute several SQL statements.

CPU is measured only within synchronous sections, on the thread doing the work.
Async spans measure wall time, including waits. Spans and query groups may overlap;
do not add their times to attribute total process CPU. The profiler only reads
small counters and bounded samples; it never traverses lifetime evidence for logs.
Debug does not turn on the separate, opt-in allocation tracer.
