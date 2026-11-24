"""Exercise the Home Assistant client's contract over real HTTP."""

import asyncio
import base64
from concurrent.futures import Future
from contextlib import asynccontextmanager
from importlib.util import find_spec, module_from_spec, spec_from_file_location
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer

# Import only the transport file: the package's entry hooks require Home Assistant.
_CLIENT_PATH = Path(__file__).resolve().parents[1] / "custom_components/proton_drive_backup/client.py"
_CLIENT_SPEC = spec_from_file_location("native_integration_transport", _CLIENT_PATH)
assert _CLIENT_SPEC is not None and _CLIENT_SPEC.loader is not None
client = module_from_spec(_CLIENT_SPEC)
_CLIENT_SPEC.loader.exec_module(client)


TOKEN = "local-pairing-token-0123456789-abcdef"
STATUS = {
    "api_version": 1,
    "installation_id": "a3126fd169234a48922b70020fb26bed",
    "connected": True,
    "delete_supported": False,
}
BACKUP = {
    "addons": [{"name": "App", "slug": "local_example", "version": "1.0"}],
    "backup_id": "0123abcdef",
    "date": "2026-11-22T17:01:00+00:00",
    "database_included": True,
    "extra_metadata": {"automatic": False, "instance_id": "some-id"},
    "folders": ["ssl", "addons/local"],
    "homeassistant_included": True,
    "homeassistant_version": "2026.11.2",
    "name": "Sicherung für Zuhause",
    "protected": True,
    "size": 8,
}
BACKUP_PATH = "/v1/backups/0123abcdef"


@asynccontextmanager
async def server(*routes):
    @web.middleware
    async def require_token(request, handler):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"error": "invalid_auth"}, status=401)
        return await handler(request)

    app = web.Application(middlewares=[require_token])
    app.add_routes(routes)
    async with TestServer(app, handler_cancellation=True) as test_server:
        yield str(test_server.make_url("/")).rstrip("/")


class NativeIntegrationClientTest(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(find_spec("proton_backup"), "paired test: add the companion app to PYTHONPATH")
    def test_confirmation_wait_covers_cli_transfer_and_verification(self):
        from proton_backup.config import PROCESS_TIMEOUT_SECONDS
        self.assertGreaterEqual(client._BACKUP_TIMEOUT.sock_read, PROCESS_TIMEOUT_SECONDS + 3600)

    async def test_delayed_confirmation_needs_a_longer_read_budget(self):
        async def upload(request):
            self.assertEqual(await request.read(), b"abcdefgh")
            await asyncio.sleep(0.12)
            return web.json_response(BACKUP)
        async def chunks():
            yield b"abcdefgh"
        async def open_stream():
            return chunks()
        async with server(web.put(BACKUP_PATH, upload)) as url:
            async with ClientSession() as session:
                api = client.BackendClient(session, url, TOKEN)
                for budget, succeeds in ((0.02, False), (0.5, True)):
                    with patch.dict(client.BackendClient._request_json.__kwdefaults__,
                                    timeout=ClientTimeout(total=None, connect=1, sock_read=budget)):
                        if succeeds:
                            await api.upload_backup(BACKUP, open_stream, lambda _: None)
                        else:
                            with self.assertRaises(client.BackendUnavailable):
                                await api.upload_backup(BACKUP, open_stream, lambda _: None)

    @unittest.skipUnless(find_spec("proton_backup"), "paired test: add the companion app to PYTHONPATH")
    async def test_client_and_shipped_http_bridge_round_trip(self):
        from proton_backup.native_api import NativeApiServer
        from proton_backup.native_storage import NativeDownload

        metadata = {**BACKUP, "name": "Private Sicherung – " + "ä" * 9_000}
        operations = []
        stored = {}

        def submit(operation, backup_id, backup_metadata, path, cancel):
            operations.append((operation, backup_id))
            result = Future()
            if operation == "upload":
                stored[backup_id] = (backup_metadata, path.read_bytes())
                result.set_result(backup_metadata)
            elif operation == "list":
                result.set_result([entry[0] for entry in stored.values()])
            elif operation == "get":
                result.set_result(stored[backup_id][0])
            elif operation == "download":
                target = private / "native" / "download-fixture.tar"
                target.write_bytes(stored[backup_id][1])
                result.set_result(NativeDownload(target, stored[backup_id][0]["size"]))
            else:
                result.set_exception(AssertionError("Unexpected mutating command"))
            return result

        async def chunks():
            yield b"abc"
            yield b"defgh"

        async def open_stream():
            return chunks()

        with TemporaryDirectory() as temporary:
            private = Path(temporary)
            bridge = NativeApiServer(
                submit=submit, snapshot=lambda: SimpleNamespace(connected=True),
                installation_id=STATUS["installation_id"], token=TOKEN, private_root=private,
                host="127.0.0.1", port=0,
            )
            await bridge.async_start()
            try:
                async with ClientSession() as session:
                    api = client.BackendClient(session, f"http://127.0.0.1:{bridge.port}", TOKEN)
                    self.assertEqual((await api.status()).installation_id, STATUS["installation_id"])
                    self.assertEqual(await api.list_backups(), [])
                    await api.upload_backup(metadata, open_stream, lambda sent: None)
                    self.assertEqual(await api.list_backups(), [metadata])
                    backup_id = metadata["backup_id"]
                    self.assertEqual(await api.get_backup(backup_id), metadata)
                    stream = await api.download_backup(backup_id)
                    self.assertEqual(b"".join([chunk async for chunk in stream]), b"abcdefgh")
                    with self.assertRaises(client.BackendDeleteUnsupported):
                        await api.delete_backup(backup_id)
                    self.assertNotIn("delete", [operation for operation, _ in operations])
                    self.assertEqual(await api.list_backups(), [metadata])
            finally:
                await bridge.async_stop()
            self.assertEqual(list((private / "native").iterdir()), [])

    async def test_health_checks_authentication_and_installation_identity(self):
        status = dict(STATUS)

        async def health(request):
            return web.json_response(status)

        async with server(web.get("/v1/status", health)) as url, ClientSession() as session:
            api = client.BackendClient(session, url, TOKEN)
            health_data = await api.status()
            self.assertEqual(health_data.installation_id, STATUS["installation_id"])
            self.assertTrue(health_data.connected)
            status["connected"] = False
            self.assertFalse((await api.status()).connected)
            status["installation_id"] = "not-a-valid-installation-id"
            with self.assertRaises(client.BackendProtocolError):
                await api.status()
            with self.assertRaises(client.BackendInvalidAuth):
                await client.BackendClient(session, url, "x" * 40).status()

    async def test_redirect_is_not_followed_even_on_same_host(self):
        redirected = False

        async def redirect(request):
            raise web.HTTPTemporaryRedirect(location="/received")

        async def destination(request):
            nonlocal redirected
            redirected = True
            return web.json_response(STATUS)

        async with server(web.get("/v1/status", redirect), web.get("/received", destination)) as url:
            async with ClientSession() as session:
                api = client.BackendClient(session, url, TOKEN)
                with self.assertRaises(client.BackendProtocolError) as caught:
                    await api.status()
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertNotIn(url, str(caught.exception))
                self.assertFalse(redirected)

    async def test_rejects_unsafe_configuration_before_network_access(self):
        async with ClientSession() as session:
            for url in (
                "ftp://localhost:8100", "http://abc:def@localhost:8100",
                "http://@localhost:8100", "http://localhost:8100?", "http://localhost:8100/#",
                "http://localhost:8100/?credential=secret", "http://localhost:8100/#secret",
                "http://localhost:99x", "http://localhost:8100/prefix", "http://",
            ):
                with self.subTest(url=url), self.assertRaises(client.BackendInvalidURL):
                    client.BackendClient(session, url, TOKEN)
            for token in ("short", "*" * 129, "💡" * 40, "a" * 40 + "\r\nHeader: x"):
                with self.subTest(), self.assertRaises(client.BackendInvalidAuth):
                    client.BackendClient(session, "http://localhost:8100", token)

    async def test_inventory_and_metadata_reject_corrupt_data(self):
        metadata = dict(BACKUP)

        async def inventory(request):
            return web.json_response({"backups": [metadata]})

        async def details(request):
            return web.json_response(metadata)

        async with server(web.get("/v1/backups", inventory), web.get(f"{BACKUP_PATH}/metadata", details)) as url:
            async with ClientSession() as session:
                api = client.BackendClient(session, url, TOKEN)
                self.assertEqual(await api.list_backups(), [BACKUP])
                self.assertEqual(await api.get_backup(BACKUP["backup_id"]), BACKUP)
                with self.assertRaises(client.BackendProtocolError):
                    await api.get_backup("../status")
                metadata["backup_id"] = "other-id"
                with self.assertRaises(client.BackendProtocolError):
                    await api.get_backup(BACKUP["backup_id"])
                metadata["size"] = True
                with self.assertRaises(client.BackendProtocolError):
                    await api.list_backups()

    async def test_metadata_limit_counts_utf8_not_http_json_escaping(self):
        metadata = {**BACKUP, "name": "ü" * 9_000}

        async def details(request):
            return web.json_response(metadata)

        async with server(web.get(f"{BACKUP_PATH}/metadata", details)) as url, ClientSession() as session:
            api = client.BackendClient(session, url, TOKEN)
            self.assertEqual(await api.get_backup(BACKUP["backup_id"]), metadata)
            metadata["name"] *= 2
            with self.assertRaises(client.BackendProtocolError):
                await api.get_backup(BACKUP["backup_id"])

    async def test_error_statuses_do_not_leak_response_bodies(self):
        status = 404
        code = "backup_not_found"

        async def failure(request):
            return web.json_response({"error": code, "details": "private debug message"}, status=status)

        async with server(web.get(f"{BACKUP_PATH}/metadata", failure), web.delete(BACKUP_PATH, failure)) as url:
            async with ClientSession() as session:
                api = client.BackendClient(session, url, TOKEN)
                with self.assertRaises(client.BackupMissing):
                    await api.get_backup(BACKUP["backup_id"])
                code = "invalid_request"
                with self.assertRaises(client.BackendProtocolError):
                    await api.get_backup(BACKUP["backup_id"])
                status, code = 501, "delete_not_supported"
                with self.assertRaises(client.BackendDeleteUnsupported):
                    await api.delete_backup(BACKUP["backup_id"])
                status, code = 503, "busy"
                with self.assertRaises(client.BackendUnavailable):
                    await api.get_backup(BACKUP["backup_id"])
                status, code = 502, "authentication_required"
                with self.assertRaises(client.BackendAuthenticationRequired):
                    await api.get_backup(BACKUP["backup_id"])
                status, code = 502, "storage_failed"
                with self.assertRaises(client.BackendError) as caught:
                    await api.get_backup(BACKUP["backup_id"])
                self.assertNotIn("private debug message", str(caught.exception))
                self.assertNotIn(TOKEN, str(caught.exception))

    async def test_upload_waits_for_remote_confirmation_after_sending_bytes(self):
        backend_has_bytes = asyncio.Event()
        confirmation = asyncio.Event()
        source_closed = asyncio.Event()
        received = {}

        async def upload(request):
            received["metadata"] = json.loads(base64.b64decode(request.headers["X-Backup-Metadata"]))
            received["body"] = await request.read()
            backend_has_bytes.set()
            await confirmation.wait()
            return web.json_response(received["metadata"])

        async def chunks():
            try:
                yield b"abc"
                yield b"defgh"
            finally:
                source_closed.set()

        async def open_stream():
            return chunks()

        async with server(web.put(BACKUP_PATH, upload)) as url, ClientSession() as session:
            api = client.BackendClient(session, url, TOKEN)
            progress = []
            sending = asyncio.create_task(api.upload_backup(BACKUP, open_stream, progress.append))
            try:
                await asyncio.wait_for(backend_has_bytes.wait(), timeout=5)
                self.assertEqual(received, {"metadata": BACKUP, "body": b"abcdefgh"})
                self.assertEqual(progress, [3, 8])
                self.assertFalse(sending.done())
            finally:
                confirmation.set()
                await asyncio.wait_for(sending, timeout=5)
            self.assertTrue(source_closed.is_set())

    async def test_upload_rejects_truncated_or_excess_streams(self):
        async def upload(request):
            try:
                await request.read()
            except ConnectionError:
                return web.json_response({"error": "storage_failed"}, status=502)
            return web.json_response(BACKUP)

        async with server(web.put(BACKUP_PATH, upload)) as url, ClientSession() as session:
            api = client.BackendClient(session, url, TOKEN)
            for data in (b"small", b"excessive"):
                with self.subTest(data=data):
                    async def chunks():
                        yield data

                    async def open_stream():
                        return chunks()

                    with self.assertRaises(client.BackendError):
                        await api.upload_backup(BACKUP, open_stream, lambda amount: None)

    async def test_upload_rejects_a_mismatched_backend_confirmation(self):
        async def upload(request):
            await request.read()
            return web.json_response({**BACKUP, "protected": False})

        async def chunks():
            yield b"abcdefgh"

        async def open_stream():
            return chunks()

        async with server(web.put(BACKUP_PATH, upload)) as url, ClientSession() as session:
            api = client.BackendClient(session, url, TOKEN)
            with self.assertRaises(client.BackendProtocolError):
                await api.upload_backup(BACKUP, open_stream, lambda amount: None)

    async def test_cancelled_upload_closes_source_and_HTTP_connection(self):
        server_has_first_chunk = asyncio.Event()
        source_closed = asyncio.Event()
        request_closed = asyncio.Event()

        async def upload(request):
            try:
                await request.content.readexactly(3)
                server_has_first_chunk.set()
                await asyncio.Future()
            finally:
                request_closed.set()

        async def chunks():
            try:
                yield b"abc"
                await asyncio.Future()
            finally:
                source_closed.set()

        async def open_stream():
            return chunks()

        async with server(web.put(BACKUP_PATH, upload)) as url, ClientSession() as session:
            api = client.BackendClient(session, url, TOKEN)
            sending = asyncio.create_task(api.upload_backup(BACKUP, open_stream, lambda sent: None))
            try:
                await asyncio.wait_for(server_has_first_chunk.wait(), timeout=5)
            finally:
                sending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(sending, timeout=5)
            await asyncio.wait_for(source_closed.wait(), timeout=5)
            await asyncio.wait_for(request_closed.wait(), timeout=5)

    async def test_upload_cannot_succeed_if_app_responds_before_body_is_complete(self):
        source_closed = asyncio.Event()

        async def upload(request):
            await request.content.readexactly(3)
            return web.json_response(BACKUP)

        async def chunks():
            try:
                yield b"abc"
                await asyncio.Future()
            finally:
                source_closed.set()

        async def open_stream():
            return chunks()

        async with server(web.put(BACKUP_PATH, upload)) as url, ClientSession() as session:
            api = client.BackendClient(session, url, TOKEN)
            with self.assertRaises(client.BackendProtocolError):
                await asyncio.wait_for(api.upload_backup(BACKUP, open_stream, lambda sent: None), timeout=5)
            self.assertTrue(source_closed.is_set())

    async def test_download_streams_bytes_and_rejects_short_read(self):
        complete = True

        async def download(request):
            response = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
            response.content_length = 8
            await response.prepare(request)
            await response.write(b"abc")
            if complete:
                await response.write(b"defgh")
            else:
                response.force_close()
            return response

        async with server(web.get(f"{BACKUP_PATH}/download", download)) as url, ClientSession() as session:
            api = client.BackendClient(session, url, TOKEN)
            stream = await api.download_backup(BACKUP["backup_id"])
            self.assertEqual(b"".join([chunk async for chunk in stream]), b"abcdefgh")
            complete = False
            stream = await api.download_backup(BACKUP["backup_id"])
            with self.assertRaises(client.BackendError):
                async for _ in stream:
                    pass

    async def test_download_close_before_consuming_releases_the_connection(self):
        first_byte_sent = asyncio.Event()
        released = asyncio.Event()
        never = asyncio.Event()

        async def download(request):
            response = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
            response.content_length = 8
            await response.prepare(request)
            try:
                await response.write(b"a")
                first_byte_sent.set()
                await never.wait()
            finally:
                released.set()
            return response

        async with server(web.get(f"{BACKUP_PATH}/download", download)) as url, ClientSession() as session:
            api = client.BackendClient(session, url, TOKEN)
            stream = await api.download_backup(BACKUP["backup_id"])
            await asyncio.wait_for(first_byte_sent.wait(), timeout=5)
            await stream.aclose()
            await asyncio.wait_for(released.wait(), timeout=5)
