# SHS app: interface and diagnostics design

Status: proposed design, 27 September 2026. Home Assistant OS first. This document
does not implement or deploy the app. See [the architecture and migration
design](app-container-design.md) for ownership, installation and implementation gates.

## Product structure

The app serves its own web interface through Home Assistant Ingress. Use the
website's React/TypeScript/Vite stack, with a small Python API serving the compiled
assets. Node is a build dependency, not a required production server. Reuse UI
primitives and pure SHS chart transformations through versioned shared packages;
do not copy website authentication, customer administration or entire page trees.

Five primary routes keep the growing product understandable:

| Screen | Main question | Content and subviews |
| --- | --- | --- |
| Overview | What is happening, and does anything need me? | Current power flow, operating mode, plan age, next changes, compact schedule, actionable issues |
| Schedule | What happened and what is planned? | Synchronized energy graphs; device action lanes; selected interval inspector; historical plan selection |
| Controller | Why did execution differ? | Plan/request/measurement comparison, device detail, decision reasons, command evidence, overrides, execution history |
| System | Is SHS healthy? | Resources, Database, Connection, Operations, Migration and versions |
| Settings | What should SHS use and be allowed to do? | Energy sources, Devices, Permissions, Connection, Appearance |

Use a horizontal primary navigation within HA's content area rather than a second
permanent wide sidebar. On a narrow screen use a compact menu, with secondary
navigation local to the current screen. Every screen, device, correction field
and selected interval has a stable URL. Browser back restores selection and range.
New ideas usually become a subview of these destinations rather than another
top-level tab.

Shared components: `AppShell`, `TimeRangeControl`, `IssueSummary`, `FieldEditor`,
`DevicePicker`, `MetricSeries`, `TimelineInspector`, and `EvidenceTable`. Parameters
express differences; independent screen-specific copies of the same form or chart
are not the design.

## Visual direction and existing branding

Use opaque neutral surfaces, SHS blue for navigation and primary actions, readable
tabular numerals, restrained separators, and explicit light/dark semantic tokens.
Reserve semantic colors for the same energy flows and statuses across all screens.
Keep supporting text readable; density comes from alignment and progressive detail,
not smaller type. Use locally bundled fonts or system fonts, not a runtime CDN.

The UX skill's refined search matched **Data-Dense Dashboard**, with Fira Sans /
Fira Code as a suitable typography option. Its sales landing-page recommendations
were not suitable and are excluded. The initial glassmorphism recommendation was
also rejected. Verified time-series guidance supports separate units, line styles,
direct labels, keyboard inspection and a data-table equivalent. Its generic
forecast confidence-band suggestion does not override SHS's constraint contract.

Reuse these existing assets first, as requested:

- `custom_components/shs_energy/brand/icon.png`: 256 × 256 SHS shield PNG.
- `custom_components/shs_energy/brand/icon@2x.png`: 512 × 512 SHS shield PNG.
- `config_panel.py`: existing panel glyph `mdi:home-lightning-bolt`.

Both image files were opened and inspected; no replacement graphic is needed for
the app header or app-store icon. The existing MDI glyph is the first sidebar
implementation to test, with the title **SHS**. Native sidebar glyphs and PNG brand
images are different interfaces; do not claim that assigning a PNG filename to
`panel_icon` works. Test reuse on the target HA version before proposing any new
vector asset. Exact shield-shaped sidebar branding remains a rendering check,
not a reason to redraw the supplied art in this design pass. HA documents local
[integration brand images](https://developers.home-assistant.io/docs/core/integration/brand_images/)
and the app's [sidebar configuration](https://developers.home-assistant.io/docs/apps/configuration/).

Provide **Show SHS in the Home Assistant sidebar** in Appearance, backed by the
Supervisor's own `ingress_panel` setting. Read its current value rather than keeping
a second preference. Also retain HA's standard app-page toggle. Validate access to
the self-options endpoint during the HAOS spike; no browser receives Supervisor
credentials. There should be one SHS sidebar entry.

## Chart engine decision

Recommend **Apache ECharts** for the new application. It is a browser JavaScript
library, not a Python plotting dependency. Python supplies typed data and handles
runtime/storage work. Visual quality comes from our layout, labels and interaction
design; no library is objectively the most beautiful.

| Candidate | Fit for this application | Decision |
| --- | --- | --- |
| ECharts | Composable chart types, linked time inspection, zoom, custom interval marks, Canvas/SVG choice | Preferred; prove the schedule with real SHS data before committing the rendering layer |
| Highcharts | Strong timeline products and built-in accessibility/keyboard support | Credible alternative; distribution/licensing needs separate evaluation |
| Plotly.js | Useful analytical plots and range controls | Better fit for an exploratory analysis workbench than this tightly composed operational UI |
| Existing custom SVG / D3 | Maximum control; website already demonstrates the desired appearance | Preserve SHS transformations; avoid independently rebuilding every interaction for the app |
| Recharts | Already a website dependency, useful for ordinary React charts | The actual reference schedule is custom SVG; its complex interactions would still require bespoke work |

ECharts documents [rendering choices](https://echarts.apache.org/handbook/en/best-practices/canvas-vs-svg/),
[axis interaction](https://echarts.apache.org/handbook/en/concepts/axis/) and
[descriptions/patterns](https://echarts.apache.org/handbook/en/best-practices/aria/).
Highcharts documents its [accessibility module](https://www.highcharts.com/docs/accessibility/accessibility-module);
Plotly documents [range controls](https://plotly.com/javascript/range-slider/).
These capabilities inform the recommendation; they do not prove our eventual
chart is accessible or fast. Keyboard controls and a table remain SHS responsibilities.

**Outcome, 2 October 2026.** The schedule was built in ECharts and compared with
the website on real SHS data. It was harder to read: one legend-less canvas of
similar lines, no band labels, and a tooltip listing every series. The Schedule
screen now draws the website's own five-panel SVG (`web/src/plan-chart/`), using
the website's geometry, price-band and power-flow modules unchanged, the same
palette, and the same tooltip. ECharts remains for the ordinary single-plot
observation and resource charts. `web/src/plan-chart/` imports nothing from the
rest of the app except the wire types, so it is the unit a shared package would
carry once the website's chart is ready to consume it too.

## Schedule: one time axis, progressively deeper evidence

Use the reference website's five aligned panels: price (currency/kWh), signed power
flows (kW), per-device consumption (kW), stored charge (%), and accumulated cost
(currency). Each quantity gets its own axis; one shared cursor and zoom range link
them. A distinct Now line separates measured history from the future, but each
series carries its own provenance: a forecast for an elapsed interval does not
become a measurement simply because time has passed.

Below the energy panels, show expandable device action lanes from the current HA
schedule. Actions retain text descriptions as well as colors. A selected quarter
opens an inspector on wide screens and beneath the chart on narrow screens:

| Evidence | Example of what the inspector explains |
| --- | --- |
| Planner | Requested charge/heat/run intent and its plan admission/issue time |
| Controller | Chosen request, configured limit, operating mode and recorded reason |
| HA dispatch | Not sent, rejected, service returned, or outcome uncertain |
| Device readback | The setting actually observed, with observation provenance |
| Physical measurement | Measured power/energy/temperature and coverage |
| Accounting | Deviation, relevant obligation and recovery disposition where available |

This ordering prevents a service receipt or requested power from being presented
as delivered energy. Verification/simulation traces are visibly hypothetical.
Inspecting history uses the plan that applied at that time, not today's plan
painted retrospectively. Archive admissions using a local identity plus digest;
`plan_id` alone is not unique in the existing domain.
Historical comparisons are available only where the relevant plan and execution
evidence were retained. Mark earlier periods without those records as unavailable;
do not reconstruct supposed past plans from current decisions.

Controls: home-local date/range, historical plan admission, device selection, common
zoom, Now, and a table toggle. Selecting or pinning a quarter updates all panels.
Keyboard arrows move by interval; visible buttons support zoom/reset without a
drag gesture. The accessible inspector contains the same information as hover.
Honor locale and currency rather than hardcoding SEK. UTC range boundaries plus
the home's timezone handle daylight-saving days with 23 or 25 hours.

Use steps for quarter-hour decisions and appropriate measured aggregation for
power. Preserve missing intervals, meter resets, source changes and estimated
prices. Do not smooth missing data, replace it with zero, or label estimates as
published prices. Aggregation preserves energy totals and annotations; a visual
point budget is not a new admissibility rule for controller data.

## Controller analysis worth adding

1. Planned and measured power/energy by device over the same period, with coverage.
2. Cumulative planned and actual energy deviation, alongside the recorded decision
   reason. Differences are information, not new forecast-validity checks.
3. Requested versus observed actuator setting, followed by physical response where
   measurable. Distinguish transport latency from device response time.
4. Battery plan/actual SOC, terminal energy versus stored-energy changes, and current
   accounting obligations. Show conversion assumptions where an estimate is used.
5. Manual overrides, permission changes, minimum-run restrictions, disconnects,
   plan admissions and replan reasons on a shared event timeline.
6. Actual cost to Now plus separately labeled projected remaining cost. Claimed
   savings require an explicit counterfactual; forecast error is not savings.
7. Measurement coverage and source issues, linked to the actual source entity.

No universal score is needed. Start with the first three, using the same query and
inspection components as Schedule.

## Resources and database diagnostics

Read HAOS container CPU, memory, block I/O and network counters from
[`/addons/self/stats`](https://developers.home-assistant.io/docs/api/supervisor/endpoints/).
Keep the source's normalization explicit. Process RSS, operation durations and
queue delay are separate metrics. App file usage under `/data` and available space
on its backing filesystem are different quantities; free space is shared, not a
private container allocation. A counter reset on restart starts a new segment.

Suggested displays: CPU/memory history; app data, logs and database bytes; available
disk space; runtime cycle and persistence durations; input backlog; cloud exchange
time; and browser/API query latency. Use bounded retention, visible last-sampled
time and unavailable states. Resource sampling does not run on each control tick.

Database view lists each database and schema version, tables/indexes, row counts,
page sizes, allocated/free pages, file size, and journal/WAL bytes when applicable.
SQLite's [`dbstat`](https://www.sqlite.org/dbstat.html) exposes per-table/index page
usage when enabled in the build. Require it in the selected app image and verify
that capability in CI; don't silently substitute misleading size estimates.
Collect expensive censuses in a background job and label their sample time.

Instrument named SQL operations for count, errors, rows, execution/fetch time,
transaction/commit time, queue delay and busy/lock failures. Store bounded
histograms and slow-operation samples. Do not log bound values, secrets or unlimited
SQL text. Do not relabel the entire query duration as lock-wait duration unless
lock wait is actually measured. Transactional row counters can serve hot status;
full counts and size scans are scheduled or requested explicitly.

SQLite has [profiling APIs](https://www.sqlite.org/profile.html), including features
requiring special compile options; it does not automatically retain the desired
historical dashboard. Start with application timing and predefined on-demand
`EXPLAIN QUERY PLAN` diagnostics. Detailed scan profiling is a separate diagnostic
build/capability decision, not a promised feature of Python's standard SQLite API.

## Data and interaction contract

API responses contain domain series, units, coverage, provenance, revisions and
events, never ECharts options or raw runtime checkpoints. Range queries are indexed,
paginated/bucketed and independent from control. The browser subscribes to small
view-revision notifications, then fetches only visible data. Unsubscribe on hidden
screens, cancel superseded queries and keep previous results visibly dated while
new data loads. The runtime continues without any browser connected.

Retain the complete [configuration error UX contract](configuration-error-ux.md):
all relevant field issues in one response; readiness and permission admission share
validation; direct route/card/field focus; conditional required fields remain visible;
manual navigation shows identical highlights; warning clearance follows revalidation.
A transport problem has a connection action, not a fabricated settings-field target.

Before accepting the UI, verify keyboard-only and touch inspection, focus behavior,
light/dark contrast, 44px controls, reduced motion, browser back/deep links, narrow
layouts, and a disconnected HA/cloud session. Replay the existing battery cross-section
correction case end to end. Compare the chart against reference SHS rows including
gaps, repeated plan IDs, late corrections and daylight-saving boundaries.
