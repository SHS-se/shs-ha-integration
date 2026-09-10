# Website settings and accepted control planning

Step 4 connects the ordinary configuration pages to the accepted definitions and
the real household optimizer. It adds no battery or pool actuator adapter and
never grants local operation permission. Deployment and commissioning remain
steps 7–8 of the [implementation plan](control-implementation-plan.md).

## Ordinary setup

1. In the website's existing device table, open **Control objective and setup**
   below the relevant device. The battery card uses the same editor. Request a
   supported method, choose whether it belongs in planning, and enter its explicit
   objective. A request can be saved before local binding. Generic contracts are
   listed, but their new objective planners remain pending and excluded; the
   implemented composed planners are battery dispatch and customer-operated pool.
2. For the battery, review reserve, export reserve, maximum SOC, grid charging and
   battery export permission. These source/export choices govern planning; they
   do not enable hardware control. Existing HA settings supply capacity and
   efficiency. The accepted local binding supplies SOC observations and ceilings.
3. For the pool, review the start/stop objective, attributable electrical meters,
   electrical draw while heating, effective heat/electricity ratio including
   auxiliaries, and heat loss in kW per degree of water/air temperature difference.
   HA's existing settings supply water volume and weather. Each model estimate
   retains its provenance; website entries are explicit user overrides. Live
   power observations are separate from running-power estimates.
4. In HA's existing configuration panel, expand **Control interface validation**
   and select **Configure battery / pool interfaces**. Select the requested
   control, its registry-backed interfaces, local operating limits and reviewed
   normal/handover settings. Save the binding. Pool binds only the customer
   request script, correlated feedback sensor and water-temperature sensor.
5. Watch separate setup, synchronization, plan and operation status. Acknowledged
   status names desired, binding and model revisions. The 15-second agreement
   poll requests a fresh household plan after acceptance. A matching plan still
   does not mean that the control is permitted or that hardware executed it.

Category labels do not select the new control association. Load characteristic
is independent of method and has its own planning-model revision; changing it
preserves bindings. The pool's electrical/thermal forecast uses reviewed model
values rather than the meter's category or instantaneous draw. Existing methods,
load selectors and inclusion toggles defer to the new control details for an
associated device. Historical meters and their keys are retained.

Forms hold the revision with the draft. A background refresh cannot turn a stale
form into an authorized overwrite. A conflicting save retains the draft and
requires a reload. Local bindings also use expected revisions and the shared
actuator reservations from step 2.

## Planning and electricity accounting

The authenticated ingest captures the server's accepted household definition.
It does not trust a supplied control context or a different HA binding owner.
The optimizer emits an explicit battery intent and both nonnegative ceilings
alongside its allocation. Grid charging and battery export require the respective
objective permission; export retains its separate SOC reserve. Native increments
round ceilings down from the allocation. Forecast watts remain forecasts.

The pool planner requires measured history for each explicitly counted source.
HA subtracts each such meter from empirical base load once, keeping its original
key. The server uses the same explicit meter allocation for both the auction's
load budget and the final household simulation. It does not use a meter named
"pool heater" as evidence of Nibe compressor consumption. Shared compressor
energy and independently running circulation need attributable measurements;
selecting a whole shared meter is not a substitute for attribution. Do not count
both a canonical source and its alias, or account the same source in a room and
the pool. Missing attribution, history or reviewed conversion produces a visible
planning error.

Heat/defer requests carry the accepted objective. A current, matching customer
feedback report of limited deferral reserves the reviewed electrical draw as
uncontrolled load in the current quarter while the explicit request remains
`defer`. Unavailable-heat feedback suppresses the current heat allocation.
The next observation and plan can change that assessment. Both the sensor report
and feedback payload must be fresh within 120 seconds (at most five seconds in
the future). Stale or mismatched feedback cannot claim current operation.
Battery replanning starts from observed SOC; a ceiling is never evidence of
energy delivered. Detailed actuator acknowledgement and request correlation
belong to the adapters in steps 5–6.

All pool Nibe writes, pump switching, sequencing, triggers, thermostat policy and
physical restoration remain in the customer's automation. SHS neither infers
those internals from temperature limits nor reserves their actuators.

## Publication and boundaries

`publish_control_optimisation` publishes the v1 command plan and existing household
chart/generic-load plan in one PostgreSQL transaction. Agreement changes, another
published plan, a changed fixed-plan revision, wrong home, or a failed row update
refuse the publication. A failed update rolls back the agreement too. An empty
command list represents a household with all composed controls excluded; the
publication boundary still checks that every included control has a command.

A current pinned watt plan has no accepted control revisions or intent policy.
It must be rescinded before planning the new definitions. SHS does not reinterpret
it as a v1 instruction. Pending or invalid definitions cannot publish a mixed
household replacement; collected prices, actuals and inventory keep their
existing storage paths. Generic controls in homes without v1 definitions keep
their existing planning path.

The current composed planner supports one battery and one pool association per
home. Hardware adapters, legacy mapping handover, enablement and real-world
verification remain later steps. No code or schema migration establishes that a
particular installation's electricity attribution or physical execution is valid.
