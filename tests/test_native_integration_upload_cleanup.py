"""Regression tests for stream ownership across an early backend error."""

import asyncio
import unittest

from aiohttp import ClientSession, web

from tests.test_native_integration_client import BACKUP, BACKUP_PATH, TOKEN, client, server


class NativeIntegrationUploadCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_early_rejection_waits_for_asynchronous_source_cleanup(self):
        source_cleanup_started = asyncio.Event()
        source_closed = asyncio.Event()

        async def reject(request):
            await request.content.readexactly(8)
            await source_cleanup_started.wait()
            return web.json_response({"error": "invalid_auth"}, status=401)

        class BackupSource:
            def __init__(self):
                self._done = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._done:
                    raise StopAsyncIteration
                self._done = True
                return b"abcdefgh"

            async def aclose(self):
                source_cleanup_started.set()
                await asyncio.sleep(0.2)
                source_closed.set()

        async def open_stream():
            return BackupSource()

        async with server(web.put(BACKUP_PATH, reject)) as url, ClientSession() as session:
            api = client.BackendClient(session, url, TOKEN)
            with self.assertRaises(client.BackendInvalidAuth):
                await asyncio.wait_for(api.upload_backup(BACKUP, open_stream, lambda _: None), timeout=5)
            self.assertTrue(source_closed.is_set())
