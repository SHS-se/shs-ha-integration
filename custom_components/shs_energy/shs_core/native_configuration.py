"""The execution boundary's subset of resolved SHS configuration.

Cloud, price, planning sources and UI preferences remain private to the app.
Native owners need reviewed mappings, device permissions, ratings and overrides.
Source subscriptions travel separately and confer no control permission.
"""
from copy import deepcopy

PREFIXES=('battery_','pool_','ev_')
FIELDS=frozenset({'device_modes','device_control_mappings','control_override_entity','excluded_device_readings','planning_mode'})

def native_options(options):
    return {key:deepcopy(value) for key,value in options.items() if key in FIELDS or key.startswith(PREFIXES)}
