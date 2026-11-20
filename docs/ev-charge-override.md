# EV charge override

User requirement, 20 November 2026. Not yet implemented.

A manual override that charges the car to a chosen state of charge within a
chosen number of hours, the way a room heating override sets a temperature for
a chosen number of minutes. It is set from two controls on the app's
"Car charging total" device.

## Scope

The override runs in the app only. The planner is not told about it and plans
around its effects afterwards, as it does for an overridden room. No deadline,
target or override state enters planning inputs, which keeps the planner's
rule that an EV departure time must not exist (24 October 2026).

App execution makes no price decisions, and the override adds none. It charges
by state of charge, time and house capacity only.

## Controls

### Desired state of charge

- Shows the car's current SOC while no override is active.
- Setting a value above the current SOC starts an override at once. Setting a
  value at or below the current SOC does nothing.
- Shows the target while an override is active, with the current SOC as an
  attribute.
- Bounded by the car's accepted range (`number.tesla_model_y_charge_limit`
  accepts 50–100%). A target above 100% is refused.

### Hours to reach it

- Shows 0 while no override is active.
- While an override is active it shows the remaining time, rounded up to a
  whole hour, and counts down. It never shows less than the fastest possible
  finish: the energy still needed at full available power, rounded up to a
  whole hour.
- A value at or below that floor means full power. Only a larger value spreads
  the charge.
- Hours set while idle are kept for the next override only and cleared when it
  ends. There is no standing default.
- When the override ends, the control returns to 0.

The fastest finish comes from (target − current SOC) × battery capacity ÷
charge efficiency, at the lower of the charger's maximum current and the house
capacity available after cutting every load that may be cut. It is recalculated
continuously, so a slow car moves the floor up rather than making the
countdown impossible.

## Charging behaviour

- The override rate is the energy still needed ÷ the time left, recalculated
  continuously. Below the charger's minimum current the car charges at the
  minimum and may reach the target early.
- The override is a floor on the plan: if the plan asks for more current, the
  car charges at the plan's rate.
- When the rate does not fit under the house import limit, the car slows down
  first and catches up later. Other loads are cut, in the ranked flexible-load
  order, only once the time left is down to what full power needs.
- At full power the car takes precedence over every load that may be cut,
  house heating included.
- The home battery may stop charging from the grid to free capacity. It never
  discharges into the car.
- When a car override and a room heating override need the same capacity, the
  one set most recently wins and the other slows down or waits.
- The house import limit and the car's and charger's physical limits are never
  exceeded.

## Charge limit

- If the target is above `number.tesla_model_y_charge_limit`, SHS saves the
  current limit and raises it to the target.
- While the override is active, the app reports the saved limit to the planner
  as the car's readiness limit, not the raised one.
- When the override ends, SHS restores the saved limit only if the car still
  reports the value SHS set. A limit changed elsewhere during the override is
  left as it is.

## Ending

The override ends, restoring the charge limit as above and returning the car
to the plan, when:

- the car reaches the target, or reports charging complete at it (Tesla often
  stops one point short);
- charging stops without SHS commanding it, for example from the Tesla app,
  the car's screen or another automation;
- the car is unplugged;
- the user ends it with an end-override button;
- the device leaves Controlling, is excluded or is manually overridden.

A missed deadline does not end it: the countdown reaches the floor and the car
charges at full power until the target is reached.

User requirement, 20 November 2026: the car cannot be unplugged while it is
charging, so the override must never restart charging that was stopped outside
SHS. Such a stop is detected as a genuine external change: the charge switch is
available and off, and SHS did not command it off. It ends the override instead
of being switched back. Stops SHS commands itself, such as slowing down below
the charger's minimum current or a plan slot with no charging, never end the
override. An unavailable switch is unknown and holds, as elsewhere. The car then
returns to the plan, where the same stop pauses planned charging for one minute
(see [control continuity](control-continuity.md#car-charging-stopped-outside-shs)).

## Durability

The override, its deadline, any pre-set hours and the saved charge limit are
stored durably like room heating overrides and survive restarts. A restart, a
lost socket, stale readings or a fault holds the last setting SHS sent and
writes nothing; see [control continuity](control-continuity.md). The override
acts only while the car charger is in Controlling.
