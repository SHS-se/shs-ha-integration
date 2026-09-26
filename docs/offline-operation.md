# Cached execution and cloud exchange

## Participation and supply validity — 15 September 2026

Persist the independently owned inclusion/planning/authority revisions and the accepted battery scope with existing validity, grant and pending-effect records. Local exclusion fences optimization writes immediately; offline cached website state cannot resurrect authority. Missing scope membership or required subgroup readings is unavailable coverage, not permission to substitute forecast demand, whole-house scope or rated power. Continue only under the existing accepted-policy and explicit release protocol; this decision adds no fallback.

See the [agreed participation and battery supply specification](device-participation-and-battery-supply.md).
Documentation only; replacement implementation and coordinated rollout remain pending.

Status: current cached-plan and exchange behaviour. Pool, EV and generic devices
already continue through restarts without a handover (see
[control continuity](control-continuity.md)); the battery runtime still hands
over on startup/unload. The [target restart contract](https://github.com/SHS-se/smart-home-solutions/blob/main/docs/energy-optimisation/reactive-controls.md#expiry-and-baseline-handover)
requires durable reconciliation and adoption without routine baseline cycling.
The target also persists operating modes, pending release and bounded retry
state; restart does not renew authority or blindly replay commands. While
Controlling, SHS automatically corrects external drift and can retry ambiguous
delivery under the adapter contract; no external-change hold is created. Notification behaviour is outside this
restart contract and needs a separate specification later.

HA executes the last validated schedule locally until `valid_until`, the end of
its supplied slots (up to 72 hours). `binding_until` describes published-price
coverage, not an execution lease: later slots use estimated prices. Hardware
bounds, live sensor checks, local overrides and explicit disablement still apply.
Battery export continues to require the published-price policy conditions.

A failed status, tariff, price or planning request retains accepted cached data.
The subscription sensor exposes the last successful status connection and latest
connection error. Missing inputs for a new snapshot do not invalidate a cached
plan. Planning configuration changes recommend a manual replan while retaining the
existing schedule for execution under current local permissions and equipment
checks. Switching between Verification and Controlling requests nothing. Invalid or non-ready replacements are refused before
replacing the cache. Expired schedules remain inspectable and never execute
beyond their end; devices then hold the last setting SHS sent until a new plan
arrives or their select releases them ([control continuity](control-continuity.md)). The [plan continuity and persistent fallback requirement](plan-continuity.md)
describes the remaining work needed for continuous operation without forecast slots.

One startup exchange follows the existing 60-second wait for HA entity providers.
Subsequent exchanges use a 15-minute interval measured from integration setup,
not shared quarter-hour boundaries. There is no one-minute cloud recovery loop.
Price sensors and controllers still advance on local market quarters without
network traffic. Explicit configuration changes send a fresh snapshot and record a replan recommendation; the server solves only for a new price release or a manual replan.
The existing plan storage is retained; no new plan-persistence mechanism is added.

The website polls Supabase every 30 seconds while visible. Its authenticated
`get_energy_portal_delta` RPC returns status metadata, a plan only when its ID
changes, history quarters only when their content hash differs, and configuration
only when its hash changes. Deletions and late corrections are included. Thermal
readiness is aggregated in SQL; raw month-long observations are never downloaded.
The website displays HA's last report and contact time, not a short execution lease.

## Rollout

Apply website migration `20260912090000_add_incremental_energy_portal_sync.sql`,
deploy the planner and website, then install the HA beta. The new website requires
the RPC; there is no legacy bulk-download path. Plans made by the old server still
have their original 75-minute expiry until replaced by a new server-generated plan.
