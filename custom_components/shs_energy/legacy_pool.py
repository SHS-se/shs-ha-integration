"""One-way recognition of explicit old pool routes, never new hardware binding."""
if __package__:
    from .control_setup import configured_targets
    from .device_controls import planning_path
else:
    from control_setup import configured_targets
    from device_controls import planning_path


def pool_mappings(options):
    mappings = options.get('device_control_mappings', {})
    meters = options.get('entities_pool_heating', [])
    # This is the old typed route: setpoint/room mappings are deliberately not
    # selected by their reporting category, even when the meter is in this list.
    keys = {key for key, mapping in mappings.items() if isinstance(mapping, dict)
            and planning_path(mapping.get('control_type'), 'pool_heating') == 'pool' and key in meters}
    targets = configured_targets(options)
    owned = set(targets.get('pool', []))
    for key in keys: owned.update(targets['device:' + key])
    conflicts = {key for key in mappings if key not in keys and owned.intersection(targets.get('device:' + key, []))}
    return keys, conflicts, owned


def assert_pool_handover_complete(journal, options):
    if journal is not None and (not isinstance(journal, dict) or not isinstance(journal.get('records', {}), dict)):
        raise ValueError('legacy_handover_required: unreadable controller journal')
    keys, conflicts, targets = pool_mappings(options)
    if conflicts:
        raise ValueError('legacy_pool_overlap_review_required: review independently mapped owners before cutover: ' + ', '.join(sorted(conflicts)))
    for key, record in (journal or {}).get('records', {}).items():
        if not isinstance(record, dict) or not isinstance(record.get('options', {}), dict):
            raise ValueError('legacy_handover_required: unreadable ownership record')
        old_keys, _conflicts, old_targets = pool_mappings(record.get('options', {}))
        if key == 'pool' or key.removeprefix('device:') in keys | old_keys or set(record.get('originals', {})) & (targets | old_targets):
            raise ValueError('legacy_handover_required: release old pool/group/companion ownership using its reviewed installed-version procedure')
