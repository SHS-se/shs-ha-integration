"""Nonblocking data-quality evidence, shared by planning and execution views."""
from copy import deepcopy

ROUTINE_LEARNING_CODES = frozenset({
    'profile_estimated', 'device_history_estimated', 'running_power_unknown',
    'pool_model_quality', 'room_model_quality', 'room_estimate', 'room_forecast_estimate',
    'limited_idle_observations', 'limited_response_observations',
    'pool_empirical_service', 'boiler_power_estimated', 'baseline_ev_load_balanced',
})


def actionable_warnings(*groups):
    return [warning for warning in merge_warnings(*groups)
            if warning['code'] not in ROUTINE_LEARNING_CODES]


def planning_warning(code, scope, message, consequence, *, severity='warning', evidence=None, entity_id=None):
    value = dict(code=code, scope=scope, severity=severity, message=message, consequence=consequence)
    if evidence is not None:
        value['evidence'] = evidence
    if entity_id:
        value['entity_id'] = entity_id
    return value


def merge_warnings(*groups):
    values = {}
    for group in groups:
        for item in group or ():
            if (isinstance(item, dict) and all(isinstance(item.get(k), str)
                    for k in ('code', 'scope', 'message', 'consequence'))
                    and item.get('severity') in ('warning', 'error')):
                values[(item['scope'], item['code'])] = deepcopy(item)
    return [values[key] for key in sorted(values)]


def warning_attention(*groups):
    return [dict(key=f"planning-warning:{w['scope']}:{w['code']}", stage='data_quality',
                 severity=w['severity'], title='Planning needs attention',
                 detail=w['message']+' '+w['consequence'],
                 fix={'kind':'entity', 'entity_id':w['entity_id']} if w.get('entity_id') else {'kind':'diagnostics'})
            for w in actionable_warnings(*groups)]
