"""Encode app-selected semantic intentions without exporting a mutable owner."""
from uuid import uuid4
from .device_controls import pool_requested_w, pool_permission


def compact_device_intent(intent):
    """Retain the native routing inputs and the owner's executable command."""
    parameters = dict(intent['parameters'])
    if 'models' in parameters:
        # Other models still participate in pool/EV routing and actuator
        # overlap checks. An explicit mapping takes precedence over local options.
        fields = ('key', 'category', 'control_type', 'mapping')
        parameters['models'] = [{field: model[field] for field in fields if field in model}
                                for model in parameters['models']]
    if 'commands' in parameters and intent['device'].startswith('device:'):
        key = intent['device'].removeprefix('device:')
        parameters['commands'] = {key: command for owner, command in parameters['commands'].items()
                                  if owner == key}
    return {**intent, 'parameters': parameters}


def device_intent(device, operation, plan, slot, *, configuration_revision, policy_revision, heating=None):
    parameters = {}
    if operation == 'heat':
        parameters = dict(revision=heating['revision'], target_c=heating['target_c'], models=plan['device_models'])
    elif operation == 'apply':
        parameters = {'start': slot['start']}
        if device == 'ev':
            parameters.update(current_a=slot['ev_target_current_a'], minimum_a=slot['ev_min_current_a'],
                              maximum_a=slot['ev_max_current_a'], models=plan['device_models'])
            if 'spare_house_w' in slot:
                parameters['spare_house_w'] = slot['spare_house_w']
            if slot.get('hold_spare_increase'):
                parameters['hold_spare_increase'] = True
        elif device == 'pool':
            parameters.update(heating_w=pool_requested_w(plan, slot), stop_temperature_c=plan['pool']['stop_temperature_c'],
                              pool_permitted=pool_permission(plan, slot),
                              models=plan['device_models'])
        elif device.startswith('device:'):
            parameters.update(commands=slot['device_commands'], models=plan['device_models'])
            if slot.get('charge_setback'):
                parameters['charge_setback'] = True
        else:
            raise ValueError('Battery control requires an admitted route')
        if slot.get('run') and device != 'pool':
            # The whole run, summarised when the plan was accepted.
            parameters['run'] = slot['run']
    return compact_device_intent(dict(request_id=uuid4().hex, device=device, operation=operation,
                configuration_revision=configuration_revision, policy_revision=policy_revision,
                parameters=parameters, headroom_reserved=False))
