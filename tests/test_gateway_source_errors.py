"""Execute the actual HA endpoint error boundary without an HA installation."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
import sys
sys.path.append(str(Path(__file__).parents[1]/'custom_components/shs_energy'))
from shs_core.household_ports import HouseholdReadError, ConfigurationChanged
from shs_core.gateway_journal import AdmissionPending, GatewayConflict

class SourceErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_source_read_failure_has_unavailable_wire_identity(self):
        path=Path(__file__).parents[1]/'custom_components/shs_energy/gateway.py'
        node=next(n for n in ast.parse(path.read_text()).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='websocket_gateway')
        node.decorator_list=[]
        scope=dict(SOCKETS='sockets',HouseholdReadError=HouseholdReadError,
                   ConfigurationChanged=ConfigurationChanged,GatewayConflict=GatewayConflict,
                   AdmissionPending=AdmissionPending)
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),scope)
        for error,code in ((HouseholdReadError('recorder failed'),'shs_source_unavailable'),
                           (ValueError('programming error'),'shs_gateway_rejected'),
                           (GatewayConflict('Interrupted device operation'),'shs_gateway_rejected'),
                           (AdmissionPending('Device intention belongs to an older configuration or policy'),
                            'shs_gateway_admission')):
            connection=Mock();peer=SimpleNamespace(request=AsyncMock(side_effect=error),closed=False)
            peer.service=SimpleNamespace(connection=peer)
            hass=SimpleNamespace(data={'sockets':{connection:peer}})
            await scope['websocket_gateway'](hass,connection,dict(id=7,operation='source',body={}))
            connection.send_error.assert_called_once_with(7,code,str(error))
        for error in (ConfigurationChanged('remapped'),asyncio.CancelledError()):
            peer.request=AsyncMock(side_effect=error)
            with self.assertRaises(type(error)):
                await scope['websocket_gateway'](hass,connection,dict(id=8,operation='source',body={}))
