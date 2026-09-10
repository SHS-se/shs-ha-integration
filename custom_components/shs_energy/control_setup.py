"""Local control proposals, registry resolution and durable setup reservations.

This module never grants permission, acquires runtime ownership or calls devices.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re
from uuid import UUID

if __package__:
    from .control_capabilities import finite_number, primitive, validate_value
else:
    from control_capabilities import finite_number, primitive, validate_value

PRESETS = json.loads(Path(__file__).with_name('control_presets.json').read_text())
CONTRACTS = PRESETS['contracts']
WRITES = {'switch', 'select', 'set_number', 'request_state'}


def identity(registry):
    if not isinstance(registry, dict) or set(registry) != {'domain', 'platform', 'unique_id'}:
        raise ValueError('registry domain, platform and unique_id are required')
    if not all(isinstance(v, str) and v.strip() for v in registry.values()):
        raise ValueError('registry identity must contain non-empty strings')
    return tuple(registry[k] for k in ('domain', 'platform', 'unique_id'))


def opaque_id(value):
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('control ID must be a server-issued UUIDv4') from None
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError('control ID must be a server-issued UUIDv4')
    return value


def normal_text(value):
    return ' '.join(str(value or '').split()).strip()


def describe_inventory(entities, *, temperature_unit='°C'):
    """Normalize registry/device observations; keep live values out of capabilities."""
    result = {}
    for entity_id, entity in entities.items():
        item = deepcopy(entity)
        item['entity_id'] = entity_id
        for key in ('manufacturer', 'model', 'name', 'platform'):
            item[key] = normal_text(item.get(key))
        try:
            registry = item.get('registry')
            identity(registry)
            if item.get('disabled'):
                raise ValueError('entity is disabled in the registry')
            if item.get('state') in (None, 'unknown', 'unavailable'):
                raise ValueError('entity is unavailable')
            item['capability'] = primitive(registry['domain'], item.get('attributes', {}), temperature_unit=temperature_unit)
            item['error'] = None
        except ValueError as err:
            item['capability'] = None
            item['error'] = str(err)
        result[entity_id] = item
    return result


def resolve(registry, inventory):
    key = identity(registry)
    found = [e for e in inventory.values() if e.get('registry') and identity(e['registry']) == key]
    if not found:
        raise ValueError('registry identity is missing; select an explicit replacement')
    if len(found) != 1:
        raise ValueError('registry identity resolves ambiguously')
    if found[0].get('error'):
        raise ValueError(found[0]['error'])
    return found[0]


def group_claims(entity, inventory, visiting=None):
    """Expand only HA groups; customer script internals are deliberately opaque."""
    key = identity(entity['registry'])
    visiting = set() if visiting is None else set(visiting)
    if not entity.get('capability'):
        raise ValueError(entity.get('error') or 'group interface is unavailable')
    if key in visiting:
        raise ValueError('cyclic group membership')
    visiting.add(key)
    result = {key}
    if entity['registry']['platform'] != 'group':
        return result
    members = entity.get('attributes', {}).get('entity_id')
    if not isinstance(members, list) or not members:
        raise ValueError('group members are missing')
    for member_id in members:
        member = inventory.get(member_id)
        if member is None or member.get('error'):
            raise ValueError(f'group member {member_id} is missing or unavailable')
        if member['capability']['operation'] != entity['capability']['operation']:
            raise ValueError('group member has a different operation')
        result.update(group_claims(member, inventory, visiting))
    return result


def discover(inventory):
    """Offer compatible candidates without selecting or overwriting any binding."""
    interfaces = [{k: e.get(k) for k in ('entity_id', 'registry', 'name', 'manufacturer', 'model', 'platform', 'capability', 'error')}
                  for e in inventory.values() if e.get('registry') and e['registry']['domain'] in
                  ('number', 'input_number', 'switch', 'input_boolean', 'select', 'input_select', 'climate', 'script', 'sensor', 'binary_sensor')]
    candidates = {}
    for name, contract in CONTRACTS.items():
        candidates[name] = {}
        for role, requirement in contract['roles'].items():
            found = []
            for entity in inventory.values():
                cap = entity.get('capability')
                if not cap or cap['operation'] != requirement['operation'] or cap['unit'] not in requirement['units']:
                    continue
                if name == 'battery_dispatch':
                    words = set(re.findall(r'[a-z0-9]+', (entity['name'] + ' ' + entity['registry']['unique_id']).lower()))
                    if entity['manufacturer'].casefold() != PRESETS['sigenergy']['manufacturer'] or not set(requirement['tokens']) <= words:
                        continue
                found.append({'registry': deepcopy(entity['registry']), 'entity_id': entity['entity_id'], 'device_id': entity.get('device_id')})
            candidates[name][role] = found
    return {'interfaces': interfaces, 'candidates': candidates, 'presets': deepcopy(PRESETS), 'control_enabled': False}


def setup_report(spec, inventory, *, other_claims=None):
    """Validate a complete local proposal and return precise, non-executable status."""
    errors, resolved, claims, capabilities = [], {}, set(), []
    def error(code, path, message):
        errors.append({'code': code, 'path': path, 'message': message})
    allowed = {'control_id', 'contract', 'roles', 'limits', 'handover', 'normal_profile', 'interface_review', 'semantics'}
    if not isinstance(spec, dict):
        raise ValueError('control setup must be an object')
    if set(spec) - allowed:
        error('unknown_fields', 'setup', 'Unknown setup fields: ' + ', '.join(sorted(set(spec) - allowed)))
    try:
        opaque_id(spec.get('control_id'))
    except ValueError as err:
        error('invalid_identity', 'control_id', str(err))
    contract = spec.get('contract')
    if not isinstance(contract, dict) or set(contract) != {'name', 'version'} or contract.get('version') != 1 or isinstance(contract.get('version'), bool) or contract.get('name') not in CONTRACTS:
        error('unsupported_contract', 'contract', 'Choose a supported version-1 contract')
        return _report(errors, resolved, claims, capabilities)
    name = contract['name']
    template = CONTRACTS[name]
    if spec.get('handover') != template['handover']:
        error('handover_required', 'handover', 'Review handover: ' + template['handover'])
    roles = spec.get('roles', {})
    if not isinstance(roles, dict):
        roles = {}
        error('invalid_roles', 'roles', 'Roles must be an object')
    for role in set(roles) - set(template['roles']):
        error('unsupported_role', 'roles.' + role, 'Role is outside this control contract')
    for role, requirement in template['roles'].items():
        try:
            if role not in roles:
                raise ValueError('Select the ' + role.replace('_', ' ') + ' interface')
            entity = resolve(roles[role], inventory)
            cap = entity['capability']
            if cap['operation'] != requirement['operation'] or cap['unit'] not in requirement['units']:
                raise ValueError(f"requires {requirement['operation']} in {'/'.join(requirement['units'])}")
            if role == 'request' and not entity.get('accepts_request'):
                raise ValueError('script service must accept the complete request field')
            owned = group_claims(entity, inventory) if cap['operation'] in WRITES else set()
            if claims & owned:
                raise ValueError('two roles claim the same actuator or group member')
            conflict = next((owner for owner, keys in (other_claims or {}).items() if keys & owned), None)
            if conflict is not None:
                raise ValueError(f'actuator already reserved by {conflict}')
            claims.update(owned)
            resolved[role] = entity
            capabilities.append({'role': role, **deepcopy(cap)})
        except ValueError as err:
            error('binding_invalid', 'roles.' + role, str(err))
    try:
        _validate_profile(spec, resolved)
    except (ValueError, KeyError, TypeError) as err:
        error('profile_invalid', 'limits/normal_profile', str(err))
    if name == 'battery_dispatch' and resolved:
        devices = {e.get('device_id') for e in resolved.values()}
        if any(e.get('manufacturer', '').casefold() != PRESETS['sigenergy']['manufacturer'] for e in resolved.values()):
            error('preset_mismatch', 'roles', 'Sigenergy bindings require a confirmed Sigenergy plant')
        if None in devices or len(devices) != 1:
            error('plant_mismatch', 'roles', 'Battery roles must belong to one confirmed plant device')
    if name == 'pool_service':
        review = spec.get('interface_review')
        expected = {'request_contract_version': 1, 'expiry_handling_reviewed': True,
                    'feedback_correlation_reviewed': True, 'release_policy_reviewed': True}
        if not isinstance(review, dict) or review != expected or any(type(review.get(k)) is not type(v) for k, v in expected.items()):
            error('customer_interface_review_required', 'interface_review', 'Review request v1, expiry, correlated feedback and release in the customer automation')
    return _report(errors, resolved, claims, capabilities)


def _report(errors, resolved, claims, capabilities):
    return {'status': 'invalid' if errors else 'ready', 'errors': errors,
            'resolved': {role: e['entity_id'] for role, e in resolved.items()},
            'capabilities': capabilities, 'claims': [list(k) for k in sorted(claims)],
            'control_enabled': False, 'agreement': 'not_evaluated'}


def _exact(values, keys, label):
    if not isinstance(values, dict) or set(values) != set(keys):
        raise ValueError(label + ' requires exactly: ' + ', '.join(keys))
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in values.values()):
        raise ValueError(label + ' requires numeric values, not strings or booleans')
    return {k: finite_number(v) for k, v in values.items()}


def _bounded(entity, low, high, normal):
    cap = entity['capability']
    if low > high or low < cap['minimum'] or high > cap['maximum']:
        raise ValueError('reviewed limits must narrow the hardware range')
    for value in (low, high, normal):
        validate_value(cap, value)
    if not low <= normal <= high:
        raise ValueError('normal setting is outside reviewed limits')


def _validate_profile(spec, resolved):
    name, limits, normal = spec['contract']['name'], spec.get('limits'), spec.get('normal_profile')
    if name == 'battery_dispatch':
        values = _exact(limits, ('charge_max_w', 'discharge_max_w', 'normal_charge_w', 'normal_discharge_w', 'minimum_soc_pct', 'maximum_soc_pct'), 'Battery limits')
        if not 0 <= values['minimum_soc_pct'] < values['maximum_soc_pct'] <= 100:
            raise ValueError('SOC limits must be ordered percentages')
        if normal != {'mode': PRESETS['sigenergy']['normal_mode']}:
            raise ValueError('review the normal self-consumption mode')
        semantics = spec.get('semantics')
        if semantics != {'preset': 'sigenergy', 'measurement_charge_positive': True} or semantics['measurement_charge_positive'] is not True:
            raise ValueError('review Sigenergy mode policy and positive-charging measurement direction')
        for direction in ('charge', 'discharge'):
            maximum, baseline = values[direction + '_max_w'], values['normal_' + direction + '_w']
            if 4294967295 in (maximum, baseline):
                raise ValueError('unset ESS limit sentinel is not a reviewed power request')
            if not 0 <= baseline <= maximum or maximum <= 0:
                raise ValueError('normal ESS ceilings must be non-negative and within reviewed ratings')
            entity = resolved.get(direction + '_ceiling')
            if entity:
                if entity['capability']['minimum'] < 0:
                    raise ValueError('ESS ceilings require non-negative hardware limits; signed inverter adjustments are not battery controls')
                scale = 1000 if entity['capability']['unit'] == 'kW' else 1
                try:
                    _bounded(entity, 0, maximum / scale, baseline / scale)
                except ValueError as err:
                    raise ValueError(f'{direction} ceiling: {err}') from None
        if 'mode' in resolved:
            for mode in set(PRESETS['sigenergy']['modes'].values()):
                validate_value(resolved['mode']['capability'], mode)
    elif name == 'pool_service':
        values = _exact(limits, ('minimum_c', 'maximum_c', 'step_c'), 'Pool operating band')
        if not values['minimum_c'] < values['maximum_c'] or values['step_c'] <= 0 or values['step_c'] > values['maximum_c'] - values['minimum_c']:
            raise ValueError('review an ordered pool range and positive step')
        if normal != {'policy': 'customer_automation'}:
            raise ValueError('customer automation must own the normal profile')
    elif name in ('temperature_target', 'adjustable_output'):
        _exact(normal, ('output',), 'Normal output')
        values = _exact(limits, ('minimum', 'maximum'), 'Reviewed output range')
        if 'output' in resolved:
            _bounded(resolved['output'], values['minimum'], values['maximum'], finite_number(normal['output']))
        if name == 'adjustable_output':
            semantics = spec.get('semantics', {})
            unit = resolved.get('output', {}).get('capability', {}).get('unit')
            if unit == 'A':
                if set(semantics) != {'meaning', 'phase_count', 'voltage'} or semantics['meaning'] != 'current_limit' or type(semantics['phase_count']) is not int or semantics['phase_count'] not in (1, 3) or finite_number(semantics['voltage']) <= 0:
                    raise ValueError('current limits require meaning, phase count and voltage')
            elif unit == '%' and (set(semantics) != {'meaning', 'rated_power_w'} or semantics['meaning'] != 'output_percent' or finite_number(semantics['rated_power_w']) <= 0):
                raise ValueError('percentage output requires an explicit rated-power model')
            elif unit == 'W' and semantics != {'meaning': 'power_limit'}:
                raise ValueError('declare the number as a power limit')
    else:
        if name == 'relay_schedule':
            values = _exact(limits, ('minimum_on_seconds', 'minimum_off_seconds'), 'Relay timings')
            if any(v < 0 for v in values.values()):
                raise ValueError('relay timings cannot be negative')
        else:
            values = _exact(limits, ('maximum_inhibit_seconds',), 'Permission limit')
            if values['maximum_inhibit_seconds'] <= 0 or spec.get('semantics') not in ({'permit_state': 'on'}, {'permit_state': 'off'}):
                raise ValueError('review maximum inhibit duration and permission polarity')
        if not isinstance(normal, dict) or set(normal) != {'output'}:
            raise ValueError('review the normal output setting')
        if 'output' in resolved:
            validate_value(resolved['output']['capability'], normal['output'])


def configured_targets(options):
    """Every existing local actuator, shared by old/new setup reservation checks."""
    targets = {}
    for key, mapping in options.get('device_control_mappings', {}).items():
        ids = []
        for field in ('actuator_entity_ids', 'companion_actuator_entity_ids'):
            ids.extend(mapping.get(field, []))
        for field in ('setpoint_entity_id', 'control_entity_id', 'offset_entity_id', 'mode_entity_id', 'permit_entity_id'):
            if mapping.get(field):
                ids.append(mapping[field])
        targets['device:' + key] = ids
    for system, fields in {
        'battery': ('battery_power_entity', 'battery_mode_entity', 'battery_authority_entity'),
        'pool': ('pool_start_temperature_entity', 'pool_stop_temperature_entity', 'pool_permission_entity'),
        'ev': ('ev_charge_switch_entity',),
    }.items():
        targets[system] = [options[field] for field in fields if options.get(field)]
    return targets


def reserved_claims(record, inventory):
    """Hold persisted members as well as any newly observed group members."""
    owned = {tuple(k) for k in record['claims']}
    for registry in record['spec']['roles'].values():
        try:
            entity = resolve(registry, inventory)
            if entity['capability']['operation'] in WRITES:
                owned.update(group_claims(entity, inventory))
        except ValueError:
            continue  # Never discard saved reservations or guess a replacement.
    return owned


def validate_legacy_targets(options, inventory, reservations):
    if not reservations:
        return
    for ids in configured_targets(options).values():
        for entity_id in ids:
            entity = inventory.get(entity_id)
            if entity:
                owned = group_claims(entity, inventory)
                for control_id, claims in reservations.items():
                    if owned & claims:
                        raise ValueError(f'{entity_id} is reserved by control {control_id}')


def read_binding_file(path):
    """Read our own store envelope, failing on corruption rather than clearing it."""
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None


class VerifiedBindingStore:
    """Do not equate a logged/queued store write with persisted configuration."""

    def __init__(self, store, read_disk, key, stopping):
        self.store, self.read_disk, self.key, self.stopping = store, read_disk, key, stopping

    def _payload(self, envelope):
        if envelope is None:
            return None
        if not isinstance(envelope, dict) or set(envelope) != {'version', 'minor_version', 'key', 'data'} or envelope['version'] != 1 or envelope['minor_version'] != 1 or envelope['key'] != self.key:
            raise ValueError('invalid binding storage envelope')
        return envelope['data']

    async def async_load(self):
        return self._payload(await self.read_disk())

    async def async_save(self, payload):
        if self.stopping():
            raise OSError('cannot save a binding while the integration host is stopping')
        await self.store.async_save(payload)
        if self._payload(await self.read_disk()) != payload:
            raise OSError('binding write did not persist; no acknowledgement was published')


def validate_binding_record(key, record):
    """Validate durable binding identity without requiring an available device."""
    opaque_id(key)
    if not isinstance(record, dict) or set(record) != {'binding_revision', 'spec', 'claims', 'capabilities', 'members'} or not isinstance(record['spec'], dict) or record['spec'].get('control_id') != key or type(record['binding_revision']) is not int or record['binding_revision'] < 1:
        raise ValueError('invalid saved control binding')
    if not isinstance(record['spec'].get('roles'), dict) or not isinstance(record['claims'], list) or not isinstance(record['capabilities'], list) or not isinstance(record['members'], dict):
        raise ValueError('invalid saved control reservation')
    for registry in record['spec']['roles'].values():
        identity(registry)
    for claim in record['claims']:
        if not isinstance(claim, list) or len(claim) != 3 or not all(isinstance(v, str) and v for v in claim):
            raise ValueError('invalid saved actuator identity')


class ControlSetup:
    """Serialize local edits; only publish a saved report after Store completes."""

    def __init__(self, store, home_id, inventory, external_claims, *, lock=None):
        self.store, self.home_id = store, home_id
        self.inventory, self.external_claims = inventory, external_claims
        self.records = {}
        self.lock = lock if lock is not None else asyncio.Lock()

    async def async_load(self):
        payload = await self.store.async_load()
        if payload is None:
            return
        if not isinstance(payload, dict) or set(payload) != {'schema_version', 'home_id', 'records'} or payload['schema_version'] != 1 or payload['home_id'] != self.home_id or not isinstance(payload['records'], dict):
            raise ValueError('invalid or foreign-home control binding store')
        for key, record in payload['records'].items():
            validate_binding_record(key, record)
        self.records = deepcopy(payload['records'])

    def _other_claims(self, inventory, exclude=None):
        claims = dict(self.external_claims(inventory))
        for key, record in getattr(self, 'runtime_records', lambda: {})().items():
            if key != exclude:
                claims['runtime:' + key] = reserved_claims(record['binding'], inventory)
        for control_id, record in self.records.items():
            if control_id != exclude:
                claims[control_id] = reserved_claims(record, inventory)
        return claims

    def report(self, spec, inventory=None):
        if not isinstance(spec, dict):
            raise ValueError('control setup must be an object')
        inventory = self.inventory() if inventory is None else inventory
        try:
            owners = self._other_claims(inventory, spec.get('control_id'))
        except ValueError as err:
            return _report([{'code': 'ownership_unresolved', 'path': 'roles', 'message': str(err)}], {}, set(), [])
        report = setup_report(spec, inventory, other_claims=owners)
        saved = self.records.get(spec.get('control_id'))
        if saved and saved['spec'] == spec and report['claims'] != saved['claims']:
            report['errors'].append({'code': 'group_members_changed', 'path': 'roles',
                                     'message': 'Group members changed; review and save a new binding revision'})
            report['status'] = 'invalid'
        if saved and saved['spec'] == spec and report['capabilities'] != saved['capabilities']:
            report['errors'].append({'code': 'capabilities_changed', 'path': 'capabilities',
                                     'message': 'Capabilities changed; review and save a new binding revision'})
            report['status'] = 'invalid'
        return report

    def summary(self):
        inventory = self.inventory()
        return {'controls': [dict(control_id=key, contract=r['spec']['contract'], binding_revision=r['binding_revision'],
                                  **self.report(r['spec'], inventory)) for key, r in self.records.items()],
                'targets': [{'contract': {'name': name, 'version': 1}, **self.report(
                    {'contract': {'name': name, 'version': 1}, 'roles': {}}, inventory)}
                    for name in ('battery_dispatch', 'pool_service') if not any(r['spec']['contract']['name'] == name for r in self.records.values())],
                'control_enabled': False}

    async def async_save(self, spec, expected_revision):
        async with self.lock:
            if not isinstance(spec, dict):
                raise ValueError('control setup must be an object')
            spec = deepcopy(spec)
            inventory = self.inventory()
            prior = self.records.get(spec.get('control_id'))
            current = prior['binding_revision'] if prior else 0
            if type(expected_revision) is not int or expected_revision != current:
                raise ValueError('binding revision conflict; reload before saving')
            report = setup_report(spec, inventory, other_claims=self._other_claims(inventory, spec.get('control_id')))
            if report['errors']:
                raise ValueError('; '.join(e['path'] + ': ' + e['message'] for e in report['errors']))
            if prior and prior['spec'] == spec and prior['claims'] == report['claims'] and prior['capabilities'] == report['capabilities']:
                return {**report, 'saved': True, 'binding_revision': current}
            records = deepcopy(self.records)
            members = {}
            for role, registry in spec['roles'].items():
                entity = resolve(registry, inventory)
                if entity['registry']['platform'] == 'group':
                    members[role] = [list(k) for k in sorted(group_claims(entity, inventory) - {identity(registry)})]
            records[spec['control_id']] = {'binding_revision': current + 1, 'spec': deepcopy(spec),
                'claims': report['claims'], 'capabilities': report['capabilities'], 'members': members}
            await self.store.async_save({'schema_version': 1, 'home_id': self.home_id, 'records': records})
            self.records = records
            return {**self.report(spec), 'saved': True, 'binding_revision': current + 1}

    def local_binding(self, control_id):
        """Project persisted installation data into the step-1 local-only schema."""
        record = self.records[control_id]
        inventory = self.inventory()
        roles = []
        for role, registry in record['spec']['roles'].items():
            item = {'role': role, 'registry': deepcopy(registry),
                    'provenance': {'owner': 'ha', 'source': 'user', 'evidence': 'Persisted local setup review'}}
            try:
                entity = resolve(registry, inventory)
                item['last_entity_id'] = entity['entity_id']
            except ValueError:
                pass  # Keep stable identity visible; report() describes the missing setup.
            if role in record['members']:
                item['members'] = [dict(zip(('domain', 'platform', 'unique_id'), k)) for k in record['members'][role]]
            roles.append(item)
        return {'kind': 'local_binding', 'schema_version': 1, 'home_id': self.home_id,
                'control_id': control_id, 'binding_revision': record['binding_revision'], 'roles': roles}
