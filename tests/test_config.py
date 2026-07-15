from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from boho_analytics_platform.config import ConfigError, load_config


VALID_CONFIG = """
schema_version = 1

[platform]
default_timezone = "UTC"
state_path = "./var/test.sqlite3"

[[clients]]
id = "example-client"
name = "Example Client"

[[sites]]
id = "example-site"
client_id = "example-client"
name = "Example Site"
canonical_url = "https://example.com"
timezone = "America/Chicago"

[[connections]]
id = "example-umami"
provider = "umami"
credential_ref = "env:EXAMPLE_REPORTING_CREDENTIAL"

[connections.options]
base_url = "http://127.0.0.1:3000"

[[bindings]]
site_id = "example-site"
connection_id = "example-umami"
resource_type = "website"
resource_id = "example-resource"
"""


class ConfigTests(unittest.TestCase):
    def _load(self, text: str):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "platform.toml"
            path.write_text(textwrap.dedent(text), encoding="utf-8")
            return load_config(path)

    def test_valid_configuration(self) -> None:
        config = self._load(VALID_CONFIG)
        self.assertEqual(config.schema_version, 1)
        self.assertEqual(config.sites[0].timezone, "America/Chicago")
        self.assertEqual(config.connections[0].provider, "umami")

    def test_inline_secret_field_is_rejected(self) -> None:
        text = VALID_CONFIG.replace(
            'credential_ref = "env:EXAMPLE_REPORTING_CREDENTIAL"',
            'credential_ref = "env:EXAMPLE_REPORTING_CREDENTIAL"\napi_key = "not-allowed"',
        )
        with self.assertRaisesRegex(ConfigError, "inline secret field"):
            self._load(text)

    def test_unknown_client_reference_is_rejected(self) -> None:
        text = VALID_CONFIG.replace('client_id = "example-client"', 'client_id = "missing"')
        with self.assertRaisesRegex(ConfigError, "unknown client"):
            self._load(text)

    def test_unknown_field_is_rejected(self) -> None:
        text = VALID_CONFIG.replace('state_path = "./var/test.sqlite3"', 'state_path = "./var/test.sqlite3"\ntyop = true')
        with self.assertRaisesRegex(ConfigError, "unknown field"):
            self._load(text)


if __name__ == "__main__":
    unittest.main()
