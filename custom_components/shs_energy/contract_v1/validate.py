"""Shared contract validation, not a runtime executor or migration runner.

Keep this bundle byte-identical in both repositories. JSON Schema owns shape;
these checks own cross-field and cross-record invariants that draft 7 cannot.
"""
from datetime import datetime
import json
from pathlib import Path

from jsonschema import Draft7Validator

ROOT = Path(__file__).parent
SCHEMA = json.loads((ROOT / 'schema.json').read_text())
Draft7Validator.check_schema(SCHEMA)
VALIDATOR = Draft7Validator(SCHEMA)
ASSOCIATIONS = {
    'battery_dispatch': ('battery', 'battery_policy'),
    'pool_service': ('pool', 'pool_temperature'),
    'temperature_target': ('room', 'room_comfort'),
    'adjustable_output': ('ev', 'ev_charge'),
    'permission': ('hot_water', 'hot_water_service'),
    'relay_schedule': ('timed_load', 'timed_run'),
}


class ContractError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ContractError(code)


def unique(values):
    require(len(values) == len(set(values)), 'duplicate_identity')


def revision(control):
    return dict(desired=control['desired']['revision'],
                binding=control['local']['binding_revision'],
                contract=control['contract'],
                planning_model=control['desired']['energy_model']['revision'])


def utc(value):
    require(value.endswith('Z'), 'invalid_time')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ContractError('invalid_time') from None


def validate(document, definition=None):
    """Validate a contract document, requiring home context for references.

    Passing this check is NOT permission to execute. Runtime registry validation,
    persisted agreement, authority lease, freshness and ownership are later steps.
    """
    require(VALIDATOR.is_valid(document), 'schema')
    kind = document['kind']
    if kind == 'definition':
        unique([s['source_id'] for s in document['sources']])
        unique([s['statistic_id'] for s in document['sources'] if 'statistic_id' in s])
        unique([c['control_id'] for c in document['controls']])
        sources = {s['source_id']: s for s in document['sources']}
        for source in sources.values():
            parents = source.get('derived_from', [])
            if source['accounting'] == 'alias':
                parent = sources.get(source['canonical_source_id'])
                require(parent is not None and parent['accounting'] == 'canonical', 'unknown_source')
            require(all(p in sources and p != source['source_id'] for p in parents), 'unknown_source')
        counted = set()
        for c in document['controls']:
            name, desired, local = c['contract']['name'], c['desired'], c['local']
            if desired['objective'] is None or local['limits'] is None:
                require(bool(local['setup_gaps']), 'missing_setup_reason')
            allowed = [ASSOCIATIONS[name]]
            if name == 'relay_schedule':
                allowed.append(('room', 'room_comfort'))
            require(any(desired['association']['kind'] == kind and
                        (desired['objective'] is None or desired['objective']['kind'] == objective)
                        for kind, objective in allowed), 'association_mismatch')
            unique([cap['role'] for cap in local['capabilities']])
            if not local['setup_gaps']:
                required_roles = {
                    'pool_service': {'request', 'feedback', 'water_temperature'},
                    'battery_dispatch': {'mode', 'charge_ceiling', 'discharge_ceiling', 'authority',
                                         'authority_confirmation', 'battery_power', 'soc', 'soc_floor',
                                         'pv_power', 'load_power', 'grid_import_power', 'grid_export_power'},
                }.get(name, set())
                require(required_roles <= {cap['role'] for cap in local['capabilities']}, 'missing_capability')
            for cap in local['capabilities']:
                if 'minimum' in cap and 'maximum' in cap:
                    require(cap['minimum'] <= cap['maximum'], 'invalid_limits')
            limits, objective = local['limits'], desired['objective']
            if name == 'pool_service':
                require(local['handover'] == 'release_customer_automation', 'customer_boundary')
                require({cap['role'] for cap in local['capabilities']} <= {'request','feedback','water_temperature'}, 'customer_boundary')
                operations = {'request': ('request_state', 'state'),
                              'feedback': ('observe', 'state'),
                              'water_temperature': ('observe', '°C')}
                require(all((cap['operation'], cap['unit']) == operations[cap['role']]
                            for cap in local['capabilities']), 'customer_boundary')
            if name == 'pool_service' and limits is not None and objective is not None:
                require(set(limits) == {'minimum_c', 'maximum_c', 'step_c'}, 'invalid_limits')
                require(limits['minimum_c'] <= objective['start_c'] < objective['stop_c'] <= limits['maximum_c'], 'invalid_band')
                for key in ('start_c', 'stop_c'):
                    ticks = (objective[key] - limits['minimum_c']) / limits['step_c']
                    require(abs(ticks - round(ticks)) < 1e-7, 'invalid_band')
            elif name == 'battery_dispatch' and limits is not None and objective is not None:
                require('charge_max_w' in limits, 'invalid_limits')
                require(0 <= limits['normal_charge_w'] <= limits['charge_max_w'] and 0 <= limits['normal_discharge_w'] <= limits['discharge_max_w'], 'invalid_limits')
                require(limits['minimum_soc_pct'] <= objective['reserve_soc_pct'] <= objective['export_reserve_soc_pct'] <= objective['maximum_soc_pct'] <= limits['maximum_soc_pct'], 'invalid_limits')
                require(local['handover'] == 'reviewed_self_consumption', 'invalid_handover')
            for link in c['sources']:
                source = sources.get(link['source_id'])
                require(source is not None, 'unknown_source')
                if link['accounting_use'] == 'count':
                    require(source['accounting'] == 'canonical' and source['role'] == 'electrical_energy', 'noncanonical_accounting')
                    require(source['source_id'] not in counted, 'duplicate_accounting')
                    require(name != 'battery_dispatch', 'storage_is_not_load')
                    counted.add(source['source_id'])
        return
    if kind == 'migration':
        unique([r['legacy_key'] for r in document['rows']])
        unique([s for r in document['rows'] for s in r['preserved_statistic_ids']])
        for row in document['rows']:
            require(row['status'] != 'needs_setup' or bool(row['gaps']), 'missing_setup_reason')
            require(row['status'] != 'mapped' or row['association'] is not None, 'association_mismatch')
        return
    require(definition is not None and definition['kind'] == 'definition', 'definition_required')
    validate(definition)
    require(document['home_id'] == definition['home_id'], 'home_scope')
    controls = {c['control_id']: c for c in definition['controls']}
    records = document['commands'] if kind == 'plan' else [document]
    unique([r['control_id'] for r in records])
    if kind == 'plan':
        require(utc(document['valid_from_utc']) < utc(document['valid_until_utc']), 'invalid_time')
    for record in records:
        c = controls.get(record['control_id'])
        require(c is not None, 'unknown_control')
        if kind in ('pool_request', 'pool_feedback'):
            require(c['contract']['name'] == 'pool_service', 'instruction_mismatch')
            if kind == 'pool_feedback':
                utc(record['reported_at_utc'])
                # Feedback for an older request is historical, not new authority.
                continue
            require(record['accepted'] == revision(c), 'revision_mismatch')
            lifetime = (utc(record['expires_at_utc']) - utc(record['issued_at_utc'])).total_seconds()
            require(0 < lifetime <= 120, 'invalid_time')
            if record['action'] == 'release':
                require(record['objective'] is None and record['plan_id'] is None, 'objective_mismatch')
            else:
                require(record['plan_id'] is not None, 'plan_required')
                require(record['objective'] == c['desired']['objective'], 'objective_mismatch')
                require(not c['local']['setup_gaps'] and c['desired']['included'], 'not_ready')
            continue
        if kind == 'local_binding':
            require(record['binding_revision'] == c['local']['binding_revision'], 'revision_mismatch')
            unique([r['role'] for r in record['roles']])
            if c['contract']['name'] == 'pool_service':
                for role in record['roles']:
                    require(role['role'] in {'request','feedback','water_temperature'} and not role.get('members'), 'customer_boundary')
                    domain = 'script' if role['role'] == 'request' else 'sensor'
                    require(role['registry']['domain'] == domain, 'customer_boundary')
            continue
        if kind == 'runtime':
            # Accepted/active may deliberately lag pending desired settings.
            if record['lease'] is not None:
                utc(record['lease']['expires_at_utc'])
            require(record['active'] is None or record['permission']['enabled'], 'permission_off_active')
            require(record['active'] is None or record['lease'] is not None, 'active_without_lease')
            continue
        require(record['accepted'] == revision(c), 'revision_mismatch')
        require(not c['local']['setup_gaps'] and c['desired']['included'], 'not_ready')
        instruction, name = record['instruction'], c['contract']['name']
        objective, limits = c['desired']['objective'], c['local']['limits']
        if name == 'battery_dispatch':
            require('intent' in instruction, 'instruction_mismatch')
            intent = instruction['intent']
            require(intent in c['local'].get('supported_intents', []), 'unsupported_intent')
            require(instruction['charge_ceiling_w'] <= limits['charge_max_w'] and instruction['discharge_ceiling_w'] <= limits['discharge_max_w'], 'invalid_limits')
            if intent in ('charge_pv_first','charge_grid_first'):
                require(objective['allow_grid_charge'], 'grid_charge_not_allowed')
                require(instruction['discharge_ceiling_w'] == 0, 'intent_limits')
            if intent == 'discharge_pv_first':
                require(objective['allow_battery_export'], 'export_not_allowed')
                require(instruction['charge_ceiling_w'] == 0, 'intent_limits')
            if intent == 'hold':
                require(instruction['charge_ceiling_w'] == instruction['discharge_ceiling_w'] == 0, 'intent_limits')
        elif name == 'pool_service':
            require('request' in instruction, 'instruction_mismatch')
            require(all(instruction[k] == objective[k] for k in ('start_c','stop_c','defer_policy')), 'objective_mismatch')
        else:
            raise ContractError('instruction_mismatch')
