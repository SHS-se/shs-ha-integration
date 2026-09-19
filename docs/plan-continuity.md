# Plan continuity and persistent fallback requirement

User requirement, 19 September 2026. This supersedes earlier requirements that
invalidate a cached schedule solely because device modes or planning configuration
changed. Forecast provenance is not local permission to operate equipment.

## Retained forecast operation

A failed planner request, missing snapshot inputs, an unusable replacement or a
rejected battery handover must not displace the last usable plan in Home Assistant.
Keep its persistent cache and the battery's accepted reference and accounting.
Requesting a replacement is independent of executing that retained plan.

Each device keeps the forecast branch it had when the plan was produced. An
explicit switch from Verification to Controlling can execute that previously
verified schedule immediately, without waiting for the website. Switching back
withdraws command permission and continues verification. Another device's mode
change does not invalidate this device's schedule. Retain the original plan and
contract identities; do not rewrite forecast history to imply a new optimization.

The permission selector depends on inclusion, the acknowledged website planning
choice and complete local equipment setup. Missing forecast instructions and an
old website refresh timestamp do not prevent saving the user's permission. The
Schedule and Status views report forecast availability separately. Configuration
errors still enumerate their actual fields and correction actions.

Current local permissions, exclusions, overrides, equipment bounds, supply
membership, measurements and command checks continue to apply. A settings change
reconciles existing command ownership and rebinds the controller to the current
settings. A forecast is never a grant to bypass these checks. Battery plan updates
must pass the actual handover validation before replacing the coordinator cache.

## Remaining gap: no forecast slots

The implemented controller can adjust a retained battery strategy to live demand
and physical limits, but it does not define a complete independent operating
policy. Pool temperature preferences, EV charging needs, device schedules, battery
economic roles and delegated recovery still come from the planner. Merely enabling
Controlling cannot create instructions that have never been supplied.

Retained forecasts still have finite slots. Once those run out, or on an
installation with no accepted plan, SHS cannot currently provide continuous
scheduled operation. The existing device release/handover behavior applies; that
is not an independent SHS control policy. Do not describe permission selection as
proof that equipment is being operated. No repeated-last-slot policy or invented
charging/heating defaults are introduced by this fix.

## Required persistent fallback plan

The planner must also provide a separate, simple fallback schedule or policy whose
behavior is designed in a separate exercise. The following are requirements for
that work, not a claim that fallback generation or execution is implemented:

- Persist the last accepted fallback in HA independently of rolling forecasts,
  planner request state, forecast expiry and transient errors. Restore it after
  restart and offline startup.
- A failed regular plan, failed fallback refresh or rejected candidate must never
  delete or invalidate the accepted fallback. It has no forecast expiry. Replace
  it atomically only after accepting and durably storing its successor.
- Prefer a usable regular forecast. Use the persistent fallback when no regular
  schedule covers the current time, and return to a regular plan after successful
  admission. Preserve physical energy accounting and pending command ownership
  across both transitions.
- Define the fallback's per-device behavior, recurring-time semantics, targets,
  battery reserve and source/export decisions separately. Do not replay an expired
  quarter, copy an obsolete deadline or invent a forecast by repeating yesterday.
- Local permission, explicit disablement, exclusion, overrides, equipment limits
  and required measurements remain authoritative. Disabling execution does not
  erase the stored fallback; changes to equipment need explicit reconciliation.
- Show whether control uses a regular forecast, a retained forecast or the
  persistent fallback, and expose planner errors independently of that state.
- Cover failed and rejected updates, horizon exhaustion, restart, offline startup,
  mode changes, permission withdrawal and recovery to regular planning in tests.

First-install behavior before any fallback is received must be resolved as part
of that design. An empty store cannot supply an unspecified operating policy.
