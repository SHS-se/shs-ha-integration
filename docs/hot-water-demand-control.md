# Hot-water permission from house demand

User requirement, 14 October 2026: permit hot water at low house consumption,
inhibit it at high consumption, and let the planner choose the relative levels.
There is no maximum pause, forced 15-minute run or minimum runtime. The old
`max_inhibit_slots` default is removed from settings, planning and execution.
Stored values are retired through the acknowledged configuration writer without
changing execution modes or restoring actuators.

The planner publishes a closed `house_demand_permission` command containing
`allow_below_w`, `inhibit_above_w` and `expected_permitted`. The last field is a
forecast for the displayed plan. Execution reads `house_consumption_power_entity`
in W or kW: gross household load, including the heater, before solar and excluding
battery charging. Low demand permits, high demand inhibits, and the deadband
retains observed switch permission. The planner sizes that band using the
declared heater draw; the controller does not subtract a shared heat-pump meter.

The existing serialized device operation owns observation, authorization,
capture and switching. Changed power readings subscribe its scheduler, including
when no battery is enabled. Native configuration includes the shared sensor
binding. Missing bindings enumerate and link the Energy field; unavailable
readings hold the last setting. Verification observes decisions without schedule
writes. Only existing explicit mode, exclusion and override choices release
ownership. Restarting or losing a plan does not grant heater permission.

Update the integration/app before deploying the producer. Old consumers reject
the new command instead of treating its expected permission as a static command.
The generated producer fixture in `tests/fixtures/schema-8-hot-water-demand-plan.json`
is validated by the actual HA plan reader. Tests exercise real switch transitions,
own-draw stability, long pauses, restarts, missing sources and configuration,
sensor subscriptions, and correction navigation.

See the [planner design](../../smart-home-solutions-t-by/docs/energy-optimisation/hot-water-demand-control-2026-10.md)
for threshold derivation and conditional forecasts. This change neither fixes
winter baseload forecasts nor removes battery target warnings, and does not
establish a hot-water thermal model or alter shared-compressor service priority.
