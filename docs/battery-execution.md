# Sigenergy battery execution

Step 5 is implemented in `battery_policy.py` and `battery_controller.py`.
This is software delivery with a simulated HA plant. It does not commission a
home, grant execution or export permission, or establish physical performance.
The integration still supplies the agreement's default local permission:
revision 0, disabled. The retired battery switch cannot enable the new adapter.
Commissioning must supply the reviewed local permission lifecycle before live use.

## Executable boundary

The adapter consumes only an unexpired v1 `battery_dispatch` instruction whose
complete accepted revision tuple matches the durable local binding and current
agreement. Forecast watts are never actuator input. The existing declarative
Sigenergy preset is the single intent/mode table for all six supported intents.
Only four roles are writable: Remote EMS authority, mode, total ESS charge
ceiling and total ESS discharge ceiling. All device identities come from the
saved registry binding. Entity renames resolve; replacements and changed
capabilities require review. Plant import/export/PV limits and the inverter
adjustment are never written.

Both explicit ceilings must be finite and nonnegative, within reviewed ratings,
and consistent with the intent and the household's grid-charge/export choices.
The policy narrows them using live SOC, the local and objective SOC limits,
export reserve, observed hardware discharge floor, capacity, efficiencies and
remaining interval. Both measured grid balance and PV/load balance bound available
connection headroom conservatively. Self-consumption and solar charging also
bound charging to PV surplus and discharge to household demand. Native W/kW
conversion rounds down to the entity's step. Zero must be representable; the
unset sentinel is rejected in commands, normal profiles and captured ownership.

The adapter runs every five seconds. Every write and each awaited boundary checks
current authority, revision, binding, permission and operating settings. Battery
capacity, efficiencies or connection limits changing under the same plan revoke
its authority and require a new plan. Measurements and settings must be available
and reported within 120 seconds, with at most five seconds of clock skew.

## Transition and observation

Before acquiring ownership, validate the reviewed handover and capture the four
current writable settings in a verified durable journal. Before each service
call, persist its pending role, value and issue time. A failed or uncertain disk
write revokes dispatch authority. No new actuator call follows it.

When claiming Remote EMS, lower both ceilings to zero first. When already owning
Remote EMS, lower each ceiling to the lesser of current and desired before
changing mode. Confirm authority and mode before raising either ceiling. The
15-second acknowledgement bound applies to service completion and fresh setting
readback; it does not demand physical delivery at a ceiling.

Execution reports setting acknowledgement separately from fresh battery, PV,
load, grid and SOC observations after the writes. Matching settings with missing
new physical reports produce `awaiting_observation`, with no active execution
claim. Lower delivery is reported as `below_ceiling`, not a write failure. Flow
above the acknowledged ceiling or conflicting with self-consumption policy
causes handover. A 100 W observation tolerance never enlarges a write ceiling.

The agreement's active report includes a `controls` map keyed by the control ID.
Only a control with acknowledged settings, current accepted revisions and an
observed operation is shown as operating in the portal. Battery operation does
not imply pool operation.

## Ownership, interruption and handover

The verified store is `shs_energy.battery_controller.<entry_id>`, schema version 1.
It retains the owned binding revision, registry identities, expected settings,
pending write and issue time, phase and external override latch. The journal is
not a permission or lease store. Current and previously owned bindings reserve
their actuators until handover completes.

Disable, expiry, invalidation, shutdown and restart hand over using the **owned**
binding's reviewed Maximum Self Consumption profile and normal ESS ceilings.
Remote EMS remains on. A new binding cannot change the old binding's release
profile. Restart revokes execution authority and attempts handover before any
new dispatch; cached plans never resume ownership.

An interrupted or timed-out submitted write must acknowledge before a restoring
write can be sent. The pending record survives failed writes, failed readback,
cancellation, restart and disk failure. Handover remains pending if devices,
readback or physical observations cannot be verified. It is never reported as
complete merely because a service call returned.

External changes to a writable setting latch an override durably. SHS stops
writing and preserves ownership evidence rather than fighting the new owner.
A local administrator can acknowledge a physically completed normal handover
through the existing admin WebSocket endpoint:

```json
{
  "id": 1,
  "type": "shs_energy/controls/setup",
  "config_entry": "<HA entry ID>",
  "action": "review_battery_handover",
  "control_id": "<owned control UUID>",
  "expected_revision": 1,
  "reviewed": true
}
```

This performs no hardware writes. It requires the owned binding revision, no
unresolved pending write, confirmed Remote EMS, the exact reviewed normal mode
and ceilings, and fresh valid physical observations. Only then does it durably
clear ownership and the override. It revokes authority and cannot resume a plan.
The status endpoint and setup discovery expose execution and handover reasons,
plus `owned_binding_revision` and `owned_normal_settings` while ownership remains.

## One-way upgrade

Config entry version 5 removes the old signed power target, sign/unit choices,
mode mappings, remote-authority mappings, measurement mapping, old override
mapping and execution switch. Physical battery model, telemetry and historical
source IDs remain intact. Previous permission intent is listed as requiring
new binding review and commissioning; it is not copied into execution permission.
Current configuration writes reject the removed keys.

Before changing entry options or version, migration reads and validates the
legacy controller journal. Any old battery ownership, including an empty
`originals` snapshot, blocks with `legacy_handover_required`. A corrupt journal
also blocks. The retired executor cannot execute battery plans or issue signed
restoration writes, and the new adapter cannot inherit that journal.

Before upgrading an installation with old ownership, use its installed version's
reviewed disable/handover procedure, verify the plant's normal settings, and allow
the old journal to record successful release. If that procedure fails, retain the
journal and resolve the failure before cutover. Do not delete the file, change
its version by hand, or reinterpret signed values as ESS limits to pass migration.
The new journal's review endpoint deliberately cannot clear a legacy journal.

## Verification and remaining acceptance

Simulated HA tests exercise every pair of allowed intents, ordered claiming,
failures at each claim-transition write, delayed acknowledgements, cancellation,
disk failure before capture and after device acknowledgement, lease/permission
revocation, clock discontinuity, binding edits, manual overrides, renames,
replacements, normal restoration and restart. Policy tests cover independent SOC
reserves, grid headroom, native steps, invalid observations and export permission.
Migration tests prove removed settings cannot be written back and unresolved old
ownership blocks before options change.

The physical tests in [battery-control.md](battery-control.md) remain for bounded
commissioning: real authority behaviour, mode/limit response, household flows,
Standby with surplus PV and observed grid-priority charging behaviour.
