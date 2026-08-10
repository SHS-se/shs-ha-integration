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
- **Automatic setup**: the integration reads the aggregate meters already
  curated in Home Assistant's Energy Dashboard, discovers supported forecast,
  battery and EV entities, and stores one compact configuration. Phase-level
  child meters are not selected when an aggregate meter exists. A multi-step
  manual path remains available for unusual installations.
- **Optional capabilities**: monitoring a category does not make it
  controllable. Solar, battery, pool, water heating and EV planning are
  independent capabilities; a home without any of them is still a valid
  integration and can use price-led planning for the equipment it does have.
- **Demo mode**: creates a clearly labelled synthetic plan for demonstrations.
  Home Assistant never exposes demo plan requests to executor automations.
- **Nightly push**: shortly after midnight the integration reads the previous
  day's change for each mapped sensor from HA's long-term statistics and
  pushes one reading per category. Pushes are idempotent and missed days are
  backfilled automatically (up to 30 days) after downtime.
- **Quarter-hour exchange**: every completed quarter the integration accepts
  only three complete HA 5-minute statistic buckets and sums them into one
  15-minute row. It requests a rolling 72-hour plan hourly. Per-second states
  and raw recorder rows never leave HA.
- **Forecast truth**: PV, supplier import and supplier export forecasts are
  explicit timestamped sources. The two price directions must be different;
  missing/stale data pauses planning instead of being repeated or substituted.
- **Local calibration**: baseload uses separate weekday/weekend median
  15-minute profiles with p10/p90 spread. PV bias is learned by lead day from
  forecast-versus-actual pairs and stays neutral until a lead bucket has 20
  observations. Only the compact profile/calibration summary is uploaded.
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
   planning → Automatic** to use the Energy Dashboard as the source of truth,
   **Manual** to review the advanced capability steps, **Demo** to show the
   feature safely, or **Off** to use reporting and tariffs without planning.

PV forecast entities expose timestamped 15-minute values in a `watts`
attribute. Their location defaults to Home Assistant's configured home
location. Import and export forecasts stay separate: automatic setup calls
`tibber.get_prices` for supplier import and
`nordpool.get_prices_for_date` for export spot, then adds the SHS grid tariff
once in the matching direction. Explicit canonical forecast entities can be
selected instead. The price area is discovered from Nord Pool/Tibber where it
is unambiguous. EV power follows a
configured current entity when available, so a car limited to 5 A is not
modelled as an invented 11 kW load. If there is no departure timestamp entity,
the next configured default departure time is used.

The GUI options are stored by Home Assistant in its config-entry storage. Do
not edit `.storage/core.config_entries` directly. The supported automation/MCP
surface is:

- `shs_energy.discover_configuration`: returns the Energy Dashboard-derived
  recommendation without changing anything; and
- `shs_energy.apply_configuration`: validates and stores an explicit mapping,
  or re-runs automatic discovery for `planning_mode: live` and
  `automatic_setup: true`.

The integration creates *Subscription*, *Grid tariff*, *Current grid cost*,
*Last push*, *Energy plan status*, *Reactive surplus*, planned request sensors
for boiler/pool/EV, and one monetary sensor for every tariff component. Removed
tariff components remain as entities with an inactive state so Home Assistant
retains their history.

## Storage and privacy budget

- At most 96 completed actual rows are sent per home/day.
- Recorder statistics get a one-quarter settling delay; the last accepted
  quarter is re-sent once by idempotent upsert so late fields can be completed
  without creating another database row.
- The large current snapshot/plan is overwritten hourly, not appended.
- The portal retains quarter-hours for 120 days and compact hourly run
  summaries for 30 days.
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
  off request inside a valid plan; and
- `Reactive surplus`: live, non-negative grid export watts for a central local
  allocator.

Advisory slots after either price series ends never produce a local planned
request, even while they remain visible in the 72-hour portal comparison.

For a binary pool/boiler executor, apply this order:

1. safety, manual override, hard temperature/hygiene and completion guards;
2. existing minimum-on/off and coupled pump/heater rules;
3. a planned request above the device's stable power threshold;
4. a reactive request only after surplus exceeds device power plus reserve for
   a stable period; and
5. measured power confirmation before treating the device as running or
   reallocating its watts.

For an EV executor, convert the requested watts to one supported current step,
clamp it to the charger/vehicle limit, retain the departure minimum as a hard
local rule, and confirm achieved charging power. A large unplanned import sheds
eligible loads in reverse service priority. Baseline schedules remain active
whenever the plan is unavailable; they are not deleted or silently recreated.

## Notes

- The server URL defaults to production; point it at the test environment's
  functions URL when developing.
- Only daily aggregates, monthly tariff components, completed 15-minute
  aggregates, and compact forecast/state snapshots leave Home Assistant.
