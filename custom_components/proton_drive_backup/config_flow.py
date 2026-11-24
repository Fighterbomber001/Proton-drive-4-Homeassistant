"""Pair Home Assistant with a local, already signed-in backup app."""

from typing import Any

import probatio

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .client import (
    BackendAuthenticationRequired, BackendClient, BackendError, BackendInvalidAuth,
    BackendInvalidURL, BackendUnavailable,
)
from .const import CONF_BACKEND_URL, CONF_INTEGRATION_TOKEN, DEFAULT_BACKEND_URL, DOMAIN, NAME


class ProtonDriveBackupConfigFlow(ConfigFlow, domain=DOMAIN):
    """Manage local app pairing. The Proton credentials stay in the app."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_pair("user", user_input)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_pair("reauth_confirm", user_input, self._get_reauth_entry())

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_pair("reconfigure", user_input, self._get_reconfigure_entry())

    async def _async_pair(
        self, step_id: str, user_input: dict[str, Any] | None, entry: ConfigEntry | None = None,
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        default_url = entry.data[CONF_BACKEND_URL] if entry is not None else DEFAULT_BACKEND_URL
        if user_input is not None:
            url = user_input[CONF_BACKEND_URL].strip()
            token = user_input[CONF_INTEGRATION_TOKEN].strip()
            try:
                client = BackendClient(async_get_clientsession(self.hass), url, token)
                status = await client.status()
            except BackendInvalidURL:
                errors[CONF_BACKEND_URL] = "invalid_url"
            except BackendInvalidAuth:
                errors[CONF_INTEGRATION_TOKEN] = "invalid_auth"
            except BackendAuthenticationRequired:
                errors["base"] = "authentication_required"
            except BackendUnavailable:
                errors["base"] = "cannot_connect"
            except BackendError:
                errors["base"] = "invalid_backend_response"
            else:
                if not status.connected:
                    errors["base"] = "authentication_required"
                else:
                    data = {CONF_BACKEND_URL: url, CONF_INTEGRATION_TOKEN: token}
                    # Keep the backup destination stable when its address or token changes.
                    await self.async_set_unique_id(status.installation_id)
                    if entry is not None:
                        self._abort_if_unique_id_mismatch()
                        return self.async_update_reload_and_abort(entry, data=data)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title=NAME, data=data)

        return self.async_show_form(
            step_id=step_id,
            data_schema=probatio.Schema({
                probatio.Required(CONF_BACKEND_URL, default=default_url): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL),
                ),
                probatio.Required(CONF_INTEGRATION_TOKEN): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off"),
                ),
            }),
            errors=errors,
        )
