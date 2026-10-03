# Integration changes

Follow [the constraint requirements](docs/constraint-requirements.md). Do not
invent or restore arbitrary validity constraints, including prediction bounds
or source-timestamp event ordering, unless the user explicitly requires them.

Whenever integration code changes, bump `custom_components/shs_energy/manifest.json`
with `bash scripts/bump.sh beta` and include the version bump in the same commit.
The Beta workflow publishes the manifest version and fails if that version has
already been published. Documentation-only changes do not require a bump.

Follow `RELEASING.md`; do not create release tags manually.

# Control continuity

Follow [control continuity](docs/control-continuity.md) for every device, the
battery included, and read its History section before touching release logic.
Only an explicit control-mode, exclusion or override setting may change a
device's settings:

- A restart, a lost app or socket, a settings or metadata change, a missing or
  expired plan, stale readings and faults all hold the last setting SHS sent. Do
  not add a handback, fallback or "safe default" write for any of them, in the
  app, the core or the Home Assistant gateway.
- Leaving Controlling on the select, for Verification too, hands the device back
  to its captured settings. Do not change that to leaving the device as it is.

Both halves have been lost before through unrelated work whose tests were
rewritten to match. A change to when a device is released needs the user's
explicit requirement. If a change makes `tests/test_control_continuity.py`,
`tests/test_battery_continuity.py`, `tests/test_pool_switch_gap.py` or the
Verification handover tests fail, stop and ask instead of editing them.

# Configuration UX

For configuration, readiness, status, or controller-error changes, follow
[the configuration error UX contract](docs/configuration-error-ux.md).
Known setup failures must enumerate their fields, highlight their real editors,
and link directly to those fields; cross-section requirements must affect readiness
and required-field visibility. Add regression coverage for the full correction path.
