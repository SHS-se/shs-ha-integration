"""Read-only planner inputs for accepted controls; no execution permission writes."""
from copy import deepcopy
from datetime import timezone

if __package__:
    from .contract_v1.validate import revision, validate, utc
    from .optimisation import build_device_load_model, OptimisationInputError
    from .control_capabilities import finite_number
else:
    from contract_v1.validate import revision, validate, utc
    from optimisation import build_device_load_model, OptimisationInputError
    from control_capabilities import finite_number


def planning_controls(agreement):
    definition = agreement.saved['definition']
    if not definition:
        return [], None
    controls = definition['controls']
    for c in controls:
        if c['desired']['included'] and agreement.saved['accepted'].get(c['control_id']) != revision(c):
            raise OptimisationInputError(c['presentation']['name'] + ': control settings await local acceptance')
    return controls, definition


def planning_options(options, setup, controls):
    """Override observation/physical inputs in a copy; operation flags stay local."""
    result = deepcopy(options)
    for c in controls:
        name, included = c['contract']['name'], c['desired']['included']
        if name not in ('battery_dispatch', 'pool_service'):
            continue
        result['battery_enabled' if name == 'battery_dispatch' else 'pool_enabled'] = included
        if not included:
            continue
        record = setup.records.get(c['control_id'])
        if not record:
            raise OptimisationInputError('Control binding is missing')
        report = setup.report(record['spec'])
        if report['status'] != 'ready' or record['binding_revision'] != c['local']['binding_revision']:
            raise OptimisationInputError('Control binding changed; wait for acknowledgement')
        roles, limits = report['resolved'], record['spec']['limits']
        if name == 'battery_dispatch':
            result.update(battery_soc_entity=roles['soc'], battery_min_soc_entity=roles['soc_floor'],
                          battery_min_soc=limits['minimum_soc_pct']/100, battery_max_soc=limits['maximum_soc_pct']/100,
                          battery_charge_max_w=limits['charge_max_w'], battery_discharge_max_w=limits['discharge_max_w'])
        else:
            result['pool_water_temperature_entity'] = roles['water_temperature']
    return result


def pool_meter_models(controls, definition, devices, actuals, horizon, local_tz):
    """Remove each attributable meter from base load once, using its old key."""
    result, reserved = [], set()
    for c in controls:
        if c['contract']['name'] != 'pool_service':
            continue
        for link in c['sources']:
            if link['accounting_use'] != 'count':
                continue
            source = next(s for s in definition['sources'] if s['source_id'] == link['source_id'])
            matches = [d for d in devices if d['statistic_id'] == source.get('statistic_id')]
            if len(matches) != 1:
                raise OptimisationInputError('Pool electricity source must resolve to one existing historical meter')
            device = matches[0]
            if device['key'] in reserved:
                raise OptimisationInputError('Pool electricity meter is counted twice')
            reserved.add(device['key'])
            if not c['desired']['included']:
                continue
            empirical = build_device_load_model(actuals, device['key'], str(local_tz), minimum_samples=2, allow_partial=True)
            if not empirical['sample_count']:
                raise OptimisationInputError(device['name'] + ': pool attribution needs measured meter history')
            result.append({**device, 'planning_role': 'controllable', 'control_type': 'switch_schedule',
                           'load_type': device.get('load_type', device['suggested_load_type']),
                           'profile_sample_count': empirical['sample_count'], 'active_power_w': empirical['active_power_w'],
                           'forecast_w_by_slot': [empirical['by_weekday'][t.astimezone(local_tz).weekday()][t.astimezone(local_tz).hour*4 + t.astimezone(local_tz).minute//15] for t in horizon]})
    return result, reserved


def observations(controls, definition, setup, read_entity, now):
    result = {}
    for c in controls:
        record = setup.records.get(c['control_id'])
        if not record:
            continue
        report = setup.report(record['spec'])
        observed = {}
        for role, entity_id in report['resolved'].items():
            capability = next(cap for cap in record['capabilities'] if cap['role'] == role)
            if capability['operation'] != 'observe':
                continue
            try:
                payload = read_entity(entity_id)
                stamp = payload['last_reported']
                if hasattr(stamp, 'isoformat'):
                    stamp = stamp.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
                else:
                    stamp = str(stamp).replace('+00:00', 'Z')
                age = (now - utc(stamp)).total_seconds()
                if not -5 <= age <= 120:
                    raise ValueError('observation is stale or future-dated')
                if role == 'feedback':
                    value = payload.get('attributes', {}).get('feedback')
                    validate(value, definition)
                    if value['control_id'] != c['control_id'] or value['accepted'] != revision(c):
                        raise ValueError('feedback belongs to another control or older definition')
                    if not -5 <= (now - utc(value['reported_at_utc'])).total_seconds() <= 120:
                        raise ValueError('feedback is stale or future-dated')
                elif capability['unit'] == 'state':
                    value = str(payload['state'])
                else:
                    value = finite_number(payload['state'])
                observed[role] = {'value': value, 'unit': capability['unit'], 'observed_at_utc': stamp}
            except (ValueError, KeyError, TypeError) as err:
                observed[role] = {'error': str(err)}
        result[c['control_id']] = {'accepted': revision(c), 'observations': observed}
    return result
