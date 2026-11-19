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
        powers = row.get('observed_device_power_statistics', {})
        if not isinstance(powers, dict) or any(k not in row['observed_device_statistics'] or
                not isinstance(v, str) or not v for k, v in powers.items()):
            raise ValueError("Historical power sources must name measured devices")
        limits = row.get('heater_energy_limits_w', {})
        if not isinstance(limits, dict) or any(k not in row['observed_device_statistics'] or
                type(v) not in (int, float) or not isfinite(v) or v <= 0 for k, v in limits.items()):
            raise ValueError("Historical attribution limits must name measured devices")
        begin, finish = _stamp(row.get('valid_from')), _stamp(row.get('valid_to'))
        if finish <= begin:
            raise ValueError("Heating source interval must have positive duration")
        clean = {**row, 'valid_from':begin.isoformat(), 'valid_to':finish.isoformat(),
                 'heater_energy_limits_w':dict(limits), 'observed_device_power_statistics':dict(powers)}
        for prior in assignments:
            if (prior['room_key'], prior['target_source_key'], prior['target_device_statistics']) == \
                    (clean['room_key'], clean['target_source_key'], clean['target_device_statistics']) \
                    and _stamp(prior['valid_from']) < finish and _stamp(prior['valid_to']) > begin \
                    and any(prior[key] != clean[key] for key in ('observed_source_key',
                            'observed_device_statistics', 'observed_device_power_statistics', 'heater_energy_limits_w')):
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
    all_assignments = matching_heating_assignments(sources, lineage)
    assignments = [row for row in all_assignments
        if _stamp(row['valid_from']) < end and _stamp(row['valid_to']) > start]
    boundaries = sorted({start, end, *(_stamp(row[field]) for row in assignments
        for field in ('valid_from', 'valid_to') if start < _stamp(row[field]) < end)})
    result = []
    for begin, finish in zip(boundaries, boundaries[1:]):
        active = {row['room_key']:row for row in assignments
                  if _stamp(row['valid_from']) <= begin and _stamp(row['valid_to']) >= finish}
        probes, meters = dict(sources.room_air_probes), dict(sources.device_statistics)
        powers, epochs = dict(sources.device_power_statistics), {}
        for binding in sources.bindings:
            history = [row for row in all_assignments if row['room_key'] == binding.room_key]
            before = max((_stamp(row['valid_to']) for row in history if _stamp(row['valid_to']) <= begin), default=None)
            after = min((_stamp(row['valid_from']) for row in history if _stamp(row['valid_from']) >= finish), default=None)
            if before is not None or after is not None:
                for key in binding.device_keys:
                    epochs[key] = (before.isoformat() if before else None, after.isoformat() if after else None)
        for room, row in active.items():
            probes[room] = row['observed_temperature_entity_id']
            meters.update(row['observed_device_statistics'])
            for key in row['observed_device_statistics']:
                powers.pop(key, None)
                epochs[key] = (row['valid_from'], row['valid_to'])
            powers.update(row.get('observed_device_power_statistics', {}))
        replaced = [replace(binding, source_key=active[binding.room_key]['observed_source_key'],
            temperature_entity_id=active[binding.room_key]['observed_temperature_entity_id'],
            heater_limits_w=active[binding.room_key]['heater_energy_limits_w'])
            if binding.room_key in active else binding for binding in sources.bindings]
        result.append(HeatingSourceSegment(begin, finish, replace(sources, bindings=replaced,
            device_statistics=meters, room_air_probes=probes, device_power_statistics=powers, counter_epochs=epochs)))
    return result


def heating_counter_sources(sources):
    """Exact scalar associations, independently of room labels or chunk dates."""
    from .counter_evidence import CounterSource
    return {key: CounterSource(energy, sources.device_power_statistics.get(key),
        begin=sources.counter_epochs.get(key, (None, None))[0],
        end=sources.counter_epochs.get(key, (None, None))[1])
        for key, energy in sources.device_statistics.items()}


def heating_counter_roster(sources, lineage, start, end):
    """Whole retained source roster, stable while the upload cursor pages."""
    result = {}
    for segment in heating_source_segments(sources, lineage, start, end):
        for device, source in heating_counter_sources(segment.sources).items():
            result.setdefault(device, {})[source.key] = source
    return result


async def prepare_heating_counter_evidence(history, sources, lineage, ledger, now):
    """Shared entry point for ordinary upload and Recorder-port replay.

    The caller owns persistence and must save the updated ledger before sending
    observations. Capture uses the returned immutable qualification snapshot.
    """
    from datetime import timedelta
    from .counter_evidence import qualification, refresh_counter_evidence
    first = min([now - timedelta(days=400),
        *(_stamp(row['valid_from']) for row in matching_heating_assignments(sources, lineage))])
    roster = heating_counter_roster(sources, lineage, first, now)
    await refresh_counter_evidence(history, ledger,
        [source for values in roster.values() for source in values.values()], now)
    return replace(sources, counter_qualification={device: [qualification(ledger, values[key])
        for key in sorted(values)] for device, values in roster.items()})


def qualified_heating_reader(history, sources, ledger):
    """Declared pairs without evidence defer native learning, never planning."""
    from .counter_evidence import envelope
    from .counter_reading import CounterReader
    bounds, pending = {}, set()
    for key, source in heating_counter_sources(sources).items():
        bound = envelope(ledger, source)
        if source.power and bound is None:
            pending.add(key)
        elif bound is not None:
            bounds[source.energy] = max(bounds.get(source.energy, 0), bound)
    return CounterReader(history, bounds), pending
