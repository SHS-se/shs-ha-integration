"""Thin registry/service boundary for local control setup; no actuator writes."""
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .legacy_pool import assert_pool_handover_complete
from .control_setup import (describe_inventory, group_claims, identity, configured_targets,
                            reserved_claims, validate_legacy_targets)


def inventory(hass):
    entities, devices = er.async_get(hass), dr.async_get(hass)
    result = {}
    for entity in entities.entities.values():
        state = hass.states.get(entity.entity_id)
        device = devices.async_get(entity.device_id) if entity.device_id else None
        domain = entity.entity_id.split('.')[0]
        data = {'registry': {'domain': domain, 'platform': entity.platform, 'unique_id': entity.unique_id},
                'name': entity.name or entity.original_name or entity.entity_id,
                'manufacturer': device.manufacturer if device else None,
                'model': device.model if device else None,
                'platform': entity.platform, 'device_id': entity.device_id,
                'disabled': entity.disabled_by is not None,
                'state': state.state if state else None,
                'attributes': dict(state.attributes) if state else {},
                'accepts_request': False}
        if domain == 'script':
            service = hass.services.async_services().get('script', {}).get(entity.entity_id.split('.', 1)[1])
            if service is not None and service.schema is not None:
                try:
                    # Schema validation only: this never invokes the script.
                    validated = service.schema({'request': {'kind': 'pool_request', 'schema_version': 1}})
                    data['accepts_request'] = validated.get('request') == {'kind': 'pool_request', 'schema_version': 1}
                except Exception:
                    data['accepts_request'] = False
        result[entity.entity_id] = data
    result = describe_inventory(result, temperature_unit=hass.config.units.temperature_unit)
    for entity in result.values():
        if entity['capability'] is None:
            continue
        domain = entity['registry']['domain']
        services = {'switch': ('turn_on', 'turn_off'), 'input_boolean': ('turn_on', 'turn_off'),
                    'number': ('set_value',), 'input_number': ('set_value',), 'climate': ('set_temperature',),
                    'select': ('select_option',), 'input_select': ('select_option',)}.get(domain, ())
        if any(not hass.services.has_service(domain, service) for service in services):
            entity.update(capability=None, error='required actuator service is unavailable')
    return result


def legacy_claims(hass, inventory_values, *, own_entry_id):
    """Reserve existing SHS mappings/journals across all local integration entries."""
    claims = {}
    for entry in hass.config_entries.async_entries('shs_energy'):
        options = entry.options
        targets = configured_targets(options)
        controller = getattr(getattr(entry, 'runtime_data', None), 'controller', None)
        if controller:
            assert_pool_handover_complete({'records': controller.records}, options)
            for key, record in controller.records.items():
                if key == 'battery':
                    raise ValueError('legacy_handover_required: unresolved signed battery ownership')
                if missing := set(record.get('originals', {})) - set(inventory_values):
                    raise ValueError(f'Pending legacy restoration {key} has missing registry entities: {sorted(missing)}')
                targets.setdefault(key, []).extend(record.get('originals', {}))
        for key, ids in targets.items():
            owned = set()
            for entity_id in ids:
                entity = inventory_values.get(entity_id)
                if entity:
                    owned.add(identity(entity['registry']))
                    if entity['registry']['platform'] == 'group':
                        owned.update(group_claims(entity, inventory_values))
            if owned:
                claims[f'legacy:{entry.entry_id}:{key}'] = owned
        if entry.entry_id != own_entry_id:
            for adapter in ('battery_controller', 'pool_controller'):
                runtime = getattr(getattr(entry, 'runtime_data', None), adapter, None)
                if runtime:
                    for key, record in runtime.records.items():
                        claims[f'runtime:{entry.entry_id}:{adapter}:{key}'] = reserved_claims(record['binding'], inventory_values)
        setup = getattr(getattr(entry, 'runtime_data', None), 'control_setup', None)
        # Other config entries share the same physical actuator registry.
        if setup and entry.entry_id != own_entry_id:
            for key, record in setup.records.items():
                claims[f'entry:{entry.entry_id}:{key}'] = reserved_claims(record, inventory_values)
    return claims


def assert_legacy_reservations(hass, options, previous):
    """Old editors must honor the same new-control reservations as new editors."""
    proposed_ids = {entity for ids in configured_targets(options).values() for entity in ids}
    previous_ids = {entity for ids in configured_targets(previous).values() for entity in ids}
    added = proposed_ids - previous_ids
    if not added:
        return  # Permission-off/removal does not acquire an actuator.
    observed = inventory(hass)
    reservations = {}
    for entry in hass.config_entries.async_entries('shs_energy'):
        setup = getattr(getattr(entry, 'runtime_data', None), 'control_setup', None)
        if setup:
            for control_id, record in setup.records.items():
                reservations[f'{entry.entry_id}:{control_id}'] = reserved_claims(record, observed)
        for adapter in ('battery_controller', 'pool_controller'):
            runtime = getattr(getattr(entry, 'runtime_data', None), adapter, None)
            if runtime:
                for key, record in runtime.records.items():
                    reservations[f'runtime:{entry.entry_id}:{adapter}:{key}'] = reserved_claims(record['binding'], observed)
    validate_legacy_targets({"device_control_mappings": {"proposal": {"actuator_entity_ids": sorted(added)}}}, observed, reservations)
