from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path

from boho_analytics_platform import __version__
from boho_analytics_platform.cli import main


class CliTests(unittest.TestCase):
    def test_version(self) -> None:
        output = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(output):
            main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn(__version__, output.getvalue())

    def test_example_config_validates(self) -> None:
        root = Path(__file__).resolve().parents[1]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["config", "validate", str(root / "examples/platform.example.toml")])
        self.assertEqual(status, 0)
        self.assertIn('"ok": true', output.getvalue())


if __name__ == "__main__":
    unittest.main()
