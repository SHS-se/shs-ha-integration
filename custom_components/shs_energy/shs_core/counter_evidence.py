"""Retained Recorder peak evidence, qualified by the physical counter epoch.

Buckets are evidence, not a rolling maximum cache: omission never retracts an
older bucket. An explicitly returned correction replaces that exact bucket.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import isfinite
from .household_ports import HouseholdReadError


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def moment(value):
    try:
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc) if value.tzinfo is not None else None
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
        if type(value) in (int, float) and isfinite(value):
            return datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return None


@dataclass(frozen=True)
class CounterSource:
    energy: str
    power: str | None
    extreme: str = 'max'
    begin: str | None = None
    end: str | None = None

    @property
    def key(self):
        return digest(asdict(self))


def validate_counter_evidence(value):
    """Optional durable evidence cannot make planning fail to start.

    Validate each versioned entry independently. A damaged entry becomes
    pending for its declared native source, while other valid sources survive.
    """
    result = {}
    if not isinstance(value, dict):
        return result
    for key, entry in value.items():
        if not isinstance(entry, dict) or type(entry.get('version')) is not int or entry['version'] != 1:
            continue
        raw = entry.get('source')
        if not isinstance(raw, dict) or set(raw) != {'energy', 'power', 'extreme', 'begin', 'end'}:
            continue
        if any(not isinstance(raw[name], str) or not raw[name] for name in ('energy', 'power')) or raw['extreme'] not in ('min','max'):
            continue
        if any(raw[name] is not None and (not isinstance(raw[name], str) or moment(raw[name]) is None)
               for name in ('begin','end')):
            continue
        begin, end = moment(raw['begin']), moment(raw['end'])
        if begin and end and begin >= end:
            continue
        source = CounterSource(**raw)
        if key != source.key or not isinstance(entry.get('buckets'), dict):
            continue
        buckets = {}
        for bucket_key, bucket in entry['buckets'].items():
            if not isinstance(bucket, dict) or bucket.get('period') not in ('hour','month'):
                continue
            start, finish = moment(bucket.get('start')), moment(bucket.get('end'))
            watts = bucket.get('value_w')
            if start is None or finish is None or finish <= start or (begin and start < begin) or (end and finish > end):
                continue
            if type(watts) not in (int,float) or not isfinite(watts) or watts < 0:
                continue
            expected_key = f"{bucket['period']}:{start.isoformat()}:{finish.isoformat()}"
            if bucket_key != expected_key:
                continue
            buckets[bucket_key] = {'period':bucket['period'], 'start':start.isoformat(),
                                   'end':finish.isoformat(), 'value_w':float(watts)}
        clean = {'version':1, 'source':asdict(source), 'buckets':buckets}
        checked = moment(entry.get('checked_at'))
        if checked is not None:
            clean['checked_at'] = checked.isoformat()
        result[key] = clean
    return result


def envelope(ledger, source):
    if not source.power:
        return None
    buckets = ledger.get(source.key, {}).get('buckets', {})
    values = [float(bucket['value_w']) for bucket in buckets.values()
              if type(bucket.get('value_w')) in (int, float) and isfinite(bucket['value_w'])]
    peak = max(values, default=0)
    return peak if peak > 0 else None


def qualification(ledger, source):
    """Effective qualification changes schedule repair; read clocks do not."""
    return {'source': asdict(source), 'bound_w': envelope(ledger, source)}


async def refresh_counter_evidence(history, ledger, sources, now):
    """Refresh declared pairs, retaining missing evidence across reads/restarts.

    Whole native months are efficient. A month crossing an epoch edge is never
    evidence for that epoch: read actual hours at that boundary instead. No
    monthly maximum is copied into fabricated hourly buckets.
    """
    now = now.astimezone(timezone.utc)
    clean = validate_counter_evidence(ledger)
    ledger.clear()
    ledger.update(clean)
    sources = {source.key: source for source in sources if source.power}
    due = []
    for key, source in sources.items():
        entry = ledger.get(key)
        if entry is None:
            entry = ledger[key] = {'version':1, 'source': asdict(source), 'buckets': {}}
        checked = moment(entry.get('checked_at'))
        if checked is None or not timedelta(0) <= now - checked < timedelta(hours=1):
            due.append(source)
    if not due:
        return
    first = now - timedelta(days=400)
    try:
        monthly = await history.statistics(first, now, {source.power for source in due},
            'month', {'power': 'W'}, {'max', 'min'})
    except HouseholdReadError:
        return
    hourly = {}
    failed = set()
    for source in due:
        begin = max(first, moment(source.begin) or first)
        finish = min(now, moment(source.end) or now)
        if finish <= begin:
            continue
        for row in _rows(monthly, source.power):
            start, end = moment(row.get('start')), moment(row.get('end'))
            if start is None or end is None or end <= start or start >= finish or end <= begin:
                continue
            # Recorder labels a leading partial reduction with full calendar
            # boundaries. It must not replace an earlier complete month or
            # supersede retained hours: read actual hours at this edge too.
            within_epoch = start >= first and (source.begin is None or start >= moment(source.begin)) and \
                           (source.end is None or end <= moment(source.end)) and end <= now
            if within_epoch:
                _record(ledger, source, 'month', row)
            else:
                span = max(start, begin), min(end, finish)
                hourly.setdefault(span, set()).add(source)
    for (begin, finish), selected in hourly.items():
        try:
            rows = await history.statistics(begin, finish, {source.power for source in selected},
                'hour', {'power': 'W'}, {'max', 'min'})
        except HouseholdReadError:
            failed.update(source.key for source in selected)
            continue
        for source in selected:
            for row in _rows(rows, source.power):
                start, end = moment(row.get('start')), moment(row.get('end'))
                if start is not None and end is not None and begin <= start < end <= finish:
                    _record(ledger, source, 'hour', row)
    for source in due:
        if source.key not in failed:
            ledger[source.key]['checked_at'] = now.isoformat()


def _rows(response, sensor):
    rows = response.get(sensor) if isinstance(response, dict) else None
    return (row for row in rows if isinstance(row, dict)) if isinstance(rows, (list,tuple)) else ()


def _record(ledger, source, period, row):
    raw = row.get(source.extreme)
    if type(raw) not in (int, float) or not isfinite(raw):
        return
    value = float(raw) * (-1 if source.extreme == 'min' else 1)
    if value < 0:
        value = 0.0
    start, end = moment(row['start']), moment(row['end'])
    buckets = ledger[source.key]['buckets']
    # Once a completed month is available, it supersedes its previously read
    # hours; otherwise old hourly peaks could defeat a corrected monthly row.
    if period == 'month':
        for key, bucket in list(buckets.items()):
            if bucket['period'] == 'hour' and start <= moment(bucket['start']) and moment(bucket['end']) <= end:
                del buckets[key]
    key = f'{period}:{start.isoformat()}:{end.isoformat()}'
    buckets[key] = {'period': period, 'start': start.isoformat(), 'end': end.isoformat(), 'value_w': value}
