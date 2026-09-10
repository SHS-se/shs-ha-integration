# Customer-operated pool execution

Step 6 adds `pool_policy.py` and `pool_controller.py`. SHS sends scheduling
requests to a customer-owned HA script and reads its feedback sensor. It does not
write Nibe registers, operate circulation, coordinate devices, inspect automation
internals or implement physical handover. Local permission remains disabled;
software delivery does not commission the installation.

## Interface and complete request

Bind the three reviewed roles from Control interface validation: `request`
(script accepting a single `request` object), `feedback` (sensor with the v1
object in its `feedback` attribute), and `water_temperature` (Celsius sensor).
Their saved registry identities resolve renames; replacement identities and
changed capabilities require review. Only the request interface is reserved.
The customer's internal pumps, switches and heat-pump entities are not bindings.

Before a heat/defer request, verify the complete accepted household plan, local
permission and binding, the objective's ordered band and local bounds/steps, and
a finite water temperature reported within 120 seconds (at most five seconds of
clock skew). Validate the full request with the v1 schema. Forecast watts never
select the action or supply its band. A newly accepted website objective reaches
the script as the complete new band.

The direct call is `script.<bound-service>` with `{"request": <poolRequest>}`.
Requests contain home/control ID, accepted tuple, unique request ID, durable
per-control sequence, plan ID, action, objective, issue time and expiry. Heat/defer
expiry is bounded by both UTC and monotonic execution authority and plan expiry,
and is at most 120 seconds after issue. All awaited boundaries recheck dispatch
permission, binding, temperature and authority before any new submission.

The adapter ticks every five seconds. An acknowledged unchanged request is not
resent each tick. With at most 30 seconds remaining, a current renewed authority
can issue a new sequence/request ID with an explicit new expiry. An unacknowledged
request may be retried every five seconds with **exactly the same** ID, sequence,
body and expiry. If acknowledgement is still missing after 15 seconds, revoke
authority and request release. Service return is not acknowledgement.

## Customer responsibilities

Before local permission can be granted, review and demonstrate these properties
in the customer interface:

- Validate the home, control, complete versioned request, objective and expiry.
- Persist the highest accepted sequence and immutable request identity; reject
  older sequences, conflicting duplicates and expired requests. Exact duplicates
  are idempotent. These rules must survive customer-automation restart.
- Retain the request expiry and autonomously yield to the customer's normal
  policy on expiry, including when SHS or HA's integration stops running.
- Honor release as relinquishing SHS scheduling. It is not a pump-off request.
- Emit request-correlated acknowledgement independently of observations of heat.
  Customer rejection/manual override must be explicit feedback for the current
  request, not a silent edit of SHS's sequence state.

This ordered receiver is what prevents a delayed heat request from superseding
a newer release. The adapter does not cancel or sequence the customer's private
hardware actions. Its software tests simulate the receiver protocol; they do not
establish that the home's current automation implements it.

## Honest feedback and independent status

Feedback must match all of home ID, control ID, request ID, sequence, accepted
tuple and plan ID. Validate its own report time as well as the HA sensor report:
it must follow issue, be no older than 120 seconds, and not be more than five
seconds in the future. Old, duplicate-for-an-earlier-request, foreign or
mismatched feedback cannot acknowledge the current request.

Report these customer-supplied states without inferring physical results:

| Feedback | SHS meaning |
| --- | --- |
| `accepted` / `scheduled` | Request acknowledged; heating scheduled, not observed |
| `accepted` / `observed_heating` | Customer reports observed heating |
| `accepted` / `limited_deferral` | Defer acknowledged; heating may continue at the local lower bound |
| `accepted` / `unavailable_heat` | Customer reports heat unavailable |
| `accepted` / `unknown` | Request acknowledged; heating observation unknown |
| `rejected` | Yield scheduling and latch customer override |
| `released` / `released` | Customer confirms relinquishment to its normal policy |
| `release_failed` | Release remains pending; preserve ownership evidence |

A heat request cannot report limited deferral. Release requires explicit release
feedback and cannot be acknowledged by a scheduled/heating report. A positive
meter value, warm water, a completed script call or a planned watt value does not
substitute for correlated feedback.

Battery and pool reports share `active.controls` but retain separate observation
timestamps. Updating or clearing one adapter report does not refresh or erase the other.
Revoking shared plan authority still invalidates the household execution envelope.
The portal checks each report, its accepted tuple and plan ID. Pool status also
requires the request's unexpired authorization and fresh correlated feedback.
Uncorrelated planner observations cannot claim execution.

## Durable ownership and release

The verified private HA store is `shs_energy.pool_controller.<entry_id>`, schema 1.
It contains sequences, owned binding and accepted definition snapshots, immutable
pending/current requests, last send time, release phase and customer overrides.
Persist the request and sequence before submission; never acknowledge a disk
write that cannot be read back. A persistence failure revokes authority. Restart
loads ownership solely to release it, and never resumes cached scheduling.
Sequence counters survive successful release and subsequent restart. A control
changing method cannot start its new adapter while another adapter still holds
that control ID; both old binding reservations and handover remain intact.

Disable, expired authority, invalidation, removed controls, changed binding,
stale temperature, service failure and shutdown send a newer release using the
**owned** interface and accepted tuple. Release uses a null plan ID and objective;
it does not need a current heating plan or fresh water reading. Its lifetime is
also bounded to 120 seconds. Retry the same release idempotently while valid;
if it expires without acknowledgement, issue a newer release sequence.

Until correlated release acknowledgement arrives, keep the journal and report
`release_pending`. Do not assign a replacement script or treat elapsed time as
proof of completed physical handover. Customer expiry remains necessary when
the old interface is unavailable or SHS itself is down.

A customer rejection latches an override. Once release is acknowledged, retain
its evidence and refuse new scheduling until a local administrator explicitly
reviews it through the existing admin WebSocket endpoint:

```json
{
  "id": 1,
  "type": "shs_energy/controls/setup",
  "config_entry": "<HA entry ID>",
  "action": "review_pool_release",
  "control_id": "<owned control UUID>",
  "expected_revision": 1,
  "reviewed": true
}
```

Review requires the owned binding revision and durably acknowledged release.
It makes no hardware calls, preserves the sequence counter and revokes authority.
It does not grant local permission. Status/discovery expose the owned revision,
request ID/sequence and release/override reason for review.

## Cutover and accounting

Config entry version 6 checks the legacy journal before changing options/version.
Old direct pool-band ownership, typed pool group/companion ownership, and captured
actuator overlap block with `legacy_handover_required`. Resolve release using the
old installed version's reviewed procedure before upgrading. Never delete its
journal or reinterpret an old hardware baseline as customer release evidence.
The new code cannot run the old pool band or restore its Nibe/pump settings.

The one-way migration removes old direct pool fields and explicit old pool routes
(`switch_schedule` in the saved pool-heating source list), including their
companions. Independently mapped overlapping owners require review instead of
being silently deleted. Setpoint/room routes remain room controls despite their
pool reporting category. Removed direct mappings cannot be recreated through the
old save or executor path. The customer automation is not modified or suspended.

Retain every historical source ID/category list, pool volume and water observation
setting. The accepted control's canonical sources are accounted once by the
existing planner path; migration does not rekey meters, create another pump load
or turn an alias into another counted source. Tests verify preserved meter/room
identity, one-time canonical subtraction and refusal of duplicate attribution.

The [installation evidence](../contracts/control/v1/pool-installation-evidence.md)
remains a commissioning checklist: dedicated request/feedback interface, expiry
and release demonstrations, temperature-chain freshness, objective/limits,
meter lineage and shared-circuit attribution still need installation evidence.
No live customer automation or physical ownership was changed by this step.
