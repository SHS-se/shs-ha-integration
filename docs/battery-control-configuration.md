# Battery configuration and execution — 11 September 2026

Integration/device selection remains a proposal in [the catalog notes](device-catalog-design.md).
It is not implemented by this change.

## Customer-facing configuration

- Rated capacity and physical power/cutoff limits accept a sensor or a number.
  Sensor selections retain entity lookup and use the sensor's reported unit;
  literal capacity is in kWh and literal power is in W.
- Maximum charge is removed. The physical full-charge boundary is 100%.
- Charging and discharging binary sensors establish measured direction. Power
  comes from the absolute magnitude of the selected power sensor, so either the
  signed or inverted sensor works. Contradictory observations produce an error.
- Mode choices come from the selected EMS selector's actual options. This checks
  valid option names; manufacturer mode semantics still need established mappings.
- Separate non-negative charge and discharge limit controls use their reported
  W/kW units, bounds and step. There is no customer command-sign setting.
- Optional settings can be removed. Saving removal persists an explicit empty
  value so an initial default cannot silently reappear. Required missing inputs
  remain visible as configuration errors. Charger voltage/phases stay internal.
- The three manual authority fields are removed, including saved values during
  configuration migration. No automatic manufacturer authority adapter is added
  here. A rejected setting or missing forced-operation response reports a control
  fault. Future adapters should handle manufacturer prerequisites automatically.

## Executable contract

Snapshot/plan schema 8 adds `battery_command` to every slot: null without a battery,
otherwise an operation, both non-negative ceilings in W, and explicit grid-charge
and battery-export permissions. The command object has its own version (1).
Charge/discharge forecasts remain separate from the executable operation. A
watt-only battery request cannot operate this controller.

| Operation | Configured mode | Charge ceiling | Discharge ceiling |
| --- | --- | --- | --- |
| `self_consumption` | Baseline | Rated charge power | Rated discharge power |
| `solar_charge` | Baseline | Planned charging | 0 |
| `grid_charge` | Charging | Planned charging | 0 |
| `supply_house` | Baseline | 0 | Planned house supply |
| `export` | Discharging | 0 | Planned total discharge |
| `hold` | Hold | 0 | 0 |

For the established Sigenergy setup, baseline is **Maximum Self Consumption**,
charging is **Command Charging (Grid First)**, export is **Command Discharging
(PV First)**, and hold is **Standby**. ESS First is not the established export
mapping. Supplying the house does not invoke forced export mode.

The planner enforces export permission, published-price eligibility, minimum
export price and reserved SOC during allocation and settlement. House supply can
use energy below the export reserve, down to the physical cutoff. The controller
rechecks current rated powers, SOC, and export permission/price/reserve before
writing. Contradictory permissions, ceilings or forecast allocations are rejected.
Locked schema-8 quarters must retain their explicit commands; an older locked
plan without those commands must be rescinded rather than guessed.

A mode transition closes both ceilings before changing mode. Repeated identical
requests do not cycle the controls. Outgoing limits are rounded down onto the
actuator step and checked against its bounds. Previous register contents,
including unset sentinels, are neither a setup requirement nor restoration data.

Service completion, register acceptance and measured response are distinct.
The controller verifies each setting and waits for fresh power/direction reports
after writes. A forced charge/export request over the 100 W measurement tolerance
must show the requested direction. Wrong direction, exceeded ceilings or absent
forced response produce a fault. Autonomous self-consumption/solar/house-supply
may legitimately deliver less than the forecast; this is reported as limited.

## Approved handover

On disable, expiry, override, fault, unload, or restart with an ownership journal,
return to **Maximum Self Consumption** and write both ceilings from the configured
rated-power sources. The reference installation currently reports **8.8 kW charge
and 9.6 kW discharge**. These are sensor values, not hardcoded restoration values.

The journal preserves the mapping before the first write. Handover resolves its
rated sources again, even after restart, and never replays prior register values.
Missing rated sources or failed writes leave a visible restoration-pending fault
and retain the journal for a retry. Handover does not depend on a valid capacity
or cutoff reading. Register readback and a new power observation are required;
normal self-consumption power may be nonzero.

## Validation and rollout

The generated schema-8 fixture is validated by both the producer and the Home
Assistant consumer. Tests cover operation mapping, export restrictions, sentinel
replacement, fresh rated-source handover after restart, unsupported old requests,
command rejection, physical non-response and independent controller failures.

Deploy the schema-8 planner/API support before installing the corresponding HA
beta. The integration deliberately rejects a server that cannot accept its new
snapshot and return a readable plan. Existing older client schemas remain served;
this change adds no inferred translation for old battery commands.

Unit tests and register acknowledgments do not prove hardware behavior. Remaining
installation checks include Standby with surplus PV, Grid First with surplus PV,
self-consumption with zero discharge ceiling, transition timing and HA outages.
Graceful shutdown can perform handover; an abruptly stopped HA process cannot
write to the inverter. No live battery control was enabled by this code change.
