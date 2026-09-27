"""Cold, sealed evidence for real gateway/inbox tests."""
from contextlib import closing
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parents[1]/'custom_components/shs_energy'))
from shs_core.command_journal import Command, CommandJournal, NativeAction, WriterLease, entry_paths
from shs_core.gateway_journal import GatewayJournal

IDENTITY = dict(entry_id='entry', migration_id='migration', export_sha256='a'*64,
                pair=dict(protocol=2, app_version='app-final', integration_version='gateway-final', core_sha256='b'*64))


def seed(directory):
    path, lock = entry_paths(directory, 'entry')
    source = CommandJournal(path).open('preparation')
    for name, status in (('pending', None), ('completed', 'service_returned'), ('failed', 'not_sent')):
        command = Command(name, 'controller', 'pool', 'plan', NativeAction('switch.pool', 'turn_on'))
        source.prepare(command)
        if status:
            source.finish(command, status)
    source.stopped()
    with WriterLease(lock) as lease:
        source.seal(lease, IDENTITY['migration_id'])
        with closing(source.connect(readonly=True)) as db:
            gateway = GatewayJournal.seed(Path(directory)/'gateway.sqlite', IDENTITY, {}, {}, db)
    return source, gateway
