from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from boho_analytics_platform.config import load_config
from boho_analytics_platform.engine import SyncEngine
from boho_analytics_platform.models import QueryWindow
from boho_analytics_platform.storage import SQLiteMetricStore
from support import config_text, write_fixture


class EngineTests(unittest.TestCase):
    def test_binding_failure_is_isolated_and_lock_is_released(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); fixture = root / "fixture.json"; write_fixture(fixture)
            text = config_text(root / "state.db", fixture)
            second_connection = f'''[[connections]]
id = "broken-connection"
provider = "fixture"
credential_ref = "none:test"
[connections.options]
path = "{(root / 'missing.json').as_posix()}"
'''
            text = text.replace("[[bindings]]", second_connection + "[[bindings]]", 1)
            second_binding = '''[[bindings]]
site_id = "example-site"
connection_id = "broken-connection"
resource_type = "website"
resource_id = "broken"
metric_groups = ["traffic"]
'''
            text = text.replace("[[reports]]", second_binding + "[[reports]]", 1)
            path = root / "platform.toml"; path.write_text(text, encoding="utf-8"); config = load_config(path)
            store = SQLiteMetricStore(root / "state.db"); store.initialize()
            window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 2, tzinfo=UTC), "UTC")
            results = SyncEngine(config, store).sync(window)
            self.assertEqual([item.status for item in results], ["success", "failed"])
            self.assertEqual(results[1].error_category, "file-not-found-error")
            with store.connect(readonly=True) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sync_locks").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM metric_facts").fetchone()[0], 3)


if __name__ == "__main__": unittest.main()
