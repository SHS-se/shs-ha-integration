"""Active failures along the planning chain, with existing correction targets."""
from copy import deepcopy


STAGES = {
    'website_choices': ('Website choices could not be refreshed', 'SHS retries the connection automatically. The last received choices remain in use.'),
    'cloud_status': ('Server connection failed', 'Check the server connection. SHS retries automatically.'),
    'tariff': ('Tariff data could not be received', 'Check the server connection and account settings.'),
    'prices': ('Electricity prices could not be received', 'Check the price source and server connection.'),
    'inputs': ('Planning data could not be collected', 'Inspect the reported source. SHS needs usable inputs to request a plan.'),
    'collection': ('Household readings could not be collected', 'Inspect the reported source. The next upload needs usable readings.'),
    'thermal_inputs': ('Room observations could not be collected', 'Inspect the reported source. Other household data can still be sent.'),
    'pool_inputs': ('Pool observations could not be collected', 'Inspect the reported source. Other household data can still be sent.'),
    'upload': ('Planning data could not be sent', 'Check the server connection. A new plan cannot be requested until the upload succeeds.'),
    'configuration': ('Server configuration could not be confirmed', 'Refresh website choices and inspect the reported configuration problem.'),
    'planning': ('The server could not create a plan', 'Inspect the reported planning failure. The previous accepted plan remains subject to its existing validity.'),
    'delivery': ('The new plan could not be received', 'SHS retries delivery automatically. The previous accepted plan remains subject to its existing validity.'),
    'plan': ('No executable plan is available', 'Inspect the planning inputs and server connection. Devices hold the last settings SHS sent.'),
    'acceptance': ('The new plan could not be accepted', 'A corrected plan is needed. Download diagnostics if the refusal persists.'),
    'acknowledgement': ('Plan acceptance could not be reported', 'SHS retries the acknowledgement automatically. The server has not confirmed receipt.'),
    'runtime_report': ('Controller status could not be sent to the server', 'Check the server connection. Website controller status may be out of date.'),
    'notifications': ('Plan notification connection failed', 'SHS retries the notification connection automatically. New requests may be delayed.'),
    'replan_request': ('The requested replan could not be completed', 'Inspect the reported failure. SHS retries the request automatically.'),
    'failure_report': ('The planning failure could not be reported', 'Check the server connection. The website has not received the failure reason.'),
    'history_upload': ('Energy history could not be sent', 'Check the server connection. SHS retries the history upload.'),
    'history_pricing': ('Energy history could not be priced', 'Inspect the reported price source. SHS retries this calculation at the next quarter-hour exchange.'),
    'background': ('An app operation failed', 'Inspect the failure below. Download app logs if it persists.'),
    'projection': ('Home Assistant status could not be updated', 'Check the Home Assistant connection. Its displayed controller status may be out of date.'),
}


def pipeline_issue(stage, detail, *, fix=None, key=None):
    title, next_step = STAGES[stage]
    if stage == 'delivery' and getattr(detail, 'code', None) == 'planning_job_not_found':
        next_step = 'The server no longer has this job. Request a new plan. Devices hold the last settings SHS sent.'
    return dict(key='pipeline:'+(key or stage), stage=stage, severity='error', title=title,
                detail=str(detail), next_step=next_step, fix=deepcopy(fix or {'kind':'diagnostics'}))


def merge_attention(*groups):
    """Stable identities give each owner one notice across repeated projections."""
    items = {item['key']:item for group in groups for item in group}
    configured = {(field['key'],field.get('scope','configuration'),field.get('device_key'))
                  for item in items.values() if not item['key'].startswith('controller:')
                  for field in item.get('fix',{}).get('fields',[])}
    duplicates = [item for key,item in items.items() if (
        key.startswith('controller:') and item.get('fix',{}).get('fields')
        and all((f['key'],f.get('scope','configuration'),f.get('device_key')) in configured
                for f in item['fix']['fields']))]
    blocked = {(f['key'],f.get('scope','configuration'),f.get('device_key'))
               for item in duplicates for f in item['fix']['fields']}
    for item in duplicates:
        items.pop(item['key'])
    for key,item in items.items():
        if not key.startswith('controller:') and any(
            (f['key'],f.get('scope','configuration'),f.get('device_key')) in blocked
            for f in item.get('fix',{}).get('fields',[])):
            items[key] = {**item, 'severity':'error'}
    return sorted(items.values(), key=lambda item: (0 if item['severity']=='error' else 1, item['key']))


def execution_attention(controllers, battery, devices):
    names = {d.get('permission',{}).get('controller_id'):d.get('name',d['key']) for d in devices}
    names.update(battery='House battery', pool='Pool heating', ev='Car charging')
    results = [pipeline_issue('inputs',detail,fix={'kind':'entity','entity_id':entity},key='battery-input:'+entity)
               for entity,detail in battery.get('input_errors',{}).items()]
    if battery.get('planning_error'):
        results.append(pipeline_issue('failure_report',battery['planning_error'],key='battery-planning'))
    for key, status in {**controllers, 'battery':battery}.items():
        failed = (status.get('state') in ('fault','unsupported','limited')
                  or status.get('execution_blocked') or status.get('plan_rejection')
                  or status.get('command_state') in ('physical_scope_blocked','native_guard_blocked','attempt_limit','journal_fault'))
        if not failed:
            continue
        rejection = status.get('plan_rejection')
        reason = (rejection.get('reason') or rejection.get('code')) if rejection else status.get('reason')
        if status.get('command_state') == 'physical_scope_blocked':
            reason = 'The battery command is blocked by the available grid capacity. '+(reason or '')
        results.append(dict(key='controller:'+key, stage='execution', severity='error',
            title=f"{names.get(key,key.removeprefix('device:'))}: plan execution is blocked",
            detail=reason or 'The controller cannot carry out its current instructions.',
            next_step=status.get('next_step') or 'Inspect the controller and source readings. SHS holds the last setting it sent.',
            fix=deepcopy(status.get('fix') or {'kind':'diagnostics'}),
            plan_id=status.get('plan_id'), slot_start=status.get('slot_start')))
    return results
