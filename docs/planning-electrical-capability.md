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
