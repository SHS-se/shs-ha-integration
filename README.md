# Smart Home Solutions Energy — Home Assistant integration

Pushes daily energy use, split into energiprestanda categories, from Home
Assistant to your [Smart Home Solutions](https://prod-smart-home-solutions.pages.dev)
energy history. It also calculates the customer's assigned electricity-grid
tariff locally and sends monthly component totals back to the portal graphs.

## How it works

- **Pairing**: generate a single-use pairing code on the *Account* page in the
  SHS customer portal and enter it in the integration's setup dialog. The code
  is exchanged for a long-lived device token stored in your HA instance. You
  can disconnect (revoke) an instance from the portal at any time.
- **Category mapping**: in the integration's *Configure* dialog, map your
  energy sensors (kWh, `total_increasing`) to categories: heating, hot water,
  comfort cooling, property energy (ventilation etc.), pool heating, EV
  charging, household, grid import/export, solar production, and total
  consumption. Multiple sensors per category are summed.
- **Nightly push**: shortly after midnight the integration reads the previous
  day's change for each mapped sensor from HA's long-term statistics and
  pushes one reading per category. Pushes are idempotent and missed days are
  backfilled automatically (up to 30 days) after downtime.
- **Staff-managed tariffs**: SHS staff assign the applicable Ellevio agreement
  and publish effective-dated rate versions. The integration downloads that
  assignment automatically; customers do not enter, copy, or maintain tariff
  terms in Home Assistant.
- **Local cost calculation**: hourly grid import/export statistics stay in Home
  Assistant and are evaluated locally for fixed fees, transfer charges, legacy
  demand peaks, energy tax, VAT, and grid-export credit. Only component-level
  monthly results are returned to the portal. A changed tariff catalogue
  triggers historical recalculation (up to 400 days).
- **Subscription aware**: the integration polls subscription status twice a
  day. If the subscription lapses, pushing pauses and a repair issue appears
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
   sensors to categories.

The integration creates *Subscription*, *Grid tariff*, *Current grid cost*, and
*Last push* sensors. Tariff assignment remains an SHS staff responsibility;
the tariff sensor is diagnostic only.

## Notes

- The server URL defaults to production; point it at the test environment's
  functions URL when developing.
- Only category *sums* per day and monthly tariff components leave Home
  Assistant — no per-device or sub-daily readings are transmitted.
