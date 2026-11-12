# Native execution and capacity recovery

The app and companion use protocol 4. Updating only one side does not authorize
native control. Existing settings, restoration baselines and historical command
outcomes survive an update or a lost connection.

A native `Send` names a logical step and a distinct physical attempt. Repeating
one attempt is idempotent; retrying definitely unsent work creates a new attempt.
The remote port holds no independent incrementing step cursor. Native service
completion and post-response-bound observations reconcile the gateway route and
the app reducer. A later reply cannot advance another step or invalidate its
success. Supported identical assignments may retry while retaining all earlier
unknown effects.

Shutdown first quiesces source callbacks and revokes admission. One service-owned
completion task settles physical work, drains the receipt worker, and releases
its journal lease in that order. Concurrent or repeatedly cancelled close callers
cannot cancel that drain. Unexpected worker termination rejects every outstanding
receiver, including a batched lookahead, after the in-flight storage call settles;
it does not rewrite a committed native outcome. Restart alone still sends no
restoration setting.

Native numbers share a decimal tick codec. It accepts floating representation
noise, never an adjacent hardware step. The Sigen adapter also preserves the
specific wire float that its truncating `int(value * 1000)` conversion needs.
Intent validation does not strip that wire encoding at the last service boundary.
The synthetic tests exhaust both W and kW surfaces and preserve real one-watt
drift as an actual setting.

The existing FIFO and SQLite capacity record retain dispatched effects and their
native response bounds. An observation's local arrival time is captured before
queueing: a slow database cannot make an earlier report into post-bound evidence.
After the supported bound, actual controls and physical flow can supersede old
reservations without rewriting an uncertain command as successful. Later unknown
effects remain reserved. Generic equipment acquires no guessed response deadline.
One-way migration rebuilds adapter metadata from the admitted installation and
routes; persisted completion or request expiry supplies a valid dispatch bound.

Sigen ceiling relief is an analytic relation under unchanged controls and native
guards. Its live envelope is rebound to current flow at preparation and dispatch.
Unrelated meter changes do not permanently invalidate an otherwise supported
reduction. Generic commissioned transitions keep their exact evidence.

A current authorized battery target that withdraws useful support first freezes
further borrowing against that support. If it needs demand reduction, the gateway
selects current Controlling native owners in reverse admission order, applies the
smallest supported native reductions, and observes their physical settlement.
The ordinary device owner and NativeExecutor perform these operations. Existing
run protections and baseline restoration rules continue to decide there; devices
without a supported reduction remain a visible physical constraint.

Durable caps prevent the ordinary scheduler from immediately undoing a necessary
reduction. The record retains physical identity, original demand, and current
owner intentions across plan changes. An old economic intention is never replayed
under a new policy. A lost session, expired target, fault or restart alone sends no
settings. Current authority is required to resume coordination. An adopted current
battery target is published through the same writer authority even when no native
transition is needed. Matching live controls, the writer grant, request revision
and policy context are checked; this replaces obsolete target credit limits while
retaining the load caps and their physical evidence.

Prepared effects reserve capacity but cannot yet explain physical house demand.
Dispatched and retained native excluded-load bounds enter battery eligibility so
a delayed excluded meter cannot turn new EV demand into eligible house base load.
The same prepared command is still checked against current house measurements;
no historical base-load floor or frozen promise bypasses final admission.

An admitted capacity episode is scoped to the affected opportunity. A positive
EV ceiling is reported as `capacity_limited`, and a blocked opportunity can be
reported as `temporarily_absent`. Ordinary live trims use the
[run-priority thresholds](run-priority.md). A feasible reduced intention can
keep executing while the cap remains active. Independent headroom evidence for
the original demand is returned during synchronization, including when the
accepted plan suppresses the owner's instruction. The gateway reports that
evidence without issuing a setting or claiming delivery. A resumed intention
must still pass admission.
