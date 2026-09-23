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
mode transitions change writer authority only, not the schedule.

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
