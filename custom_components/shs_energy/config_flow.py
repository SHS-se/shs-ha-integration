"""Initial pairing flow for Smart Home Solutions Energy."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ShsApiClient, ShsApiError, ShsPairingError
from .const import (
    CONF_BASE_URL,
    CONF_CUSTOMER_NAME,
    CONF_DEVICE_NAME,
    CONF_DEVICE_TOKEN,
    CONF_DEVICE_TOKEN_ID,
    CONF_HOME_ID,
    CONF_PAIRING_CODE,
    DEFAULT_BASE_URL,
    DOMAIN,
)


class ShsEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair with the SHS portal using a single-use pairing code."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Exchange a pairing code for this home's device credential."""
        errors: dict[str, str] = {}

        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].rstrip("/")
            client = ShsApiClient(async_get_clientsession(self.hass), base_url)
            try:
                result = await client.pair(
                    user_input[CONF_PAIRING_CODE].strip().upper(),
                    user_input[CONF_DEVICE_NAME].strip() or "Home Assistant",
                )
            except ShsPairingError:
                errors["base"] = "invalid_pairing_code"
            except ShsApiError:
                errors["base"] = "cannot_connect"
            else:
                token_id = result["device_token_id"]
                await self.async_set_unique_id(token_id)
                self._abort_if_unique_id_configured()
                customer_name = result.get("customer_name") or "Smart Home Solutions"
                return self.async_create_entry(
                    title=customer_name,
                    data={
                        CONF_BASE_URL: base_url,
                        CONF_DEVICE_TOKEN: result["device_token"],
                        CONF_DEVICE_TOKEN_ID: token_id,
                        CONF_HOME_ID: result["home_id"],
                        CONF_CUSTOMER_NAME: customer_name,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PAIRING_CODE): str,
                    vol.Required(CONF_DEVICE_NAME, default="Home Assistant"): str,
                    vol.Required(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
                }
            ),
            errors=errors,
        )
