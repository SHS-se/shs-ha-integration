# Configuration errors and correction navigation

This is the required interaction contract for integration configuration, readiness,
status, and runtime errors. It applies across Energy, Devices, Schedule, and Status.
It supplements the configuration cleanup and device participation designs.

## One actionable result

A user must not discover configuration requirements by repeatedly attempting control,
reading raw exceptions, downloading diagnostics, and searching unrelated cards.
A known configuration problem must identify every invalid or missing field in the
current validation pass and provide a direct correction action for each field.

- Share validation between setup readiness, permission admission, and runtime
  preflight. A card cannot say “Controls configured” when a prerequisite in another
  section is missing. Distinguish configured controls from current operation.
- Carry structured field keys, scopes, device identifiers where relevant, and
  specific messages from the detecting validator. Do not infer destinations from
  exception text or assume that an affected device owns every required setting.
- Resolve those identifiers through the field/editor catalog. A correction action
  must select the owning tab, expand the owning card, scroll to the field, and focus
  it. The same field must be highlighted when the user navigates there manually.
- Show a problem count on the owning section. Render empty required fields directly;
  never hide them under “Add a setting.” Requirements depend on the enabled feature
  and must disappear when that feature is not applicable.
- Use the same field errors on Status, the affected device, and the owning editor.
  Suppress duplicate runtime configuration warnings only when the corresponding
  setup warning already provides the correction. Keep unrelated failures visible.
- Distinguish missing configuration, invalid/duplicate sources, unavailable
  readings, insufficient history, and service failures. “Waiting for measurements”
  must not describe an unconfigured source. Do not route non-configuration failures
  to arbitrary setup fields; provide source-specific or diagnostic actions instead.
- Changing configuration never grants control permission. Clearing a warning must
  follow fresh validation. Retain execution safety checks after UI validation.

## Battery measurements

An included house battery requires four distinct instantaneous power sources for
control. The shared validator checks all missing and duplicate bindings together.

| Setting | Editor | Meaning |
| --- | --- | --- |
| Measured battery power | Devices / House battery / Controls | Signed terminal power: positive charging, negative discharging |
| Instantaneous house consumption | Energy / Solar and electrical measurements | Gross appliance power excluding battery charging |
| Instantaneous solar production | Energy / Solar and electrical measurements | Reported plant solar power |
| Signed grid power | Energy / Solar and electrical measurements | Positive import, negative export |

The three Energy fields are visible and marked required while the battery is
included. A missing shared measurement makes battery setup incomplete even if its
actuators and modes are configured. The battery card and Status link to the Energy
fields; they must not offer a generic “Edit house battery setup” for those errors.
Export-only power, energy totals, and forecasts do not satisfy the signed-grid or
instantaneous-power requirements. With no included battery, these shared fields
remain optional. SHS minimum continuous on/off settings were retired on 4 October 2026 and are absent from every mode.

## Measurement issues are not configuration errors

A reading that is unavailable, not a number or physically impossible is a source problem, not a setup failure. It leaves only
its own device out of the snapshot and the plan, exactly as that store's switch
would; the rest of the home is planned as usual. The plan's `measurement_issues`
name each reading. The panel shows them above every tab with **Inspect source
entity** for each sensor, and the affected device's planning status says why it
has no plan. Do not route these to setup fields or block unrelated readiness. A
missing or mistyped entity, or a sensor with the wrong unit, remains a
configuration error with its field targets. When the reading recovers, the server
recommends a manual replan, because plans are kept between price releases.

Source timestamps and report ages are diagnostic information only (user
requirement, 15 October 2026). They must not create field errors, source-repair
warnings, readiness failures, verification faults or planning exclusions. An
available unchanged reading does not need periodic reports to remain usable.

## Required regression coverage

Configuration changes must test the entire correction path, not just exception
wording: missing and duplicate fields, complete enumeration, readiness and permission,
conditional required visibility, error highlighting, correct tab/card/focus navigation,
and warning clearance. Runtime tests must prove that a configuration failure retains
structured correction targets and cannot issue commands. Cross-section prerequisites
must have explicit coverage using the battery case above or the affected feature.

## Every failed stage is visible

User requirement, 15 October 2026: any failure SHS can observe while collecting
or sending inputs, obtaining a server plan, receiving or accepting it, reporting
acceptance, or carrying out its commands must be shown prominently in the UI.
Logs and diagnostic attributes alone do not satisfy this requirement.

The global attention notice remains above Overview, Schedule, Controller,
System and Settings, including retained snapshots and interrupted startup.
Identify the affected stage or device and the reason, with the existing field,
source-entity, website or diagnostic action. A retained valid plan is still
shown, alongside failures affecting its execution or replacement.

Clear each failure only when that same stage succeeds or its own execution
status recovers. A telemetry upload does not prove plan delivery, acceptance or
execution. Beginning a retry proves no recovery. Existing receipt identities
and serialized owners prevent stale completions from clearing newer failures.
Ordinary Verification, deliberately stopped devices and unchanged available
sensor timestamps do not create failure notices. Failed command/deadline waits
are visible immediately; their reporting does not change control or handover.
