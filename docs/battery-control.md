# Sigenergy battery control design

Updated 2026-09-10 from Phil's installation observations and explicit operating
policy. Status: accepted design for implementation, not a claim that the current
executor implements it or that every transition has been tested. This supersedes
the earlier mode-plus-signed-power design. Battery is the first device to control;
this documentation change does not enable control or authorise live test writes.

## Operating policy

| Intent | Remote EMS Control Mode | Battery limits and expected behaviour |
| --- | --- | --- |
| Normal operation | `Maximum Self Consumption` | PV serves house demand; surplus charges the battery, then exports when it cannot be stored. Battery supplies the house deficit without exporting battery energy. Grid does not charge the battery. |
| Solar-only charging | `Maximum Self Consumption` | Set the charge ceiling. This mode also permits battery discharge for house demand; it is not a promise to hold SOC when PV falls. If discharge must be prohibited, apply a zero discharge ceiling and verify that combination during acceptance. |
| Charge with PV priority and grid top-up | `Command Charging (PV First)` | Set the total ESS charge ceiling. Prefer PV and use grid to supplement charging. |
| Charge with grid priority | `Command Charging (Grid First)` | Set the total ESS charge ceiling. Grid priority is not a ban on PV charging. Phil expects surplus PV to contribute; exact allocation in that condition remains to be observed. |
| Discharge for house supply and deliberate export | `Command Discharging (PV First)` | Set the total ESS discharge ceiling. Preserve PV priority; house demand is served before remaining generation is exported. The ceiling includes battery energy serving both house and grid, not just export. |
| Hold battery idle | `Standby` | Phil reports that PV continues supplying the house while the battery does not contribute. Surplus-PV export is expected but explicitly untested. Confirm no battery charging as well as no discharging in acceptance. |
| Relinquish optimisation | `Maximum Self Consumption` | Restore reviewed normal ESS limits as well as the mode; temporary limits must not persist into normal operation. |

Never select `Command Discharging (ESS First)` for ordinary optimisation or grid
export: it may curtail PV. Maintenance operations that deliberately prioritise
emptying the battery are outside this controller's scope. Do not select `PCS
Remote Control`, `V2G`, or unknown modes as alternative execution paths.

`Maximum Self Consumption` is the normal operating policy, not an explicit zero
battery request. An authorised hold uses `Standby`. Missing or expired authority
causes handover to normal operation, not a synthetic hold request.

## Actuators and measurements

Reference installation entity IDs are below. Store mappings rather than assuming
these names on every installation.

| Purpose | Entity |
| --- | --- |
| Behaviour | `select.sigen_plant_remote_ems_control_mode` |
| Total charge ceiling, non-negative kW | `number.sigen_plant_ess_max_charging_limit` |
| Total discharge ceiling, non-negative kW | `number.sigen_plant_ess_max_discharging_limit` |
| Claim Remote EMS | `switch.sigen_plant_remote_ems_controlled_by_home_assistant` |
| Authority confirmation | `sensor.sigen_plant_ems_work_mode` must report `Remote EMS` |
| Actual battery power | `sensor.sigen_plant_battery_power`, positive charging / negative discharging |
| SOC | `sensor.sigen_plant_battery_state_of_charge` |
| Hardware discharge floor | `sensor.sigen_plant_discharge_cut_off_soc` |
| PV and house load | `sensor.sigen_plant_pv_power`, `sensor.sigen_plant_total_load_power` |
| Actual grid exchange | `sensor.sigen_plant_grid_import_power`, `sensor.sigen_plant_grid_export_power` |

`number.sigen_inverter_active_power_fixed_adjustment` is **not a signed battery
power command**. Phil observed positive values constraining discharge and negative
values failing to force charging. Do not write it for charge, discharge, hold, or
handover in this design. Record its existing setting during commissioning because
an independent inverter constraint can prevent the requested behaviour. Its exact
effect across modes is not inferred from the signed writable range.

`binary_sensor.sigen_plant_exporting_to_grid` reports measured export; it is not
an export-enable switch. The same applies to the importing/charging/discharging
binary sensors. Use numeric measurements to confirm allocation.

The plant also exposes `number.sigen_plant_grid_import_limitation`,
`number.sigen_plant_grid_export_limitation`, and
`number.sigen_plant_pv_max_power_limit`. These are whole-plant constraints, not
independent battery source/destination controls. Preserve installed constraints;
do not set grid import to zero to implement solar-only charging, or grid export
to zero to prevent battery export: use self-consumption policy for those intents.
Any future allocator writes to plant limits require explicit ownership and
restoration. Routine battery dispatch must not curtail PV.

## Planner and executor contract

Express the battery instruction as an operating intent and two non-negative
charge/discharge ceilings in W. Convert to kW only at the HA actuator boundary,
rounding down to supported steps. Direction belongs to the intent, not the sign
of a writable number. Measurement retains its own sign convention.

Net planned battery watts alone cannot distinguish solar-only charging from
grid-assisted charging, house-only discharge from export, or hold from normal
self-consumption. Extend the versioned executable plan contract to carry that
intent; validate it on both server and integration. Do not infer source policy
solely from a forecast or silently translate missing intent into a mode. Keep
forecast battery energy/power separate from the limits the hardware is asked to
enforce. Battery remains a plant-level store, not a second schedulable house load.

Clamp ceilings to reviewed ratings and currently permitted SOC/grid headroom.
Reference ratings are 8.8 kW charge, 9.6 kW discharge and 13.2 kW plant active
power. Respect the measured hardware SOC floor (5% at the survey), operating
reserve, upper SOC and export reserve. Those are distinct policies. A ceiling is
not a guarantee of delivered power; available PV, demand, SOC, plant constraints
and internal protection can reduce actual power.

Treat each intent and its two ceilings as one desired state. Define both ceilings
explicitly, including zero where that direction is prohibited; do not inherit a
stale cap from a previous slot. Validate the entire state before writing. Capture
ownership and a restorable normal profile first. Verify remote authority, apply
restrictive limits before entering a mode that could increase flow, and confirm
mode and limit readback. Only relax limits once the intended mode is confirmed.
Commission this ordering against delayed/failed writes; the registers are not an
atomic transaction. Passive observations of natural flow reversal do not establish
safe command ordering or a command-response timeout.

Use one executor and a shared mode-policy table rather than separate controllers
for each energy path. Reject unsupported intents, stale inputs, unavailable
controls, invalid bounds and incomplete mappings. Do not retain a signed-target
execution path as compatibility behaviour.

## Confirmation and handover

Distinguish accepted settings from observed operation. Confirm fresh mode and
limit readbacks, then evaluate measured power against the mode, ceilings and
actual PV/load. Do not require every mode to reach its ceiling within 15 seconds.
A self-consumption battery below its limit is not a failed actuator. Likewise,
successful readback alone is not proof of delivered charge or export. Record
constrained/shortfall outcomes and actual energy for replanning; calibrate
measurement tolerance and response timing through acceptance tests.

On disable, plan expiry, loss of required sources/authority, startup recovery or
orderly unload, hand back to `Maximum Self Consumption` and the reviewed normal
charge/discharge limits. Confirm settings and mode-consistent physical behaviour;
normal battery power can be nonzero. Never zero the inverter adjustment as the
handover action. Record a failure if handover cannot be completed rather than
claiming restoration succeeded.

The observed value `4294967.295` on unset plant limits is an all-ones sentinel,
not a valid power request. Never round-trip it through the number service.
Commission a valid normal profile or a verified supported reset operation before
owning those limits; do not silently substitute zero or a guessed unlimited cap.

Normal self-consumption is itself a Remote EMS mode. Returning to it does not
release the remote-authority switch. Hardware behaviour during abrupt HA/network
loss and explicit authority release still needs verification; no software write
can restore a plant while HA is down.

## Implementation and acceptance

The [step 5 adapter](battery-execution.md) now implements this policy with explicit
intent and ceilings, separate setting/physical observations, verified durable
ownership and reviewed normal handover. The signed executor and its configuration
have been retired through config-entry v5 migration; unresolved legacy ownership
blocks upgrade. Simulated HA verification does not commission the installation.
Local operation permission remains off until the bounded commissioning step.

Acceptance should cover:

- Each allowed intent with accepted mode/limits and measured PV, load, battery,
  SOC and grid flows; grid-priority surplus-PV behaviour remains an observation
  to collect, not a prerequisite to deciding the mode policy.
- Standby with PV both below and above house demand: verify zero battery flow,
  continued PV generation and the still-untested surplus export behaviour.
- Self-consumption: surplus-PV charging, house-deficit discharge, no grid charging,
  no battery export, and zero discharge ceiling when solar-only charging must
  also preserve the reserve.
- PV First command discharge: house served before export, correct total battery
  ceiling, and no routine selection of ESS First or PV curtailment.
- Rate changes, SOC/reserve boundaries, stale data, write rejection, partial
  transition, authority loss, restoration and restart recovery. Test restoration
  from every allowed active intent without requiring zero battery power.
- Planner/integration agreement on explicit intent and ceilings; old or missing
  intent is rejected. Test that limit-constrained delivery is distinguishable
  from a failed command and that no path writes negative ESS limits or the
  inverter adjustment.

These are implementation verification tasks. The household's normal mode, export
mode and prohibition on ESS First are settled by Phil's 10 September decision.
