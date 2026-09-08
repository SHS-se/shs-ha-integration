# Phase 5 beta rollout — 8 September 2026

The beta rollout and persistence checks passed against the user's live HA
installation and the **test** SHS website. Hardware commissioning remains open;
no new control permission was enabled. The production website was not promoted.

## Release evidence

- Website `cdd0ff1`: [CI, migration, functions and portal deployment](https://github.com/SHS-se/smart-home-solutions/actions/runs/34277744281)
  all succeeded on `dev` for the test project. These changes were already deployed
  when this rollout check began.
- HA beta.26 was already installed. Its brief setup retry resolved, and a fresh
  exchange returned explicit per-device commands and home battery inclusion.
- The check found one reporting defect: all inventory items were counted as
  requested while only included meters were counted as ready. `f4705ae` fixes
  both counts using the included equipment shown in the panel.
- [Beta.27 CI](https://github.com/SHS-se/shs-ha-integration/actions/runs/34279143061)
  passed and published [0.8.0-beta.27](https://github.com/SHS-se/shs-ha-integration/releases/tag/0.8.0-beta.27).
  HACS installed that version and HA restarted. The website subsequently recorded
  an accepted plan from beta.27 at 23:15 Stockholm time.
- Local checks: 334 Python tests, 10 frontend behavior tests, compilation and
  whitespace checks passed. The existing CI runs the migration/schema/controller
  logic without HA dependencies; the live API checks below exercise HA persistence.

## Live checks

| Check | Result |
| --- | --- |
| Current configuration model | All 62 persisted top-level keys and every saved device field belong to the current schema. No legacy archive. |
| Save before restart | Saving the existing three-phase EV value changed only the review timestamp. Source selections, inventory and setup stayed identical. |
| Restart | Entry loaded; all resolved settings and persisted key membership exactly matched the pre-restart snapshot. |
| Associations | 19 saved device configurations and 12 room records retained. All 45 active meter identities retained. |
| Public entities | All 25 entity IDs and unique IDs retained. No public entity removed or renamed. |
| Electrical history | Accepted-through marker advanced from 20:30 UTC before restart to 20:45 UTC afterwards. |
| Thermal history | Accepted-through marker advanced from 20:45 UTC to 21:00 UTC. |
| Subsequent saves | A retired `ev_min_current_a` field was rejected before persistence. A same-value current-field save succeeded; only the review timestamp changed. Legacy keys remained absent. |
| Operational agreement | Panel, status endpoint and HA plan-status sensor all reported Ready for the same accepted plan. Latest planning error was empty. |
| Corrected counts | 18 included equipment rows and 18 complete planning setups, including the home battery. This does not mean 18 devices have executable commands or control permission. |
| Execution authority | All local permissions remained off, before and after saves and restart. Controller sensors remained disabled. |

The fresh exchange accepted additional electrical and thermal quarters. The
payload's explicit device commands are visible in the timeline/status endpoint;
hot-water permission is available, and unsupported or unmodelled targets carry
an unavailable reason. A portal check independently showed HA's acknowledgement
and the beta.27 client version.

The bounded post-restart log contained one HTTP 546 response during startup at
23:14. The following exchange at 23:15 succeeded and cleared the current error.
This is a recovered server failure, not evidence of uninterrupted availability;
repeat occurrences warrant a separate server-runtime investigation.

## Remaining commissioning and review

- The current EV inventory key is `sensor.car_charging_total_energy`, excluded
  and not reviewed on the website. The saved configuration for older EV keys is
  retained. Choosing the replacement meter and assigning its setup is a deliberate
  change, not part of the migration verification.
- Room observations are being accepted, but current room commands report no
  executable planning model. Pool-heater history is still accumulating. These
  devices must obtain usable instructions before execution can be commissioned.
- All device permissions are off. Physical actuation, competing controller
  ownership, acknowledgements and hardware handover have therefore **not** been
  proven on this installation. Controller lifecycle behavior is covered locally.
- A connector search found no HA automation/script/scene/dashboard references to
  the SHS sensor prefix. That does not cover external Node-RED consumers or prove
  an entity obsolete. Public entities and their histories were retained.

Phase 5's software rollout is verified. Its physical commissioning completion
condition remains open; do not describe disabled equipment as controlled.
