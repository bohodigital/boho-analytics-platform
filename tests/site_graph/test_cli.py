from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from boho_analytics_platform.cli import main
from tests.site_graph.support import VALID_MANIFEST


class SiteGraphCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "manifest.yaml"
        self.path.write_text(VALID_MANIFEST, encoding="utf-8")

    def test_manifest_validation_needs_no_platform_config_and_is_sanitized(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--config", "does-not-exist.toml", "site-graph", "manifest", "validate", "--manifest", str(self.path)])

        payload = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["site_key"], "fixture-static")
        self.assertNotIn(str(self.path.parent), output.getvalue())
        self.assertNotIn("expected_remote", output.getvalue())
        self.assertNotIn("fixture-account", output.getvalue())

    def test_invalid_manifest_returns_nonzero_without_traceback(self):
        self.path.write_text(VALID_MANIFEST + "unknown: true\n", encoding="utf-8")
        errors = io.StringIO()
        with redirect_stderr(errors):
            status = main(["site-graph", "manifest", "validate", "--manifest", str(self.path)])

        payload = json.loads(errors.getvalue())
        self.assertEqual(status, 2)
        self.assertFalse(payload["ok"])
        self.assertIn("unknown field", payload["error"])
        self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
