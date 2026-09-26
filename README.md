# Smart Home Solutions Energy — Home Assistant integration

## Replacement controller specification — 17 September 2026

[Plan execution and deviation accounting](docs/controller-plan-execution.md) is
the normative replacement controller design. The planner owns economic strategy;
the controller follows it, accounts for measured debt/credit and recovers only
within explicit planner authority. It supersedes conflicting economic-controller
requirements below and in earlier design documents. A complete implementation
rewrite is permitted. This documentation change does not change installed control.

## Agreed design; implementation pending — 15 September 2026

HA Devices owns Included/Excluded; the website owns Monitoring/Planned; HA Schedule shows only Planned equipment with Verification (default) or Controlling. Device-specific metadata as well as readings must stop on exclusion. The replacement battery policy communicates an explicit house-supply scope, evaluates measured demand and future cost, and supersedes the v35 rating-wide permission shortcut. The implementation descriptions below retain their deployed-version scope.

See the [agreed participation and battery supply specification](docs/device-participation-and-battery-supply.md).
Documentation only; replacement implementation and coordinated rollout remain pending.

Controller status, 14 September 2026: see the
[architecture review and battery release gates](docs/controller-architecture-review.md)
for completed offline stages, current recovery gaps and the mixed-mode design.
The new runtime is not connected to live control; the review does not recommend
turning battery Controlling back on.

Historical config-entry migration (`0.8.0-beta.27`): read the [four-page configuration release notes](docs/releases/0.8.0-beta.26.md) and [rollout checks](docs/releases/0.8.0-beta.27.md). Deploy the website database/API changes first. Config-entry version 4 prevents earlier builds from loading the upgraded entry.

Pushes privacy-bounded energy data from Home Assistant to your
[Smart Home Solutions](https://prod-smart-home-solutions.pages.dev) portal. It
keeps the existing daily energy/tariff exchange and adds a home-scoped,
15-minute planning path for solar, battery, pool, hot water and EV scheduling.

## How it works

- **Pairing**: generate a single-use pairing code on the *Account* page in the
  SHS customer portal and enter it in the integration's setup dialog. The code
  is exchanged for a long-lived device token stored in your HA instance. You
  can disconnect (revoke) an instance from the portal at any time.
- **One home per credential**: the portal binds the pairing code and device
  token to the selected home. A token cannot submit or read another home's
  optimisation data.
- **Reviewed automatic setup**: the integration reads the aggregate meters
  already curated in Home Assistant's Energy Dashboard and discovers supported
  forecast, battery and control candidates. Its full-page configuration panel
  shows the evidence as a draft before anything is saved. Phase-level child
  meters are not selected when an aggregate exists, and no measured load is
  assumed to be deferrable. A user must explicitly select each flexible load
  on the website, map its local entities and review its operating values in
  Home Assistant. Fixed-load power proposals use only local five-minute
  recorder statistics, with their sample count shown; those source rows never
  leave HA.
- **Optional capabilities**: monitoring a category does not make it
  controllable. Solar, battery, pool, water heating and EV planning are
  independent capabilities; a home without any of them is still a valid
  integration and can use price-led planning for the equipment it does have.
- **Optional scheduled control**: a device Planned on the website with a
  complete local mapping is included in the plan automatically. Its Schedule
  select chooses Verification (the default: requests are logged, not sent) or
  Controlling (requests are sent). The mode never changes the plan: both use the
  same schedule, and switching requests no new plan or replan.
  Controlling executes accepted slots until their explicit `valid_until`,
  subject to local guards; `binding_until` marks published-price coverage.
- **One bad reading affects one device**: a reading that is unavailable, not a
  number or physically impossible (a state of charge outside 0–100%, a pool
  outside −5–60 °C, negative remaining energy) leaves only that device out of the
  plan; the rest of the home is planned as usual, and the panel and the website
  name the reading. Realistic state, such as a car above its charge limit, an
  empty car or a battery below a raised cut-off, is planned as it is.
- **Website-only example**: the portal can render a promotional scenario from
  fixed numbers bundled with the website. Home Assistant cannot create or
  upload demo data, and the ingestion database accepts live plans only.
- **Nightly push**: shortly after midnight the integration reads the previous
  day's change for each mapped sensor from HA's long-term statistics and
  pushes one reading per category. Pushes are idempotent and missed days are
  backfilled automatically (up to 30 days) after downtime.
- **Quarter-hour exchange**: every completed quarter the integration accepts
  only three complete HA 5-minute statistic buckets and sums them into one
  15-minute row. A scheduled exchange uploads the quarter and a fresh snapshot
  and reports runtime. The server re-solves only for a new published price
  release or a manual replan from the website, so an accepted plan otherwise
  runs its original horizon; other changes show a replan recommendation. Failed
  attempts retry through later exchanges. Accepted local slot changes do not require a network call.
  Per-second states and
  raw recorder rows never leave HA.
- **Forecast truth**: PV is an explicit timestamped Home Assistant source.
  Supplier import and export are distinct server-calculated series based on
  public Swedish spot prices and the supplier selected on the SHS home profile;
  missing/stale data pauses planning instead of being repeated or substituted.
- **Local calibration**: each complete Energy Dashboard device gets a
  weekday/weekend empirical 15-minute profile. Only devices classified as
  controllable are subtracted from whole-home history and added back exactly
  once in planner headroom, battery and grid calculations. Every other device
  remains represented by its real measured use inside baseload. PV bias is learned by
  lead day and stays neutral until a lead bucket has 20 observations. Only the
  compact profile/calibration summary is uploaded.
- **Staff-managed tariffs**: SHS staff publish one global, effective-dated
  Ellevio catalogue for every customer. The customer's main fuse and solar
  status come from their SHS home-profile answers; customers do not select or
  maintain tariff terms in Home Assistant.
- **Local cost calculation**: hourly grid import/export statistics stay in Home
  Assistant and are evaluated locally for fixed fees, transfer charges, legacy
  demand peaks, energy tax, VAT, and grid-export credit. Only component-level
  monthly results are returned to the portal. A changed tariff catalogue
  triggers recalculation from the earliest published version for which HA has
  recorder statistics.
- **Subscription aware**: the integration refreshes subscription and tariff status
  through its exchanges; the coordinator has no hourly polling interval. If the
  subscription lapses, pushing pauses and a repair issue appears
  in HA; it clears automatically on renewal.

## Installation

### HACS (custom repository)

1. HACS → Integrations → ⋮ → *Custom repositories*
2. Add this repository URL, category *Integration*
3. Install **Smart Home Solutions Energy** and restart HA

### Manual

Copy `custom_components/shs_energy/` into your HA `config/custom_components/`
directory and restart.

## Setup

1. Portal → Account → *Home Assistant* → **Generate pairing code**
2. HA → Settings → Devices & services → **Add integration** →
   *Smart Home Solutions Energy*
3. Enter the code within 10 minutes and restart Home Assistant after the
   integration is installed or upgraded.
4. Open the integration's **Configure** cogwheel. Energy shows shared readings,
   Devices holds equipment setup, Schedule shows the plan and control permissions,
   and Status explains current operation.
5. Use **Review sources from HA Energy** to create a draft, then save it after
   checking the proposed sources. Discovery never saves by itself.
6. Choose each controllable device and control method on the SHS website.
   Reopen the page or press **Refresh website choices**; restarting the
   integration is not required. Devices excluded from the plan need no control setup.
7. Review local equipment limits, then explicitly turn on **Let SHS operate it**
   for each device you want controlled. Selecting entities does not enable control.

Each controllable-device card is saved independently. A card changes to
**Ready** only after Home Assistant validates the mapping and the SHS server
acknowledges it. Saving a card publishes its mapping and refreshes every
readiness summary on the page without waiting for the next quarter-hour
exchange. Because plans change only on a new price release or a manual replan,
the save sends a fresh snapshot and records a replan recommendation instead of
rebuilding the plan; use **Replan now** on the website's Plan tab to apply it at
once. **Refresh website roles** does the same when the website's roles changed. The top-level back arrow returns to Home Assistant's integration page;
the header Save/Discard actions remain for the non-device configuration tabs.
Leaving with unsaved edits requires confirmation. The website's **Example**
view is independent of this integration.

Current shipped scheduling: heating comfort is configured by **Home Assistant room**, not by Energy
Dashboard meter or entity name. A setpoint mapping selects the room-temperature
sensor and every heater/climate actuator that can serve it. The integration
derives the room from each controlled actuator's entity area or parent device
area. Saving is rejected when an actuator has no area or the selected actuators
belong to different rooms.
Several meters and actuators can therefore share one room objective. The SHS
Comfort tab displays the live room name and those controlled entities. A yellow
quarter means the room must already be at its Comfort temperature when that
quarter begins; the planner may preheat during preceding blue Setback quarters
and stagger recovery across rooms.

Current planned-control cards contain the following setup. The target design below
retires SHS minimum-on/off fields and replaces ordinary hard comfort targets with
curve-valued intent; those changes are not implemented here:

- a switch schedule has its actuator(s), optional companion actuator(s), one
  optional Power field (a W/kW entity or reviewed watts), and current generic
  minimum-on/off settings;
- a setpoint schedule has its measured temperature, optional direct setpoint,
  controlled heater/climate actuator(s), optional companion actuator(s), and
  optional Power field; its room is derived from the controlled actuators, and
  scheduled comfort/setback helpers and reactive override fields are not part
  of this mapping; and
- variable-power control uses one number entity plus optional minimum and
  maximum values. Home Assistant proposes the entity's
  bounds when available, while explicitly entered bounds take precedence.

The integration sends a complete Energy Dashboard device inventory during its
device exchanges. Live friendly names and HA area names supersede older copied
labels, removed devices are retired by the server, and a device that reappears
with the same stable key becomes active again.

PV forecast entities expose timestamped 15-minute values in a `watts`
attribute. Their location defaults to Home Assistant's configured home
location. Supplier and Swedish price area are selected on the SHS home profile.
The SHS service fetches `elprisetjustnu.se`, applies the effective-dated
supplier terms, and returns separate import/export series; no Tibber or Nord
Pool Home Assistant integration is required. For an EV current entity,
automatic setup shows both its raw
selector bounds and the proposed usable minimum, maximum and increment. Those
operating values are saved in the vehicle editor on Devices; this matters when
an entity exposes an `off` value such as 0 A below the charger's real charging
floor. The planner chooses one confirmed valid current for every 15-minute slot
and derives power using the reviewed phase count and voltage. It
never treats the entity's instantaneous state as fixed charger power. Usable
battery capacity is derived from live remaining energy and SOC, charging
efficiency uses the reviewed vehicle setting, and a timezone-aware departure timestamp is
optional. When it is absent, the current target SOC is planned over the rolling
72-hour horizon. Derived capacity is read-only; electrical parameters remain
reviewable in the vehicle editor.

Configuration has four pages: **Energy**, **Devices**, **Schedule** and **Status**.
Setup and permission to operate are separate. Invalid or expired plans never
appear as actionable schedules; normal history accumulation is informational.
See the [beta.26 release notes](docs/releases/0.8.0-beta.26.md) for the required
website database/API deployment before upgrading HA.

The configuration page is available only to Home Assistant administrators and
stores reviewed settings in Home Assistant's config-entry storage. Do not edit
`.storage/core.config_entries` directly. Initial registration still uses the
native pairing dialog; all post-install configuration uses the full-page panel.
The supported automation/MCP surface is:

- `shs_energy.discover_configuration`: returns the Energy Dashboard-derived
  recommendation, per-field provenance and confidence, missing facts, and the
  capabilities requiring review without changing anything; and
- `shs_energy.apply_configuration`: validates and stores only the explicit
  non-device options supplied by the caller. It never re-runs discovery while applying;
  device setup is saved independently from each editor. Website inclusion and
  local permission to operate remain separate; and
- `shs_energy.backfill_prices`: reprices `days` of history and pushes it. Every
  exchange already sends the all-in price for the quarters around it, so this is
  only needed to cover history recorded before the integration started sending
  prices. Supplier prices are re-fetched for the requested dates and combined
  with the effective-dated grid tariff, so a past quarter is resolved exactly
  rather than estimated. Existing quarters are overwritten, so re-running is
  safe.

The portal stores no historical electricity price of its own — deriving one
there would mean a second implementation of the grid transfer and energy tax,
free to drift from the price that actually spent the customer's money. So the
all-in figure the planner optimises against is sent from here and is the only
price the portal's history reporting uses.

The integration creates *Subscription*, *Grid tariff*, *Current grid cost*,
*Last push*, *Energy plan status*, *Reactive surplus*, planned request sensors
for boiler/pool/EV, a dedicated *EV planned current* sensor, and one monetary
sensor for every tariff component. Removed tariff components remain as entities
with an inactive state so Home Assistant retains their history.

The *Total import price* and *Total export price* sensors expose a `forecast`
attribute with 15-minute entries (`start` in UTC and `price_sek_per_kwh`). Each
price includes both supplier and grid charges or credits, from the current
quarter through the end of tomorrow where both prices are published. Missing
prices are omitted; if either source is unavailable the forecast is empty.
The *Grid import price* sensor has no forecast attribute. *Grid export price*
retains its grid-only forecast because export credits can vary by load period.

Every device declared in Home Assistant's Energy Dashboard is also published
as a stable home-local inventory item with complete 15-minute energy values.
The portal proposes one of four editable electrical characteristics: fixed full
load, variable full load, thermostat duty cycle, or inverter load. Suggested
types and empirical weekday/weekend profiles come from Home Assistant. It also
proposes a separate planning role (base load or controllable) and, for
controllable devices, a reviewed control type. Hot water defaults to
permit/inhibit, pool heating to an on/off schedule and EV charging to current
control; all other devices conservatively remain in baseload. Customer and
staff overrides are returned by the backend on the next exchange.

## Storage and privacy budget

- At most 96 completed aggregate rows and 96 values per declared device are
  sent per home/day. No raw state changes or per-second samples leave HA.
- Recorder statistics get a one-quarter settling delay; the last accepted
  quarter is re-sent once by idempotent upsert so late fields can be completed
  without creating another database row.
- The large current snapshot/plan is overwritten whenever it is refreshed,
  normally every 45–60 minutes, rather than appended.
- The portal retains aggregate and per-device quarter-hours for 120 days and
  compact hourly run summaries for 30 days.
- A full retained quarter-hour history is 11,520 sparse rows per home, not
  millions of per-second sensor states.
- HA retains the detailed source history used for local aggregation and
  calibration.

## Scheduled controller

Current implementation, checked 13 September 2026: supported devices execute
locally accepted plans. Configure → Schedule selects each device's participation:
Monitoring, Planning, Control verification or Controlling. Equipment mappings
are on Devices; new control permissions default to off. See
[operating modes](docs/device-operating-modes.md).

The future joint economic allocator and durable restart continuation are design
work, not current runtime features. Their canonical specification is the
[household control design](https://github.com/SHS-se/smart-home-solutions/blob/main/docs/energy-optimisation/reactive-controls.md).

- **Battery:** schema 8 carries explicit operations and separate non-negative
  ESS charge/discharge ceilings, with grid-charge and export permissions.
  Transitions close both ceilings before changing mode. The controller checks
  settings and fresh physical power/direction; a ceiling is not an exact-power
  promise. Forced operations need the expected response. Current autonomous
  operation may deliver less than forecast and report `limited`. Handover uses
  Maximum Self Consumption and fresh configured rated-power limits. See the
  implemented [battery contract](docs/battery-control-configuration.md).
- **EV:** uses reviewed supported current steps and a charging start/stop switch.
  Positive slots set amperes before starting; zero slots stop without writing
  invalid 0 A. Cable state and live SOC/target gate actual charging. The backend
  already plans EV charging while unplugged; projected SOC is conditional on
  that charging occurring. Handover restores the captured current and switch state.
- **Pool:** captures the installed Celsius start/stop band. Heat slots use that
  band; deferral lowers it below measured temperature within reviewed bounds,
  preserving hysteresis. Handover restores the captured band and permission.
  A limiting lower bound is reported as `limited`. The thermostat and shared
  compressor still determine delivered heat; an accepted band is not a meter.

Mapped manual overrides release scheduled authority; unknown override state
prevents execution. Current source freshness windows are 120 seconds for battery
and 900 seconds for EV/pool. Entity limits and supported steps bound commands.

Execution responds to scoped state events, accepted-plan changes, local quarter
boundaries and one-shot confirmation/guard/expiry deadlines. Unchanged reports
can refresh observation age without a full decision. This is not a periodic
five-second whole-controller poll. The accepted plan can continue through later
slots to `valid_until`, subject to guards. Per-device faults and pending
restoration remain observable; failed restoration is retained for retry.

Ownership and original mappings/settings are journalled before writes. **Startup
and orderly unload hand nothing back:** pool, EV and generic devices keep the last
setting SHS sent and resume from the journal (see
[control continuity](docs/control-continuity.md)); the battery runtime still
hands over. The target design replaces routine restart/reload cycling with
checkpoint, reconciliation and adoption of unchanged valid requests. Disable,
genuine expiry and relinquished authority still require appropriate handover.

Controller sensors expose requests, reasons and faults. Battery confirmation
uses mode-aware physical evidence; EV `commanded` and pool `scheduled` indicate
accepted actuator settings, not delivered energy. The Devices controller sensor
covers room/hot-water execution and pending restoration. Current service and
confirmation waits have timeouts but can delay other SHS decisions under the
shared execution lock. Current non-observation failures latch against the
plan/slot and hold the last setting; a new plan/slot clears the latch, and only
the select releases the device.
The target uses a short synchronous event-loop reducer for SHS bookkeeping,
with asynchronous actuator groups and no global action mutex. It assumes users
and other automations can write HA entities. While Controlling, SHS has full
authority over the supported device surface and automatically reconciles/reasserts
its current request over external changes. No external-change hold or explicit
resume is required. Ambiguous commands may be retried under bounded adapter
repeat/ordering rules; possible effects and retry pacing survive replans/restarts. See
[shared-entity reconciliation](https://github.com/SHS-se/smart-home-solutions/blob/main/docs/energy-optimisation/control-reconciliation.md).

Room ranking in the target follows editable temperature-value curves and total
household consequences, not a permanent room priority. There is no planner
or commissioning minimum-runtime setting in the target: economic run length
and a soft heat-pump start cost are separate from native equipment protection.
Current generic relay minimum-on/off configuration and enforcement still exist;
the target explicitly retires those fields and SHS runtime locks. The current schedule UI's
comfort targets remain current implementation, pending that model change.

The replacement design has no fixed slot grid energy budgets: additional grid
energy remains an economic option under real limits. It includes deliberate
economically useful battery drawdown to leave capacity for intermittent PV peaks,
with native fast buffering instead of mode changes for every cloud. It adds
bounded transient sensor degradation and visible control faults. Unplugged but
planned EV charging is one example of how future notifications could work; the
notification framework is out of scope and needs a full specification later.
Desired charging remains independent of current cable/location; actual execution
and achieved service stay separate. These additions are target work.
Direct room +/- and charge-by-time API/entity design is deferred to a later
user-control discussion; these requests must use the same versioned intent owner.

Review mappings and competing automations before enabling the current controller;
its external-change handling is not yet uniform across devices. The target
corrects external user/automation changes while Controlling. Users request via
SHS controls or leave Controlling for Monitoring, Planning or Control verification
to operate elsewhere. Planning is the same in every mode; Verification and
Controlling change only whether SHS writes. Detailed transition behaviour remains
to be specified; logged verification actions never supply real headroom.
This documentation update enables no controls or hardware
writes. An abrupt HA/machine outage cannot execute handover; equipment watchdog
behaviour still requires physical evidence. No independent higher battery backup
reserve is promised for that outage.

## Notes

- The server URL defaults to production; point it at the test environment's
  functions URL when developing.
- Only daily aggregates, monthly tariff components, completed 15-minute
  aggregates, and compact forecast/state snapshots leave Home Assistant.
