# Smart Home Solutions Energy — Home Assistant integration

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
  forecast, battery and control candidates. It then shows its evidence before
  saving. Phase-level child meters are not selected when an aggregate exists,
  and no measured load is assumed to be deferrable. A user must explicitly
  select each flexible load and review its operating values. Fixed-load power
  proposals use only local five-minute recorder statistics, with their sample
  count shown; those source rows never leave HA. A multi-step manual path
  remains available for unusual installations.
- **Optional capabilities**: monitoring a category does not make it
  controllable. Solar, battery, pool, water heating and EV planning are
  independent capabilities; a home without any of them is still a valid
  integration and can use price-led planning for the equipment it does have.
- **Website-only example**: the portal can render a promotional scenario from
  fixed numbers bundled with the website. Home Assistant cannot create or
  upload demo data, and the ingestion database accepts live plans only.
- **Nightly push**: shortly after midnight the integration reads the previous
  day's change for each mapped sensor from HA's long-term statistics and
  pushes one reading per category. Pushes are idempotent and missed days are
  backfilled automatically (up to 30 days) after downtime.
- **Quarter-hour exchange**: every completed quarter the integration accepts
  only three complete HA 5-minute statistic buckets and sums them into one
  15-minute row. It refreshes the rolling 72-hour plan before expiry and
  retries on the next quarter after a failed attempt. Per-second states and
  raw recorder rows never leave HA.
- **Forecast truth**: PV, supplier import and supplier export forecasts are
  explicit timestamped sources. The two price directions must be different;
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
- **Subscription aware**: the integration polls subscription and tariff status hourly. If the
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
3. Enter the code (within 10 minutes), then open **Configure**. Choose **Live
   planning → Automatic** to discover from the Energy Dashboard and review a
   compact proposal, **Manual** for every advanced mapping, or **Off** to use
   reporting and tariffs without planning. The website's **Example** view is
   independent of this integration.

PV forecast entities expose timestamped 15-minute values in a `watts`
attribute. Their location defaults to Home Assistant's configured home
location. Import and export forecasts stay separate: automatic setup calls
`tibber.get_prices` for supplier import and
`nordpool.get_prices_for_date` for export spot, then adds the SHS grid tariff
once in the matching direction. Explicit canonical forecast entities can be
selected instead. The price area is discovered from Nord Pool/Tibber where it
is unambiguous. For an EV current entity, automatic setup shows both its raw
selector bounds and the proposed usable minimum, maximum and increment. Those
operating values, phase count and voltage must be reviewed explicitly; this
matters when an entity exposes an `off` value such as 0 A below the charger's
real charging floor. The planner chooses one confirmed valid current for every
15-minute slot and derives power from the commissioned phase count and voltage;
it never treats the entity's instantaneous state as fixed charger power. If
there is no departure timestamp entity, the next configured default departure
time is used.

The GUI options are stored by Home Assistant in its config-entry storage. Do
not edit `.storage/core.config_entries` directly. The supported automation/MCP
surface is:

- `shs_energy.discover_configuration`: returns the Energy Dashboard-derived
  recommendation, per-field provenance and confidence, missing facts, and the
  capabilities requiring review without changing anything; and
- `shs_energy.apply_configuration`: validates and stores only the explicit
  mapping supplied by the caller. It never re-runs discovery while applying.
  Enabling pool, water heating, or EV planning requires the corresponding
  `*_deferrable_confirmed` value; EV planning also requires
  `ev_electrical_confirmed` after phase count and voltage are reviewed.

The integration creates *Subscription*, *Grid tariff*, *Current grid cost*,
*Last push*, *Energy plan status*, *Reactive surplus*, planned request sensors
for boiler/pool/EV, a dedicated *EV planned current* sensor, and one monetary
sensor for every tariff component. Removed tariff components remain as entities
with an inactive state so Home Assistant retains their history.

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

## Planned and reactive automations

The integration deliberately does not turn relays or chargers on directly.
Commission one visible executor automation (or an existing Node-RED gate) per
device. Both planned and reactive logic feed that same executor so they cannot
race each other.

The executor should consume:

- `Energy plan status`: only `ready` authorises a planned request;
- `<device> planned request`: bounded watts for the current binding quarter;
  unavailable means the planner has no authority, while zero is an explicit
  off request inside a valid plan. For the boiler specifically, reviewed rated
  watts means the thermostat is permitted to cycle and zero means inhibited;
  the separate empirical `expected_power_w` attribute is never a forced-on
  command; and
- `EV planned current`: the forecast target in amperes plus the current
  quarter's deadline-safe minimum and hardware maximum as attributes; and
- `Reactive surplus`: live, non-negative grid export watts for a central local
  allocator.

Advisory slots after either price series ends never produce a local planned
request, even while they remain visible in the 72-hour portal comparison.

For a binary pool executor, apply this order:

1. safety, manual override, hard temperature/hygiene and completion guards;
2. existing minimum-on/off and coupled pump/heater rules;
3. a planned request above the device's stable power threshold;
4. a reactive request only after surplus exceeds device power plus reserve for
   a stable period; and
5. measured power confirmation before treating the device as running or
   reallocating its watts.

For a water boiler, keep the local thermostat in charge of cycling. Treat a
positive boiler request only as permission and zero as a temporary inhibit,
after applying hygiene, hard-temperature, manual and maximum-off guards. Never
turn the element on merely because `expected_power_w` is positive.

For an EV executor, begin from `EV planned current`, then let the local reactive
controller trim it in supported steps inside the published minimum/maximum
envelope using actual import/export and battery state. Track delivered energy
against the departure obligation, retain local connection/SOC/manual gates,
and confirm achieved charging power. A large unplanned import sheds eligible
loads in reverse service priority. Baseline schedules remain active whenever
the plan is unavailable; they are not deleted or silently recreated.

## Notes

- The server URL defaults to production; point it at the test environment's
  functions URL when developing.
- Only daily aggregates, monthly tariff components, completed 15-minute
  aggregates, and compact forecast/state snapshots leave Home Assistant.
