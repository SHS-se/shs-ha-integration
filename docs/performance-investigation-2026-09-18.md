# CPU and Supabase egress investigation, 18 September 2026

The supplied controller diagnostics establish substantial avoidable integration
work, but do not measure process CPU. The supplied history CSV contains equipment
states, not HA CPU readings. A before/after measurement on HA is still needed to
quantify the overall improvement and determine whether other integrations also
contribute.

## Home Assistant findings and changes

The captured beta.15 session spans about 32 minutes. It records 162 controller
sweeps triggered by coordinator updates, versus 38 triggered by state changes.
Several devices have 158 evaluations with unchanged inputs. Coordinator-triggered
sweeps accumulated 638 seconds of elapsed wall time, including awaits; this must
not be presented as CPU time. The maximum dispatch delay was 17.35 seconds.

The battery input/status refresh runs every five seconds and notified the same
listeners as shared control changes. Those notifications caused unnecessary
whole-house controller sweeps. Battery status now still refreshes the HA entities,
while a separate control subscription receives ordinary coordinator updates.
Entity events, configuration changes, plan replacement, slot/expiry deadlines,
and device safety deadlines retain their existing scheduling.

The battery account in the capture contains 5,499 meter receipts, 1,649 state
observations and 34 plan admissions. Every checkpoint previously converted the
whole execution session to JSON and hashed its history again, even when the
content-addressed pages were already stored. Checkpoints now reuse unchanged
immutable fields and completed tuple pages by object identity. New receipts and
historical corrections still produce durable pages before publishing the root.
The cache retains the last successfully saved tree; a failed publication does
not replace it. Existing stored roots remain readable without a format change.

Local median CPU measurements over three runs, using the captured account and a
store that discards writes:

| Checkpoint update | Whole-session serialization | Incremental serialization |
| --- | ---: | ---: |
| Counter only | 379.5 ms | 0.030 ms |
| Append one meter receipt | 389.9 ms | 0.894 ms |

These isolate serialization and hashing, not disk I/O, total runtime refresh cost,
or the HA machine's CPU. The first save still serializes the history. Reproduce
with Python 3.13 and the then-current `scripts/benchmark-execution-archive.py <diagnostics.json.gz>`.
That page-store benchmark was retired with the SQLite migration; current storage
measurements use `scripts/benchmark-execution-storage.py`.

Regression coverage verifies that battery status updates still refresh entities
without running other controllers, shared updates still run them, and archive
reuse preserves earlier roots, appended/corrected evidence, and save-failure retry.

## Supabase plan review

The sibling repository's egress plan identifies a retired endpoint that fetched
full plan JSON twice per request. Current HA code no longer calls that endpoint.
The sibling changes remove the endpoint, dedicated exchange module and tests,
generated delivery fixture/tooling, and deployment dependency entry.

A TypeScript syntax-based guard rejects whole plan/snapshot reads, wildcard
reads, omitted select arguments and dynamic selections. It was run before the
removal and caught the obsolete endpoint. JSON-path projections remain allowed.

One discrepancy in the proposed plan: the manual fixed-plan activation endpoint
also selected the full plan and snapshot. Its status polling already uses a narrow
RPC. Activation needs the complete input snapshot for schedule validation, so
that manual read is explicitly allowed. The plan portion has been narrowed to
price outlook, issue time and priority slots, and unused status fields removed.
The three user-triggered website analysis/download exceptions remain explicit.
There is no edge-function exception for a complete plan read.

## Rollout

The HA change is version 0.9.0-beta.16. These are local changes; no live performance
improvement can occur until the integration is installed and HA restarted.

Deleting source does not delete the already deployed Supabase function. After
review/deployment, remove the obsolete TEST endpoint with:

```sh
supabase functions delete energy-battery-policy --project-ref vxqpgbzseckgceopitpm
```

The plan's claimed billing/log totals were supplied evidence, not independently
rechecked against the live Supabase project in this investigation. After rollout,
compare HA CPU and dispatch latency over similar workloads and verify that the
retired endpoint no longer appears in Supabase traffic logs.
