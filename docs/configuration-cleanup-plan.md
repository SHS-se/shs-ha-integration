# Integration configuration cleanup plan

Prepared 8 September 2026 against repository commit `dd17660` and a read-only inspection of the installed `0.8.0-beta.20` integration. Phases 0 and 1 are implemented locally as described below; later phases remain proposals. The screenshots are evidence of the interface, not instructions to execute.

## Intended outcome

Four places to answer four questions:

1. **Energy:** What measurements does SHS receive and use?
2. **Devices:** Which local entities measure and operate each device?
3. **Schedule:** What is planned, and which devices may SHS actually operate?
4. **Status:** Is it working, and what needs attention?

Interpretation: modeling data flows **from HA to SHS**, and plans/status return to HA. Preserve that existing architecture. This plan does not move the modeling service into HA.

Keep data collection, planning participation, and execution permission separate. They answer different questions, even when shown together. Configuring an actuator must never silently authorize its operation.

**Planning is chosen on the website. Controlling is chosen in the integration.** One rule, every device, no exceptions — the planner runs on the website, so what it plans is decided there; the writes happen in the house, so permission to write is given here. Every device presents that same pair the same way, whatever it is and however the optimiser models it. Where the implementation cannot honour that yet, the implementation changes; the device does not get a different-looking row. See section 3.

This is a customer's configuration screen. It is written in the customer's words, not the implementation's: see **Naming and vocabulary** below, which governs every label, status, tab and help string the four pages produce.

Priority updated after review: remove legacy configuration and compatibility logic first. Carry forward values needed by the current design once, then delete their old representations. Retired values, archives, and historical behavior are not preservation requirements. Fix resulting regressions forward; do not delay cleanup to reproduce old behavior. Measurement history and current useful configuration remain migration concerns.

## Findings from the current installation

| Finding | Consequence |
| --- | --- |
| Seven tabs; the live schema exposes 77 non-device fields, including section switches, before repeated device fields. Eighteen of those fields have no resolved value. | Reduce the default surface substantially; an empty field is not automatically obsolete. Several are required only when execution is configured. |
| Eighteen website-requested devices have complete local mappings, while the saved mapping dictionary contains 19 records. | Retain unrequested mappings, but move them out of the active device list. |
| The extra mapping is `sensor.car_charging_lifetime_energy`; the requested EV mapping uses `sensor.tesla_model_y_charge_energy_added`. Both reference the same current actuator. | Investigate the meter semantics and inventory identity before merging. Lifetime charger energy and vehicle energy-added readings are not necessarily interchangeable. |
| Six saved mappings retain old power fields alongside canonical `power`, including hot water and pool heating. | Keep the canonical value, import a needed value once if missing, and delete the old fields. |
| Top-level EV current settings and boiler/pool settings coexist with device mappings. | Audit their consumers. Consolidate only settings describing the same fact; availability, operating limits, and physical measurements can have different meanings. |
| Planning is live, but battery, EV, and pool controller sensors all report `disabled`. | State execution permission explicitly. “Ready” currently does not mean that a device is being controlled. |
| The panel exposes raw plan status `ready`, while the plan-status entity reports `invalid`. The last exchange error is `invalid device (devices[0])`. | Use one validated operational status everywhere. Identify the actual rejected device/field rather than leaving an array index as the main error. The root cause of this rejection was not established by this audit. |
| The pool is collecting history; its configured temperature-band entities have no configured minimum/maximum bounds. | Show learning as progress. Preserve the incomplete band as setup work; never invent its bounds. The message claiming its switches “still run” is not evidence of execution. |
| Thirteen thermal mappings describe twelve rooms. | Model shared rooms once and reference them from devices; do not treat multiple heaters serving one room as duplicate equipment. |
| The local controller implements battery, EV, and pool execution, not generic execution for all 18 mapped devices. | Meeting the requested scope requires additional executor support, not just moving existing toggles. External automation ownership still needs an inventory before enabling those executors. |
| `ev_phase_count` and `ev_charge_efficiency` are active editable options but also listed in `RETIRED_PLANNING_OPTIONS`. Startup removes them and runtime supplies defaults. | Give these current hardware parameters one canonical representation and remove the conflicting legacy logic in the cleanup. Reproducing historical startup behavior is not a prerequisite. |
| The entity search returned 24 matching SHS sensors plus its update entity. Planned-request sensors currently have unknown values. | Unknown values during an invalid plan are not grounds for deleting entities or their history. |
| Every push declares `device_inventory_complete`, and the server retires each stored device absent from that list. | An upload exclusion must not drop a device from the declared inventory. Being uploaded and being present are different facts. |
| A device's planning role is populated from the integration's own suggestion by an insert trigger and is never null, so a deliberate exclusion and a role nobody has ever set are the same stored value. | The integration cannot choose between silence and a warning. The server must record that the choice was made. |
| Every device carries its planning choice as a role on its meter, the pool and vehicle included. The house battery is modelled plant-level, has no such record, and is kept out of the plan only by a local flag. | An internal modelling distinction, surfacing as a device that behaves unlike its neighbours. The server needs a home-level planning choice for the battery. |
| The website may request a current-limit control method that the integration does not implement. The device reports itself unconfigured indefinitely, with nothing said. | Show an unsupported request as unsupported, naming the method and where it was chosen. |
| Nothing anywhere reads `_legacy_configuration_archive`. The retired EV phase count and efficiency survive only inside it. | Read those two back into their canonical keys before the archive is deleted; afterwards the values are unrecoverable. |
| Continuous integration installs no dependencies and runs `unittest discover`. `test_module_boundaries.py` keeps tests out of the Home Assistant modules, where the migration and every save path live. | Their only present coverage is string matching against source text. Migration and save logic must reach the pure tier before any of it can be tested. |
| The panel generates customer-facing labels from stored identifiers — Switch Schedule, Setpoint, Fixed Full Load — beside hand-written jargon such as "Website-requested controllable devices". | No interface text may be produced from a code identifier or name an internal concept. |
| A device's shown name is the live Home Assistant friendly name, which is a meter's name: "Floor heater Energy". Identity is the statistic id and is unaffected by it. | Derive a device name for display, following renames. Never let a name reach the key. |
| The config entry is version 1 with no migration hook, and every startup re-runs the whole conversion. | Adopting the versioned hook raises the entry version, which makes rolling back to an earlier build fail to load rather than degrade. |

The connector's general options response appeared to flatten some nested content. Counts and mapping comparisons above use the integration's own `shs_energy/config/get` response with `refresh_roles=false`, avoiding a requested role refresh/replan.

## Proposed interface

### Naming and vocabulary

No label, tab, badge, status or help string may be generated from a stored identifier, and none may name an internal concept. A customer configuring their house should not have to learn how the integration is built in order to read its own screen.

**Pages.** Four, one word each. The word is what the customer came to look at, not what the code does with it.

| Page | Answers |
| --- | --- |
| **Energy** | What SHS measures and receives |
| **Devices** | What is in the home, and how each one is set up |
| **Schedule** | What is planned, and what SHS may operate |
| **Status** | Whether it is working |

**Write every label that is currently generated.** The panel title-cases raw stored values into the interface, which is where "Switch Schedule", "Setpoint" and "Fixed Full Load" come from. Each needs a written phrase instead, and the shared field schema is where they belong so that one table serves the panel, validation and any future surface:

| Stored value | Shown as |
| --- | --- |
| `switch_schedule` | Turns on and off |
| `setpoint` | Holds a temperature |
| `permit_inhibit` | Allowed to run |
| `variable_power` | Runs at a chosen power |
| `current_limit` | Charges at a chosen current |
| `fixed_full_load` | Runs at full power |
| `variable_full_load` | Varies its power |
| `duty_cycle` | Cycles on and off |
| `inverter` | Adjusts continuously |
| `ready` | Ready |
| `invalid` | Needs attention |
| `not_configured` | Not set up |

**Retire the vocabulary of the implementation** from anything a customer reads: mapping, control mapping, executor, planning path, store, actuator, telemetry, snapshot, watermark, base load, role, and headings of the shape "Website-requested controllable devices". Say what the person would say — the devices SHS can schedule; the switch SHS uses; counted with the rest of the house; the last reading received. Where a fact genuinely belongs to the website, name the website as the place it is set, not as an adjective on the thing itself.

The names above are proposals. Shorter or plainer alternatives are welcome; generated identifiers are not.

### 1. Energy

- Show compact summaries of selected whole-home/grid, solar, battery, device-energy, temperature, and forecast sources. Expand a group to edit it; avoid displaying every entity picker at once.
- Offer an explicit **Review sources from HA Energy** action, with additions/removals shown before applying. Existing reviewed choices remain effective until changed.
- Give each source a live value/unit, availability, purpose, and origin: selected in HA Energy, explicitly configured, or derived.
- Provide one device-meter inventory with an inclusion choice for modeling uploads. Explain what still contributes to aggregate household consumption when individual device upload is excluded.
- **An upload exclusion suppresses readings only.** Every device stays in the inventory the integration declares. That declaration is sent as complete, and the server retires any stored device missing from it — which withdraws the device from the website along with the planning choice made there. Whether a meter's readings are uploaded and whether the device exists are different facts and must travel in different fields. Excluding a device also removes it from the modelled set, so its energy stays inside the household total exactly once, counted neither twice nor not at all.
- Put shared outdoor measurements here. Room observations reference the same device/room records used by Devices; they are not copied into a second editable form.
- Show what is sent: daily aggregates, completed 15-minute readings, compact device profiles and thermal observations, with the existing privacy limits. Preserve all currently shared sources during migration; make new upload exclusions deliberate changes.
- Show solar location inherited from HA as a summary. Expose an explicit override only when needed, retaining all existing overrides. Keep genuinely meaningful electrical limits and hardware parameters available.

Meter category membership, portal device classification, whole-home totals, and control mappings currently serve different consumers. Trace those consumers before unifying their representation. A whole-home meter and its constituent device meters must not be added together as independent loads. Separate telemetry selection from control eligibility and from aggregate baseload accounting.

### 2. Devices

One searchable list, with room/type filters and compact rows. Clicking a row opens one editor containing its measurement sources, actuator mapping, relevant hardware limits, and setup status. Use a shared editor parameterized by device capability and control method.

**Names come from Home Assistant; the key never does.** A device is identified by its statistic entity id, and no rename changes it. The shown name is derived on every read, so renaming a device or entity in Home Assistant renames it here — and, because the name is uploaded, on the website too. Derive it by taking the live friendly name and dropping a trailing meter word (energy, power, consumption, kWh, meter, sensor): "Floor heater Energy" is a meter, "Floor heater" is a device. Keep the full name when nothing would be left. Where two devices derive the same name, separate them by room rather than restoring the suffix. There is no local copy of the name and no rename field: Home Assistant owns it, which is exactly why renaming there is enough.

- **Room heater:** temperature, heater/climate actuator, and power source when used. Show one configured command method; expose alternative command methods only when supported or already populated.
- **EV:** place vehicle telemetry, reviewed current entity/range/step, start/stop entity, and electrical parameters together. Refer to the existing EV meter identity rather than making another device copy.
- **Pool:** place temperature, volume, heater/pump relationships, operating limits, and band entities together. Store the band once for the pool, even if several meters are involved.
- **Battery:** place telemetry, capacity/rating, actuator setup, and limits together. Sign, units, authority, and restoration settings belong to execution setup and remain required where applicable.
- **Hot water and other loads:** show only the supported schedule/permit/variable-power method and its constraints.

Each mapping is edited in one place. Schedule links to this editor without reimplementing it. Website-owned comfort objectives and planning roles appear as summaries with an edit link to their owner.

Devices can retain a clearly marked incomplete draft. Validated runtime configuration remains separate, so saving a partial form cannot replace a working mapping. Use the same Save/Cancel interaction and save-state feedback for every editor.

### 3. Schedule

Make this the default landing page once initial setup is complete.

#### One rule, every device, no exceptions

**Planning is chosen on the website. Controlling is chosen in the integration.**

That holds for every device SHS knows about — a room heater, the hot water, the pool, the vehicle, the house battery, a metered load nobody has looked at yet. Same two lines, same order, same words, same place on the row, whatever the device is and however the optimiser happens to model it internally. A customer reading down the list sees one pattern repeated, not a family of special cases.

Where the current implementation cannot yet honour that for some device, the fix is to change the implementation, not to give that device a different-looking row. Two such gaps exist and are named below; both are server work and both belong in the same release as this interface.

#### The two lines

1. **Include in the plan.** SHS works out when this device should run and shows it on the timeline. Nothing is switched. Off means its energy is simply counted with the rest of the house. **Chosen on the website.** Shown here as a stated fact with a link to where it is set — never as a local control, never as a local copy, so the two can never disagree.
2. **Let SHS operate it.** SHS writes to the device at the planned times. Off means the plan is advice and the device's own controls keep deciding. **Chosen here**, and this is the only one of the two the integration owns.

The division follows the work: the planner runs on the website, so what it plans is decided there; the writes happen in the house, so permission to write is given here. That sentence is worth putting in the interface, once, where both lines meet.

Wording may improve; the separation may not. Neither line sits among the entity pickers — they belong in their own row, visibly apart from the setup fields, so that filling in an entity can never read as granting permission. The second cannot be turned on while the first is off, nor while required setup is missing; in both cases it is disabled with the reason shown, never present and quietly ineffective.

**Only for equipment the home has.** A home without a pool shows no pool row, no pool section and no pool warning. Existence is a third and separate question — is this equipment here at all — answered locally, because the integration is what can see the hardware. It is not one of the two lines and never appears among them; it decides whether the device is listed.

#### A device leaving the plan is a normal outcome

It is not an error, not a warning, and not a degraded state. It means one thing: that device's energy is counted with the rest of the house again. Four of the five obligations that follow are already met and must not regress; the fifth is the work.

- **The setup demand clears itself.** The mapping issue is raised only for devices the website currently asks to be controllable, so a removed device leaves that list and its warning goes with it. Already correct.
- **The local setup survives.** Its entities, limits and room stay saved and out of the active list, ready if the device is included again. Nothing is deleted on removal, and re-inclusion asks no questions that were already answered. Already the intended behaviour, and the reason the plan keeps the unrequested mapping rather than treating it as legacy.
- **Anything being operated is handed back.** When the plan carries no capability for a device the controller restores it and reports it idle with the reason, rather than faulting. Already correct, and the restoration journal keeps that true across a restart.
- **Nothing is written afterwards.** Restoration returns the device to the settings captured before control, and the executor stays out until the device is included again. Already correct.
- **No warning about equipment the customer removed on purpose.** This is the gap, and the first of the two server changes.

#### The two server changes this needs

Both are small, both are contained, and the consistent interface cannot ship without them.

**1. Record that a planning choice was made.** The unplanned-service warning fires when a home has telemetry for a pool, vehicle or battery that nothing is routed to control. It exists for a good reason — a car once sat plugged in below its charge limit for two days while surplus was exported, and every surface said ready — and it should not be deleted. But it cannot tell a deliberate exclusion from an accidental omission, so switching the pool out of the plan on the website would raise "A service is configured but not being planned" and send the customer back to fix what they had just chosen.

The reason it cannot tell is on the server: the planning role is populated from the integration's own suggestion by an insert trigger and the column is not nullable, so "the customer excluded this" and "it defaulted to counted-with-the-house and nobody has ever looked at it" are the same stored value. No client can distinguish them. So the server records that the choice was made — a set-at timestamp or equivalent that survives later pushes — and returns it with the device configuration. The integration then stays silent for a deliberate exclusion and keeps warning when nothing was ever decided.

The suppression itself already exists and already states the principle: a store the customer switched off is not a home missing a control route, and reporting it would send them to the website to fix a meter they deliberately took out of planning. It is keyed on the local equipment flag today; it needs the website's answer as its source instead.

**2. Give the house battery a planning record.** Every other device carries its planning choice as a role on its meter, which is how the pool and the vehicle are reached too — through the role on the meter that heats or charges them. The battery is modelled plant-level rather than as a controllable meter, so no such record exists, and today the only thing that keeps it out of the plan is a local flag.

That is an internal modelling distinction and it must not surface as a battery row that behaves unlike its neighbours. The server gains a home-level planning choice for the battery, editable in the same place as every other one. Until it does, the battery's first line has nothing truthful to show, so this is a prerequisite for the four-page interface rather than a follow-up.

#### What the local equipment flags become

"This home has a house battery / a heated pool / an electric vehicle" stops being a planning switch and answers only what it says. It gates whether the equipment is listed at all, which is what makes a home without a pool show nothing about pools. Today it doubles as the planning switch — the help text says switching one off means nothing is planned — and that is precisely the conflation this split ends.

Migration: an equipment flag currently switched off carries forward as *this home does not have that equipment*. The visible outcome is identical — the section is hidden and nothing is planned — and no customer sees a device start being planned because a switch changed meaning underneath them. Someone who does own the equipment and only wanted it out of the plan turns existence back on and makes that choice on the website, where it now lives. Say this in the release notes.

- Global planning on/off, independent of monitoring.
- One row per device, built the same way for all of them: readings, the two lines above, what is actually happening, and the next scheduled action. Distinguish excluded, collecting history, set up, scheduled, paused, blocked, and unsupported.
- One execution permission per controllable target, expressed as the second line above and identical for every device. Preserve existing settings for battery, vehicle and pool; newly supported device types start off.
- Planning participation is website-owned and stays there. Display it as a stated fact with a link to the page that owns it. There is no local copy and no local override, so the two can never disagree.

Implementation clarification from review: “no local copy” means no independently editable or authoritative local choice. The integration still needs cached server configuration for operation. Show its last refresh and stale/unavailable status; do not promise instantaneous agreement during an outage. The new choice timestamp must record an actual decision, not be backfilled from the existing inferred role, which cannot establish that intent.
- A 24-hour timeline, expandable to the available horizon, with per-device on/off windows, EV current, battery charge/discharge, and temperature targets. Show the present time, plan issue time, and the boundary between binding instructions and advisory future slots.
- Plot planned versus observed activity only where telemetry supports that comparison. A successful HA service call is not proof of physical delivery. Invalid or expired plans must not appear as an actionable schedule.
- Disabling execution returns ownership using the existing documented restoration behavior. Pauses, external overrides, and blocked execution have explicit reasons.

Do not equate one physical system with one energy meter. A pool heater and pump may be distinct measured loads with shared actuator dependencies. The execution registry must prevent conflicting writers and account for shared power exactly once.

### 4. Status

One operational summary, then expandable details for Data delivery, Planner, and Device execution.

- Use the same backend status calculation for this page, the plan page, and HA status entities.
- Separate latest attempt, latest success, accepted-history watermark, current plan validity, and controller state. A historical ready response does not override current validation failure.
- Show actionable issues once, with an affected device and a direct link to its relevant setting. Use only a compact global indicator outside this page, except for an active execution problem that needs immediate visibility.
- Treat normal learning/history accumulation as information with progress and next eligibility condition. A zero count in the latest exchange must not imply that all historical data is missing.
- Show inactive, incomplete control setup in that device's editor. Elevate it when execution is requested or active; never hide missing bounds needed for safe execution.
- Include local-time timestamps, source freshness, expected versus observed values, plan/request IDs, and recent errors in expandable details.
- Add a redacted diagnostics download and a concise migration report listing imported, removed, and invalid settings. Do not retain deleted legacy values in that report.

Existing useful HA entities remain available. New low-level diagnostic entities should default to disabled where appropriate. Before removing or retiring an existing entity, audit automation, dashboard, Node-RED, and external consumers. Preserve its identity/history where retained; never delete simply because it is currently unknown, zero, or inactive.

## Rules for optional fields and duplicates

| Situation | Treatment |
| --- | --- |
| Empty field for an unused optional feature | Remove from the ordinary form; offer a small “Add…” action where the feature is supported. Do not persist empty placeholder values. |
| Field required by the selected command method | Show and validate it, even if empty. Pool band bounds and EV start/stop control are examples. |
| Populated optional field supported by the current design | Migrate and show it in the relevant editor. A populated legacy field is still eligible for deletion. |
| Disabled feature with saved configuration | Collapse it behind a summary, retaining its values. |
| Runtime default or derived value | Show a read-only value and source; allow an override only when it has a real supported purpose. Preserve existing overrides. |
| Value owned by Home Assistant, such as a device or room name | Derive it on every read and show it. No local copy, no local edit field, and never part of a key. |
| Value owned by the website that the customer expects to change here | Either write through to its owner with revision checking, or show it read-only with a link. Never a local copy that can disagree. |
| Equivalent old/new keys | Keep one canonical value and delete the old keys; no archive. |
| Conflicting aliases | The explicit canonical value wins. If it is absent, use a documented one-time conversion; report invalid unresolved setup without retaining alternative legacy values. |
| Semantically different current values | Keep separate fields only where the current design needs both. |
| Unused legacy configuration | Delete the values, archive entries, readers, writers, and compatibility branches in the first implementation phase. |

Do not merge the battery's operating minimum SOC, horizon-end minimum, preferred target, and export reserve: they constrain different behavior. Group and explain them. Likewise, measured power, rated power, a temperature observation, and a writable temperature target are not interchangeable just because they concern the same device.

## Configuration architecture

Introduce a typed canonical configuration with these conceptual owners:

- **Sources:** aggregate/device meters selected for sharing, shared weather/forecast inputs, explicit source overrides.
- **Devices:** existing stable device key, meter references, measurement sources, control method, actuator references, hardware limits, and execution permission.
- **Rooms/systems:** shared room observations, pool band/volume, or other truly shared configuration referenced by device records.
- **Planning:** global planning mode and home-wide electrical constraints; server-owned role/comfort records are synchronized state, not independently editable copies.
- **Migration metadata:** schema version and a concise conversion/removal report, without copies of retired values.

Use a shared field/capability schema for frontend rendering, backend validation, discovery proposals, and the supported service interface. Replace the current broad allowlist that accepts every existing option key with explicit public fields. Unknown writes fail clearly. Delete stored keys outside the canonical schema after extracting any needed current values; do not carry unknown values into an archive.

Split frontend navigation/status/device editors into reusable components. Keep discovery evidence and computed readiness out of editable business configuration. Keep local baseload accounting intact when filtering outbound per-device data.

## One-time migration and legacy removal

1. Define the current schema and classify old fields as current, convertible, or deleted. Limit the inventory to what is needed to perform this cleanup; historical behavior parity is not a release gate.
2. Use Home Assistant's versioned config-entry migration hook and supported update API. Raising the entry version is a one-way door for the customer: Home Assistant refuses to load an entry stored at a higher version than the running integration, so someone who rolls back to an earlier beta gets an entry that does not start rather than one that behaves oddly. Say so in the release notes, and put the version bump in a release worth staying on. Perform old-format reads only inside the one-time migration. Give coordinator storage and the controller ownership journal explicit migration handling if their schemas change. See the [official config-entry migration documentation](https://developers.home-assistant.io/docs/config_entries_index/).
3. Copy useful settings into their canonical destination when needed. An explicit canonical value wins over an alias. Treat zero and false as valid values. Extract any required current value from the existing archive once, then delete the archive itself. **The EV phase count and charging efficiency are the concrete case and the ordering is not optional:** nothing reads the archive today, so a value a customer set survives only in there, and it must be read back into its canonical key in the same migration that removes the archive. Delete first and a single-phase installation becomes indistinguishable from a three-phase one, planned at three times the power its cable can deliver, with no error raised anywhere.
4. Delete obsolete fields and alias values from persisted configuration. Remove runtime legacy recovery, duplicate readers/writers, compatibility branches, and tests whose only purpose is preserving retired behavior. No replacement legacy archive or configuration snapshot subsystem.
5. Preserve measurement history, current stable device/entity/room identities, portal links, history watermarks, calibration, and pending acknowledgements where still used by the current design. Legacy configuration removal does not require rewriting recorder or server history.
6. Carry forward existing execution permissions without enabling new targets. Validate current hardware limits; missing required setup produces a clear error rather than restoring a legacy field or guessing a value.
7. Construct the canonical configuration before committing it. Keep migration idempotent and use supported storage writes. If multiple stores must change, define a restart-safe commit order; do not introduce a general historical recovery framework.
8. Keep the controller's operational restoration journal: it records commands still owned by the integration, not obsolete configuration. Preserve original actuator addresses until handover is complete. Configuration migration itself issues no device commands.
9. Report imported/deleted field names and any current setup errors without retaining old values. The completed migration leaves only the current schema and operational state.
10. Fix migration or runtime regressions forward. Do not restore old compatibility paths, require reproduction of every historical behavior, or hold legacy removal until unrelated defects are fixed.

Historical raw recorder data and SHS server history are not rewritten as part of this UI/configuration cleanup. Any necessary device identity or API-contract change requires a coordinated server migration and explicit history continuity tests.

## Implementation order and completion checks

### Phase 0 — Two fixes everything else rests on

- **Restore the EV electrical model.** `ev_phase_count` and `ev_charge_efficiency` are editable in the panel and members of `RETIRED_PLANNING_OPTIONS`, so every startup moves the saved value into the archive and the runtime default takes over. A single-phase charger set correctly reverts to three phases at the next restart and is then planned at three times the power it can deliver, silently, because a wrong number produces a confident plan rather than an error. This is a live defect in `0.8.0-beta.20`, not untidiness, and it should ship as its own fix rather than waiting inside a cleanup.
- **Move the option transformation into the pure tier.** CI installs no dependencies and runs `unittest discover`, and `test_module_boundaries.py` keeps tests out of `__init__.py`, `config_panel.py`, `configuration.py` and `coordinator.py`. The migration lives in `async_setup_entry` and every save path lives in `config_panel.py`, so none of it can be executed by a test; the existing coverage asserts on source text instead. Extract the transformation into a pure module listed in `PURE_MODULES`, leaving the entry hook as its caller.

**Complete when:** a saved phase count survives a restart, and the migration is a pure function with a fixture test that actually runs in CI. Until then, no completion check below that mentions a test can be met.

**Implemented locally, 8 September 2026:** `migration.py` now owns the option transformation, with registry areas and entity limits supplied by the startup adapter. Phase count and charging efficiency are no longer retired. Archived values are recovered only when the current key is absent, then their archive entries are removed. Explicit current values win. Remaining legacy removal and the config-entry versioned hook stay in Phase 1.

Five behavioral tests cover the sanitized beta.20 fixture, repeated JSON save/reload and migration, explicit-value precedence, later edits, absent settings, registry-assisted mapping conversion, input immutability, and the remaining retirement behavior. The dependency-free Python 3.13 suite passes all 302 tests; compile checks pass. The release manifest is `0.8.0-beta.22`. This has not been deployed or verified by restarting live Home Assistant; restart coverage here exercises persisted option conversion directly.

### Phase 1 — Remove legacy configuration

- Identify the fields needed by the current design and their canonical destinations as part of implementation.
- Import useful values once, delete power/current aliases and obsolete top-level boiler/pool/EV settings, and remove `_legacy_configuration_archive`.
- Remove compatibility branches, repeated startup recovery, and obsolete schema acceptance. Resolve the active/retired EV-key collision in the same cleanup.
- Use a sanitized current-installation fixture to check canonical values and removal, rather than building an exhaustive historical behavior baseline. This depends on the Phase 0 extraction.
- Expect the source-text assertions in `test_supplier_price.py` to fail. They are the only present coverage of this behavior, so replace them with fixture tests against the pure migration rather than repairing the string matches.

**Complete when:** migrated storage contains no retired values or archive, runtime reads only canonical fields, current required settings and execution permissions are carried forward, and rerunning migration does not change the result. Historical behavior parity is not required.

**Implemented locally in `0.8.0-beta.23`:** HA config-entry version 2 calls the pure conversion through `async_migrate_entry`; normal startup no longer converts options. Canonical write contracts reject historical field names. General panel saves send only editable fields, including settings in disabled sections. Device saves no longer invoke migration or retain arbitrary submitted fields.

| Previous representation | Current destination or removal |
| --- | --- |
| `_legacy_configuration_archive` | Recover absent current EV phase count/efficiency, then delete the archive. |
| `_configuration_schema_version`, `_mapping_schema_version` | Removed; HA's config-entry version owns the upgrade. |
| Mapping `power_entity_id`, `power_w` | Import into absent `power`, then delete aliases. Explicit `power` wins. |
| Mapping `current_limit`, old current/power entity keys and amp bounds | Convert once to `variable_power`, `control_entity_id`, `minimum_value`, `maximum_value`. |
| Old EV card observations and electrical facts | Import unambiguous values into their current top-level EV fields, then remove card copies. |
| Top-level EV current entity/range | Import missing fields only into an identified EV mapping, then delete top-level aliases. Hardware selector bounds are not guessed during migration. |
| `_migrated_room_area_id`, `area_id` | Import into the current `room_area_id` association, then remove old fields. |
| Old comfort/override helpers, availability and minimum-run mapping fields | Removed; no current runtime consumer. Current device power and inhibit limits remain. |
| Old top-level boiler/pool schedule, power and enable/confirmation fields | Removed; current runtime uses device models/mappings and the current equipment/execution switches. |
| Unknown options and obsolete discovery evidence | Removed; the explicit current schema determines retained fields. |
| Unrequested device records | Retained under their existing keys. Their current fields survive re-inclusion. |
| Controller ownership journal and recorder/server history | Untouched. Operational handover still uses its original addresses. |

Diagnostics includes an expandable upgrade report containing field names only, with imports, removals, and setup gaps found at upgrade time. It is not a legacy-value archive. Tests cover the conversion fixture, alias precedence, invalid/missing setup, unrequested devices, rejected legacy writes, the actual entry-hook adapter, and the actual frontend save method. CI now explicitly sets up Node and runs that dependency-free frontend test alongside the Python suite. Local checks pass: 304 Python tests, one frontend test, compilation, and diff validation. Live HA has not been upgraded or restarted for this phase.

### Phase 2 — Consolidate current configuration

- Establish the shared field schema, strict public write interface, and canonical runtime accessor.
- Consolidate current device/room/system records without changing measurement-history identities. Retain the extra EV mapping as inactive unless confirmed obsolete; being unrequested alone does not make a current mapping legacy.
- Test current-schema saves, restarts, invalid configuration, and controller recovery. Repair regressions against the new model without reintroducing old keys.

**Complete when:** every current setting has one owner and editor, current values survive save/reload, invalid setup is explicit, and no runtime compatibility path exists.

### Phase 3 — Four-page interface and consistent status

- Land the two server changes first: a record that a planning choice was made, and a home-level planning choice for the house battery. Without them the rows cannot be built consistently and the interface should not ship.
- Replace seven tabs with Energy, Devices, Schedule and Status; implement compact summaries and conditional editors from the shared schema.
- Carry the written labels from **Naming and vocabulary** in that shared schema and delete `_human()` and the hand-written jargon it sits beside.
- Build the two per-device lines as their own row, apart from the setup fields, identical for every device, shown only for equipment the home has, with the second disabled and explained whenever it cannot be used.
- Derive device display names from Home Assistant on every read, following renames without touching keys.
- Add the canonical operational-status view and plan timeline endpoint/visualization. The current config payload does not include the complete schedule or controller reports, so this requires backend work as well as rendering.
- Update documentation and remove stale statements that imply execution is impossible or that mapped switches are necessarily running.

**Complete when:** ordinary laundry setup shows its populated temperature, actuator, and power fields; empty alternative controls are absent. Editing EV or pool setup requires one editor. The panel and status sensor agree on the current invalid-plan case. No customer-facing string is produced from a stored identifier. Renaming a device in Home Assistant changes what is shown and nothing else. Every device — heater, hot water, pool, vehicle, battery alike — shows the same two lines in the same place, planning stated from the website and controlling switched here, with no device presented as a special case. A home without a pool sees nothing about pools. A device the website takes out of the plan produces no warning anywhere, keeps its saved setup, and is handed back if it was being operated. Desktop, narrow-screen, keyboard, unsaved-edit, and error-state checks pass.

### Phase 4 — Complete per-device execution support

- Reuse the existing controller's command ownership, locking, acknowledgement, journaling, and restoration mechanisms through capability-specific adapters.
- Add supported room setpoint, switch-schedule, and permit/inhibit execution paths, plus variable-power support where a valid plan contract exists. Establish the website/plan API requirements before implementing a new adapter.
- Resolve shared actuators, minimum-run/inhibit constraints, local thermostat ownership, and existing automations before enabling a target.
- Keep unsupported methods visibly unsupported; do not create cosmetic enable switches that do nothing. New adapters remain disabled until explicitly enabled. The current-limit method is the live example: the website can request it, the integration has no adapter, and the device reports itself unconfigured indefinitely with nothing shown. Name the method, say it is not supported yet, and say where it was chosen.

**Complete when:** each enabled target demonstrably follows a valid binding slot, respects its hardware constraints and external ownership, and restores correctly on disable, expiry, override, error, restart, mapping edit, or removal from the plan on the website. A failed acknowledgement is visible and is not reported as delivered power.

### Phase 5 — Controlled rollout and final cleanup

- Run repository checks and migration/controller integration tests, then release a beta through the existing release workflow. CI installs nothing and runs `unittest discover`, so a test that needs Home Assistant does not exist as far as the pipeline is concerned: either it runs pure, or the workflow gains a dependency step deliberately and that becomes part of this work.
- Verify current source selections, device/room associations, controller permissions, history watermarks, and telemetry payloads. Check that obsolete keys and the legacy archive remain absent after restart and subsequent saves.
- Verify monitoring and plan delivery before explicitly enabling any newly supported device. Keep invalid/missing-plan behavior covered.
- Legacy configuration removal is already complete in Phase 1. Finish remaining UI cleanup and review public entities separately, including their automation consumers and history. Do not infer entity obsolescence from a single live snapshot.

**Complete when:** the integration uses only the current configuration model, useful current settings are migrated, measurement history is retained, only relevant setup fields are exposed, every supported target has a clear control choice, and operational state is consistent. Any regression is fixed forward rather than by restoring legacy support.

## Main code locations

- `custom_components/shs_energy/config_panel.py`: field schema, payload, validation, save endpoints.
- `custom_components/shs_energy/frontend/shs-energy-config-panel.js`: navigation, editors, repeated warnings/status.
- `custom_components/shs_energy/configuration.py`: defaults, Energy Dashboard inventory and discovery.
- `custom_components/shs_energy/device_controls.py`: current mapping readiness and control routing.
- `custom_components/shs_energy/migration.py`, `configuration_schema.py`: one-time import and strict current write contracts.
- `custom_components/shs_energy/__init__.py`, `config_flow.py`, `const.py`: HA migration hook and version ownership.
- `custom_components/shs_energy/planning.py`, `coordinator.py`: model inputs, exchange state, history and plan validity.
- `custom_components/shs_energy/controller.py`, `sensor.py`: execution, restoration, and public state.
- `tests/test_device_controls.py`, `test_controller.py`, `test_attention_surface.py`, `test_store_toggles.py`, `test_planning.py`: existing coverage to extend; add real config-entry migration/save/reload tests once Phase 0 makes them runnable.
- `tests/test_module_boundaries.py`, `.github/workflows/beta.yml`: the constraint that decides where migration and save logic can live at all. A new pure migration module is added to `PURE_MODULES`.
- `custom_components/shs_energy/frontend/shs-energy-config-panel.js`: `_human()` is the generated-label problem in one function; the written labels replace it.

Phases 0 and 1 change integration code, tests, CI checks, and the release manifest locally. No website code, live HA configuration, actuator permissions, or device states were changed.
