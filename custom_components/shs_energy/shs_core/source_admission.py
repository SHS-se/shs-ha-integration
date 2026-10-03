"""Consumer interests distinguish ordered facts from replaceable readings."""
from datetime import datetime, timedelta
from math import isfinite
from .controller_inputs import configured_entity_ids
from .native_configuration import native_options
from .battery_live import planned_power_bindings

USES = frozenset(('capture', 'counter', 'device', 'pool_temperature', 'reference'))
CAPTURE_KEYS = ('house_consumption_power_entity', 'solar_production_power_entity',
    'battery_power_measurement_entity', 'battery_soc_entity', 'grid_power_entity',
    'battery_mode_entity', 'battery_charge_limit_entity', 'battery_discharge_limit_entity')
COUNTERS = ('grid_import', 'grid_export', 'battery_charge', 'battery_discharge')
# Only fields consumed by physical/capture policy travel as execution facts.
FACT_ATTRIBUTES = frozenset(('unit_of_measurement', 'device_class', 'state_class',
    'entity_id', 'min', 'max', 'step', 'options', 'hvac_modes', 'hvac_mode',
    'temperature', 'current_temperature', 'min_temp', 'max_temp', 'target_temp_step',
    'preset_mode', 'preset_modes'))


def source_bindings(options, models):
    bindings = {entity:{'reference'} for entity in configured_entity_ids(options)}
    def use(entities, role):
        for entity in entities:
            if entity:
                bindings.setdefault(entity, set()).add(role)
    use((options.get(key) for key in CAPTURE_KEYS), 'capture')
    use(planned_power_bindings(options, models).values(), 'capture')
    for category in COUNTERS:
        use(options.get('entities_'+category, ()), 'counter')
    native = native_options(options)
    temperature = native.pop('pool_water_temperature_entity', None)
    use(configured_entity_ids(native), 'device')
    use((temperature,), 'pool_temperature')
    return {entity:sorted(uses) for entity,uses in sorted(bindings.items())}


def validate_bindings(value):
    if (type(value) is not dict or any(type(entity) is not str or not entity
            or type(uses) is not list or not uses or len(set(uses)) != len(uses)
            or any(type(use) is not str or use not in USES for use in uses)
            for entity,uses in value.items())):
        raise ValueError('Invalid source consumer bindings')
    return {entity:frozenset(uses) for entity,uses in value.items()}


def ordered(uses, *, pool_idle=False):
    return bool(uses & {'capture', 'counter', 'device'}
                or 'pool_temperature' in uses and not pool_idle)


def owned_device_sources(record):
    # Pool temperature retains its thermal role; ownership keeps the actuators,
    # permissions and overrides needed to complete or explicitly release work.
    return set(record['originals']) | configured_entity_ids(
        {key:value for key,value in record['options'].items() if key != 'pool_water_temperature_entity'})


def pool_paused(status, slot, plan, read, ownership):
    """A successful paused decision and observed OFF eliminate thermal work."""
    if (not slot or not plan or slot.get('pool_w') != 0 or status.get('state') not in ('scheduled','verified')
            or status.get('plan_id') != plan.get('plan_id') or status.get('slot_start') != slot['start']
            or status.get('requested_power_w') != 0 or status.get('requested_switch_state') != 'off'
            or status.get('handover_pending') or ownership.records.get('pool',{}).get('restoration_pending')):
        return False
    state = read(status['control_entity'])
    return (state is not None and state.state == 'off'
        and not any(row.get('pending_start') or row['active'] for row in ownership.runs.records.values()
                    if row['binding']['owner'] == 'pool'))


def temperature_available(state):
    if state is None or state.attributes.get('unit_of_measurement') != '°C':
        return False
    try:
        return isfinite(float(state.state))
    except (TypeError, ValueError):
        return False


def temperature_metadata(state):
    return (state.attributes.get('unit_of_measurement'), state.attributes.get('entity_id')) if state is not None else None


def pool_temperature_sources(options, read, platform):
    result = set()
    entity = options.get('pool_water_temperature_entity')
    while entity and entity not in result:
        result.add(entity)
        state = read(entity)
        entity = state.attributes.get('entity_id') if state is not None and platform(entity) == 'filter' else None
    return result


def validate_pool_pause(value):
    if value is None:
        return
    if (type(value) is not dict or set(value) != {'entity','until'}
            or type(value['entity']) is not str or not value['entity']
            or type(value['until']) is not str or datetime.fromisoformat(value['until']).tzinfo is None):
        raise ValueError('Invalid pool attention deadline')


def native_pool_idle(pause, declared, context, read, ownership, now):
    """Inspect current physical obligations before accepting a paused interest."""
    if (ownership.records.get('pool',{}).get('restoration_pending')
            or any(row.get('pending_start') or row['active'] for row in ownership.runs.records.values()
                   if row['binding']['owner'] == 'pool')):
        return False
    if declared is not None and now < datetime.fromisoformat(declared['until']):
        state = read(declared['entity'])
        if state is not None and state.state == 'off':
            return True
    if (pause is None or pause['configuration_revision'] != context['configuration_revision']
            or pause['policy_revision'] != context['policy_revision']):
        return False
    start = datetime.fromisoformat(pause['start'].replace('Z','+00:00'))
    state = read(pause['entity'])
    return start <= now < start+timedelta(minutes=15) and state is not None and state.state == 'off'
