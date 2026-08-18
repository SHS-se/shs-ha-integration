"""Constants for the Smart Home Solutions Energy integration."""

from __future__ import annotations

from urllib.parse import urlparse

DOMAIN = "shs_energy"

# Supabase edge-functions origin (prod). Overridable in the config flow so the
# same build can point at the test project (vxqpgbzseckgceopitpm).
DEFAULT_BASE_URL = "https://oosxndduqzhvrorgogaw.supabase.co/functions/v1"
PRODUCTION_BACKEND_HOST = "oosxndduqzhvrorgogaw.supabase.co"
TEST_BACKEND_HOST = "vxqpgbzseckgceopitpm.supabase.co"
CONF_BASE_URL = "base_url"
CONF_DEVICE_TOKEN = "device_token"
CONF_DEVICE_TOKEN_ID = "device_token_id"
CONF_CUSTOMER_NAME = "customer_name"
CONF_HOME_ID = "home_id"
CONF_PAIRING_CODE = "pairing_code"
CONF_DEVICE_NAME = "device_name"


def backend_attributes(base_url: str) -> dict[str, str | None]:
    """Return non-secret connection details suitable for diagnostics."""
    host = urlparse(base_url).hostname
    environment = (
        "production"
        if host == PRODUCTION_BACKEND_HOST
        else "test"
        if host == TEST_BACKEND_HOST
        else "custom"
    )
    return {"backend_environment": environment, "backend_host": host}

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
OPTIMISATION_EXTRA_CATEGORIES: tuple[str, ...] = (
    "battery_charge",
    "battery_discharge",
)
CONFIGURABLE_CATEGORIES = CATEGORIES + OPTIMISATION_EXTRA_CATEGORIES

OPT_PREFIX_ENTITIES = "entities_"  # e.g. options["entities_heating"] = [...]

# The supplier sells the energy itself; the grid tariff never covers it. Point
# these at whatever integration provides those prices to get an all-in figure.

# Price-forecast slot length. Swedish settlement moved to quarter-hours, and an
# optimiser wants the series on its own timestep, so this is configurable.
OPT_FORECAST_RESOLUTION_MINUTES = "forecast_resolution_minutes"
RETIRED_SUPPLIER_PRICE_OPTIONS = frozenset({
    "supplier_import_price_entity",
    "supplier_export_price_entity",
    "supplier_import_forecast_entity",
    "supplier_export_forecast_entity",
    "electricity_price_area",
})
DEFAULT_FORECAST_RESOLUTION_MINUTES = 15

# Planning is opt-in. Monitoring and tariff uploads keep working when planning
# is disabled. Promotional examples live only in the website bundle.
OPT_PLANNING_MODE = "planning_mode"
PLANNING_MODE_DISABLED = "disabled"
PLANNING_MODE_LIVE = "live"
DEFAULT_PLANNING_MODE = PLANNING_MODE_DISABLED
OPT_AUTOMATIC_SETUP = "automatic_setup"
OPT_DISCOVERY_EVIDENCE = "discovery_evidence"
OPT_CONFIGURATION_REVIEWED_AT = "configuration_reviewed_at"
OPT_DEVICE_CONTROL_MAPPINGS = "device_control_mappings"
OPT_CONFIGURATION_SCHEMA_VERSION = "_configuration_schema_version"
OPT_LEGACY_CONFIGURATION_ARCHIVE = "_legacy_configuration_archive"
CONFIGURATION_SCHEMA_VERSION = 3

# Live optimisation inputs. Forecast entities must expose timestamped values;
# the integration does not infer a provider, unit, location or missing series.
OPT_PV_FORECAST_ENTITIES = "pv_forecast_entities"
OPT_PV_FORECAST_LATITUDE = "pv_forecast_latitude"
OPT_PV_FORECAST_LONGITUDE = "pv_forecast_longitude"
OPT_BATTERY_SOC_ENTITY = "battery_soc_entity"
OPT_GRID_EXPORT_POWER_ENTITY = "grid_export_power_entity"

# Thermal-zone observations. Room temperature, comfort band and actuator state
# already belong to each setpoint device's control mapping; these two name the
# shared outdoor sources that no single zone owns. Observations and forecast
# are separate entities on purpose: a `weather.*` entity carries a forecast but
# reports a provider's regional temperature, while a local sensor measures the
# air the building actually loses heat to.
OPT_OUTDOOR_TEMPERATURE_ENTITY = "outdoor_temperature_entity"
OPT_WEATHER_FORECAST_ENTITY = "weather_forecast_entity"

OPT_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
OPT_BATTERY_CHARGE_MAX_W = "battery_charge_max_w"
OPT_BATTERY_DISCHARGE_MAX_W = "battery_discharge_max_w"
OPT_BATTERY_MIN_SOC = "battery_min_soc"
OPT_BATTERY_MAX_SOC = "battery_max_soc"
OPT_BATTERY_TARGET_SOC = "battery_target_soc"
OPT_BATTERY_TARGET_IS_HARD = "battery_target_is_hard"
OPT_BATTERY_CHARGE_EFFICIENCY = "battery_charge_efficiency"
OPT_BATTERY_DISCHARGE_EFFICIENCY = "battery_discharge_efficiency"
OPT_BATTERY_EXPORT_ENABLED = "battery_export_enabled"
OPT_BATTERY_EXPORT_RESERVE_SOC = "battery_export_reserve_soc"
OPT_BATTERY_EXPORT_MIN_PRICE = "battery_export_min_price_sek_per_kwh"
OPT_GRID_IMPORT_LIMIT_W = "grid_import_limit_w"
OPT_GRID_EXPORT_LIMIT_W = "grid_export_limit_w"
OPT_TERMINAL_SOC_MIN = "terminal_soc_min"
OPT_TERMINAL_ENERGY_VALUE = "terminal_energy_value_sek_per_kwh"

# The pool is a store, not a load with a daily budget
# (ENERGY_OPTIMISATION_ARCHITECTURE.md §8.3). Water temperature is the state the
# planner schedules against, and volume is what converts a kWh into a degree.
# Everything else about the pool — its loss coefficient and the heat pump's COP
# against air temperature — is fitted from that series, the outdoor forecast and
# the pool heater's already-metered energy, so none of it is asked for.
OPT_POOL_WATER_TEMPERATURE_ENTITY = "pool_water_temperature_entity"
OPT_POOL_VOLUME_M3 = "pool_volume_m3"

OPT_EV_CONNECTED_ENTITY = "ev_connected_entity"
OPT_EV_SOC_ENTITY = "ev_soc_entity"
OPT_EV_TARGET_SOC_ENTITY = "ev_target_soc_entity"
OPT_EV_DEPARTURE_ENTITY = "ev_departure_entity"
OPT_EV_ENERGY_REMAINING_ENTITY = "ev_energy_remaining_entity"

# Charger electrical characteristics are installation invariants, not customer
# preferences. The number entity still supplies its commissioned current range
# and increment, while every supported charger uses three 230 V phases.
EV_CHARGE_EFFICIENCY = 0.92
EV_MIN_RUN_SLOTS = 1
EV_PHASE_COUNT = 3
EV_PHASE_VOLTAGE = 230.0

# Preserve superseded UI-owned values during migration, but never use them to
# decide whether a website-selected device appears in an advisory plan.
RETIRED_PLANNING_OPTIONS = frozenset({
    "pool_planning_enabled",
    "pool_deferrable_confirmed",
    "pool_deadline",
    "pool_baseline_start",
    "boiler_planning_enabled",
    "boiler_deferrable_confirmed",
    "ev_planning_enabled",
    "ev_deferrable_confirmed",
    "ev_electrical_confirmed",
    "ev_battery_kwh",
    "ev_charge_efficiency",
    "ev_min_run_slots",
    "ev_phase_count",
    "ev_voltage",
    "ev_default_departure",
})

OPTIMISATION_ACTUAL_BACKFILL_HOURS = 72
OPTIMISATION_PROFILE_DAYS = 10

# Thermal history rides the same quarter-hour grid as electrical actuals. The
# recorder's `purge_keep_days` bounds how far back a gap can still be filled;
# a 72-hour sweep re-offers recent quarters on every push so a late-settling
# sensor is picked up, while the server's upsert keeps the row count at 96/day.
THERMAL_BACKFILL_HOURS = 72
MAX_THERMAL_SLOTS_PER_PUSH = 288

# Published prices, not measurements: they need no recorder and no watermark,
# so a quarter far outside the actual-slot window can still be priced. The
# ceiling matches the portal's 120-day retention for the quarters being priced,
# and the chunk keeps one spot fetch inside the price endpoint's 62-day limit
# and one push inside its 2,880-slot cap.
PRICE_BACKFILL_MAX_DAYS = 120
# 28 days is 2,688 quarters against the server's 2,880 cap. The headroom is
# deliberate: the two DST changeover days are 92 and 100 quarters long, so a
# chunk sized to exactly fill the cap would fail twice a year.
PRICE_BACKFILL_CHUNK_DAYS = 28
OPTIMISATION_HORIZON_HOURS = 72
OPTIMISATION_PUSH_SECOND = 20
OPTIMISATION_STARTUP_DELAY_SECONDS = 60
OPTIMISATION_STARTUP_RETRY_SECONDS = 15
OPTIMISATION_STARTUP_ISSUE_GRACE_SECONDS = 120
ISSUE_OPTIMISATION_CONFIGURATION = "optimisation_configuration"
ISSUE_OPTIMISATION_PLAN_REFUSED = "optimisation_plan_refused"
ISSUE_DEVICE_CONTROL_MAPPING = "device_control_mapping"
ISSUE_UNPLANNED_SERVICE = "unplanned_service"

# Storage keys for push bookkeeping.
STORAGE_VERSION = 1
STORAGE_KEY_TEMPLATE = "shs_energy.{entry_id}"

STATUS_POLL_INTERVAL_HOURS = 1
PRICE_REFRESH_SECOND = 5
PUSH_TIME_HOUR = 0
PUSH_TIME_MINUTE = 20
BACKFILL_MAX_DAYS = 30

# Deep supplier-cost sweep. Under the portal's 500-row limit so a whole history
# still fits in one push, and re-priced only on the first run and on catalogue
# changes rather than nightly.
SUPPLIER_BACKFILL_MAX_DAYS = 450

# Mirrors the portal's own per-day sanity bound. Swapping a device behind a
# total_increasing sensor resets its counter, and the recorder books the whole
# new total as one day's change — the portal refuses the entire batch over that
# single row, so it is dropped here instead.
MAX_KWH_PER_READING = 10000

ISSUE_SUBSCRIPTION_INACTIVE = "subscription_inactive"
ISSUE_MISSING_CUSTOMER_INPUT = "missing_customer_input"
