"""Home Assistant BackupAgent implementation for Proton Drive."""

from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, override

from homeassistant.components.backup import (
    AgentBackup, BackupAgent, BackupAgentError, BackupNotFound, OnProgressCallback,
)
from homeassistant.components.backup.agent import BackupAgentUnreachableError
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey

from .client import (
    BackendAuthenticationRequired, BackendClient, BackendDeleteUnsupported, BackendError,
    BackendInvalidAuth, BackendProtocolError, BackendUnavailable, BackupDownload, BackupMissing,
)
from .const import DOMAIN

_AGENTS: HassKey[dict[str, "ProtonDriveBackupAgent"]] = HassKey(f"{DOMAIN}.backup_agents")
_LISTENERS: HassKey[list[Callable[[], None]]] = HassKey(f"{DOMAIN}.backup_agent_listeners")


async def async_get_backup_agents(hass: HomeAssistant, **kwargs: Any) -> list[BackupAgent]:
    """Return only agents whose configuration entry passed backend validation."""
    return list(hass.data.get(_AGENTS, {}).values())


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant, *, listener: Callable[[], None], **kwargs: Any,
) -> Callable[[], None]:
    """Notify Backup whenever this integration adds or removes an agent."""
    listeners = hass.data.setdefault(_LISTENERS, [])
    listeners.append(listener)

    @callback
    def unsubscribe() -> None:
        listeners.remove(listener)
        if not listeners:
            hass.data.pop(_LISTENERS, None)

    return unsubscribe


@callback
def async_set_agent(
    hass: HomeAssistant, entry_id: str, agent: "ProtonDriveBackupAgent | None",
) -> None:
    """Update the available agents before refreshing Home Assistant's agent list."""
    agents = hass.data.setdefault(_AGENTS, {})
    if agent is None:
        if agents.pop(entry_id, None) is None:
            return
        if not agents:
            hass.data.pop(_AGENTS, None)
    else:
        agents[entry_id] = agent
    for listener in tuple(hass.data.get(_LISTENERS, [])):
        listener()


def _agent_error(error: BackendError) -> BackupAgentError:
    """Expose a safe, translated failure via Home Assistant's backup interface."""
    kind: type[BackupAgentError] = BackupAgentError
    if isinstance(error, BackupMissing):
        kind, key = BackupNotFound, "backup_not_found"
    elif isinstance(error, BackendDeleteUnsupported):
        key = "delete_not_supported"
    elif isinstance(error, BackendInvalidAuth):
        key = "invalid_auth"
    elif isinstance(error, BackendAuthenticationRequired):
        key = "authentication_required"
    elif isinstance(error, BackendUnavailable):
        kind, key = BackupAgentUnreachableError, "backend_unavailable"
    elif isinstance(error, BackendProtocolError):
        key = "invalid_backend_response"
    else:
        key = "operation_failed"
    return kind(str(error), translation_domain=DOMAIN, translation_key=key)


class _AgentDownload(AsyncIterator[bytes]):
    """Translate failures thrown after the download response was opened."""

    def __init__(self, download: BackupDownload) -> None:
        self._download = download

    async def __anext__(self) -> bytes:
        try:
            return await anext(self._download)
        except BackendError as error:
            raise _agent_error(error) from None

    async def aclose(self) -> None:
        """Close even an unread or partly read response."""
        await self._download.aclose()


class ProtonDriveBackupAgent(BackupAgent):
    """A native destination backed by the paired HAOS app."""

    domain = DOMAIN

    def __init__(self, name: str, installation_id: str, client: BackendClient) -> None:
        self.name = name
        self.unique_id = installation_id
        self._client = client

    @override
    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        try:
            return [AgentBackup.from_dict(item) for item in await self._client.list_backups()]
        except BackendError as error:
            raise _agent_error(error) from None

    @override
    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup:
        try:
            return AgentBackup.from_dict(await self._client.get_backup(backup_id))
        except BackendError as error:
            raise _agent_error(error) from None

    @override
    async def async_upload_backup(
        self, *, open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup, on_progress: OnProgressCallback, **kwargs: Any,
    ) -> None:
        try:
            await self._client.upload_backup(
                backup.as_dict(), open_stream, lambda sent: on_progress(bytes_uploaded=sent),
            )
        except BackendError as error:
            raise _agent_error(error) from None

    @override
    async def async_download_backup(self, backup_id: str, **kwargs: Any) -> AsyncIterator[bytes]:
        try:
            return _AgentDownload(await self._client.download_backup(backup_id))
        except BackendError as error:
            raise _agent_error(error) from None

    @override
    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        try:
            await self._client.delete_backup(backup_id)
        except BackendError as error:
            raise _agent_error(error) from None
