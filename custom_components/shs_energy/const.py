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


# Where the customer-facing site lives, per backend. A repair that says "on the
# website" without saying which page is only marginally better than silence.
WEBSITE_ORIGIN_BY_ENVIRONMENT = {
    "production": "https://smarthomesolutions.se",
    "test": "https://test-smart-home-solutions.pages.dev",
}


def website_url(base_url: str, path: str) -> str | None:
    """Absolute link to a page on the customer's own portal, or None.

    None for a custom backend rather than a guess: an integration pointing at
    somebody's self-hosted deployment cannot know where their site is, and a
    wrong link is worse than a path the panel prints as text.
    """
    environment = backend_attributes(base_url)["backend_environment"]
    origin = WEBSITE_ORIGIN_BY_ENVIRONMENT.get(str(environment))
    return f"{origin}{path}" if origin else None

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
OPT_DEVICE_CONTROL_MAPPINGS = "device_control_mappings"
CONFIG_ENTRY_VERSION = 4
ROOM_AREA_FIELD = "room_area_id"

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

# Whether a store is part of this home at all.
#
# Distinct from every other option in that it answers a question about the
# building rather than about a preference: a house with no pool is not a house
# whose pool is warm enough. Off means the planner is told the equipment is not
# there, which is a different claim from "configured but not routed" — the one
# `unplanned_services` exists to report — and the two must not look alike.
#
# Defaults are True in `optimisation_defaults`, so an installation that predates
# these keys plans exactly as it did before and the toggle is only ever a
# deliberate opt-out.
OPT_BATTERY_ENABLED = "battery_enabled"
OPT_POOL_ENABLED = "pool_enabled"
OPT_EV_ENABLED = "ev_enabled"

OPT_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
OPT_BATTERY_CHARGE_MAX_W = "battery_charge_max_w"
OPT_BATTERY_DISCHARGE_MAX_W = "battery_discharge_max_w"
OPT_BATTERY_MIN_SOC = "battery_min_soc"
# The inverter enforces its own discharge cut-off. Reading it beats
# trusting a number typed beside it, which drifts the moment either moves.
OPT_BATTERY_MIN_SOC_ENTITY = "battery_min_soc_entity"
OPT_BATTERY_MAX_SOC = "battery_max_soc"
OPT_BATTERY_TARGET_SOC = "battery_target_soc"
OPT_BATTERY_TARGET_IS_HARD = "battery_target_is_hard"
OPT_BATTERY_CHARGE_EFFICIENCY = "battery_charge_efficiency"
OPT_BATTERY_DISCHARGE_EFFICIENCY = "battery_discharge_efficiency"
OPT_BATTERY_EXPORT_ENABLED = "battery_export_enabled"
OPT_BATTERY_EXPORT_RESERVE_SOC = "battery_export_reserve_soc"
OPT_BATTERY_EXPORT_MIN_PRICE = "battery_export_min_price_sek_per_kwh"

# The storage executor's mapping. Deliberately plant-level rather than a
# device control type: there is one battery, it is already modelled as a store
# with its own charge and discharge variables, and routing it through
# `planning_path` would additionally schedule it as a controllable load and
# subtract it from base load — counting the same plant twice.
#
# Commanding the battery stays off until someone turns it on. Reading a
# register is not permission to write it, so the mapping being complete is not
# the same as being authorised to use it.
OPT_BATTERY_CONTROL_ENABLED = "battery_control_enabled"
OPT_BATTERY_MODE_ENTITY = "battery_mode_entity"
# Charge-first and discharge-first are separate modes on real inverters rather
# than the sign of one request, so reversing flow is a mode write plus a power
# write. These name the option strings that mean each, per installation.
OPT_BATTERY_MODE_CHARGE = "battery_mode_charge"
OPT_BATTERY_MODE_DISCHARGE = "battery_mode_discharge"
OPT_BATTERY_MODE_IDLE = "battery_mode_idle"
OPT_BATTERY_POWER_ENTITY = "battery_power_entity"
# Sigenergy writes kW where the planner speaks W, and publishes both a signed
# power sensor and an inverted copy. Both are per-installation facts that must
# be settled by measurement at commissioning, not assumed.
OPT_BATTERY_POWER_UNIT = "battery_power_unit"
OPT_BATTERY_DISCHARGE_IS_NEGATIVE = "battery_discharge_is_negative"
# Authority is a handshake: one entity claims remote control, another confirms
# it was granted. Losing the confirmation is a loss of a required control
# source, not a reason to keep writing.
OPT_BATTERY_AUTHORITY_ENTITY = "battery_authority_entity"
OPT_BATTERY_AUTHORITY_CONFIRM_ENTITY = "battery_authority_confirm_entity"
OPT_BATTERY_AUTHORITY_CONFIRM_STATE = "battery_authority_confirm_state"
# Confirm from measurement, never from the command that was sent.
OPT_BATTERY_POWER_MEASUREMENT_ENTITY = "battery_power_measurement_entity"

BATTERY_POWER_UNITS = ("W", "kW")
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

# The band the pool water is held in, for equipment that runs to a hysteresis
# window rather than an on/off command.
#
# A property of the store, not of each meter that heats it. The pool service is
# built from every device routed to it — a heater and its circulation pump —
# and they share one body of water and one pair of registers. Hanging the band
# off each device mapping asked for it once per meter, invited two mappings to
# disagree about the same window, and left two writers for one actuator.
OPT_POOL_START_TEMPERATURE_ENTITY = "pool_start_temperature_entity"
OPT_POOL_STOP_TEMPERATURE_ENTITY = "pool_stop_temperature_entity"
OPT_POOL_TEMPERATURE_MINIMUM = "pool_temperature_minimum"
OPT_POOL_TEMPERATURE_MAXIMUM = "pool_temperature_maximum"

OPT_EV_CONNECTED_ENTITY = "ev_connected_entity"
OPT_EV_SOC_ENTITY = "ev_soc_entity"
OPT_EV_TARGET_SOC_ENTITY = "ev_target_soc_entity"
OPT_EV_DEPARTURE_ENTITY = "ev_departure_entity"
OPT_EV_ENERGY_REMAINING_ENTITY = "ev_energy_remaining_entity"
OPT_EV_PHASE_COUNT = "ev_phase_count"
OPT_EV_PHASE_VOLTAGE = "ev_phase_voltage"
OPT_EV_CHARGE_EFFICIENCY = "ev_charge_efficiency"
OPT_EV_KWH_PER_KM = "ev_kwh_per_km"

# Defaults for the vehicle's electrical model, every one of them overridable.
#
# These were fixed constants, and a fixed phase count is not a detail: a
# single-phase 16 A charger delivers 3.7 kW and was modelled at 11 kW, so the
# planner believed it could fill a car three times faster than the cable can.
# Nothing surfaced that, because a wrong number produces a confident plan rather
# than an error. Anything that changes what a plan means has to be reachable
# from the panel; see OPT_EV_PHASE_COUNT and friends below.
EV_CHARGE_EFFICIENCY = 0.92
EV_PHASE_COUNT = 3
EV_PHASE_VOLTAGE = 230.0
DEFAULT_EV_KWH_PER_KM = 0.16

OPTIMISATION_ACTUAL_BACKFILL_HOURS = 72
OPTIMISATION_PROFILE_DAYS = 10

# What a device must still be doing to be planned as its own load rather than
# left inside base load.
#
# `build_base_load_model` subtracts every modelled device from the whole-home
# total per quarter, and discards any quarter a modelled device is missing
# from — correctly, because treating it as zero would count that device twice.
# But a device qualified for that set on quarter-of-day coverage *pooled over
# the window*, which a device that died mid-window still passes on its older
# days. One pool heater that stopped reporting therefore voided every quarter
# since it stopped, and the home went unplanned.
#
# So a device now has to cover the window it gates. Below either bar it is
# dropped from the model set, which leaves its energy inside base load: not
# scheduled, not double counted, and not able to void anybody else's quarter.
DEVICE_PROFILE_MIN_COVERAGE = 0.5
DEVICE_PROFILE_MAX_SILENCE_HOURS = 6

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
ISSUE_DEGRADED_DEVICE = "degraded_device"
# Kept apart from the degraded issue on purpose. A meter that has gone quiet
# wants someone to go and look at the equipment; a meter that has only just
# started reporting wants nobody to do anything at all. Sharing one
# notification told the second group to check a sensor that was working.
ISSUE_WARMING_DEVICE = "warming_device"
# Raised only when someone has switched battery control on. A complete
# mapping is not authorisation, but an incomplete one that has been
# authorised is the half-configured executor this warning exists to catch.
ISSUE_BATTERY_CONTROL = "battery_control"
ISSUE_POOL_CONTROL = "pool_control"

# Storage keys for push bookkeeping.
STORAGE_VERSION = 1
STORAGE_KEY_TEMPLATE = "shs_energy.{entry_id}"

STATUS_POLL_INTERVAL_HOURS = 1
# How soon a replan asked for on the website is picked up. The status endpoint
# is one small read, so this can be far shorter than the hourly poll that also
# refreshes the tariff catalogue and supplier prices. A house that never hears
# the request is not stranded: the server settles it from the ordinary
# quarter-hour push, so this only decides how long the person waits.
REPLAN_POLL_INTERVAL_MINUTES = 5
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

# A meter that loses power comes back reporting slightly less than the recorder
# last saw (7862.724003 -> 7862.724), so the period's change is a microscopic
# negative. That is the counter's own rounding, not energy nobody measured, and
# reading it as "this meter is missing" costs the whole category that day. Below
# this magnitude a negative change is worth zero; a larger drop is a reset whose
# energy really is unknown, and stays dropped.
MAX_NEGATIVE_CHANGE_KWH = 0.05

ISSUE_SUBSCRIPTION_INACTIVE = "subscription_inactive"
ISSUE_MISSING_CUSTOMER_INPUT = "missing_customer_input"

# Scheduled execution is independent of inclusion in the plan.
OPT_EV_CONTROL_ENABLED = "ev_control_enabled"
OPT_POOL_CONTROL_ENABLED = "pool_control_enabled"
OPT_EV_CHARGE_SWITCH_ENTITY = "ev_charge_switch_entity"
OPT_POOL_PERMISSION_ENTITY = "pool_permission_entity"
OPT_BATTERY_MODE_BASELINE = "battery_mode_baseline"
OPT_BATTERY_MEASUREMENT_CHARGE_POSITIVE = "battery_measurement_charge_positive"
