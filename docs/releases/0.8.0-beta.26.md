# 0.8.0-beta.26 — Four-page configuration

Phase 3 is implemented on top of Phase 4. The schema-7 command channel and
journaled controller remain in place; this release does not enable new targets.

- **Energy:** collapsed source groups, live readings and update times, a review
  of HA Energy discovery changes, and per-device sharing choices. Excluding
  individual readings retains the complete device inventory and existing history.
  Excluded devices are removed from the separately modeled load, so their
  consumption remains in household totals once. Shared room observations may
  still be sent for another heater in that room.
- **Devices:** searchable equipment list with room/type filters. The same editor
  handles device sources and controls; EV, pool and battery hardware settings
  each appear in one editor. Empty alternatives are absent; supported optional
  settings can be added. Populated settings and dependent required bounds remain
  visible. Save failures preserve drafts; refresh and discovery preserve edits.
- **Schedule:** the default after setup, with a 24-hour/full-horizon timeline,
  local-time labels, current-time marker, instructions/advice distinction and
  quarter details. Every device uses the same two lines: website-owned inclusion
  and locally owned permission to operate. Observations are labeled separately
  from requests and acknowledgements.
- **Status:** one validated plan-state calculation supplies the panel, timeline,
  HA status entity and current executable slot. Invalid/expired plans have no
  actionable timeline. Details separate attempts, successful exchanges and
  accepted history. Learning is informational; execution failures stay visible.
  The redacted download excludes entity addresses, names, options and raw errors.

Device display names follow live HA names and drop trailing meter words; keys,
room references, recorder history and controller journal addresses do not change.
Current config-entry version remains 4. No compatibility or legacy-value archive
is introduced. Previously disabled equipment flags still mean the equipment is
absent; enable presence locally and choose inclusion on the website if needed.

## Website changes required before upgrading HA

1. Apply `20260908160000_record_energy_planning_choices.sql`.
2. Deploy the updated ingest function and portal along with the Phase 4 schema-7
   optimiser. The website records actual device decisions in `planning_choice_at`
   and adds a home-level battery choice. Existing inferred choices are **not**
   stamped as user decisions. Existing battery planning remains included by
   default, visibly unreviewed, until the customer decides.
3. Upgrade HA to beta.26. Its inventory exchange publishes battery presence and
   caches the website choices plus their refresh time. A missing new website
   contract fails explicitly. Inventory pushes never overwrite choice timestamps.

An exclusion clears obsolete setup/learning warnings, retains saved setup and
hands back controlled devices, including battery/EV with an older cached plan.
Permission can still be switched off when setup or the current plan is broken.
New enables require fresh website choices, complete setup and a usable plan.

## Validation and rollout boundary

333 dependency-free Python tests, 10 frontend behavior tests, 77 website
optimiser/contract tests, and website typechecking pass. The ingest function
also passes Deno checking. Local Chromium checks cover all four pages, desktop
and 390px layouts, keyboard edits, preserved edits during refresh, failed saves,
and absence of browser exceptions or page-width overflow.

The SQL migration has been reviewed but not applied to a database. No code has
been pushed or deployed, and no live HA configuration or actuator was changed.
Database migration, coordinated deployment, HA restart and physical execution
confirmation remain Phase 5.
