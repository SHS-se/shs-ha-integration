# Event admission and paced execution

The Home Assistant companion classifies configured sources by their consumers.
Battery capture inputs, energy counters, device controls, overrides and physical
obligations retain ordered durable receipts. Shared sources take the union of
their interests, including retained ownership and Filter upstream sources.
Reference readings use a replaceable live view; an ordinary report returns before
payload encoding, ownership work and SQLite writes.

Pool temperature becomes replaceable only after a successful paused decision,
an observed OFF switch, and no pending start, active run or restoration. The pause
belongs to the current configuration, policy and scheduled interval. Heating,
cooling after a temperature cutoff, unavailable readings, changed sensor metadata,
overrides and control transitions still receive attention. A physical OFF switch
alone does not establish a pause, including in Verification.

The app promptly accepts ordered meter and complete capture evidence using the
same acceptance rules and received order as before. Ordinary evidence ingestion
does not run economic selection, command driving or execution tracing. Battery
decisions run on the existing five-second cadence. Pending commands, their
confirmations, explicit mode/override changes, recovery and runtime deadlines
retain immediate processing. No new source-timestamp ordering rule is introduced.

An ordinary checkpoint can wait up to the next five-second refresh. A command
transition must commit its dependent evidence before dispatch. The native journal
and app inbox retain the uncommitted prefix; only a completed atomic checkpoint
allows its retirement. Restart replays that prefix. No restart, disconnection,
missing plan, stale input or fault returns a device to its original settings.
Explicit handover, including Controlling to Verification, still restores the
captured settings.

Native journal transactions group only facts already adjacent in the queue, with
FULL durability. Configuration, command and snapshot barriers separate batches.
Notification waits replace idle receipt/request polling. Source checkpoints update
changed rows at the committed prefix. Frozen live frames cannot overwrite a newer
ordered value or a value from another configuration. Immutable plans are adapted
once per replacement; display projection excludes the private policy tree.

## Reproducible evidence

`scripts/benchmark-event-processing.py` uses temporary SQLite and fake native
device ports. It performs no HA calls or physical device writes. The fixture has
540 reports over 60 simulated seconds: four power readings, SOC and four counters
each second, including equal-value later knots and older corrections.

Measured locally on 3 October 2026, comparing core commit `0386d30` with this change:

| Measurement | Previous | Paced | Reduction |
| --- | ---: | ---: | ---: |
| Process CPU, including storage workers | 2,382 ms | 275 ms | 8.7× |
| App checkpoint saves | 1,019 | 36 | 28.3× |
| Named evidence query groups | 5,726 | 656 | 8.7× |
| Preserved meter facts | 248 | 248 | Identical |
| Preserved SOC facts | 63 | 63 | Identical |
| Completed receipt | 540 | 540 | Identical |
| Fake native commands | 2 | 2 | Identical |

Both evidence digests were
`5e2dd98a6eb85c4d5f324774985ecdfd4516fb7962923718f9964c8d02f713cf`.
CPU timing varies by machine and run. This replay excludes reference filtering,
paused pool filtering, native batching, notification polling and plan projection.
It does **not** demonstrate the requested 10× reduction in live process CPU or
physical disk I/O. Checkpoint counts and JSON byte counts are not disk-write bytes.

To reproduce, extract the old integration without changing the current checkout:

```sh
mkdir -p /tmp/shs-event-reference
git archive 0386d30 custom_components/shs_energy | tar -x -C /tmp/shs-event-reference
python scripts/benchmark-event-processing.py --mode legacy --core-root /tmp/shs-event-reference/custom_components/shs_energy
python scripts/benchmark-event-processing.py --mode paced
```

## Live acceptance

After paired app and companion deployment, compare several settled one-minute
intervals with the same devices, modes and source rates. The supplied log baseline
averaged 58.87% of one CPU core, 800.4 app saves/minute and 89,364.8 named query
groups/minute. The corresponding 10× targets are at most 5.89% CPU and 80 saves/minute.
There is no physical disk-byte baseline in that log.

Info logs now show ordered/replaceable source rates, native fact transactions,
app disk-write bytes and write system calls alongside CPU, backlog and saves.
Controller diagnostics retain the raw process I/O gauges and interval deltas.
Linux `/proc/self/io` reports storage writes charged to the app process; write
calls also include sockets and other writes. Sample HA process I/O separately
with `scripts/profile-ha.py` for the companion side. Its totals include other HA
integrations. Compare actual storage bytes for both processes; do not infer the
physical I/O target from transaction counts. Counters reset on restart/reconnect.

This change requires the paired companion's source-admission and notification
capabilities. Upgrade the companion and restart HA Core before using the new app.
The installer recognizes the exact previous `0.9.0-beta.75` companion as replaceable;
unknown local modifications still fail explicitly.
