import os
import unittest
from unittest.mock import patch

from scripts import capture_dashboard_headless


class ScreenshotCaptureTests(unittest.TestCase):
    class _Response:
        status = 200

        def read(self):
            return b'{"ok":true,"version":"0.1.1.dev0","database_schema":3}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Process:
        @staticmethod
        def poll():
            return None

    def test_non_fixture_connection_is_rejected(self) -> None:
        config = """
[[connections]]
provider = "umami"
credential_ref = "env:private-token"
"""

        with self.assertRaisesRegex(RuntimeError, "every demo provider must be fixture"):
            capture_dashboard_headless._assert_fixture_only(config)

    def test_capture_matrix_includes_seeded_site_graph(self) -> None:
        routes = [route for _filename, route in capture_dashboard_headless.CAPTURES]
        self.assertTrue(any(route.startswith("/site-graph?site=fixture-static") for route in routes))
        self.assertIn("key: fixture-static", capture_dashboard_headless.DEMO_SITE_GRAPH_MANIFEST)

    @patch.dict(os.environ, {"VERY_SECRET_API_KEY": "sentinel"})
    def test_child_environment_does_not_receive_provider_secrets(self) -> None:
        environment = capture_dashboard_headless._safe_environment()

        self.assertNotIn("VERY_SECRET_API_KEY", environment)
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(
            environment["PYTHONPATH"],
            str(capture_dashboard_headless.ROOT / "src"),
        )

    @patch("scripts.capture_dashboard_headless.urlopen", return_value=_Response())
    def test_health_wait_accepts_provenance_bearing_json(self, _urlopen) -> None:
        capture_dashboard_headless._wait_for_server(8787, self._Process())


if __name__ == "__main__":
    unittest.main()
