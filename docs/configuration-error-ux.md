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
- Distinguish missing configuration, invalid/duplicate sources, stale or unavailable
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
remain optional. Minimum continuous on/off times remain optional in every mode.

## Required regression coverage

Configuration changes must test the entire correction path, not just exception
wording: missing and duplicate fields, complete enumeration, readiness and permission,
conditional required visibility, error highlighting, correct tab/card/focus navigation,
and warning clearance. Runtime tests must prove that a configuration failure retains
structured correction targets and cannot issue commands. Cross-section prerequisites
must have explicit coverage using the battery case above or the affected feature.
