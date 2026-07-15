from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from boho_analytics_platform.config import ConfigError, load_config
from support import config_text, write_fixture


class ConfigTests(unittest.TestCase):
    def _load(self, mutate=lambda value: value):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name); fixture = root / "fixture.json"; write_fixture(fixture)
        path = root / "platform.toml"; path.write_text(mutate(config_text(root / "state.db", fixture)), encoding="utf-8")
        return load_config(path)

    def test_valid_schema_v2_configuration(self):
        config = self._load(); self.assertEqual(config.schema_version, 2); self.assertEqual(config.reports[0].subreports[0].id, "forms")

    def test_inline_secret_is_rejected_anywhere(self):
        with self.assertRaisesRegex(ConfigError, "inline secret field"):
            self._load(lambda text: text.replace('path = "', 'api_key = "bad"\npath = "'))

    def test_unknown_field_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "unknown field"):
            self._load(lambda text: text.replace("default_sync_days = 30", "default_sync_days = 30\ntyop = true"))

    def test_unknown_client_reference_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "unknown client"):
            self._load(lambda text: text.replace('client_id = "example-client"', 'client_id = "missing"', 1))

    def test_unauthenticated_non_loopback_server_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "loopback"):
            self._load(lambda text: text.replace('bind_host = "127.0.0.1"', 'bind_host = "0.0.0.0"'))

    def test_basic_auth_requires_credential_reference(self):
        with self.assertRaisesRegex(ConfigError, "requires username"):
            self._load(lambda text: text.replace('auth_mode = "none"', 'auth_mode = "basic"'))


if __name__ == "__main__": unittest.main()
