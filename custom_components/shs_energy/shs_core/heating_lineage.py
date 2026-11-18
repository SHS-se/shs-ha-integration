"""Reviewed, dated physical sources shared by normal capture and history replay."""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
import json


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("Heating source dates must be timestamps")
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.tzinfo is None:
        raise ValueError("Heating source dates must include a timezone")
    return at.astimezone(timezone.utc)


def validate_heating_lineage(value):
    if not isinstance(value, dict) or not isinstance(value.get('revision'), str) or not value['revision'] \
            or not isinstance(value.get('assignments'), list):
        raise ValueError("Heating history sources require a revision and assignments")
    assignments = []
    for row in value['assignments']:
        if not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key] for key in
                ('room_key', 'target_source_key', 'observed_source_key', 'observed_temperature_entity_id')):
            raise ValueError("Heating source identity is incomplete")
        if sha256(row['observed_temperature_entity_id'].encode()).hexdigest() != row['observed_source_key']:
            raise ValueError("Historical temperature identity does not match its source")
        for field in ('target_device_statistics', 'observed_device_statistics'):
            if not isinstance(row.get(field), dict) or any(not isinstance(k, str) or not k or
                    not isinstance(v, str) or not v for k, v in row[field].items()):
                raise ValueError("Heating meter source identities are incomplete")
        if set(row['target_device_statistics']) != set(row['observed_device_statistics']):
            raise ValueError("Historical meters must name the same devices")
        limits = row.get('heater_energy_limits_w', {})
        if not isinstance(limits, dict) or any(k not in row['observed_device_statistics'] or
                type(v) not in (int, float) or not isfinite(v) or v <= 0 for k, v in limits.items()):
            raise ValueError("Historical attribution limits must name measured devices")
        begin, finish = _stamp(row.get('valid_from')), _stamp(row.get('valid_to'))
        if finish <= begin:
            raise ValueError("Heating source interval must have positive duration")
        clean = {**row, 'valid_from':begin.isoformat(), 'valid_to':finish.isoformat(),
                 'heater_energy_limits_w':dict(limits)}
        for prior in assignments:
            if (prior['room_key'], prior['target_source_key'], prior['target_device_statistics']) == \
                    (clean['room_key'], clean['target_source_key'], clean['target_device_statistics']) \
                    and _stamp(prior['valid_from']) < finish and _stamp(prior['valid_to']) > begin \
                    and any(prior[key] != clean[key] for key in ('observed_source_key',
                            'observed_device_statistics', 'heater_energy_limits_w')):
                raise ValueError("Historical heating source assignments conflict")
        assignments.append(clean)
    return {'revision':value['revision'], 'assignments':assignments}


@dataclass(frozen=True)
class HeatingSourceSegment:
    start: datetime
    end: datetime
    sources: object


def matching_heating_assignments(sources, lineage):
    """A reviewed old source remains eligible only for its current target binding."""
    assignments = []
    bindings = {binding.room_key:binding for binding in sources.bindings}
    for row in (lineage or {}).get('assignments', []):
        room = row['room_key']
        current = sources.room_air_probes.get(room)
        if current is None or sha256(current.encode()).hexdigest() != row['target_source_key']:
            continue
        binding = bindings.get(room)
        if binding and {key:sources.device_statistics.get(key) for key in binding.device_keys} != row['target_device_statistics']:
            continue
        assignments.append(row)
    return assignments


def heating_lineage_revision(sources, lineage):
    # A peer's changed meter can invalidate its reviewed probe assignment and
    # thus change the historical house median for every other room.
    return sha256(json.dumps({'revision':lineage['revision'] if lineage else None,
        'assignments':matching_heating_assignments(sources, lineage)},
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def heating_source_segments(sources, lineage, start, end):
    """Apply the same source epoch to heater inputs and every house contributor."""
    assignments = [row for row in matching_heating_assignments(sources, lineage)
        if _stamp(row['valid_from']) < end and _stamp(row['valid_to']) > start]
    boundaries = sorted({start, end, *(_stamp(row[field]) for row in assignments
        for field in ('valid_from', 'valid_to') if start < _stamp(row[field]) < end)})
    result = []
    for begin, finish in zip(boundaries, boundaries[1:]):
        active = {row['room_key']:row for row in assignments
                  if _stamp(row['valid_from']) <= begin and _stamp(row['valid_to']) >= finish}
        probes, meters = dict(sources.room_air_probes), dict(sources.device_statistics)
        for room, row in active.items():
            probes[room] = row['observed_temperature_entity_id']
            meters.update(row['observed_device_statistics'])
        replaced = [replace(binding, source_key=active[binding.room_key]['observed_source_key'],
            temperature_entity_id=active[binding.room_key]['observed_temperature_entity_id'],
            heater_limits_w=active[binding.room_key]['heater_energy_limits_w'])
            if binding.room_key in active else binding for binding in sources.bindings]
        result.append(HeatingSourceSegment(begin, finish, replace(sources, bindings=replaced,
            device_statistics=meters, room_air_probes=probes)))
    return result
