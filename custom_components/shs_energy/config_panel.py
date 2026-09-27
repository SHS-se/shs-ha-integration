"""The integration cogwheel opens the app; settings and diagnostics live there."""
from pathlib import Path
import voluptuous as vol
from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from .shs_core.const import DOMAIN


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required('type'):'shs_energy/app_link'})
@websocket_api.async_response
async def websocket_app_link(hass, connection, msg):
    entries = hass.config_entries.async_entries(DOMAIN)
    urls = {entry.runtime_data.projection.get('app_url') for entry in entries
        if getattr(entry,'runtime_data',None) and entry.runtime_data.projection}
    urls.discard(None)
    if len(urls) != 1:
        connection.send_error(msg['id'],'app_unavailable','Open SHS Energy from Home Assistant’s Apps page while the app connects.')
        return
    connection.send_result(msg['id'],{'url':urls.pop()})


async def async_register_config_panel(hass):
    await hass.http.async_register_static_paths([StaticPathConfig('/shs_energy_frontend',str(Path(__file__).parent/'frontend'),False)])
    await panel_custom.async_register_panel(hass,frontend_url_path='shs-energy',
        webcomponent_name='shs-app-link',module_url='/shs_energy_frontend/shs-app-link.js',
        sidebar_title=None,require_admin=True,config_panel_domain=DOMAIN)
    websocket_api.async_register_command(hass,websocket_app_link)
