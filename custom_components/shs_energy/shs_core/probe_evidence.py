"""Interval-local source facts; statistical freshness decisions belong to learning."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from math import isfinite


COMPANION_CLASSES = frozenset(('humidity', 'carbon_dioxide', 'sound_pressure'))


def probe_source_topology(probes, entities, devices, read_state):
    """Resolve physical companions from registry membership, never names or rooms."""
    result = {}
    for primary in probes:
        entity = entities.get(primary)
        device_id = getattr(entity, 'device_id', None)
        device = devices.get(device_id)
        parent = getattr(device, 'via_device_id', None)
        companions = []
        if device_id:
            for candidate in entities.values():
                if candidate.entity_id == primary or candidate.device_id != device_id or candidate.disabled_by is not None:
                    continue
                state = read_state(candidate.entity_id)
                attributes = getattr(state, 'attributes', {}) if state is not None else {}
                if attributes.get('device_class') in COMPANION_CLASSES and attributes.get('state_class') == 'measurement':
                    companions.append(candidate.entity_id)
        result[primary] = {'companions':sorted(companions),
            'cohort_key':sha256(parent.encode()).hexdigest() if isinstance(parent, str) and parent else None}
    return result


def _number(value):
    return type(value) in (int, float) and isfinite(value)


def _time(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromtimestamp(value, timezone.utc) if _number(value) else None


def native_probe_bounds(rows, start, end, seconds):
    """Require every underlying measurement interval, including the predecessor."""
    duration = 900 if seconds == 300 else 3600
    parts = {}
    for row in rows:
        at = _time(row.get('start'))
        if at is None or at.timestamp() % seconds or not start <= at < end:
            continue
        finish = _time(row.get('end'))
        if finish is not None and finish != at + timedelta(seconds=seconds):
            continue
        # Recorder's weighted mean can round just outside identical extrema.
        # Neutral change evidence comes from the recorded min/max pair, not
        # floating-point agreement with that independently computed mean.
        if not all(_number(row.get(key)) for key in ('mean', 'min', 'max')) or row['min'] > row['max']:
            continue
        parts[at] = row
    buckets = {at.replace(minute=(at.minute//15)*15 if duration == 900 else 0, second=0, microsecond=0) for at in parts}
    result = {}
    for at in buckets:
        stamps = [at + timedelta(seconds=i) for i in range(0, duration, seconds)]
        if at < start or at + timedelta(seconds=duration) > end or any(stamp not in parts for stamp in stamps):
            continue
        result[at] = (sum(parts[stamp]['mean'] for stamp in stamps)/len(stamps),
                      min(parts[stamp]['min'] for stamp in stamps), max(parts[stamp]['max'] for stamp in stamps))
    return result


def probe_interval_facts(at, duration, probes, topology, bounds, means):
    def unchanged(entity):
        current = bounds.get(entity, {}).get(at)
        previous = bounds.get(entity, {}).get(at - timedelta(seconds=duration))
        if current is None or previous is None:
            return None
        return current[1] == current[2] == previous[1] == previous[2]
    facts = []
    # Matches the existing median's one physical vote and deterministic area.
    area_by_entity = {entity:room for room, entity in sorted(probes.items())}
    for entity, room in sorted(area_by_entity.items()):
        metadata = topology.get(entity) or {}
        own = unchanged(entity)
        companions = metadata.get('companions') or []
        changes = [unchanged(companion) for companion in companions]
        joint = None if own is None or not changes or any(value is None for value in changes) else own and all(changes)
        facts.append({'source_key':sha256(entity.encode()).hexdigest(), 'area_key':room,
            'mean':means.get(entity, {}).get(at), 'unchanged':own, 'joint_unchanged':joint,
            'cohort_key':metadata.get('cohort_key'), 'reported':None, 'unavailable':None})
    return {'start':at.isoformat(), 'duration_s':duration, 'probes':facts}
