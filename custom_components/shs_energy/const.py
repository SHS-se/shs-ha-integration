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
OPT_SUPPLIER_IMPORT_PRICE = "supplier_import_price_entity"
OPT_SUPPLIER_EXPORT_PRICE = "supplier_export_price_entity"

# Price-forecast slot length. Swedish settlement moved to quarter-hours, and an
# optimiser wants the series on its own timestep, so this is configurable.
OPT_FORECAST_RESOLUTION_MINUTES = "forecast_resolution_minutes"
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

# Live optimisation inputs. Forecast entities must expose timestamped values;
# the integration does not infer a provider, unit, location or missing series.
OPT_PV_FORECAST_ENTITIES = "pv_forecast_entities"
OPT_SUPPLIER_IMPORT_FORECAST_ENTITY = "supplier_import_forecast_entity"
OPT_SUPPLIER_EXPORT_FORECAST_ENTITY = "supplier_export_forecast_entity"
OPT_ELECTRICITY_PRICE_AREA = "electricity_price_area"
OPT_PV_FORECAST_LATITUDE = "pv_forecast_latitude"
OPT_PV_FORECAST_LONGITUDE = "pv_forecast_longitude"
OPT_BATTERY_SOC_ENTITY = "battery_soc_entity"
OPT_GRID_EXPORT_POWER_ENTITY = "grid_export_power_entity"

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

OPT_POOL_POWER_W = "pool_power_w"
OPT_POOL_PLANNING_ENABLED = "pool_planning_enabled"
OPT_POOL_DEFERRABLE_CONFIRMED = "pool_deferrable_confirmed"
OPT_POOL_ENABLED_ENTITY = "pool_enabled_entity"
OPT_POOL_MIN_RUN_SLOTS = "pool_min_run_slots"
OPT_POOL_DEADLINE = "pool_deadline"
OPT_POOL_BASELINE_START = "pool_baseline_start"
OPT_BOILER_POWER_W = "boiler_power_w"
OPT_BOILER_PLANNING_ENABLED = "boiler_planning_enabled"
OPT_BOILER_DEFERRABLE_CONFIRMED = "boiler_deferrable_confirmed"
OPT_BOILER_MAX_INHIBIT_SLOTS = "boiler_max_inhibit_slots"
OPT_EV_CONNECTED_ENTITY = "ev_connected_entity"
OPT_EV_PLANNING_ENABLED = "ev_planning_enabled"
OPT_EV_DEFERRABLE_CONFIRMED = "ev_deferrable_confirmed"
OPT_EV_ELECTRICAL_CONFIRMED = "ev_electrical_confirmed"
OPT_EV_SOC_ENTITY = "ev_soc_entity"
OPT_EV_TARGET_SOC_ENTITY = "ev_target_soc_entity"
OPT_EV_DEPARTURE_ENTITY = "ev_departure_entity"
OPT_EV_POWER_W = "ev_power_w"
OPT_EV_BATTERY_KWH = "ev_battery_kwh"
OPT_EV_CHARGE_EFFICIENCY = "ev_charge_efficiency"
OPT_EV_MIN_RUN_SLOTS = "ev_min_run_slots"
OPT_EV_CHARGE_CURRENT_ENTITY = "ev_charge_current_entity"
OPT_EV_MIN_CURRENT_A = "ev_min_current_a"
OPT_EV_MAX_CURRENT_A = "ev_max_current_a"
OPT_EV_CURRENT_STEP_A = "ev_current_step_a"
OPT_EV_ENERGY_REMAINING_ENTITY = "ev_energy_remaining_entity"
OPT_EV_PHASE_COUNT = "ev_phase_count"
OPT_EV_VOLTAGE = "ev_voltage"
OPT_EV_DEFAULT_DEPARTURE = "ev_default_departure"

OPTIMISATION_ACTUAL_BACKFILL_HOURS = 72
OPTIMISATION_PROFILE_DAYS = 10
OPTIMISATION_HORIZON_HOURS = 72
OPTIMISATION_PUSH_SECOND = 20
OPTIMISATION_STARTUP_DELAY_SECONDS = 60
OPTIMISATION_STARTUP_RETRY_SECONDS = 15
OPTIMISATION_STARTUP_ISSUE_GRACE_SECONDS = 120
ISSUE_OPTIMISATION_CONFIGURATION = "optimisation_configuration"

# Storage keys for push bookkeeping.
STORAGE_VERSION = 1
STORAGE_KEY_TEMPLATE = "shs_energy.{entry_id}"

STATUS_POLL_INTERVAL_HOURS = 1
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
