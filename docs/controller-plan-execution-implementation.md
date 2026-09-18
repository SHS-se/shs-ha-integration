# Battery plan execution implementation

18 September 2026. The battery replacement is implemented across the server
planner, Home Assistant runtime, diagnostics and existing battery card. It follows
[the replacement specification](controller-plan-execution.md). The integration
version is **0.9.0-beta.6**. This work is committed locally; it has not been pushed,
published or deployed, and has not been validated on equipment.

The production rollout remains **home battery first, pool heater next, EV
afterwards**. This change completes the battery implementation. Pool and EV keep
their current execution paths until their own service models are migrated.

## Ownership and contract

The server attaches `battery_execution` to the real production plan response. It
uses the final physical schedule in Controlling mode. In Verification it uses the
battery-only conditional schedule, with other devices fixed to the physical
schedule. Ingestion, worker results, saved plans and API responses carry the same
contract. An excluded battery has no execution contract.

Every interval declares its exact duration, integer mWh allocation, storage
reference, nominal operation, source/export permissions and power ceilings. The
AC balance includes load, solar, battery charge/discharge, import/export,
curtailment, unserved energy and explicit rounding. The reference declares the
planner's conversion assumptions. Measured DC counter increments are already on
the battery terminal basis and are not multiplied by nominal efficiency again.

Recovery is delegated within an existing selected charging window, with nominal
demand allocated first. The executor cannot buy energy in another economic
window on its own. Household supply follows actual eligible demand. Physical
import/export limits, local minimum storage and the separate export reserve stay
authoritative. The empirical loss collector and fitter remain in use for live
AC/DC power conversion; their evidence and selected model appear in diagnostics.

The integration's former economic selector, policy exchange and outlook modules
are removed. There is one battery execution owner. There is no old-controller
fallback when a compatible contract is unavailable: the runtime reports the
condition and follows its existing physical release protocol. The server retains
planner-side projection code used outside this new execution path; it does not
grant a second live controller authority.

## Accounting and persistence

The account retains reference admissions, measured directional meter receipts,
source-time corrections, physical state observations, objective versions and
handover dispositions. Missing measurements remain unknown. Requested settings,
transport acceptance and measured energy are separate evidence.

A replan atomically captures a local generation, exact receipt prefix, observed
state, outstanding obligations and pending native effects before network I/O.
Responses must match that request and local setup before replacing the cached
plan. The planner starts from observed state and explicitly incorporates or
retires prior responsibilities. Handover never adds the already incorporated debt
twice. Late evidence corrects history without rewriting historical commands.
Capacity changes record a common-instant state reconciliation rather than an
invented energy transfer.

Native attempts, writer grants, confirmation, ambiguous effects and release
survive restarts. The one-way old-journal migration retires economic requests but
retains issued physical effects and release authority. Fresh local evidence is
required before new commands. Verification evaluates and records assessments
without issuing optimisation writes; previously owned settings may still require
release when changing modes.

Execution evidence is stored in immutable, content-addressed pages before the
command checkpoint publishes their root. Missing or corrupt pages fail explicitly.
Completed pages are reused after appends. Planning requests contain only live
responsibilities plus a hash and receipt summary of settled history, rather than
resending every historical objective version.

The archive retains full evidence for the initial Verification rollout. Individual
storage records are bounded; total disk use, diagnostic download size and the
hydrated in-memory history grow with the retained run. There is no automatic
history compaction or deletion. Replay cost also grows with history. This is a
material operational limitation to measure during extended Verification runs.

## Card and diagnostic replay

The card keeps its existing layout and configuration controls. Its text explains
what the plan asks for, what the battery is doing, any difference, and the next
action in ordinary language. Verification statements describe what the controller
would request; they do not claim that energy was delivered. Technical evidence
remains in the downloadable controller diagnostics.

A new controller dump contains a top-level `battery_execution` section with:

- the full accounting journal and derived debt/credit, residuals and outcomes;
- the captured replan payload and accepted reference identity;
- timestamped evaluation inputs, receipt prefixes, assessments and conversion
  models, including the inputs preceding native effects;
- pending commands, confirmations and the current physical command journal.

Replay a new JSON or compressed JSON dump locally:

```bash
python3 scripts/replay-battery-execution.py '/path/to/shs-controller-diagnostics.json.gz'
```

The command reconstructs accounting, recalculates the current assessment and
checks each recorded assessment against its original receipt prefix, observation
prefix and conversion model. It returns exit code 0 for matching results, 1 for a
mismatch and 2 for an unreadable/incompatible dump. Old-controller dumps predate
this journal and cannot be replayed with this command.

Matching replay establishes deterministic execution/accounting; it does not prove
planner economics, source calibration or physical command delivery. During
Verification, compare the published schedule, actual load/solar and directional
counters, recorded shortfall, authorised recovery and replacement-plan disposition.
Include plan replacement, unavailable readings, late counter updates and a restart.
Keep the battery in Verification until those traces meet your acceptance criteria.

## Validation

- Integration: 756 Python tests and 75 frontend tests pass, including fake native
  services, no-write Verification, crash/restart and release, configuration
  correction, delayed/corrected counters, archive failure and diagnostic replay.
- Backend: 1,194 Deno tests and 32 mocked-backend Playwright tests pass. TypeScript
  checks and the production frontend build pass.
- Backend ESLint: zero errors, 26 existing warnings in unrelated frontend code.
  The production build retains its existing large-chunk warning.
- The real planner-generated execution fixture and integration copy are identical.
  Regenerating the existing HA API fixture produced no changes.

Deploy the compatible server code before installing the integration beta. The
existing production publication and device mode controls remain the rollout gates;
this implementation does not switch any installation to Controlling.

## Plan handover and rejected updates (beta.9)

A handover happens when the controller accepts a newly generated battery reference,
including after periodic replanning, changed inputs or a controller replan request.
Moving into another quarter of the same accepted plan is not a handover. Acceptance
acknowledges the captured request and measured evidence, and records how the new
plan handles earlier obligations. It does not reset measured delivery or erase old
shortfalls.

Objective IDs identify an obligation's lifetime, not just a kind and deadline. The
planner reuses an ID only when the captured feedback still carries that live
responsibility. A returning window gets a new plan/generation-scoped ID after its
previous obligation was incorporated or retired. Retained target changes still
require explicit amendments; the controller's admission checks are unchanged.

Rejected replacement plans remain visible until a new reference is accepted.
Reoffering the already accepted reference, taking another measurement or restarting
does not clear the warning. An obsolete response older than the accepted local
generation stays in diagnostic history without replacing the current status.
The previously accepted reference keeps only its original lifetime and permissions.

The Schedule battery card and Status warnings use plain-language explanations and
link to diagnostics, not unrelated configuration fields. The battery controller
sensor and battery mode select expose `plan_status` (`accepted`, `rejected` or
`awaiting_plan`), `plan_rejection` (time in milliseconds, rejected contract ID,
local generation and technical reason), `accepted_plan_id` and
`accepted_reference_id`. These fields also appear in controller diagnostics.
The controller sensor reports `fault` while rejection needs attention; this does
not itself assert a hardware fault or change the selected operating mode.

Checkpoint schema 9 and archived session reads upgrade the previous session shape
once by adding an empty rejection record. Existing meter evidence, accepted plans
and pending physical effects are retained.

The supplied September 18 replay originally failed with “changed objective target
needs an explicit retained amendment”. Regenerating it with the corrected planner
successfully admits its generation-35 reference to the supplied account, preserving
all four earlier admissions and every meter receipt. Deployment requires the
server fix as well as integration beta.9; changing the integration alone cannot
repair the planner's reused identities.

## Measurement revisions after restart (beta.10)

The battery adapter reserves one local revision per measurement capture, under its
capture lock, above both its previously reserved revision and all restored runtime
watermarks. That revision is shared by the native observation, household frame and
battery conditions. Two captures queued before either is applied therefore remain
distinct after restart. Device timestamps do not determine update order; the
existing freshness checks still apply. The regression test restarts a persisted
runtime, queues two different captures before delivery and verifies that both are
accepted, including a later capture with an earlier but still fresh source time.
This fixes the revision collision; it does not establish that the separate
measurement-processing delays observed in production have been resolved.
