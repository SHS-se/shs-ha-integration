"""Release-independent admission of the closed companion protocol.

Capabilities name exact message schemas, not optional fallback behaviours.
Migration identities describe provenance and never participate in negotiation.
"""

PROTOCOL = 3
CAPABILITIES = frozenset({
    'ordered-receipts-v1', 'native-devices-v1', 'native-battery-v1',
    'source-queries-v1', 'entity-projection-v1', 'app-requests-v1',
    'app-configuration-v1',
})


def _version(value):
    if type(value) is not str or not value:
        raise ValueError('A nonempty release version is required')
    return value


def hello(app_version):
    return dict(protocol=PROTOCOL, app_version=_version(app_version),
                requires=sorted(CAPABILITIES))


def offer(companion_version):
    return dict(protocol=PROTOCOL, companion_version=_version(companion_version),
                capabilities=sorted(CAPABILITIES))


def _capabilities(value):
    if (type(value) is not list or any(type(item) is not str or not item for item in value)
            or len(set(value)) != len(value)):
        raise ValueError('Protocol capabilities must be unique nonempty names')
    return set(value)


def admit(requirement, available):
    if (type(requirement) is not dict or set(requirement) != {'protocol', 'app_version', 'requires'}
            or type(available) is not dict or set(available) != {'protocol', 'companion_version', 'capabilities'}):
        raise ValueError('Unsupported SHS handshake; update the app and companion')
    _version(requirement['app_version'])
    _version(available['companion_version'])
    if (type(requirement['protocol']) is not int or type(available['protocol']) is not int
            or requirement['protocol'] != PROTOCOL or available['protocol'] != PROTOCOL):
        raise ValueError(f'SHS requires companion protocol {PROTOCOL}; update the app and companion')
    missing = _capabilities(requirement['requires']) - _capabilities(available['capabilities'])
    if missing:
        raise ValueError('Update the SHS companion; required capabilities are missing: ' + ', '.join(sorted(missing)))
