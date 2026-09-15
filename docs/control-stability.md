# Control stability before commissioning

## Scope-aware response and stability — 15 September 2026

Evaluate scoped battery house supply from aligned current conditions through the existing economic policy. Scope membership and PV-attribution revisions invalidate dependent requests; a fixed forecast cap or universally rated cap is not a stability rule. Subgroup scope requires declared sampling/actuation latency and native enforcement evidence. Missing inputs follow the existing explicit coverage/release protocol, with no new fallback.

See the [agreed participation and battery supply specification](device-participation-and-battery-supply.md).
Documentation only; replacement implementation and coordinated rollout remain pending.

The planner's priority scenario compares a proposed current-quarter battery/pool request against the previous issued request, using fresh observations and the current device limits. It tests both a directly amended trajectory and a reoptimized continuation. A feasible, fully validated alternative is preferred when its modeled whole-horizon objective is no more than 0.05 SEK worse. This is an explicit engineering deadband, not a minimum run time or a claim about battery wear. Material gains and changed constraints can still change commands immediately.

The backend must deploy planner v30 and planning protocol 2 together. This integration does not retain obsolete battery commands: fresh battery observations and the newly issued command remain mandatory.

For a pool already controlled successfully, an unknown/unavailable water-temperature reading can suspend writes for at most 15 seconds. The suspension also ends at the original raw-source freshness deadline, slot end or plan expiry, whichever comes first. Repeated missing reports cannot renew it. Recovery revalidates the temperature and normally requires no write. Persistent failure restores the native band through the existing handover path.

Missing actuators, changed authority or requests, malformed/stale measurements and failures after a write attempt do not qualify. Verification runs do not establish this live-control evidence. The native thermostat retains its accepted band during the bounded gap.

Tests exercise the actual controller against fake Home Assistant services. They do not demonstrate inverter or heat-pump response. Commission the battery alone under supervision first; verify mode, both power limits, actual flow, SOC protection and handover before unattended operation or adding the pool.
