# Battery execution policy binding

The `battery-execution-policy-v1` contract replaces the runtime's former
exact-anchor, one-millisecond binding. The complete architecture and wire contract
are in [the execution design](battery-execution-design.md).

The backend publishes current operation ceilings and a bounded family of future
continuation cost functions. HA computes the current native response from the
remaining time, stored energy, PV and real household load, then evaluates those
functions at the predicted endpoint energy and terminal import power. The
comparison preserves `C_a - C_ref + F_a = J_a - J_ref`. Gross measured energy is
accounted separately; neither forecasts nor prior consumption create a new energy
allowance.

`battery_execution_policy.py` owns closed parsing and bounded economic evaluation.
`home_runtime.py` is the sole owner of the accepted battery policy, its selected
request, local native catalog, execution scope and writer grant. Its existing
command reducer remains the sole producer of prepared attempts and `Send` effects.
The old `battery_policy.py` reader and its backend compiler remain offline
analytical tools; the runtime no longer accepts that exact-anchor policy format.

## Current conditions and native operations

The execution window has absolute start, refresh and expiry times. Fresh changes
in SOC, PV, load and meter revisions do not require a new policy while the current
conditions remain covered. Changes in configured authority, model, scope, tariff
or future external scenario invalidate their corresponding policy identity.
Expired or uncovered policy cannot authorise new optimisation writes. Expired
conditions also withdraw the desired operation and invoke only approved release;
missing observations immediately after restart wait for fresh evidence.

The six operations retain separate semantics: self consumption, solar charging,
house supply, grid-assisted charging, export and hold. Grid-assisted charging maps
to **Command Charging (PV First)**. **Command Charging (Grid First) is excluded
from every normal operating scenario** because it curtails solar to favour grid
charging. The native catalog checks complete targets, exact actuator quantum,
capabilities and response-model identity. Its software tests are not hardware
commissioning evidence.

Automatic saturation is modelled only at physical capacity and cutoff. An export
that is predicted to cross the configured export reserve is rejected; the model
does not invent a native reserve-stop register. Export additionally requires its
explicit source and price permissions. This first execution profile contains no
PV-curtailment witnesses.

## Mixed modes, ownership and recovery

Only the battery belongs to the new execution policy. Other participants keep
their existing modes and owners. Real external demand and unresolved possible
effects are accounted once; Planning and Verification proposals cannot release
physical headroom or count as delivered service. External projection input has
explicit real/expected-behaviour provenance, separate from conditional plans.

Mode selection is a request, not proof of writer exclusivity. The current mode
and mode revision must match the policy scope; entry or a new mode revision
requires a newly identified scope and corresponding policy. The runtime requires
a confirmed writer grant with an epoch and matching configuration/control-surface
identity. Every send carries that identity for a final dispatch check. Mode exit
cancels unsent optimisation, retains issued uncertainty and executes only the
approved release contract. Release has its own revision and may remain pending or
faulted while the requested mode is passive. Rapid re-entry and restart reconcile
old issued effects before accepting incompatible writes. A fresh re-entry policy
can supersede an unissued release without a baseline cycle.

An unchanged operation renews its request without cycling settings. Existing
prepared attempts retain their original dispatch deadline and durability proof;
a policy refresh cannot extend either. Discretionary changes use a small deadband
and sustained advantage. Physical ineligibility does not wait for that filter.

Policy replacement closes the previous accounting period at the new policy's
source watermark, including when the policy arrives later. Settled prefixes and
retained meter tails are reconciled once. A source cut older than the available
settlement evidence is rejected. Schema-5 checkpoints retain requests, grants,
ledger settlement and unresolved attempts, but restore writer grants as unconfirmed and discard fresh physical observations.
The persisted requested mode remains durable intent. Schema 4 is rejected without a compatibility execution path.

## What this does not deploy

These are software compiler/evaluator/reducer contracts. The installed
`ScheduledController` is still the live owner. Before enabling the new battery
runtime, implement and test the HA event/observation/meter/policy/journal/transport
ports; enforce the same grant at every legacy and new final write path, including
restoration; commission native transition/readback/delay and outage behaviour;
and validate policy coverage, compile latency and economic quality on installation
data. The finite-family scoring guarantee is not a certificate of continuous
optimality or measured hardware response.
