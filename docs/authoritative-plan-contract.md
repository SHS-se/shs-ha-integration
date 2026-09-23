# Authoritative plan and truthful interfaces

User requirement, 23 September 2026. Supersedes contradictory earlier designs.

The website Plan chart and the Home Assistant integration Schedule chart MUST
show the actual selected plan intended for execution, with the same plan identity,
quarters, device requests, energy flows and battery trajectory. An alternative
scenario must never be labelled as the actual plan. Verification simulates the
same requests; controlling permits hardware writes. It must not change bidding.
Historical measurements and controller adjustments are separate evidence, not
substitutes for the selected plan. Any planner/contract change MUST update and
verify both UIs and their shared consumer fixtures in the same delivery.

A UI exists to explain actual system behavior and provide effective controls.
A curve labelled current must be the resolved curve consumed by that plan, not
stored preferences or a reconstruction using newer measurements. Unsaved edits
must be labelled previews. Preserve meaningful controls; remove controls whose
meaning cannot be explained against the actual calculation.

Plans last for their original 72-hour forecast horizon. Routine measurement
exchanges must not rebid or extend that horizon. Automatic bidding happens on a
new published price release, after value curves are regenerated. Explicit manual
replanning remains available on Plan. Other former triggers recommend replanning
through shared persistent warnings, with reason and event time, in both UIs.
A completed replan clears the recommendations it addresses. Verification/control
mode transitions change writer authority only, not the schedule: they request no
plan and recommend no replan, and a plan or battery reference captured under
either mode is accepted and executed under the other.

Realistic state above a desired target is not infeasibility. Invalid measurements
must be isolated to the affected device and explained; they must not discard
otherwise valid household planning. Targets and sensor validity are distinct.

## Replan recommendations

Quarterly telemetry exchanges do not authorize a new solve. Ingest compares the
published price rows with those recorded for the accepted plan; new publication
or an outstanding manual request permits the next solve. Elapsed quarters simply
leave the remaining original horizon shorter. Forecast padding never identifies
a new price release. The original validity endpoint does not move.

The current row owns recommendation reasons and occurrence times. Both UIs read
that state. Curve/settings and planning configuration writes append a reason;
integration events which previously forced a solve submit a recommendation.
A successful plan publication clears reasons that predate its solve; an event
that occurred during the solve remains visible. A failed solve clears nothing.

Compare four consecutive completed quarters entirely covered by the accepted
plan. Warn when each quarter differs by more than 50%, or when the absolute
difference of the **rolling four-quarter totals** exceeds 6 kWh. Missing quarters
break the sequence. Pack warnings compare measured energy against the accepted
plan's state at capture time and trigger above 4 kWh. No threshold requests a
solve. Expiry recommends a manual replan and leaves the original endpoint intact.

## Measured state and impossible readings

Implemented in `marginal-value-planner-v43`.

A store bound limits what a schedule does to a store, not where the store may
be. A car above its charge limit, a pool warmed past its stop temperature, a
pack below a cut-off raised after it discharged, and configured battery targets
below a live cut-off are planned as they are. Only charge that raises a store
further above its ceiling, or discharge that lowers it further below its floor,
is infeasible; the trajectory carries the measured state rather than moving it to
the bound. This applies equally to the auction, the independent scorer, cost
refinement and replan continuity.

A reading no device can produce isolates that device alone: a state of charge
or charge limit outside 0–100 %, a car capacity outside 1–500 kWh derived from
its usable energy, or a pool water temperature outside −5–60 °C. The device
leaves the plan exactly as a disabled capability does, and its historical load
remains household demand. Home Assistant also leaves out a device whose reading
is unavailable, non-numeric or impossible and reports it in the snapshot's
`measurement_issues`. The plan publishes the complete list in
`measurement_issues`; the website Plan page and the Home Assistant panel both
show it. Configuration errors remain setup failures and are not isolated. When
a left-out device reports usable readings again, ingest recommends a manual
replan (`measurement_recovered_<device>`), because plans are kept between price
releases.

In this integration `measurements.py` checks each device's readings before the
snapshot is built and switches off only the affected store for that snapshot,
exactly as the customer's own store switch would. Setup warnings keep reading
the configured options. The panel names the devices the current plan leaves
out, with a link to each source entity, and the affected device's status says
why it has no plan. A battery left out is handed back until a plan includes it;
no replan is requested, because the server recommends one once its reading is
real again. A percentage unit decides how a state of charge is scaled, so a car
at 1 % is not read as full; an empty car is planned with the battery size last
derived from its own readings; a departure outside the horizon, already past or
unset leaves that plan without one.
