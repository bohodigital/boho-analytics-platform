import os
import unittest
from unittest.mock import patch

from scripts import capture_dashboard_headless


class ScreenshotCaptureTests(unittest.TestCase):
    def test_non_fixture_connection_is_rejected(self) -> None:
        config = """
[[connections]]
provider = "umami"
credential_ref = "env:private-token"
"""

        with self.assertRaisesRegex(RuntimeError, "every demo provider must be fixture"):
            capture_dashboard_headless._assert_fixture_only(config)

    @patch.dict(os.environ, {"VERY_SECRET_API_KEY": "sentinel"})
    def test_child_environment_does_not_receive_provider_secrets(self) -> None:
        environment = capture_dashboard_headless._safe_environment()

        self.assertNotIn("VERY_SECRET_API_KEY", environment)
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(
            environment["PYTHONPATH"],
            str(capture_dashboard_headless.ROOT / "src"),
        )


if __name__ == "__main__":
    unittest.main()
