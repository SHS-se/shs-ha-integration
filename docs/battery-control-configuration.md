# Battery configuration and execution

## Replacement supply semantics — 15 September 2026

The operation/mode tables below describe the current native adapter. The target adds explicit None/Whole house/Base/Selected/Base+selected house-supply scope, separately from forecast watts, economic action and hardware ceilings. The adapter must enforce the measured eligible-deficit bound for every operation that supplies the house, including forced routes. Solar attribution must be explicit; no forecast/rated-power fallback is introduced. Existing handover is a separate approved ownership protocol.

See the [agreed participation and battery supply specification](device-participation-and-battery-supply.md).
Documentation only; replacement implementation and coordinated rollout remain pending.

Current schema-8 implementation record (11 September), with target-design corrections
on 13 September 2026 and the forced-charging mapping corrected from installation
evidence on 15 September. Current and target lifecycle behaviour are distinguished below.

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
and battery-export permissions. The command object has its own version (2 and 3
are accepted; see [rollout](battery-intent-v2.md#rollout)).
Charge/discharge forecasts remain separate from the executable operation. A
watt-only battery request cannot operate this controller.

| Operation | Configured mode | Charge ceiling | Discharge ceiling |
| --- | --- | --- | --- |
| `self_consumption` | Baseline | Rated charge power | Rated discharge power |
| `solar_charge` | Baseline | Rated charge power | 0 |
| `grid_charge` | Charging | Planned charging | 0 |
| `supply_house` | Baseline | Rated charge power | Planned house supply |
| `export` | Discharging | 0 | Planned total discharge |
| `hold` | Baseline | Rated charge power | 0 |
| `idle` | Hold | 0 | 0 |

A charge ceiling under the baseline mode is a permission, not a request. Whether
to spend stored energy and whether to absorb surplus are separate questions, so
`hold` and `supply_house` keep the rated charge permission and the plant decides
how much real surplus to take. Only `grid_charge` sizes its charge ceiling,
because that ceiling bounds a purchase. Schema 2 gave `hold` both ceilings at zero
and the hold mode; schema 3 moves that meaning to `idle`, which the planner emits
only when it compared storing the surplus against selling it and chose the grid.
Commands are validated against the schema they declare, so a schema-2 `hold`
keeps its original meaning.

For the established Sigenergy setup, baseline is **Maximum Self Consumption**,
forced charging is **Command Charging (PV First)**, export is **Command Discharging
(PV First)**, and hold is **Standby**. ESS First is not the established export
mapping. Supplying the house does not invoke forced export mode.

**Command Charging (Grid First) is excluded from normal SHS operation.** On the
reference installation it curtails solar production to charge from the grid.
Phil explicitly rejects that behaviour for every normal operating scenario.
Forced/grid-assisted charging must use **Command Charging (PV First)** so available
solar remains productive while grid power supports deliberate replenishment.
The `grid_charge` operation authorises grid support; it does not require a
Grid First hardware mode or permission to suppress solar in favour of grid energy.
This supersedes the earlier Grid First mapping and any claim that either charging
mode is acceptable. The charge ceiling remains a total ESS ceiling; this correction
does not promise additional solar charging above that ceiling.

Installation evidence supplied on 15 September (`history.csv`, timestamps UTC):
with the charge ceiling fixed at 3.0 kW, PV was 0.948 kW and grid import 2.659 kW
immediately before selecting Grid First at 07:08:09.508. At 07:08:14, PV was zero
and import 3.753 kW. Selecting PV First at 07:08:21.700 was followed at 07:08:27 by
0.936 kW PV and 2.661 kW import. This observed reversal supports the mode correction;
it does not establish every transition, response bound or outage behaviour.

The planner enforces export permission, published-price eligibility, minimum
export price and reserved SOC during allocation and settlement. House supply can
use energy below the export reserve, down to the physical cutoff. The controller
rechecks current rated powers, SOC, and export permission/price/reserve before
writing. Contradictory permissions, ceilings or forecast allocations are rejected.
Locked quarters must retain their explicit commands; a locked plan whose commands
predate the current version must be rescinded rather than guessed.

A mode transition closes both ceilings before changing mode. Repeated identical
requests do not cycle the controls. Outgoing limits are rounded down onto the
actuator step and checked against its bounds. Previous register contents,
including unset sentinels, are neither a setup requirement nor restoration data.

Service completion, register acceptance and measured response are distinct.
The controller verifies each setting and waits for fresh power/direction reports
after writes. A forced charge/export request over the 100 W measurement tolerance
must show the requested direction. Wrong direction, exceeded ceilings or absent
forced response produce a fault. Autonomous self-consumption/solar/house-supply
may legitimately differ from the forecast; confirmation checks the permitted
ceilings instead. Solar capture can stop normally when the battery is full.

## Current approved handover

In the current implementation, on disable, expiry, override, fault, unload, or restart with an ownership journal,
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

Unit tests and register acknowledgments do not prove hardware behavior. Two
earlier checks are now answered from the installation: Standby exports surplus PV
without charging (controller diagnostics, 20 September 2026, SOC pinned at 37.5%
while export rose to 0.71 kW), and self-consumption with a zero discharge ceiling
holds SOC while still absorbing surplus. Remaining installation checks include
broader PV First forced-charging conditions, transition timing and HA outages.
Graceful shutdown can perform handover; an abruptly stopped HA process cannot
write to the inverter. No live battery control was enabled by this code change.

## Planned evolution: economic policy and restart continuation

Decision updated 13 September 2026; not implemented here. Current non-baseline
schema-8 ceilings are validated against forecast allocations and remain binding
until a versioned replacement is supported. They must not be silently widened.

The replacement separates predicted draw from executable modes/ceilings and
physical state constraints. Actual house demand may exceed its forecast; useful
solar uptake may exceed nominal charging. Additional grid energy remains an
economic option when physically available and authorised, at its price. There
are no fixed per-slot grid energy budgets or automatically transferable nominal
allowances. Actual gross energy, battery state and outstanding obligations are
still reconciled across segments, replans and restarts. Export is separately
authorised; a forecast alone grants no source/destination permission.

One home runtime selects bounded complete household alternatives. These include
economically useful drawdown before intermittent PV peaks, leaving spare battery
capacity instead of exporting every peak from a full battery at near-zero prices.
Compare correlated subquarter paths, native response and future costs without a
fixed SOC target or daily-yield trigger. Both appropriate flow directions must
remain available for fast native buffering; SHS mode switching per cloud
is not the buffering mechanism. Their current
cost and conditional future consequences are derived from the final validated
whole-home trajectory with future recovery jointly rescheduled. Editable service
curves determine tradeoffs; no mandatory household shortage tiers or permanent
auction ranking. Unconditional protections remain binding; conditional service
reservations release only with an explicitly paired service sacrifice. Current
supply, wear and transitions are counted once. See the canonical
[economic policy](https://github.com/SHS-se/smart-home-solutions/blob/main/docs/energy-optimisation/controller-policy.md)
and [runtime](https://github.com/SHS-se/smart-home-solutions/blob/main/docs/energy-optimisation/reactive-controls.md).

The target uses a synchronous event-loop reducer for SHS shared bookkeeping,
not a global action lock. Controlling grants full SHS operational authority
over the supported device controls. Per-group tasks
perform I/O. [Shared-entity reconciliation](https://github.com/SHS-se/smart-home-solutions/blob/main/docs/energy-optimisation/control-reconciliation.md)
defines explicit not-sent/accepted/ambiguous outcomes, automatic correction of
external drift and bounded adapter-supported retries. An external mode change
does not suspend SHS or require explicit resume: recompute a valid whole-group
transition to the current SHS mode and ceilings. Do not blindly reopen limits
under the unexpected mode without accounting for its physical effect. Current
timeout/restoration behaviour remains current code until this protocol is implemented.

Mode-change paths include the temporary loss of supply while both ceilings are
closed. This needs extra relief only where actual grid/phase headroom cannot
cover it; no general overload claim is made. Private actuator-group tasks carry
issued steps and possible late effects while the home runtime continues making
short decisions. Timeout/cancellation alone cannot establish a stopped physical
operation or release reserved headroom.

Routine HA restart/reload becomes bounded suspension followed by durable journal
recovery, live-state reconciliation and adoption of unchanged valid requests.
It preserves operating modes, release progress and bounded retry state as well. It must not
reset devices to defaults, erase actual energy, reset deadlines or
cycle through baseline merely because HA restarted. Current startup/unload
handover above remains an implementation fact until this replacement is built.
Explicit disable/removal, genuine expiry and lost authority hand back control
through the approved device release transition. Leaving Controlling fences
further optimisation sends, while already-issued effects and handover remain
accounted for. An external edit during Controlling does not withdraw authority
or silently replace the captured baseline. Forced operations require bounded behaviour across suspension; unknown
or expired state cannot support an unconditional continuity promise.

Transient unknown/unavailable sources retain last-valid age and uncertainty;
continue only when bounds support the operation and recover automatically.
Persistent data/control faults remain visible. Their notification handling belongs
to the deferred notification framework and needs a full specification later.
A pending plan never extends authority.
Rated-limit baseline can consume a higher economic reserve. No independently
persistent higher backup reserve is requested for an unexpected HA outage, and
the inverter's read-only cutoff is unchanged.

Future confirmation judges the effective operation/envelope, separately from
forecast drift. No live command, schema, version, configuration, notification or
handover behaviour changes as a result of this documentation revision.
