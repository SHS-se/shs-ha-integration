# Cached execution and cloud exchange

HA executes the last validated schedule locally until `valid_until`, the end of
its supplied slots (up to 72 hours). `binding_until` describes published-price
coverage, not an execution lease: later slots use estimated prices. Hardware
bounds, live sensor checks, local overrides and explicit disablement still apply.
Battery export continues to require the published-price policy conditions.

A failed status, tariff, price or planning request retains accepted cached data.
The subscription sensor exposes the last successful status connection and latest
connection error. Missing inputs for a new snapshot do not invalidate a cached
plan. Changing control configuration retains the old plan for inspection but
blocks commands until a matching replacement arrives. Invalid replacements are
refused. Expired schedules remain inspectable and never execute beyond their end.

One startup exchange follows the existing 60-second wait for HA entity providers.
Subsequent exchanges use a 15-minute interval measured from integration setup,
not shared quarter-hour boundaries. There is no one-minute cloud recovery loop.
Price sensors and controllers still advance on local market quarters without
network traffic. Explicit configuration changes may request an immediate replan.
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
