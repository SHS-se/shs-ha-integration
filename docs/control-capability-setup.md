# Local control capability setup (step 2)

Step 2 adds local discovery, complete binding validation and durable reservations.
It does not implement the new battery/pool executors, website revision agreement,
leases or commissioning. No new control can be enabled through this setup API.
Current monitoring and executable plan schema 7 remain in place.

## Available behavior

- `control_capabilities.py` supplies the same primitive description/value checks
  used by `ScheduledController.command()` and local setup. Switches need no
  appliance brand/model. Numbers require observed units, physical bounds/steps
  and declared role; unknown units and out-of-step values fail explicitly.
  Climate writes require advertised single-target support, target step and an
  active Celsius heating mode. Select writes require an advertised option.
- `control_setup.py` resolves exact `(domain, platform, unique_id)` identities,
  validates complete role sets, local limits and reviewed normal/handover
  profiles. It does not copy live state into configuration or infer a rating
  from current watts. Discovery offers candidates without selecting one or
  overwriting a saved user value.
- `control_presets.json` defines generic roles and Sigenergy proposals/mode policy.
  Sigenergy candidates require manufacturer metadata and semantic name tokens;
  multiple candidates remain visibly ambiguous. Bindings must reference one
  confirmed plant device. Non-negative ESS ceilings are distinct from signed
  inverter adjustments, and the unset ESS sentinel is never a normal profile.
- Pool setup accepts only a customer request script, feedback observation and
  water-temperature observation. The script service schema must accept a
  `request` object, and the customer must review v1 request handling, expiry,
  feedback correlation and release behavior. These are explicit declarations,
  not proof of physical operation. The script's internal Nibe/pump actions are
  never enumerated, reserved or executed by SHS.
- Setup reserves all writable roles, including expanded HA group members. A
  group and its member cannot have separate SHS owners. Existing generic/system
  mappings and pending restoration journals also reserve their known entities,
  even when permission is currently off. A previous mapping may therefore need
  a reviewed cutover before the new proposal can become locally valid.
- Local saves use an integration-entry Store, scoped to the configured home,
  and a shared HA save lock. A save must match its expected binding revision.
  Atomic Store writes are verified by reading our own disk envelope before the
  saved response. This detects logged write failures and refuses saves while HA
  is stopping. Disk failure leaves the prior
  memory/reservations intact. Reload restores the same revision/reservations,
  never an execution lease or permission. A lost response can be resolved by
  reading the saved revision and resubmitting the same definition idempotently.
- Registry renames update resolved display names without changing identity or
  revision. Missing/replaced identities require mapping. Capability/group edits
  invalidate the local report until reviewed and saved at a new binding revision;
  original member reservations remain held in the meantime. A hardware change
  during save is reflected in the returned report instead of stale readiness.

## Inspect and save

The existing Devices page contains **Control interface validation** with local
reports and missing battery/pool setup. It distinguishes “Validated locally ·
Control off” from execution readiness. Step 4 will connect the ordinary setup
editors and website requests to these definitions; the admin WebSocket endpoint
makes the local layer inspectable and usable in the meantime.

All operations require a loaded SHS config entry and HA administrator access:

```json
{
  "id": 1,
  "type": "shs_energy/controls/setup",
  "config_entry": "<HA config entry ID>",
  "action": "discover"
}
```

Discovery returns normalized `interfaces`, role-specific `candidates`, declarative
`presets`, current `controls`/missing `targets` and `saved_setups`. It performs no
service calls or settings writes. Entity identities are local installation data;
this response is not uploaded to the website.

For `validate`, supply a complete `setup` object. For `save`, also supply
`expected_revision` (0 for a new server-issued control ID). A minimal generic
relay proposal is:

```json
{
  "control_id": "00000000-0000-4000-8000-000000000002",
  "contract": { "name": "relay_schedule", "version": 1 },
  "roles": {
    "output": {
      "domain": "switch",
      "platform": "<observed integration>",
      "unique_id": "<observed registry unique ID>"
    }
  },
  "limits": { "minimum_on_seconds": 30, "minimum_off_seconds": 60 },
  "normal_profile": { "output": "off" },
  "handover": "restore_reviewed_profile"
}
```

IDs and values above are illustrative, not installation defaults. The server
allocates the control ID; local setup never invents/rekeys a historical meter.
The configured integration entry determines home scope. Endpoint saves cannot
modify that scope. Server-side ID membership and accepted desired revisions are
part of step 3. Local validation reports `agreement: not_evaluated`; the separate
`control_agreement` status reports persisted/server acknowledgement and authority.

`status: ready` means only that the complete local binding proposal validated.
`errors` carries field paths/codes/messages. `control_enabled` is always false.
Unknown fields (including attempts to add permission) are rejected. An invalid
proposal is not saved; the `validate` response lets an editor retain a draft.

Battery limits use W and percentage points:
`charge_max_w`, `discharge_max_w`, `normal_charge_w`, `normal_discharge_w`,
`minimum_soc_pct`, `maximum_soc_pct`. Both ESS ceiling roles are checked against
native W/kW ranges and steps. `normal_profile` is
`{"mode":"Maximum Self Consumption"}`; `semantics` is
`{"preset":"sigenergy","measurement_charge_positive":true}`. This setup
supports validation only; allowed intent commissioning remains step 5/8.

Pool limits are `minimum_c`, `maximum_c`, `step_c`; normal profile is
`{"policy":"customer_automation"}` and handover is
`release_customer_automation`. `interface_review` must explicitly contain
`request_contract_version: 1`, `expiry_handling_reviewed: true`,
`feedback_correlation_reviewed: true`, `release_policy_reviewed: true`.
No direct Nibe or pump roles are accepted. The agreed objective belongs to the
website and is not duplicated as a local setting.

Generic temperature/current/power outputs have `minimum`, `maximum` and a reviewed
`normal_profile.output`. Current limits require meaning `current_limit`,
`phase_count` (1 or 3) and `voltage`. Percentage outputs require meaning
`output_percent` and `rated_power_w`; W outputs require meaning `power_limit`.
Permissions require `maximum_inhibit_seconds`, explicit `permit_state` polarity
and a normal output. None of these energy models turn a switch into a variable
power actuator.

## Integration boundary and validation

Versioned local bindings live at `shs_energy.control_bindings.<entry_id>` so they
can be durably saved before any later acknowledgement. This is the new binding
representation, not an alternate executable inventory. Existing option mappings
are not automatically converted or removed by step 2. The one-way migration and
old ownership-journal release remain governed by the step-1 migration spec.

The new save path does not publish an acknowledgement to the website. Existing
schema-7 mapping-report synchronization is replaced during step 3; do not treat
its old readiness fields as agreement with these new bindings. The full physical
battery/pool command adapters remain steps 5/6. No live settings were changed or
deployment performed as part of this implementation.

Tests exercise primitive execution, complete target setup, wrong units, sentinel
rejection, overlaps/cycles, registry rename/replacement, capability changes,
failed/interrupted/concurrent saves and schema-1 local-binding projection. Run:

```sh
python -m pip install -r tests/requirements.txt
python -m unittest discover -s tests -v
python -m compileall -q custom_components/shs_energy
node --test tests/frontend-config-save.test.js
```
