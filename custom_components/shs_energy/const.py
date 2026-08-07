"""Constants for the Smart Home Solutions Energy integration."""

from __future__ import annotations

DOMAIN = "shs_energy"

# Supabase edge-functions origin (prod). Overridable in the config flow so the
# same build can point at the test project (vxqpgbzseckgceopitpm).
DEFAULT_BASE_URL = "https://oosxndduqzhvrorgogaw.supabase.co/functions/v1"
CONF_BASE_URL = "base_url"
CONF_DEVICE_TOKEN = "device_token"
CONF_DEVICE_TOKEN_ID = "device_token_id"
CONF_CUSTOMER_NAME = "customer_name"
CONF_PAIRING_CODE = "pairing_code"
CONF_DEVICE_NAME = "device_name"

# Options: each category maps to a list of energy sensor entity_ids
# (total / total_increasing kWh sensors). Daily deltas are summed per category.
CATEGORIES: tuple[str, ...] = (
    "heating",
    "hot_water",
    "cooling",
    "property_energy",
    "pool_heating",
    "ev_charging",
    "household",
    "grid_import",
    "grid_export",
    "solar_production",
    "total_consumption",
)

OPT_PREFIX_ENTITIES = "entities_"  # e.g. options["entities_heating"] = [...]

# The supplier sells the energy itself; the grid tariff never covers it. Point
# these at whatever integration provides those prices to get an all-in figure.
OPT_SUPPLIER_IMPORT_PRICE = "supplier_import_price_entity"
OPT_SUPPLIER_EXPORT_PRICE = "supplier_export_price_entity"

# Storage keys for push bookkeeping.
STORAGE_VERSION = 1
STORAGE_KEY_TEMPLATE = "shs_energy.{entry_id}"

STATUS_POLL_INTERVAL_HOURS = 1
PUSH_TIME_HOUR = 0
PUSH_TIME_MINUTE = 20
BACKFILL_MAX_DAYS = 30

# Mirrors the portal's own per-day sanity bound. Swapping a device behind a
# total_increasing sensor resets its counter, and the recorder books the whole
# new total as one day's change — the portal refuses the entire batch over that
# single row, so it is dropped here instead.
MAX_KWH_PER_READING = 10000

ISSUE_SUBSCRIPTION_INACTIVE = "subscription_inactive"
ISSUE_MISSING_CUSTOMER_INPUT = "missing_customer_input"
