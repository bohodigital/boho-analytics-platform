from __future__ import annotations

import unittest
from pathlib import Path


class BackupScriptTests(unittest.TestCase):
    def test_retention_is_confined_to_a_dedicated_scheduled_directory(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "backup_runtime.sh").read_text(encoding="utf-8")
        self.assertIn('scheduled_dir="$BOHO_ANALYTICS_BACKUP_DIR/scheduled"', script)
        self.assertIn('destination="$scheduled_dir/analytics-$timestamp.sqlite3"', script)
        self.assertIn('find "$scheduled_dir"', script)
        self.assertNotIn('find "$BOHO_ANALYTICS_BACKUP_DIR"', script)


if __name__ == "__main__":
    unittest.main()
