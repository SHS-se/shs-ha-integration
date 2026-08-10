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
- **Category mapping**: in the integration's *Configure* dialog, map your
  energy sensors (kWh, `total_increasing`) to categories: heating, hot water,
  comfort cooling, property energy (ventilation etc.), pool heating, EV
  charging, household, grid import/export, solar production, and total
  consumption. Battery charge/discharge energy can also be mapped for the
  optimisation actuals but is not duplicated into daily energiprestanda.
  Multiple sensors per category are summed.
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
3. Enter the code (within 10 minutes), then open **Configure** to map your
   sensors, forecast sources, measured device capabilities and service
   deadlines. Pool heating also requires an explicit season/enabled entity.
   EV planning requires its energy meter as well as connection, SOC, target,
   departure and real available charging power. These values are required
   rather than guessed.
   Forecast entities must expose timestamped 15-minute values and provenance:
   PV entities use a `watts` attribute plus latitude/longitude, and both price
   entities declare `SE1`–`SE4` as well as `SEK/kWh`.

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
