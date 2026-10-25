"""Durable, owner-scoped evidence for mandatory household replanning.

The household serializes this record with planning receipts. Plan replacement
never resets an episode: unchanged equipment cannot create a replan loop.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Literal, TypedDict


class BlockedInterval(TypedDict):
    start: str
    end: str


class ExecutionChange(TypedDict):
    owner: str
    status: Literal['temporarily_absent', 'faulted', 'recovered']
    cause: str
    blocked_intervals: list[BlockedInterval]


class ExecutionReplan(TypedDict):
    request_id: str
    accepted_plan_id: str
    observed_at: str
    changes: list[ExecutionChange]


CAUSE_REASON_CHARS = 200


def fault_cause(error):
    """`Type:entity`, then the refusal's own words so the plan can say why.

    Only the first token identifies the fault: a reworded message for the same
    fault is not new evidence and must not request another plan.
    """
    identity = f"{type(error).__name__}:{getattr(error, 'entity', '') or ''}"
    reason = ' '.join(str(error).split()).rstrip('.')[:CAUSE_REASON_CHARS]
    return f'{identity} {reason}' if reason else identity


def cause_identity(cause):
    return cause.split(' ', 1)[0]


def owner_key(device):
    return device if device.startswith('$') else device.removeprefix('device:') if device.startswith('device:') else '$' + device


def requested(owner, slot):
    if not slot:
        return False
    if owner == '$ev':
        return (slot.get('ev_target_current_a') or slot.get('ev_w') or 0) > 0
    if owner == '$pool':
        if 'pool_permitted' in slot and slot['pool_permitted'] is None:
            return False
        return (slot.get('pool_w') or 0) > 0
    if owner == '$battery':
        return (slot.get('battery_charge_w') or slot.get('battery_discharge_w') or 0) > 0
    command = slot.get('device_commands', {}).get(owner, {})
    if command.get('type') == 'unavailable':
        # Nothing executes it, so recovery cannot resume it: it needs a plan.
        return False
    return bool(command.get('permitted') or command.get('on_seconds', 0) or command.get('value', 0)
                or command.get('type') == 'setpoint' or slot.get('device_loads_w', {}).get(owner, 0))


def observe(journal, owner, slot, condition, *, recovery_requires_replan=False):
    """Merge evidence; return whether there is a newly blocked/recovered service.

    Temporary absence blocks its absolute opportunity only. Faults remain
    unresolved until a real successful observation, regardless of slot/plan ID.
    """
    rows = journal.setdefault('owners', {})
    pending = journal.setdefault('pending', {})
    previous = rows.get(owner)
    if condition is None:
        if previous is None:
            return False
        rows.pop(owner)
        journal.get('recovery_sources', {}).pop(owner, None)
        journal.get('recovery_configuration', {}).pop(owner, None)
        # A still-executable accepted request can resume directly. Work
        # suppressed by either absence or fault needs a replacement schedule.
        if recovery_requires_replan or not requested(owner, slot):
            pending[owner] = {'owner': owner, 'status': 'recovered',
                              'cause': 'execution_recovered', 'blocked_intervals': []}
            return True
        pending.pop(owner, None)
        return False
    status, cause = condition['status'], condition['cause']
    if status not in ('faulted', 'temporarily_absent'):
        raise ValueError('unsupported execution availability')
    intervals = []
    if slot and requested(owner, slot):
        start = datetime.fromisoformat(slot['start'].replace('Z', '+00:00'))
        intervals = [{'start': start.isoformat(), 'end': (start + timedelta(minutes=15)).isoformat()}]
    change = {'owner': owner, 'status': status, 'cause': cause, 'blocked_intervals': intervals}
    if previous and previous['status'] == status and cause_identity(previous['cause']) == cause_identity(cause):
        if (status == 'faulted' and previous['blocked_intervals']) or not intervals or previous['blocked_intervals'] == intervals:
            return False
    # Keep the last blocked absolute opportunity through off slots/replacement.
    if previous and status == 'temporarily_absent' and not intervals:
        change['blocked_intervals'] = previous['blocked_intervals']
    rows[owner] = change
    sources = condition.get('recover_when_entities')
    if sources:
        journal.setdefault('recovery_sources', {})[owner] = list(sources)
    else:
        journal.get('recovery_sources', {}).pop(owner, None)
    # A refusal with no source to watch is retried once the settings change.
    configuration = condition.get('recover_when_configuration')
    if configuration:
        journal.setdefault('recovery_configuration', {})[owner] = configuration
    else:
        journal.get('recovery_configuration', {}).pop(owner, None)
    if not requested(owner, slot):
        return False
    pending[owner] = deepcopy(change)
    journal.pop('retry_on_exchange', None)
    return True


def admitted(journal, changes):
    """Clear only evidence included in this immutable accepted capture."""
    pending = journal.get('pending', {})
    for change in changes:
        if pending.get(change['owner']) == change:
            pending.pop(change['owner'])


def failed(journal, changes):
    """Keep failed evidence for the next existing exchange, without a solve loop."""
    for change in changes:
        # Recovery observed while the job ran must not resurrect old absence.
        if change['status'] != 'recovered' and change['owner'] not in journal.get('owners', {}):
            continue
        journal.setdefault('pending', {}).setdefault(change['owner'], deepcopy(change))
    journal['retry_on_exchange'] = True


def retain_authority(journal, owners):
    """Explicit authority exit ends an episode, without claiming physical recovery."""
    for field in ('owners', 'pending', 'recovery_sources', 'recovery_configuration'):
        if field in journal:
            journal[field] = {owner: value for owner, value in journal[field].items() if owner in owners}
    if not journal.get('pending'):
        journal.pop('retry_on_exchange', None)
