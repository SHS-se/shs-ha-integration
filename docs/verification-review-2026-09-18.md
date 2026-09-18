# Battery Verification evidence review — 18 September 2026

**Recommendation: these files do not justify enabling Controlling for the
replacement controller.** They describe the previous controller. No operating
mode was changed as part of this review.

Evidence supplied:

- `shs-controller-diagnostics (28).json.gz`, exported at
  2026-09-18 02:52:43 UTC (04:52:43 Stockholm).
- `plan-replay-55228455-7b57-4973-9394-54f3dd28d581-2026-09-18T08-30-00+00-00.json`.

The diagnostic session identifies integration **0.9.0-beta.4**, started on
17 September at 11:51:49 UTC. The battery is in `control_verification`, but its
current result is a fault: “Waiting for battery policy for the current quarter”.
The retained 64 fault groups include 167 occurrences of `plan_not_acknowledged`,
30 waits for a current-quarter policy, 41 `policy_authority_changed` events, one
invalid policy response, five unavailable responses without further reason and
one source-watermark/current-validity rejection. These are retained grouped
counts, not estimates of time in fault or failure rates.

There are no battery verification-attempt groups for the current session. Older
battery groups in the file include integration 0.8.0-beta.94 and cannot establish
coverage for this session or for the replacement. The diagnostic file contains
no replacement `battery_execution` journal. The replay input has no
`battery_execution_feedback`, and its expected planner output has no execution
contract. The selected replay quarter is 08:30 UTC; that is a future scheduled
quarter relative to the 02:52 UTC diagnostic capture, not an observed execution.

Calibration has produced useful installation measurements: 67 grid-charge windows
covering about 20.69 kWh and 72 discharge windows covering about 10.49 kWh. The
selected charge curve has gain 0.9660 and about 106 W fixed overhead; the discharge
curve has gain 0.9655 and about 125 W overhead. These describe reported meter
boundaries and installation losses, not cell-internal efficiency. Solar charging
still uses configured assumptions because its observed source paths are not
isolated. This supports preserving the empirical calibration code; it does not
prove controller command response.

Install the compatible server and replacement integration, keep the battery in
Verification, and collect a new controller dump containing `battery_execution`.
Use the [implementation guide](controller-plan-execution-implementation.md) to
replay its reference, actuals, deviations and recorded assessments. A newer live
installation may already have progressed beyond these attached files; this review
makes no claim about unprovided later evidence.
