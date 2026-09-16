# Live battery control and measured conversion losses

**16 September 2026: the production command path is implemented. Deployment and
physical testing have not been performed.** This replaces the earlier diagnostics-
only connection. [Implementation status](scoped-participation-implementation.md)
and [participation decisions](device-participation-and-battery-supply.md) describe
its relationship to the planner and other devices.

## What now operates the battery

The existing household controller remains the coordinator. Its battery owner joins
live readings, the cloud's future-cost policy, the durable command journal and the
Sigen adapter. In **Controlling**, it sends real `select.select_option` and
`number.set_value` calls. **Verification** evaluates the same choices without new
optimization writes. Previously issued commands must still be released when
leaving Controlling; changing a selector cannot undo a physical command.

The legacy forecast/rating command path is bypassed for this battery. A durable
writer fence and the household command lock prevent competing owners. Commands are
saved before sending, rechecked after acquiring the lock and checked against new
register reports. Restart restores the saved owner and approved release without
waiting for a cloud request. Release approval is renewed independently of the
operating-mode revision; a day-long outage cannot expire the only release path.
Missing Sigen entities delay HA setup for an automatic retry. Shutdown stops new
admission before draining the owner. Missing economic coverage withdraws optimization and
releases to Maximum Self Consumption with configured hardware ceilings. It does
not invoke an alternative forecast-based charging strategy.

Supported actions are Standby, solar-only charging, house supply, PV First forced
charging and explicitly enabled PV First export. Grid First is never selected.
Limits use one-watt battery-terminal DC increments. Candidate limits include
bounded fractions, live supply-scope limits and future energy-boundary candidates;
future prices and stored-energy value decide between them. There is no rule that
always charges cheaply or always discharges at maximum.

## Configuration and live evidence

Saved entity mappings determine meaning; this is not a new manual certification
workflow. The runtime requires instantaneous W/kW measurements for:

| Role | Installation mapping / convention |
|---|---|
| Gross house consumption | `sensor.sigen_plant_total_load_power`; before PV, excluding battery charging |
| PV production | `sensor.sigen_plant_pv_power` is the current discovery proposal; an explicitly saved inverter-PV mapping remains unchanged |
| Battery-terminal power | `sensor.sigen_plant_battery_power`; positive charging, negative discharging |
| Signed grid exchange | `sensor.sigen_plant_grid_active_power`; positive import, negative export |
| Planned appliances | Their configured, non-overlapping power meters, including Verification devices |

Battery SOC and cumulative grid import/export and battery charge/discharge energy
counters are also required. The counters retain the original policy source cut;
late delivery never treats receipt time as the start of accounting. Missing or
reset history remains explicit. Statistics are not used as live power readings.

Each live source keeps its own HA report time: maximum age 30 seconds, maximum
alignment difference 15 seconds. A missing Planned meter cannot become zero or
silently move into base consumption. Website demotion to Monitoring changes the
partition; Verification alone does not.

Sigen control entities can remain unchanged for hours. The integration explicitly
refreshes their Sigen coordinator, checks the actual holding-register fields and
publishes that readback. It does not borrow a power sensor's timestamp or accept
the vendor number entity's default zero when a register is absent. All three
native controls must be the corresponding Sigen registers on one coordinator;
this check does not require household appliance meters to share an HA device.

The [Sigenergy Modbus V2.9 protocol](https://github.com/TypQxQ/Sigenergy-Local-Modbus/blob/main/Modbus_reference_documentation/Sigenergy%20Modbus%20Protocol%20EN_V2.9.pdf)
defines ESS ceilings at battery connection point 2, distinct from inverter AC point
3 and grid point 4. The adapter reduces both ceilings before changing EMS mode,
then installs the chosen ceilings. Conservative transport/effect reconciliation
uses 75-second bounds per assignment; a multi-setting transition or recovery can
take several minutes. A service acknowledgement alone does not confirm a physical
response. Schedule reports measured charging/discharging/idle separately from
accepted settings, including a requested direction that is not yet observed.

Before another SHS adapter changes a load, it reserves unknown pending demand. A
queued battery increase cannot bypass that reservation while awaiting the shared
lock. Existing grid charging must be relieved before admitting that load. The real
non-battery frame and incremental battery reservation are added once; the earlier
suspected double-counting issue required no change to that accounting.

## Loss measurement

The model uses stable, aligned, complete five-minute HA statistics from the last
two days, refreshed hourly. The raw signed installation balance is:

`loss = PV + grid import/export − battery charge/discharge − house consumption`

Do not fit the clipped display percentage. That percentage combines fixed standby
consumption and variable conversion loss, so it cannot be multiplied by every load.

For isolated night-time discharge, input is `−battery power`; output is
`house power − signed grid power`. Grid import must therefore be subtracted even
when there is no PV or export. For isolated grid charging, input is
`grid power − house power`; output is positive battery power. The fitted relationship
is `output = max(0, gain × input − fixed overhead)`. It describes the reported
installation boundaries, not cell-internal round-trip efficiency.

A fit needs at least six stable windows, 0.2 kWh input and a 500 W spread in input
power, plus physically plausible coefficients and a bounded residual error.
Otherwise the configured directional efficiency remains explicitly labelled as
an initial assumption. Idle overhead is measured separately when enough isolated
windows exist; zero unmeasured idle overhead is labelled in diagnostics.

Read-only inspection of this installation yielded 139 suitable discharge windows:
approximately **161 W fixed overhead plus 1.3% of battery power**, with about 22 W
fit residual. These numbers are evidence from that sample, not hardcoded defaults.
The runtime recalculates its own versioned model. Available grid-charge windows
were too similar in power to distinguish fixed from variable loss reliably.

**Solar conversion remains an approximation.** `PV − AC house consumption` does
not independently identify PV-to-battery DC efficiency. Solar residuals are
reported as observations but never promoted to a pure DC efficiency fit. This
version uses reported PV in the site-balance model; it does not separately model
PV-to-house AC conversion. Configured solar charging efficiency remains labelled.
Night-time charge/discharge fits do not remove that daylight limitation.

The same versioned curves travel to the backend and are used for current action
costs and future continuation scoring. Battery energy uses DC power; grid costs
and household delivery use converted power; wear retains its declared basis.
Fixed overhead is counted once per active branch. Converter activation points
have explicit point/interval domains, including their discontinuities.

## Deployment and test sequence

1. Apply the battery-projection migration and deploy the protocol-3 planning worker,
   ingest and `energy-battery-policy` endpoint together. The plan now carries an
   explicit `battery_supply_scope`; current generated plans choose `whole_house`.
2. Install the corresponding HA beta. Save the signed grid-power mapping and verify
   that all configured instantaneous sources report at the required cadence.
   Discovery does not overwrite an existing PV selection.
3. Generate a fresh plan. A policy belongs to its original current quarter; an old
   plan cannot authorize this connection simply by advancing to its next display slot.
4. Select Controlling for the Planned battery to exercise real commands. Verification
   deliberately sends none. Inspect current choice, native target, actual battery
   response and measured/configured loss provenance in Schedule and diagnostics.

Software tests cover real adapter service calls against fake HA, Verification,
missing/stale sources, failed journals, release without an economic policy,
cloud-independent restart, external-load coordination and provider/consumer
numerical parity. They do not establish physical latency or efficiency accuracy
on the installed inverter. No deployment, settings change or live battery command
was made while implementing this change.

## Design review and software validation

The independent Claude/Codex review retained the existing future-cost policy and
durable household writer, adding an explicit DC response model and production
host. A price-only override or an unconditional maximum-discharge command would
lose the agreed future-value and supply-scope behavior.

Validation for this change: 697 integration tests, 61 configuration-panel tests,
1,155 backend tests, 32 local browser tests and the test-mode website build passed.
Targeted TypeScript checks passed. Repository lint has zero errors and 26 existing
warnings; the build retains its bundle-size warning. Generated AC and DC
provider/consumer fixtures match byte-for-byte. Fifteen production-owner tests
include a 25-hour outage, delayed Sigen startup and shutdown admission fencing.
