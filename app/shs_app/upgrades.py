"""Explicit active-runtime adoption of release-independent storage authority."""
from shs_core.gateway_journal import GatewayConflict
from shs_wire.protocol import PROTOCOL

from .records import RecordStore

SCHEMA = 1


async def open_runtime_schema(root, identity, activation, release):
    """Upgrade the existing active installation, without altering import evidence.

    This record versions app-owned storage orchestration; individual databases
    retain their own schemas. It is written under AppEngine's writer lease.
    A cold import still requires its exact source release and separate verification.
    """
    if activation is None:
        if identity['pair'] != release:
            raise GatewayConflict('App binary differs from the imported migration pair')
    elif activation.get('identity') != identity or activation.get('state') not in ('active', 'pending'):
        raise GatewayConflict('Runtime activation belongs to another migration or has an unsupported state')
    record = RecordStore(root / 'runtime-schema.json')
    expected = dict(schema=SCHEMA, protocol=PROTOCOL, identity=identity)
    current = await record.async_load()
    if current is None:
        # A pending initial activation is not evidence that the old cutover
        # finished. Active upgrades keep the original activation and import pair.
        if activation is not None and activation['state'] != 'active' and identity['pair'] != release:
            raise GatewayConflict('Finish the original runtime activation before upgrading')
        await record.async_save(expected)
    elif current != expected:
        raise GatewayConflict('Unsupported app runtime schema or installation identity; an explicit upgrade is required')
    return current or expected
