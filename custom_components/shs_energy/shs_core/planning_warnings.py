"""Nonblocking data-quality evidence, shared by planning and execution views."""
from copy import deepcopy


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
                 severity=w['severity'], title='Planning continues with uncertain data',
                 detail=w['message']+' '+w['consequence'],
                 fix={'kind':'entity', 'entity_id':w['entity_id']} if w.get('entity_id') else {'kind':'diagnostics'})
            for w in merge_warnings(*groups)]
