"""Pair and register the Proton Drive native backup destination."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .backup import ProtonDriveBackupAgent, async_set_agent
from .client import (
    BackendAuthenticationRequired, BackendClient, BackendError, BackendInvalidAuth,
    BackendInvalidURL, BackendUnavailable,
)
from .const import CONF_BACKEND_URL, CONF_INTEGRATION_TOKEN, DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry[ProtonDriveBackupAgent]) -> None:
    """Register the agent only after its app and Proton sign-in are verified."""
    try:
        client = BackendClient(
            async_get_clientsession(hass), entry.data[CONF_BACKEND_URL], entry.data[CONF_INTEGRATION_TOKEN],
        )
        status = await client.status()
    except BackendInvalidAuth as error:
        raise ConfigEntryAuthFailed(
            str(error), translation_domain=DOMAIN, translation_key="invalid_auth",
        ) from None
    except BackendUnavailable as error:
        raise ConfigEntryNotReady(
            str(error), translation_domain=DOMAIN, translation_key="backend_unavailable",
        ) from None
    except BackendAuthenticationRequired as error:
        raise ConfigEntryNotReady(
            str(error), translation_domain=DOMAIN, translation_key="authentication_required",
        ) from None
    except (KeyError, BackendInvalidURL):
        raise ConfigEntryError(
            "Invalid saved app connection settings.", translation_domain=DOMAIN,
            translation_key="invalid_url",
        ) from None
    except BackendError as error:
        raise ConfigEntryError(
            str(error), translation_domain=DOMAIN, translation_key="invalid_backend_response",
        ) from None
    if entry.unique_id != status.installation_id:
        raise ConfigEntryError(
            "The backup app installation has changed.", translation_domain=DOMAIN,
            translation_key="installation_mismatch",
        )
    if not status.connected:
        raise ConfigEntryNotReady(
            "Sign in to Proton in the backup app.", translation_domain=DOMAIN,
            translation_key="authentication_required",
        )
    entry.runtime_data = ProtonDriveBackupAgent(entry.title, status.installation_id, client)
    async_set_agent(hass, entry.entry_id, entry.runtime_data)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry[ProtonDriveBackupAgent]) -> None:
    """Remove the backup destination and notify the Backup integration."""
    async_set_agent(hass, entry.entry_id, None)
