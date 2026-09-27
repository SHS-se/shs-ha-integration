"""HA mode entities relay intent to the app's single configuration writer."""
from copy import deepcopy
from uuid import uuid4

from .shs_core.gateway_journal import GatewayConflict


async def async_execution_devices(hass, entry, choices=None, *, include_suggestions=False):
    catalogue = entry.runtime_data.catalogue
    if catalogue is None:
        raise GatewayConflict('Waiting for SHS app entity catalogue')
    return deepcopy(catalogue['devices'])


async def configuration_request(entry, operation, body):
    coordinator = entry.runtime_data
    if coordinator.projection is None or not coordinator.online:
        raise GatewayConflict('SHS app has not published its configuration')
    return await coordinator.service.request_app('configuration',dict(operation=operation,body={**body,
        'expected_revision':coordinator.projection['configuration']['revision'],'request_id':uuid4().hex}))


async def async_set_execution_mode(hass, entry, device_key, mode):
    return await configuration_request(entry,'control',dict(device_key=device_key,mode=mode))
