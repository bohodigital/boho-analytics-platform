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

    def test_empty_sync_is_warning_and_does_not_advance_watermark(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); fixture = root / "fixture.json"
            fixture.write_text('{"points":[]}', encoding="utf-8")
            path = root / "platform.toml"
            path.write_text(config_text(root / "state.db", fixture), encoding="utf-8")
            config = load_config(path); store = SQLiteMetricStore(root / "state.db"); store.initialize()
            window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 3, tzinfo=UTC), "UTC")

            result = SyncEngine(config, store).sync(window)[0]

            self.assertEqual(result.status, "warning")
            self.assertEqual(result.error_category, "empty-result")
            with store.connect(readonly=True) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM watermarks").fetchone()[0], 0)
                run = db.execute(
                    "SELECT status,result_kind,window_start,window_end,data_through FROM sync_runs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
            self.assertEqual(tuple(run), (
                "warning", "empty", window.start.isoformat(), window.end.isoformat(), None,
            ))

    def test_nonempty_sync_watermark_tracks_data_through_not_requested_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); fixture = root / "fixture.json"; write_fixture(fixture)
            path = root / "platform.toml"
            path.write_text(config_text(root / "state.db", fixture), encoding="utf-8")
            config = load_config(path); store = SQLiteMetricStore(root / "state.db"); store.initialize()
            window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 3, tzinfo=UTC), "UTC")

            result = SyncEngine(config, store).sync(window)[0]

            self.assertEqual(result.status, "success")
            with store.connect(readonly=True) as db:
                watermark = db.execute("SELECT completed_through FROM watermarks").fetchone()[0]
                run = db.execute("SELECT result_kind,data_through FROM sync_runs ORDER BY started_at DESC LIMIT 1").fetchone()
            self.assertEqual(watermark, "2026-07-02T00:00:00+00:00")
            self.assertEqual(tuple(run), ("data", "2026-07-02T00:00:00+00:00"))


if __name__ == "__main__": unittest.main()
