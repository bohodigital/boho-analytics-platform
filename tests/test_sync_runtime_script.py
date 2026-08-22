from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class SyncRuntimeScriptTests(unittest.TestCase):
    def test_site_failure_does_not_starve_later_sites(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "sync_runtime_by_site.sh"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "calls.log"
            timeout_log = root / "timeouts.log"
            config = root / "platform.toml"
            config.write_text("schema_version = 2\n", encoding="utf-8")
            fake_cli = root / "fake-cli"
            fake_cli.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$SYNC_TEST_LOG\"\n"
                "case \" $* \" in\n"
                "  *' --site broken-site '*) exit 7 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_timeout = root / "fake-timeout"
            fake_timeout.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$SYNC_TIMEOUT_TEST_LOG\"\n"
                "shift 3\n"
                "exec \"$@\"\n",
                encoding="utf-8",
            )
            for path in (fake_cli, fake_timeout):
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
            env = {
                **os.environ,
                "BOHO_ANALYTICS_CLI": str(fake_cli),
                "BOHO_ANALYTICS_CONFIG": str(config),
                "BOHO_ANALYTICS_SYNC_SITES": (
                    "first-site broken-site final-site"
                ),
                "BOHO_ANALYTICS_SYNC_CONNECTIONS": "gsc umami",
                "BOHO_ANALYTICS_TIMEOUT_COMMAND": str(fake_timeout),
                "SYNC_TEST_LOG": str(log),
                "SYNC_TIMEOUT_TEST_LOG": str(timeout_log),
            }

            result = subprocess.run(
                ["/bin/sh", str(script)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(calls), 3)
            self.assertIn("--site first-site", calls[0])
            self.assertIn("--site broken-site", calls[1])
            self.assertIn("--site final-site", calls[2])
            self.assertTrue(all("--connection gsc" in item for item in calls))
            self.assertTrue(all("--connection umami" in item for item in calls))
            timeout_calls = timeout_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(timeout_calls), 3)
            self.assertTrue(all("3600s" in item for item in timeout_calls))
            self.assertIn("attempted=3 failed=1", result.stderr)

    def test_invalid_identifier_fails_before_invoking_cli(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "sync_runtime_by_site.sh"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "platform.toml"
            config.write_text("schema_version = 2\n", encoding="utf-8")
            env = {
                **os.environ,
                "BOHO_ANALYTICS_CLI": "/does/not/run",
                "BOHO_ANALYTICS_CONFIG": str(config),
                "BOHO_ANALYTICS_SYNC_SITES": "valid-site bad/site",
                "BOHO_ANALYTICS_SYNC_CONNECTIONS": "gsc",
            }

            result = subprocess.run(
                ["/bin/sh", str(script)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("invalid site id", result.stderr)


if __name__ == "__main__":
    unittest.main()
