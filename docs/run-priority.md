# Live priority across whole runs

**User requirement, 10 November 2026.** This replaces the charger-first relief
rule of earlier that day (reduce the car or battery before any heater setback).
Where [house import safety](house-import-safety-2026-10.md) or
[plan execution](controller-plan-execution.md) describe that order, this
document takes precedence. [Control continuity](control-continuity.md) is
unchanged and still takes precedence over everything here.

## What decides

The website planner owns prices, schedules, targets, comfort requirements and
authorised recovery windows. Home Assistant uses live data to decide which
eligible load yields power right now. Nobody ranks devices by hand.

The quarter says what is authorised now. The whole heating or charging run says
what is urgent. Each eligible load gets one number, its **slack**: the hours it
could go without its present power and still meet its accepted objective.

| Load | Slack |
|---|---|
| Car | Capacity left in its contiguous authorised windows, less the planned energy still to deliver and the shortfall carried from measured delivery, divided by its present power. |
| House battery | The same, against its stored-energy objective: target minus measured state of charge, up to the objective's deadline. A nearly full battery is urgent when its window is about to close. |
| Room heater | The pause the room can tolerate at its current comfort floor, constrained by future accepted floors. The estimate uses the thermostat's own temperature (the room sensor for a relay), cooling while paused and warming after resuming. A room with no known current floor is treated as at its limit. |

Capacity a run can recover in comes from its own windows at their authorised
maximum, limited by what the plan leaves unused under the import limit. That
unused capacity is shared only where the runs' authorised windows overlap.
The calculation keeps each charger's own capacity and the amount they can
use together, so a charger with other available windows need not use the
only one its neighbour can use. The planner's battery recovery recipes are
offers against the same grid headroom. Overlapping offers for one charger
count once, using the larger eligible rate.

A rising comfort floor can make heating urgent before the new floor starts.
The estimate allows a pause only if cooling during the pause and warming
after it can still meet the accepted floor while it is in effect. Negative
slack means the room is already behind that estimate.

## Sharing the import limit

The measured budget is the import limit less everything that is not a
candidate: the greater of what the grid meter shows and the reserved demand of
other equipment, pending starts included. The most pressing run is served
first; what a less pressing run is not given is what it yields, in the smallest
step its equipment supports (car amperes, a continuous battery ceiling, a
heater on or off). The same calculation restores power as capacity returns.

- Sizing is absolute, so a meter that has not caught up cannot ratchet a
  setting, and a command in flight needs no second adjustment.
- An increase is funded only by capacity that is measurably free. Until its
  meter shows the drop, a load still holds the greater of its setting and its
  measured draw. Pending starts stay reserved separately.
- Choices within six minutes or a tenth of each other count as the same impact
  and keep the current allocation.
- A boost above the plan earns delivery credit, so it is reclaimed first only
  while the run stays on track without it.
- A heater pause starts only on measured import above the limit. An idle
  heater contributes its measured draw. Heater relief needs a grid meter and a
  power meter on the heater.
- Equipment that has not responded to a reduction after 60 seconds keeps being
  asked, and meanwhile counts as the fixed load it measurably is, so another
  run takes the reduction.
- With no run summary, slack is zero and the earlier order decides: battery,
  then car, then heat.

A shortfall is carried across quarter boundaries and recalculated from measured
delivery. Only SHS's own reductions build it. A replacement plan starts a new
reference, because its snapshot already holds the measured state. When a
limited run can no longer finish inside its authorised windows, the device
reports `capacity_limited` or `temporarily_absent` with cause
`house_import_capacity` through the
[existing replan path](controller-plan-execution.md#implemented-forced-replan-transport).
Other devices keep executing. The planner decides whether a later window makes
sense. The battery keeps its own replan rule.

## Thermostat pauses keep the display on

A controller-driven pause never sends `Off`, which blanks the display of the
laundry, master-bathroom and pool-bathroom Moes thermostats. The thermostat
stays in Heat and only its setpoint moves, to the highest supported step at or
below its own temperature. That ends the call for heat whatever the dead zone:
in the master bathroom 21 °C left the thermostat idle at 20.5 °C and 22 °C
started it. If it still calls for heat 60 seconds after acknowledging, the
setpoint goes one more step down. Capacity is freed only when the measured draw
falls. The accepted target returns when the pause clears, through normal import
admission.

Scheduled import relief and manual heating-override relief share this routine
(`pause_thermostat`). A unit an earlier version paused with `Off` returns to
Heat at its next admitted evaluation, and from then on only its setpoint moves.
An explicit handover still restores the captured settings.

## Cost

Home Assistant must run well on Raspberry Pi-class hardware.

- A compact summary of each run is prepared once when a plan is accepted
  (`summarize_runs`): a few coalesced rows per run, carried on the device
  intention. Live control never scans the plan, queries recorder history or
  runs a solver.
- The costly part of a decision is the reserved demand of other equipment.
  Owners evaluated on the same readings share it, so a burst of meter reports
  costs one pass however many devices it wakes. Acknowledgements and physical
  responses are still processed in receipt order.
- Carried delivery is saved once a quarter, not on every report.

`scripts/benchmark-run-priority.py` measures it on the real decision path with
a car, a battery and three thermostats, all woken by every update. On an Apple
M3, 10 November 2026: 1.5 ms median and 1.7 ms at the 95th percentile per
update, 1.6 ms of CPU, one reserved-demand pass per update when every meter
moves and none when only the grid meter moves. The N100 host measured 2 to 2.5
times slower in earlier profiling. This has not been measured on the installed
system.

## Holding

Missing required readings hold the last setting: the car and battery are not
resized, and a paused heater stays paused. A heater that is running with no
evidence of an overload follows its schedule. A room whose own temperature is
missing neither yields nor claims. Restarts and plan gaps hold as before.
Explicitly leaving Controlling restores the captured settings.

## Implementation

`shs_core/run_priority.py` holds the arithmetic. `gateway.py` builds the
candidates and shares the budget (`priority_pass`), `limit_ev_current`,
`ev_spare_current`, `scheduled_heat_shed` and the `battery_capacity` query
apply each owner's part, and `battery_runtime.py` sizes charging to it.
Tests: `tests/test_run_priority.py`, the live-priority section of
`tests/test_grid_capacity.py`, and `tests/test_heating_overrides.py`.
