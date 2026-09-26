# CPU investigation handoff (2026-09-18)

## Problem
With shs_energy enabled, HA CPU (`sensor.system_monitor_processor_use`, 4-core Intel N100) sat at ~30%, rising to ~36–40%. With it disabled, CPU was ~14%. The step from ~21% to ~30% came right after the 03:04Z restart that shipped battery execution accounting (0.9.0-beta.6). The user disabled the integration at 12:48Z.

## How the information was gathered
- **CPU history:** the user's HA history CSV exports in `~/Downloads` (`history (26).csv`, `CPU history.csv`), bucketed by hour and by 10 minutes.
- **Controller diagnostics:** the user's downloads in `~/Downloads` (`shs-controller-diagnostics (27–36).json.gz`). They supplied:
  - `controller_metrics` (tick counts and wall time)
  - the verification journal (samples, evaluations, attempts, configurations)
  - the battery `accounting_journal`
  - the full plan
- **Benchmarks:** the integration's own pure modules run locally against that real data. Use `/opt/homebrew/bin/python3.13` with a scratch venv that has `orjson`. Timings are from an M3 Max; the N100 is roughly 2–2.5× slower.
- **HA MCP tools:** `ha_get_system_health` (hardware, HAOS, MariaDB 14 GB recorder), `ha_get_logs source=logger/system`. HA core had logged SHS entity updates taking 0.70–0.80 s.
- **Read-only SSH** to the HAOS host, `ssh -p 22222 root@192.168.10.20` (BusyBox tools):
  - `.storage` file sizes and counts via `find`/`stat`
  - `docker exec homeassistant grep ...` to confirm Core's `Store` defaults (`serialize_in_event_loop=True`, `STORAGE_DIR`, `async_remove`)

## Findings
Before the fixes:
1. **5 s fan-out.** `async_battery_inputs_refresh` → `BatteryRuntime.refresh()` called `coordinator.async_update_listeners()`. That rewrote every entity twice every 5 s and woke a full controller tick. Metrics: the controller was busy 38% of wall time, with ~3.9 s per tick.
2. **Plan re-validation.** `validate_plan_contract` on the 4.2 MB plan (12 ms per call) ran ~40+ times per 5 s from sensors, selects and `check_authority`.
3. **Battery snapshot cost.** `snapshot()` → `planner_feedback` rebuilt and sha256-hashed the full objective history (17 ms, growing), about 4× per refresh.
4. **Verification journal.** The store (`shs_energy.verification.*`) was 65 MB and rewritten ~5×/min on the event loop (~300 MB/min of disk writes). 58% of evaluation groups differed only by `trigger`.
5. **Store re-reads.** The 7.6 MB coordinator store was re-read and re-parsed ~5× per 5 s.
6. **Evidence leak.** Execution-evidence pages were never deleted: 101,225 files (772 MB) in ~10 h, 6–20k new files per hour.

## Fixes
All are committed on `main` but **not pushed or deployed**. The user deploys with `scripts/deploy.sh`, which installs beta.19; beta.19 includes beta.17 and beta.18.
- **`fc964f1` (0.9.0-beta.17): fixes 1–2.**
  - Coordinator battery channel `async_add_battery_listener`. The 5 s refresh now updates only the battery sensor, the battery execution-mode select, and `controller.publish_battery_status()`.
  - Controller report listeners are scoped per device.
  - `optimisation.PlanContractCache` caches verdicts per plan object and clock regime (issue/expiry comparisons only). `binding_plan_for` went from 9.9 ms to 0.006 ms.
- **`15e36c4` (0.9.0-beta.18): fixes 3–4.**
  - `plan_execution.live_feedback` is the display view: no full-history copy or hash, reused per account object for 1 s (17.5 → 2.5 ms).
  - The planner payload (`planner_feedback`, sha256 included) is byte-identical, verified on real accounts.
  - The journal is now schema 5. Samples moved to `shs_energy.verification_samples.*`, saved with a 600 s delay and flushed on stop.
  - Configurations are stored once per id (94 → 32); they are expanded on load, so the export is unchanged.
  - `trigger` is dropped from the decision signature and kept as `last_trigger` (1,705 → 721 groups).
- **`0824313` (0.9.0-beta.19): fixes 5–6.**
  - `durable_record.DurableRecord` wraps the coordinator store. `async_read` returns the shared read-only copy; `async_load` returns a private copy; saves refresh the shared copy via a JSON round-trip. It holds ~20 MB of RAM.
  - `ExecutionArchive` tracks the pages each saved tree reaches. `collect()` removes up to 500 unreached pages per checkpoint.
    - It runs only after the checkpoint read back from disk names the new root, because HA's Store logs write errors instead of raising.
    - New page writes wait for in-flight removals.
    - The old backlog drains over ~200 checkpoints.

## Still open (suggested next work)
- The decision journal (~16.7 MB) is still rewritten in full on each new decision group (~2/min).
- `_prepared_device_inventory` scans all HA states and registries on every controller tick, via `async_cached_planning_configuration`.
- Per-refresh O(meters) scans (`_meter`, `Account.__post_init__`, MeterIndex rebuild) grow with history. The docs retain full history by design.

## Repo rules
- Bump with `bash scripts/bump.sh beta` in the same commit as any integration code change (AGENTS.md).
- Tests: `/opt/homebrew/bin/python3.13 -m unittest discover -s tests` (809 passing).
- New modules must be listed in `tests/test_module_boundaries.py`. Pure modules must not mention `homeassistant`.
- The user's README/docs edits are uncommitted and unrelated; don't commit them.
