# Live battery connection and Sigen commissioning boundary

Status: read-only capture and durable writer fencing implemented; the policy
runtime is not yet connected to physical battery commands. No deployment or HA
configuration changes were made during this investigation.

## What is connected

HA setup opens the battery writer journal before starting the existing controller.
A missing journal initializes legacy ownership; an existing runtime journal opens
fenced, without a grant. Invalid or unreadable journals fail setup. All physical
legacy battery commands, including startup/shutdown restoration, check this fence.
Ordinary legacy shutdown can finish restoration before closing the fence.

The coordinator samples configured house, PV, battery and Planned-device power,
SOC and native control readbacks every five seconds. Each entity retains its own
`last_reported` timestamp; taking a snapshot does not make a stale sensor fresh.
Native number units, range and quantum are inspected independently of electrical
boundary or response claims. A 0.001 kW step means one native watt; it does not
mean one AC watt. Diagnostic samples persist at most once per minute. Schedule
and diagnostics show missing/stale readings separately from policy delivery.

Website Planned membership remains present even if local control setup is
incomplete. Verification devices still belong to that partition. Excluded devices
are omitted from individual capture, and a missing Planned power sensor never
turns into a rated-power estimate or silently disappears into base load.

`MeasurementProfile` is a strict helper for reviewed, disjoint AC source mappings.
Only an explicitly supplied profile and supply scope can invoke the shared
proportional-solar accounting. Production does not currently load or admit one;
raw capture therefore reports `ac_boundary_unverified`. Profiles do not grant
native or writer authority. The production writer has no admitted runtime identity,
so no caller can acquire a runtime grant through this connection yet.

`mark_retained_actuals` can establish a delayed policy's original source cut using
retained counter history. Every stream must retain an anchor at or before the cut.
Partial intervals keep their uncertainty bounds; receipt time never replaces the
source cut. This helper is preparation for the future host connection, not a claim
that live policy settlement is already running.

## Installation evidence collected on 16 September 2026

Read-only HA inspection found:

- `sensor.sigen_plant_total_load_power`: live house load in kW.
- The supplied `sensor.sigen_inverter_pv_power` reported zero but had not reported
  for more than two hours. Its age must not be reset using another sensor's clock.
- `sensor.sigen_plant_pv_power` was reporting zero frequently. It is a candidate
  source to investigate, not an automatically approved AC replacement.
- The native ESS charge/discharge number entities use kW with 0.001 kW steps.
- During Command Charging (PV First), the ESS charge limit was 8.8 kW, measured
  battery power about 8.741 kW, and measured plant AC input about 9.264 kW. These
  are asynchronous observations, not a calibrated loss curve or transition test.
- The local battery mode remained Verification; no service commands were sent.

The [Sigenergy Modbus protocol V2.9](https://github.com/TypQxQ/Sigenergy-Local-Modbus/blob/main/Modbus_reference_documentation/Sigenergy%20Modbus%20Protocol%20EN_V2.9.pdf),
printed pages 15–17, distinguishes ESS limits at the battery connection (point 2),
PCS limits at the inverter AC connection (point 3), and grid limits (point 4).
Registers 40032/40034 are ESS limits and apply globally regardless of EMS mode in
this protocol version. Older V2.5 comments limiting their applicability to command
charge/discharge modes must not be used as current evidence.

**The current `pv-first-v1` catalog's direct native-watt-to-AC-watt binding does not
represent this Sigen ESS interface.** The measured difference is consistent with
the documented distinction. Neither the native quantum nor a configured 95%
efficiency proves the conversion over the supported operating range. Battery
energy changes, inverter AC exchange, gross household consumption and PV generation
must retain their separate physical meanings.

## Design decision and remaining work

The architect review selected the Codex proposal's evidence boundaries and durable
ownership model, retaining Claude's per-entity report-age checks. Shared HA device
IDs were rejected as proof of a shared electrical boundary. Different Planned
meters legitimately live on different HA devices. A whole-house-only workaround
would also discard the agreed supply-scope product design.

An initially proposed change to runtime power reservation was discarded after
reading and testing the actual response fields. `RankedOperation.possible_import_w`
and `possible_export_w` represent battery reservation increments, not whole-house
power. The existing additive reservation code remains unchanged. The future host
must provide the real non-battery physical frame; setting it to zero would conceal
an accounting error.

Before activation:

1. Represent and validate the Sigen native command boundary and AC/storage response
   explicitly, including shared PV, directional conversion, limits and saturation.
   Do not relabel ESS watts as AC watts or multiply by an unverified constant.
2. Establish compatible house/PV/Planned power sources, energy-counter boundaries,
   report age/alignment limits and directional response evidence. Nighttime zero
   PV observations cannot commission behavior under solar production.
3. Validate the intended native modes, settings order, transition effects,
   confirmation and transport timing. A response label is not evidence of a test.
4. Connect admitted evidence, counter ingestion, policy delivery and the actual
   commissioned adapter to `HomeHost`; drain legacy ownership before granting the
   new writer. Persisted uncertainty must retain the fence.
5. Replay the reported incidents and a case where conserving energy is cheaper,
   then perform the coordinated backend/HA rollout.

Tests use explicitly synthetic boundary profiles and grants. They verify source
liveness, proportional accounting, missing setup, historical cuts, persistence
failure, lock drainage, configuration changes, restart and legacy restoration
fencing. They do not certify native response or assert that the live house is now
running the new policy runtime.
