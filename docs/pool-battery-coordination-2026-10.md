# Pool start and battery coordination investigation

15 October 2026. Investigated controller diagnostics from integration
`0.9.0-beta.107` and the accompanying SHS app log. Household captures remain
outside the repository; regressions use synthetic readings.

## Pool start

The accepted plan requested heating from 21:30 Europe/Stockholm. At the 21:46
capture the pool was Controlling, water was below the plan cutoff, and 34
evaluations had withheld the switch-on command while waiting for battery
charging to release grid capacity.

The final HA check called `native_readings.power` with a retired `now_ms`
argument. Its caught `TypeError` became a failed headroom check, so every
physical pool start was refused even with a zero charge ceiling and negligible
battery flow. Remove that argument; keep the existing ceiling, measured-flow,
unit and availability checks.

The runtime also retained its temporary external-demand reservation until the
oldest source timestamp advanced beyond the response window. An unchanged SOC
could therefore suppress charging indefinitely. Expire the reservation against
the local capture clock, before capture reuse is considered. The following
capture uses the current house reading, including the pool's measured load.
Sensor report times and ages remain diagnostic evidence.

Regression coverage sends the real pool intention through `DeviceGateway` and
HA's actual headroom validator: a nonzero charge ceiling or continuing charging
blocks the switch; zero ceiling and idle flow permit it, including an unchanged
source. Runtime coverage proves battery charging can resume after the response
window even when no source reports again. Ownership and explicit handover rules
are unchanged.

## Other log errors

| Evidence | Finding and action |
| --- | --- |
| Three HA socket closures, subsequent HTTP 502s, ten ERROR entries | Each episode is followed by successful reconnection and restored runtime. The log alone cannot establish why HA was unavailable. Retain the existing reconnection and continuity behavior. |
| Twenty `Committed owner removed: sensor.hot_water_energy` rejections | Backend meter-inventory handling was already corrected in `smart-home-solutions-t-by` commit `af1b945`. The later diagnostics contain a ready, validated replacement plan with the new boiler identity. No further change here. |
| Fourteen forecast warnings about missing timestamped `watts` | The parser received no usable timestamp/value series. The log does not preserve those forecast attributes, so provider failure versus source-delivery timing cannot be established. The later accepted plan contains a parsed PV forecast. Do not invent zero production or reuse a forecast as a fallback. |
| Three incomplete Water boiler setup warnings | The later diagnostics show ready mapping, no field errors, and successful Verification. No continuing setup failure is demonstrated. |
| One source-binding/configuration mismatch | Reproduced a settings commit overtaking admission of the preceding configuration's bindings. Hold the existing configuration lock while capturing and admitting bindings. Test the overlap and subsequent admission of the new revision. |
| One replan notification connection warning | An interrupted backend notification request. This log contains no later recovery evidence; the existing notification loop owns retry. It does not explain the pool command's deterministic local rejection. |

Prepared integration `0.9.0-beta.108` and app `0.1.0-beta.70`. Local verification
does not demonstrate physical heat delivery or deploy either release.

Validation: 1,171 integration tests, 142 app tests (including localhost gateway
servers), and 89 frontend configuration tests pass. Compilation, app packaging
and version metadata checks pass. The protected continuity and Verification
handover tests pass without edits.
