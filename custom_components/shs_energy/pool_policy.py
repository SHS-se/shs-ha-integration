"""Complete customer pool requests and correlated feedback; no plant controls."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

if __package__:
    from .contract_v1.validate import validate, revision, utc
    from .control_capabilities import finite_number
else:
    from contract_v1.validate import validate, revision, utc
    from control_capabilities import finite_number


def stamp(now):
    return now.isoformat().replace('+00:00', 'Z')


def request(definition, control_id, sequence, now, *, command=None, plan_id=None, expires=None):
    control = next(c for c in definition['controls'] if c['control_id'] == control_id)
    objective = deepcopy(control['desired']['objective']) if command else None
    if command:
        if command['accepted'] != revision(control) or command['control_id'] != control_id:
            raise ValueError('pool instruction revision mismatch')
        instruction = command['instruction']
        if set(instruction) != {'request', 'start_c', 'stop_c', 'defer_policy'} or instruction['request'] not in ('heat', 'defer'):
            raise ValueError('complete heat/defer request required')
        if any(instruction[k] != objective[k] for k in ('start_c', 'stop_c', 'defer_policy')):
            raise ValueError('pool instruction does not match the accepted objective')
        limits = control['local']['limits']
        low, high, step = (finite_number(limits[k]) for k in ('minimum_c', 'maximum_c', 'step_c'))
        start, stop = (finite_number(objective[k]) for k in ('start_c', 'stop_c'))
        if not low <= start < stop <= high or step <= 0 or any(abs((v-low)/step-round((v-low)/step)) > 1e-5 for v in (start, stop)):
            raise ValueError('pool objective exceeds reviewed bounds or supported steps')
        if expires is None or not now < expires <= now + timedelta(seconds=120):
            raise ValueError('pool request requires current bounded authority')
    else:
        expires, plan_id = now + timedelta(seconds=120), None
    payload = dict(kind='pool_request', schema_version=1, home_id=definition['home_id'], control_id=control_id,
                   request_id=str(uuid4()), request_sequence=sequence, accepted=revision(control),
                   action=command['instruction']['request'] if command else 'release', plan_id=plan_id,
                   objective=objective, issued_at_utc=stamp(now), expires_at_utc=stamp(expires))
    validate(payload, definition)
    return payload


def feedback(value, sent, definition, now):
    validate(value, definition)
    if value['kind'] != 'pool_feedback' or any(value[k] != sent[k] for k in
            ('home_id', 'control_id', 'request_id', 'request_sequence', 'accepted', 'plan_id')):
        raise ValueError('feedback belongs to another pool request')
    reported = utc(value['reported_at_utc'])
    if reported < utc(sent['issued_at_utc']) or not -5 <= (now-reported).total_seconds() <= 120:
        raise ValueError('pool feedback is stale, future-dated or predates the request')
    ack, operation = value['acknowledgement'], value['operation']
    if sent['action'] == 'release':
        if ack not in ('released', 'release_failed') or (ack == 'released') != (operation == 'released'):
            raise ValueError('release requires explicit correlated release feedback')
    elif ack in ('released', 'release_failed') or operation == 'released':
        raise ValueError('heat/defer request has incompatible release feedback')
    if operation == 'limited_deferral' and sent['action'] != 'defer':
        raise ValueError('limited deferral must refer to a defer request')
    return deepcopy(value)
