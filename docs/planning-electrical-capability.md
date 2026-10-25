# Planning electrical capability

Pool ON draw and historical average consumption have different owners.
`forecast_w_by_slot` remains the empirical expected consumption profile.
`active_power_w` for a pool member is its electrical ON bound, captured from the
same `grid_capacity.device_power_bound` evidence used by physical dispatch.

The household requests those bounds through its read-only
`electrical_power_bounds` port. The app reads `planning_power_bounds` from the
HA gateway, which checks the captured mappings against canonical settings and
uses the retained electrical peak evidence. Numeric Power configuration remains
an explicit bound. Sensor mappings use retained measured peaks, conditioned on
the observed native power setting under the existing execution contract.
An instantaneous low reading or a quarter average cannot replace that bound.

Absent electrical evidence remains unknown and produces the existing
`running_power_unknown` warning. No rating is invented from a lower current
sample, a quarter average, or a previous model's ambiguous active-power field.
This does not assert that an observed maximum is a manufacturer's rated limit;
it deliberately shares the existing execution evidence and its limitations.

The producer and consumer must be updated together: integration beta.158 and
app beta.115 add the new source operation and caller. Both components retain the
existing plan schema and command/ownership behavior. No EV departure concept or
cable gate is added, and no restart, model refresh or missing evidence releases
a device.

Tests cover a synthetic 3,600 W supported ON bound alongside 1,200 W live and
quarter-average observations, explicit missing electrical evidence, canonical
mapping mismatch, and unchanged room/EV behavior. The planner repository's
pinned cross-repository qualification executes the full household capture,
planner and physical command path against independently generated inputs.


## Room operating electricity

Room `active_power_w` is expected draw while the actual heater operates. It is
separate from both unconditional `forecast_w_by_slot` consumption and the
conservative maximum retained by the native gateway. An idle thermostat's live
standby watts cannot describe future heating, and a historical peak cannot be
substituted for typical heating input.

`shs_core/room_electrical.py` owns this evidence. Its batched history adapter
reads normalized five-minute power means and actual actuator activity over the
existing completed profile window. A bucket contributes only when every
instant in it has explicit heating activity: thermostat `hvac_action=heating`
or direct switch ON. Climate mode `heat` alone is permission, not activity.
Other actuators sharing the meter must be explicitly inactive. Mixed, unknown
or incomplete intervals are not prorated into fictitious ON energy.

The duration-weighted ON mean, support hours, interval count and mapping
condition are captured in `inference.on_power`; configured numeric Power is
explicit operating evidence. There is no minimum sample count, positive-power
threshold, peak substitution, prior-model substitute or persisted fallback.
Missing ON evidence stays unknown with the existing diagnostic. Unconditional
empirical consumption, comfort intent, native admission and thermal fitting
retain their separate owners. Historical replay captures keep their original
values; correcting current evidence does not silently rewrite history.

The native gateway continues reserving its retained electrical bound. Its
conservative reservation can differ from the planner's expected operating
mean; this correction does not introduce a new planner feasibility policy or
claim that the two quantities are interchangeable. Sparse electrical/thermal
excitation remains a model-quality limitation.

Integration beta.159 and app beta.116 supply this corrected existing field.
No server protocol or Rust action, priority, constraint, EV connection or
EV departure rule changes are required.
