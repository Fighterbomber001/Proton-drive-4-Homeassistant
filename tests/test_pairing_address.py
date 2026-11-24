"""The default must resolve to the separately installed App Store package."""

import hashlib
from pathlib import Path
import runpy
import unittest


class PairingAddressTest(unittest.TestCase):
    def test_fresh_install_targets_the_companion_repository_not_a_local_app(self):
        constants = runpy.run_path(str(Path(__file__).resolve().parents[1]
                                       / "custom_components/proton_drive_backup/const.py"))
        # Supervisor hashes the canonical repository URL in lowercase, then
        # uses hyphens instead of underscores for the internal DNS hostname.
        repository = "https://github.com/Fighterbomber001/Proton-drive-4-Homeassistant-App"
        prefix = hashlib.sha1(repository.lower().encode(), usedforsecurity=False).hexdigest()[:8]
        self.assertEqual(constants["DEFAULT_BACKEND_URL"], f"http://{prefix}-proton-drive-backup:8100")
