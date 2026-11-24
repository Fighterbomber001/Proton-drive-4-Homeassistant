"""Bounded, authenticated transport for the local Proton Drive backup app."""

import asyncio
from base64 import b64encode
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
import json
import re
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout
from yarl import URL

MAX_BACKUP_BYTES = 100 * 1024**3
MAX_METADATA_BYTES = 24_576
MAX_INVENTORY_BYTES = 32 * 1024**2
CHUNK_BYTES = 256 * 1024
# A JSON response may escape Unicode even if the app counts compact UTF-8 metadata.
_MAX_METADATA_RESPONSE_BYTES = MAX_METADATA_BYTES * 3
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_INSTALLATION_ID = re.compile(r"[a-f0-9]{32}")
_TOKEN = re.compile(r"[A-Za-z0-9._~-]{32,128}")
_HEALTH_TIMEOUT = ClientTimeout(total=8)
# The app waits for the complete cloud transfer before confirming or streaming.
# Its CLI allows 2h per transfer; leave headroom for metadata and verification.
_BACKUP_TIMEOUT = ClientTimeout(total=None, connect=10, sock_read=10_800)
_FOLDERS = frozenset(("share", "addons/local", "ssl", "media"))
_BOOLEAN_FIELDS = (
    "database_included", "homeassistant_included", "protected",
)
_METADATA_FIELDS = frozenset((
    "addons", "backup_id", "date", "extra_metadata", "folders",
    "homeassistant_version", "name", "size", *_BOOLEAN_FIELDS,
))


class BackendError(Exception):
    """The backend could not complete the requested operation."""


class BackendProtocolError(BackendError):
    """The HTTP peer did not follow the expected protocol."""


class BackendInvalidURL(BackendError):
    """The configured backend URL is not an HTTP(S) origin."""


class BackendInvalidAuth(BackendError):
    """The local pairing token is missing, invalid, or rejected."""


class BackendUnavailable(BackendError):
    """The backend is unreachable or currently unavailable."""


class BackendAuthenticationRequired(BackendError):
    """The user needs to sign back in to Proton in the app."""


class BackupMissing(BackendError):
    """The requested backup was not found."""


class BackendDeleteUnsupported(BackendError):
    """This backend cannot safely delete a remote backup."""


class BackendStatus(NamedTuple):
    """Validated identity and connection state of the paired installation."""

    installation_id: str
    connected: bool


def _validate_url(url: str) -> URL:
    """Accept only a plain HTTP(S) origin, never embedded secrets or suffixes."""
    if (
        not isinstance(url, str)
        or len(url) > 2048
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
        or any(char in url for char in "@?#\\")
    ):
        raise BackendInvalidURL("Use a private HTTP(S) address without credentials or a path.")
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.path not in ("", "/")
            or parts.port == 0
        ):
            raise ValueError
        return URL(url).origin()
    except (ValueError, UnicodeError):
        raise BackendInvalidURL("Use a private HTTP(S) address without credentials or a path.") from None


def _verify_id(backup_id: str) -> str:
    if not isinstance(backup_id, str) or not _ID.fullmatch(backup_id):
        raise BackendProtocolError("Invalid backup identifier.")
    return backup_id


def _metadata_bytes(metadata: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise BackendProtocolError("Invalid backup metadata.") from None
    if len(encoded) > MAX_METADATA_BYTES:
        raise BackendProtocolError("Backup metadata exceeds the size limit.")
    return encoded


def _validate_metadata(metadata: Any, backup_id: str | None = None) -> dict[str, Any]:
    if not isinstance(metadata, dict) or metadata.keys() != _METADATA_FIELDS:
        raise BackendProtocolError("Invalid backup metadata.")
    stored_id = _verify_id(metadata["backup_id"])
    if backup_id is not None and stored_id != backup_id:
        raise BackendProtocolError("Unexpected backup identifier in metadata.")
    addons, fields, folders = metadata["addons"], metadata["extra_metadata"], metadata["folders"]
    if (
        not isinstance(addons, list)
        or any(
            not isinstance(addon, dict)
            or addon.keys() != {"name", "slug", "version"}
            or not isinstance(addon["slug"], str)
            or not (addon["name"] is None or isinstance(addon["name"], str))
            or not (addon["version"] is None or isinstance(addon["version"], str))
            for addon in addons
        )
        or not isinstance(fields, dict)
        or any(not isinstance(key, str) or not isinstance(value, (bool, str))
               for key, value in fields.items())
        or not isinstance(folders, list)
        or any(not isinstance(folder, str) or folder not in _FOLDERS for folder in folders)
        or not isinstance(metadata["name"], str)
        or not isinstance(metadata["date"], str) or "T" not in metadata["date"]
        or not (metadata["homeassistant_version"] is None or isinstance(metadata["homeassistant_version"], str))
        or any(type(metadata[field]) is not bool for field in _BOOLEAN_FIELDS)
        or type(metadata["size"]) is not int
        or not 0 <= metadata["size"] <= MAX_BACKUP_BYTES
    ):
        raise BackendProtocolError("Invalid backup metadata.")
    try:
        datetime.fromisoformat(metadata["date"])
    except ValueError:
        raise BackendProtocolError("Invalid backup date.") from None
    _metadata_bytes(metadata)
    return metadata


async def _read_json(response: ClientResponse, limit: int) -> dict[str, Any]:
    if response.content_type != "application/json":
        raise BackendProtocolError("Expected a JSON response from the backup app.")
    if response.content_length is not None and response.content_length > limit:
        raise BackendProtocolError("The backup app response exceeds the size limit.")
    body = bytearray()
    async for chunk in response.content.iter_chunked(min(limit, CHUNK_BYTES)):
        if len(body) + len(chunk) > limit:
            raise BackendProtocolError("The backup app response exceeds the size limit.")
        body.extend(chunk)
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise BackendProtocolError("Invalid JSON from the backup app.") from None
    if not isinstance(data, dict):
        raise BackendProtocolError("Invalid JSON from the backup app.")
    return data


async def _check_status(response: ClientResponse) -> None:
    if response.status == 200:
        return
    if response.status == 401:
        raise BackendInvalidAuth("The backup app rejected the local pairing token.")
    if response.status == 503:
        raise BackendUnavailable("The backup app is temporarily unavailable.")
    if response.status in (404, 501, 502):
        try:
            code = (await _read_json(response, 4096)).get("error")
        except BackendProtocolError:
            code = None
        if response.status == 404 and code == "backup_not_found":
            raise BackupMissing("Backup not found.")
        if response.status == 501 and code == "delete_not_supported":
            raise BackendDeleteUnsupported("Deleting backups from Proton Drive is not supported.")
        if response.status == 502 and code == "authentication_required":
            raise BackendAuthenticationRequired("Sign in again in the Proton Drive Backup app.")
        if response.status == 502:
            raise BackendError("Proton Drive did not complete the backup operation.")
    raise BackendProtocolError("Unexpected HTTP status from the backup app.")


class BackupDownload(AsyncIterator[bytes]):
    """An owning iterator that can close the response before its first read."""

    def __init__(self, response: ClientResponse, size: int) -> None:
        self._response = response
        self._remaining = size
        self._closed = False

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        try:
            chunk = await self._response.content.read(CHUNK_BYTES)
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except (ClientError, OSError, TimeoutError):
            await self.aclose()
            raise BackendUnavailable("The backup download was interrupted.") from None
        self._remaining -= len(chunk)
        if self._remaining < 0 or (not chunk and self._remaining):
            await self.aclose()
            raise BackendProtocolError("The backup download has an unexpected size.")
        if not chunk:
            await self.aclose()
            raise StopAsyncIteration
        return chunk

    async def aclose(self) -> None:
        """Release the HTTP response on normal completion, cancellation, or abandon."""
        self._closed = True
        self._response.close()


class BackendClient:
    """Talk to a paired backup backend without owning the injected session."""

    def __init__(self, session: ClientSession, url: str, token: str) -> None:
        self._url = _validate_url(url)
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise BackendInvalidAuth("The local pairing token must have 32 to 128 URL-safe characters.")
        self._session = session
        self._headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"}

    def _request(self, method: str, path: str, *, timeout: ClientTimeout, **kwargs: Any):
        # Only the configured app origin should receive the pairing token.
        return self._session.request(
            method, self._url.with_path(path), timeout=timeout,
            allow_redirects=False, raise_for_status=False, auto_decompress=False,
            headers={**self._headers, **kwargs.pop("headers", {})}, **kwargs,
        )

    async def _request_json(
        self, method: str, path: str, *, limit: int = _MAX_METADATA_RESPONSE_BYTES,
        timeout: ClientTimeout = _BACKUP_TIMEOUT, **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            async with self._request(method, path, timeout=timeout, **kwargs) as response:
                await _check_status(response)
                return await _read_json(response, limit)
        except (ClientError, OSError, TimeoutError):
            raise BackendUnavailable("Unable to reach the local backup app.") from None

    async def status(self) -> BackendStatus:
        """Identify the paired app and report its Proton sign-in status."""
        data = await self._request_json("GET", "/v1/status", timeout=_HEALTH_TIMEOUT, limit=4096)
        installation_id = data.get("installation_id")
        if (
            type(data.get("api_version")) is not int or data["api_version"] != 1
            or not isinstance(installation_id, str) or not _INSTALLATION_ID.fullmatch(installation_id)
            or type(data.get("connected")) is not bool
            or data.get("delete_supported") is not False
        ):
            raise BackendProtocolError("Invalid or incompatible backup app status.")
        return BackendStatus(installation_id, data["connected"])

    async def list_backups(self) -> list[dict[str, Any]]:
        """Fetch validated backup metadata, with no duplicate IDs."""
        data = await self._request_json("GET", "/v1/backups", limit=MAX_INVENTORY_BYTES)
        if not isinstance(backups := data.get("backups"), list):
            raise BackendProtocolError("Invalid backup inventory.")
        entries = [_validate_metadata(item) for item in backups]
        if len({entry["backup_id"] for entry in entries}) != len(entries):
            raise BackendProtocolError("The backup inventory contains duplicate identifiers.")
        return entries

    async def get_backup(self, backup_id: str) -> dict[str, Any]:
        """Fetch validated metadata for one backup."""
        data = await self._request_json("GET", f"/v1/backups/{_verify_id(backup_id)}/metadata")
        return _validate_metadata(data, backup_id)

    async def upload_backup(
        self, metadata: dict[str, Any],
        open_stream: Callable[[], Awaitable[AsyncIterator[bytes]]],
        on_progress: Callable[[int], None],
    ) -> None:
        """Stream a TAR and wait for the app to confirm committed metadata."""
        metadata = _validate_metadata(metadata)
        backup_id, expected_size = metadata["backup_id"], metadata["size"]
        header = b64encode(_metadata_bytes(metadata)).decode("ascii")
        writer_task: asyncio.Task[Any] | None = None
        cleanup_task: asyncio.Future[Any] | None = None
        fully_sent = False

        async def send() -> AsyncIterator[bytes]:
            nonlocal writer_task, cleanup_task, fully_sent
            writer_task = asyncio.current_task()
            position = 0
            try:
                source = await open_stream()
                try:
                    async for chunk in source:
                        if not isinstance(chunk, bytes) or position + len(chunk) > expected_size:
                            raise BackendProtocolError("The backup upload has an unexpected size.")
                        if chunk:
                            yield chunk
                            position += len(chunk)
                            on_progress(position)
                    if position != expected_size:
                        raise BackendProtocolError("The backup upload has an unexpected size.")
                finally:
                    if close := getattr(source, "aclose", None):
                        # An early response can cancel aiohttp's writer even
                        # while it is closing the Home Assistant backup reader.
                        cleanup_task = asyncio.ensure_future(close())
                        await asyncio.shield(cleanup_task)
            except BackendError:
                raise
            except Exception:
                raise BackendError("Unable to read the backup archive for upload.") from None
            fully_sent = True

        sending = send()
        try:
            result = await self._request_json(
                "PUT", f"/v1/backups/{backup_id}", data=sending,
                headers={"X-Backup-Metadata": header, "Content-Type": "application/octet-stream"},
            )
        finally:
            # aiohttp can stop the writer on early errors without exhausting its iterator.
            if sending.ag_running and writer_task is not None:
                if not writer_task.cancelling():
                    writer_task.cancel()
                await asyncio.gather(writer_task, return_exceptions=True)
            try:
                await sending.aclose()
            finally:
                if cleanup_task is not None:
                    # Join a close the cancelled HTTP writer could not wait for.
                    await asyncio.gather(cleanup_task, return_exceptions=True)
        if not fully_sent or _validate_metadata(result, backup_id) != metadata:
            raise BackendProtocolError("The backup app did not confirm this upload.")

    async def download_backup(self, backup_id: str) -> BackupDownload:
        """Check status before returning a streaming, explicitly closable TAR."""
        response = None
        try:
            response = await self._request(
                "GET", f"/v1/backups/{_verify_id(backup_id)}/download", timeout=_BACKUP_TIMEOUT,
            )
            await _check_status(response)
            size = response.content_length
            if (
                response.content_type not in ("application/octet-stream", "application/x-tar")
                or response.headers.get("Content-Encoding", "identity") != "identity"
                or size is None or not 0 <= size <= MAX_BACKUP_BYTES
            ):
                raise BackendProtocolError("Invalid backup archive response.")
        except (ClientError, OSError, TimeoutError):
            if response is not None:
                response.close()
            raise BackendUnavailable("Unable to open the backup download.") from None
        except BaseException:
            if response is not None:
                response.close()
            raise
        return BackupDownload(response, size)

    async def delete_backup(self, backup_id: str) -> None:
        """Ask the version-one API; it must report that deletion is unsupported."""
        await self._request_json(
            "DELETE", f"/v1/backups/{_verify_id(backup_id)}", limit=4096, timeout=_HEALTH_TIMEOUT,
        )
        raise BackendProtocolError("Unexpected backup deletion response.")
