"""Compact app-owned values projected into stable HA entities and editors."""
from copy import deepcopy

FIELDS = (
    'configuration_busy', 'data', 'actuals_accepted_until', 'demand_charge', 'grid_operator', 'grid_price_forecast', 'grid_prices',
    'incomplete_readings', 'last_actual_slots_accepted', 'last_calculation_error', 'last_connection_error',
    'last_connection_success', 'last_optimisation_error', 'last_optimisation_push', 'last_optimisation_attempt',
    'last_price_error', 'last_push_date', 'last_push_error', 'last_tariff_error', 'latest_calculation',
    'latest_display_components', 'missing_questions', 'operational_status', 'optimisation_degraded_devices',
    'optimisation_missing_inputs', 'optimisation_missing_fix', 'optimisation_missing_remedy',
    'optimisation_plan', 'optimisation_unplanned_services', 'reactive_surplus_w', 'skipped_readings',
    'supplier_cost_days', 'supplier_prices', 'tariff_catalog', 'tariff_components', 'tariff_status',
    'total_price_forecast', 'last_thermal_slots_accepted', 'last_runtime_report', 'last_runtime_error',
    'attention_items', 'device_control_mapping_gaps', 'replan_recommendations',
)


def runtime_projection(household, cached, repairs, *, plan):
    battery = household.battery_runtime
    return dict(schema=1, values={key:plan if key == 'optimisation_plan' else deepcopy(getattr(household,key)) for key in FIELDS},
        controllers=deepcopy(household.controller.status), cached=deepcopy(cached), repairs=deepcopy(repairs),
        battery=battery.snapshot(), battery_live_inputs=household.battery_live_inputs.snapshot(),
        battery_writer=household.battery_writer.snapshot(),
        diagnostics={'network_traffic':household.client.traffic.snapshot(),
            'controller_metrics':household.controller.metrics.snapshot(),
            'resource_profiling':battery.profiler.snapshot(battery.resource_counts())})
