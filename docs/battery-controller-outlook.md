# Battery controller outlook

The battery card shows one compact forecast row below the current live settings.
It shows the first future charging and discharging quarters in the controller's
selected continuation, expected battery power, household consumption and solar
power in those quarters, the modeled economic benefit, and the forecast horizon.
Times follow Home Assistant's configured time zone (or the user's local-time
preference). The row wraps on narrow screens without clipping information.

This replaces the battery card's planner-derived “Next quarter” text. The full
planner schedule remains available. Controller forecasts are conditional: new
measurements, permissions, prices or policies can change the selected continuation.
They are not future commands already queued for execution, proof of delivered
power, or a certified globally optimal strategy. Verification mode is labelled.
“No later charge forecast” means no charging in this selected continuation through
the displayed horizon, not a promise never to charge.

## Meaning of the benefit

The estimate compares the selected live action plus its best published continuation
against **standby for the rest of this quarter plus standby's best published
continuation**. Both alternatives use the same economic objective, including grid
purchases and sales, conversion losses, wear, shaping and ramp costs, and terminal
stored-energy value. This is modeled benefit, not realized cash profit or a
comparison against leaving the battery idle for the whole horizon. Negative
values remain visible when the controller retains an incumbent. If a feasible
standby alternative is absent, the benefit is explicitly unavailable.

## Data ownership

The compiler delivers `battery-execution-outlook-v1` beside the executable policy.
It retains only the common next-quarter forecast and each emitted witness's anchor
and first suffix charge/discharge. The suffix excludes its artificial first seed
quarter. HA derives that quarter's actual forecast from the selected operation's
predicted end energy and the compiler's AC/DC bridge equations. Selection follows
`session.selected_id`, including hysteresis, rather than the first ranked candidate.

The exchange validates the explanation against the policy's source hash, family,
power basis and exact witness set. Malformed explanatory metadata clears the
outlook without rejecting an otherwise valid executable policy. The display also
requires a current policy, decision and measurements; it hides during runtime
faults or failed panel refreshes. No planner schedule is substituted for missing
controller evidence.

Explanations are not persisted in execution checkpoints and never grant control.
Restart recovery retains the existing policy/checkpoint format; the forecast
returns after a matching delivery. Full AC/DC compiler fixtures cover the consumer
boundary, with regressions for bridge energy balance, suffix selection, retained
incumbents, negative/missing benefit, stale data, and rendered card text.

## Release coordination

This change requires both the backend `battery-policy-exchange` deployment and the
integration update/restart. The delivery envelope now requires `outlook`. Its
strict readers do not accept mixed old/new delivery formats, so coordinate both
updates; a mismatched deployment temporarily stops new policy admission. No
compatibility path or actuation-policy change is included in this feature.
