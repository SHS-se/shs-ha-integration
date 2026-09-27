"""Small bridge status; detailed diagnostic downloads are owned by the app."""
from .shs_core.api_contract import INTEGRATION_VERSION

async def async_get_config_entry_diagnostics(hass,entry):
    coordinator=entry.runtime_data
    return dict(integration_version=INTEGRATION_VERSION,connected=coordinator.online,
        diagnostic_downloads=coordinator.app_url+'/#system' if coordinator.app_url else None)
