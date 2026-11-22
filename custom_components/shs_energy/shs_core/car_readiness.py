"""Location readiness: one observed home visit, frozen on departure.

The gateway folds this record atomically with ordered location receipts. It is
shared by every backend; capturing a plan never changes the clock.
"""
from datetime import datetime, timezone

READINESS_SECONDS = 60 * 3600
MISSING = (None, '', 'unknown', 'unavailable')


def _time(value, default):
    if value is None:
        return default
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo is not None else default
    except (ValueError, TypeError, AttributeError):
        return default


def _elapsed(record, at):
    if record['home'] is True:
        return min(READINESS_SECONDS, max(0, (at - _time(record['home_since'], at)).total_seconds()))
    return record['elapsed_seconds']


def observe_location(record, entity_id, observation, now):
    """Fold one ordered reading. Repeated home reports cannot renew grace."""
    if not record or record['entity_id'] != entity_id:
        record = dict(entity_id=entity_id, home=None, home_since=None,
                      elapsed_seconds=0, available=False, estimated=True)
    state = (observation or {}).get('state')
    if state in MISSING:
        # A gap can hide an entire home visit. Retain measured evidence; the
        # projector separately uses a mature clock until an arrival repairs it.
        return {**record, 'available': False,
                'estimated': record['estimated'] or record['home'] is not True}
    changed = min(_time(observation.get('last_changed'), now), now)
    home = state == 'home'
    if home and record['home'] is not True:
        return dict(entity_id=entity_id, home=True, home_since=changed.isoformat(),
                    elapsed_seconds=0, available=True,
                    estimated=record['home'] is False and not record['available'])
    if not home and record['home'] is True:
        return dict(entity_id=entity_id, home=False, home_since=None,
                    elapsed_seconds=_elapsed(record, changed), available=True,
                    estimated=record['estimated'] or not record['available'])
    return {**record, 'home': home, 'available': True}


def location_readiness(record, entity_id, now):
    """Project measured time, or an explicitly estimated conservative bound."""
    known = record is not None and record['entity_id'] == entity_id
    estimated = not known or not record['available'] or record['estimated']
    clock = dict(elapsed_seconds=READINESS_SECONDS if estimated else _elapsed(record, now),
                 running=bool(known and record['home'] is True),
                 observed_at=now.astimezone(timezone.utc).isoformat())
    return clock, estimated
