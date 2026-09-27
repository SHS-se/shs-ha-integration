"""Encode app-selected semantic intentions without exporting a mutable owner."""
from uuid import uuid4


def device_intent(device, operation, plan, slot, *, configuration_revision, policy_revision):
    parameters = {}
    if operation == 'apply':
        parameters = {'start': slot['start']}
        if device == 'ev':
            parameters.update(current_a=slot['ev_target_current_a'], minimum_a=slot['ev_min_current_a'],
                              maximum_a=slot['ev_max_current_a'], models=plan['device_models'])
        elif device == 'pool':
            parameters.update(heating_w=slot['pool_w'], stop_temperature_c=plan['pool']['stop_temperature_c'],
                              models=plan['device_models'])
        elif device.startswith('device:'):
            parameters.update(commands=slot['device_commands'], models=plan['device_models'])
        else:
            raise ValueError('Battery control requires an admitted route')
    return dict(request_id=uuid4().hex, device=device, operation=operation,
                configuration_revision=configuration_revision, policy_revision=policy_revision,
                parameters=parameters, headroom_reserved=False)
