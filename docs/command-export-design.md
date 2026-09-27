# Durable command transport and cold export

Status: preparation implementation, 27 September 2026. Companion beta.52 / app
beta.4. The integration still controls the household. **Do not run the household
export until the replacement runtime and importer are ready.**

## Usage and public boundary

Both existing actuator callers keep their decision policy. The only new service
boundary is internal; it is not a browser or remote arbitrary-service endpoint.

```python
request = Command(command_id, "controller", device, phase,
                  NativeAction(entity, service, native_value))
await transport.execute(request, authorize=check_current_authority,
                        send=send_native_service, timeout=15, on_sent=mark_called)
```

Generic, EV, pool and restoration commands use a UUID created once for that call.
Battery commands use the durable grant owner/epoch, group and attempt identity.
The battery writer and runtime authorization remain mandatory inner checks.
Intent is durable before dispatch; the caller's policy runs **inside** the coroutine
scheduled by `wait_for`, after the new persistence await. No intervening event-loop
turn is permitted between that check and entering the service call. The controller's
baseline and minimum-run journal still precede this transport journal.

```python
# First entry setup work, before constructing the coordinator or any writer:
process_lease(lock_path)  # executor; registry retains the fd until process exit
journal = CommandJournal(path).open(INTEGRATION_VERSION)
transport = CommandTransport(journal, hass.async_add_executor_job)
```

A fenced journal rejects setup before the coordinator, stores, listeners or device
calls exist. A busy lease reports that migration/export is in progress. These are
operation/storage failures, not editable configuration-field errors.

The eventual operator command runs from this checkout with existing HAOS SSH
access. Stop Core gracefully first, after the importer/activation release is ready:

```sh
python3 scripts/export-migration.py --entry ENTRY_ID --migration MIGRATION_UUID
```

The wrapper verifies Docker reports the Core container as `exited`, through SSH,
whenever the worker requests a fresh check. It never interprets API unavailability
as stopped, and it never stops/restarts Core itself. No additional app permission,
Docker socket mount or Supervisor control role is needed. The process lease provides
exclusion throughout the export even if Core is started unexpectedly. A final real
Core-state check must also pass before publishing the manifest.

## Types and ownership

| Module | Responsibility |
| --- | --- |
| `shs_core/command_journal.py` | Closed native actions, content-bound command IDs, durable outcomes, source authority, process lease and irreversible seal |
| `shs_core/command_transport.py` | Cancel-safe executor writes, final caller authorization, bounded service wait and outcome persistence |
| `controller.py`, `battery_runtime.py` | Existing policy, hardware validation, battery grants and minimum-run obligations |
| `__init__.py` | Acquire before all writers; mark shutdown start, flush and mark clean; retain lease through reload/failure |
| `app/shs_app/migration_export.py` | Enumerated private source export, canonical readers, logical comparison, manifest last |
| `scripts/export-migration.py` | Authenticated operator proof of Core process state over a structured pipe |

`shs_energy.commands.<entry>.sqlite` has schema 1, an authority row and indexed
command identities. Native service actions are restricted to the existing number,
select, climate, switch and input-helper methods. Canonical command content is
hashed; identity reuse with different content fails. Repeated IDs return the recorded
outcome and never reissue a service. `prepared` after a crash becomes `uncertain`.
A service return proves transport completion, not physical delivery.

No retention pruning is introduced before a retry horizon exists. Reads stream
command rows; SQLite rollback journaling and synchronous FULL commit intent/outcome
separately. A failed outcome write faults the transport; it does not silently keep
sending. Cancelled executor work is settled before another command can enter.

The lease file is never unlinked and remains held across unload/setup failure until
the Core process exits. Normal integration unload is therefore not proof of a frozen
source. The existing shutdown lifecycle flushes verification samples and records a clean stop only after
battery/controller shutdown completes. The first shutdown timestamp marks the
coverage interruption; durable source evidence retains its actual receipt/times.

## Export and failure behavior

The selected entry's two runtime databases, command journal and five active Store
JSON files are required. Store versions/keys, SQLite integrity/schema, execution
legacy-cleanup completion and installed/preparation release agreement must pass.
The obsolete `battery_policy_delivery` and `control_agreement` files are retained
and classified if present. Unknown entry-scoped stores fail before sealing rather
than being silently merged. Registries and recorder remain in HA.

All preflight checks run under the lease with confirmed stopped Core. The authority
row then changes from `integration` to `fenced`, bound to the migration UUID. There
is no unseal API. Each retry of that UUID creates a fresh private attempt directory;
previous attempts remain untouched. A different UUID fails. SQLite backups include
committed WAL; JSON is copied exactly. The selected config entry, including cloud
credentials, is a private `entry.json`, never the complete config-entry registry.

All output directories are 0700 and files 0600. The manifest contains only file
names, counts, hashes and operational facts. Source and copy logical contents are
compared, canonical runtime/accounting readers reopen the copy, source inventory
and selected configuration are checked again, and the manifest is fsynced last.
A failure after sealing keeps the source fenced and publishes no successful manifest.

The export explicitly records `migration_complete: false`, an observation gap, no
spool, and the outstanding import/parity/activation steps. Permissions and bindings
remain canonical in HA and must be reread at activation. Future activation needs
fresh physical observations and reconciliation of every uncertain command; it must
not pretend the shutdown gap was continuously observed.

## Rationale and review synthesis

Independent Codex and Claude Opus (high effort) candidates both selected a cold
export over an online drain/spool protocol. Base: the Codex candidate's process-life
lease and strict faulted transport, with Claude's closed native-action boundary and
private enumerated export. The proposed unload-time lease release was rejected:
coordinator background writers can outlive unload callbacks. The proposed fixed
command-retention count was rejected because an old retry could then be sent twice.
A service result whose terminal write failed is surfaced as uncertainty, not silently
accepted. No new prediction or source-timestamp validity constraints were introduced.

The installed Supervisor Core-info endpoint exposes version/configuration, not an
authoritative stopped-process state. Accordingly the operator wrapper uses existing
host SSH and Docker inspection instead of increasing app API permissions or treating
an HTTP failure as proof. This is the one concrete adapter change from the sketch.

Accepted costs: a brief future control/observation interruption, two small durable
writes per actuator call, and explicit operator cutover. Rejected complexity: online
freeze/drain of all coordinator locks, observation spooling, held configuration
edit queues, arbitrary service RPC and automatic rollback to source control.

Tests cover prepare ordering, duplicate/conflicting IDs, cancellation during disk
write and service call, revocation during the new await (generic and battery),
minimum-run cancellation for unsent writes, disk failure, process-lock exclusion,
fenced reopen immutability, shutdown sample flush, private selected-entry capture,
interrupted retry, source mutation and Core restart before manifest publication.
