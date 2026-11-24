"""Optional adapter tests that use real Home Assistant Core 2026.11.2 classes."""

from importlib.util import find_spec
from tempfile import TemporaryDirectory
from types import MappingProxyType
import unittest

from aiohttp import web

from tests.test_native_integration_client import BACKUP, BACKUP_PATH, STATUS, TOKEN, server


@unittest.skipUnless(find_spec("homeassistant"), "Home Assistant Core 2026.11.2 is not installed")
class HomeAssistantNativeAgentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.const import __version__
        from homeassistant.core import HomeAssistant

        if __version__ != "2026.11.2":
            self.skipTest("This test targets Home Assistant Core 2026.11.2")
        config = TemporaryDirectory()
        self.addCleanup(config.cleanup)
        self.hass = HomeAssistant(config.name)
        self.addAsyncCleanup(self.hass.async_stop, force=True)

    def entry(self, url):
        from homeassistant.config_entries import SOURCE_USER, ConfigEntry

        return ConfigEntry(
            domain="proton_drive_backup", title="Proton Drive", version=1, minor_version=1,
            data={"backend_url": url, "integration_token": TOKEN}, options=None,
            discovery_keys=MappingProxyType({}), source=SOURCE_USER, subentries_data=None,
            unique_id=STATUS["installation_id"],
        )

    async def test_only_healthy_matching_entry_registers_and_notifies(self):
        from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

        from custom_components.proton_drive_backup import async_setup_entry, async_unload_entry
        from custom_components.proton_drive_backup.backup import (
            async_get_backup_agents, async_register_backup_agents_listener,
        )

        status = {**STATUS, "connected": False}

        async def health(request):
            return web.json_response(status)

        async with server(web.get("/v1/status", health)) as url:
            changes = []
            remove = async_register_backup_agents_listener(self.hass, listener=lambda: changes.append(1))
            entry = self.entry(url)
            with self.assertRaises(ConfigEntryNotReady):
                await async_setup_entry(self.hass, entry)
            status.update(connected=True, installation_id="b" * 32)
            with self.assertRaises(ConfigEntryError):
                await async_setup_entry(self.hass, entry)
            self.assertEqual(await async_get_backup_agents(self.hass), [])
            self.assertEqual(changes, [])
            status.update(STATUS)
            await async_setup_entry(self.hass, entry)
            [agent] = await async_get_backup_agents(self.hass)
            self.assertEqual(agent.agent_id, f"proton_drive_backup.{STATUS['installation_id']}")
            self.assertEqual(changes, [1])
            await async_unload_entry(self.hass, entry)
            self.assertEqual(await async_get_backup_agents(self.hass), [])
            self.assertEqual(changes, [1, 1])
            remove()

    async def test_agent_model_progress_download_and_unsupported_delete(self):
        from homeassistant.components.backup import AgentBackup, BackupAgentError, BackupNotFound

        from custom_components.proton_drive_backup import async_setup_entry, async_unload_entry
        from custom_components.proton_drive_backup.backup import async_get_backup_agents

        async def health(request):
            return web.json_response(STATUS)

        async def inventory(request):
            return web.json_response({"backups": [BACKUP]})

        async def details(request):
            return web.json_response(BACKUP)

        async def upload(request):
            if await request.read() != b"abcdefgh":
                raise web.HTTPBadRequest()
            return web.json_response(BACKUP)

        async def download(request):
            if request.match_info["backup_id"] != BACKUP["backup_id"]:
                return web.json_response({"error": "backup_not_found"}, status=404)
            return web.Response(body=b"abcdefgh", content_type="application/octet-stream")

        async def delete(request):
            return web.json_response({"error": "delete_not_supported"}, status=501)

        async def chunks():
            yield b"abc"
            yield b"defgh"

        async def open_stream():
            return chunks()

        routes = (
            web.get("/v1/status", health), web.get("/v1/backups", inventory),
            web.get(f"{BACKUP_PATH}/metadata", details), web.put(BACKUP_PATH, upload),
            web.get("/v1/backups/{backup_id}/download", download), web.delete(BACKUP_PATH, delete),
        )
        async with server(*routes) as url:
            entry = self.entry(url)
            await async_setup_entry(self.hass, entry)
            [agent] = await async_get_backup_agents(self.hass)
            [backup] = await agent.async_list_backups()
            self.assertIsInstance(backup, AgentBackup)
            self.assertEqual(backup.as_dict(), BACKUP)
            self.assertEqual(await agent.async_get_backup(backup.backup_id), backup)
            progress = []
            await agent.async_upload_backup(
                open_stream=open_stream, backup=backup,
                on_progress=lambda **value: progress.append(value),
            )
            self.assertEqual(progress, [{"bytes_uploaded": 3}, {"bytes_uploaded": 8}])
            stream = await agent.async_download_backup(backup.backup_id)
            self.assertEqual(b"".join([chunk async for chunk in stream]), b"abcdefgh")
            with self.assertRaises(BackupNotFound):
                await agent.async_download_backup("missing")
            with self.assertRaises(BackupAgentError) as error:
                await agent.async_delete_backup(backup.backup_id)
            self.assertEqual(error.exception.translation_key, "delete_not_supported")
            await async_unload_entry(self.hass, entry)
