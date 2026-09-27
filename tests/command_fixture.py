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


def native_executor(hass):
    from shs_core.native_commands import NativeExecutor
    async def send(domain, service, data):
        await hass.services.async_call(domain, service, data, blocking=True)
    return NativeExecutor(command_transport(), lambda entity: hass.states.get(entity),
                          lambda: hass.config.units.temperature_unit, send)


def controller_inputs(hass):
    from shs_core.controller_inputs import ControllerInputs
    return ControllerInputs(lambda entity: hass.states.get(entity),
                            lambda: hass.config.units.temperature_unit, lambda entity: None)
