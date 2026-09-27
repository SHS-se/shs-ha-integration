"""Real durable command transport for controller fixtures."""
import asyncio
import atexit
from pathlib import Path
import tempfile
from uuid import uuid4
from shs_core.command_journal import CommandJournal
from shs_core.command_transport import CommandTransport

_TEMP = tempfile.TemporaryDirectory()
atexit.register(_TEMP.cleanup)


def command_transport():
    journal = CommandJournal(Path(_TEMP.name) / (uuid4().hex + '.sqlite')).open('fixture')
    return CommandTransport(journal, asyncio.to_thread)
