# Mixed-mode execution (0.8.0-beta.92)

## Next participation contract — 15 September 2026

Schema 9 below remains dated implementation evidence. Its four local modes are replaced in the agreed design by HA inclusion, website planning and HA Verification/Controlling authority. Verification devices remain separately Planned but external in the executable demand model. Base consumption excludes all Planned devices; execution external demand adds those without effective authority once. Explicit battery supply scope selects accounting demand independently of whether each Planned device is physically controlled.

See the [agreed participation and battery supply specification](device-participation-and-battery-supply.md).
Documentation only; replacement implementation and coordinated rollout remain pending.

The integration now sends schema 9 with the captured device modes, physical
owners and empirical demand for devices outside Controlling mode. A completed
meter quarter conditions their current-quarter demand; missing evidence stays
unknown and future quarters retain their empirical forecasts.

The backend returns a separate execution plan. Controlling devices use that
plan; Control verification uses the hypothetical planning preview. All captured
modes must still match before each write. Changing any device mode requests a
new plan and prevents reuse of the preceding execution scope.

The Schedule view and planned-request sensors select the same branch as each
device. Runtime readiness reflects execution feasibility when any device is
Controlling. The portal offers Live operation and Planning preview separately.

Deploy the schema 9 backend before installing this beta, then restart HA and
obtain a fresh plan. Cached older plans cannot authorize control. Existing fixed
plans must be rescinded before schema 9 planning. This release changes demand
accounting, not battery reserves or forecast uncertainty settings.

Regression evidence covers the generated cross-repository fixture, current
meter evidence, live-versus-verification requests, cached mode changes and a
mode change during an awaited service call. The supplied September 15 replay
raised current execution demand from about 0.83 kW to 2.92 kW and removed the
hypothetical 1.24 kW solar charging surplus. Actual evening adequacy still needs
to be assessed from subsequent demand and solar conditions.

## Subsequent charge-timing decision

The [15 September opportunity-cost design](battery-opportunity-cost.md) specifies
how charging now competes with waiting from the actual state. Both alternatives
include changed future purchases, demand and PV headroom. It extends the existing
compiled-policy architecture; no SOC catch-up rule or time-of-day trigger is added.
The mixed-mode accounting fix remains live, while continuation-policy host wiring
and forecast-risk validation remain separate work.
