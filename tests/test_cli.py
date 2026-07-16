from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from boho_analytics_platform import __version__
from boho_analytics_platform.cli import main
from support import config_text, write_fixture


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name); self.fixture = root / "fixture.json"; write_fixture(self.fixture)
        self.config = root / "platform.toml"; self.state = root / "state.db"
        self.config.write_text(config_text(self.state, self.fixture), encoding="utf-8")

    def call(self, *args):
        output = io.StringIO()
        with redirect_stdout(output): status = main(["--config", str(self.config), *args])
        return status, output.getvalue()

    def test_version_is_stable(self): self.assertEqual(__version__, "0.1.0")

    def test_config_init_sync_and_report(self):
        self.assertEqual(self.call("config", "validate")[0], 0)
        self.assertEqual(self.call("db", "init")[0], 0)
        status, output = self.call("sync", "--start", "2026-07-01", "--end", "2026-07-02")
        self.assertEqual(status, 0); self.assertIn('"points": 3', output)
        status, output = self.call("report", "summary", "--start", "2026-07-01", "--end", "2026-07-02")
        self.assertEqual(status, 0); self.assertIn('"forms_pipeline"', output)

    def test_report_csv_output(self):
        self.call("db", "init"); self.call("sync", "--start", "2026-07-01", "--end", "2026-07-02")
        status, output = self.call("report", "summary", "--start", "2026-07-01", "--end", "2026-07-02", "--format", "csv")
        self.assertEqual(status, 0); self.assertTrue(output.startswith("metric,site_id,source"))

    def test_relative_days_window_uses_current_local_date(self):
        status, output = self.call("sync", "--days", "30")
        self.assertEqual(status, 0)
        self.assertIn('"status": "success"', output)


if __name__ == "__main__": unittest.main()
