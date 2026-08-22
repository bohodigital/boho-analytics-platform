from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from boho_analytics_platform.config import load_config
from boho_analytics_platform.engine import SyncEngine
from boho_analytics_platform.models import (
    AcquisitionBatch,
    AcquisitionSlice,
    Completeness,
    MetricPoint,
    QueryWindow,
    TimeGrain,
)
from boho_analytics_platform.storage import SQLiteMetricStore
from support import config_text, write_fixture


class EngineTests(unittest.TestCase):
    def test_provenance_aware_connector_records_history_and_current_fact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "fixture.json"
            write_fixture(fixture)
            path = root / "platform.toml"
            path.write_text(
                config_text(root / "state.db", fixture), encoding="utf-8"
            )
            config = load_config(path)
            store = SQLiteMetricStore(root / "state.db")
            store.initialize()
            window = QueryWindow(
                datetime(2026, 7, 1, tzinfo=UTC),
                datetime(2026, 7, 2, tzinfo=UTC),
                "UTC",
            )
            point = MetricPoint(
                "example-client", "example-site", "fixture",
                "umami.pageviews", "count", window.start, window.end,
                TimeGrain.DAY, Decimal(12), (), Completeness.FINAL,
                datetime(2026, 7, 3, tzinfo=UTC),
            )
            batch = AcquisitionBatch(
                AcquisitionSlice(
                    "fixture.daily", "fixture.traffic", window.start, window.end,
                    Completeness.FINAL, "fixture", "fixture", (), "fixture",
                    1, 1, 1, 0, "fixture-complete",
                ),
                (point,),
            )

            class ProvenanceConnector:
                def collect_batches(self, connection, credential, request):
                    return (batch,)

            with patch(
                "boho_analytics_platform.engine.build_connector",
                return_value=ProvenanceConnector(),
            ):
                result = SyncEngine(config, store).sync(window)[0]

            self.assertEqual((result.status, result.points), ("success", 1))
            with store.connect(readonly=True) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM acquisition_slices").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM metric_fact_observations"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    db.execute("SELECT value FROM metric_facts").fetchone()[0],
                    "12",
                )

    def test_failed_later_request_keeps_completed_attempts_without_publishing_facts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "fixture.json"
            write_fixture(fixture)
            path = root / "platform.toml"
            path.write_text(
                config_text(root / "state.db", fixture), encoding="utf-8"
            )
            config = load_config(path)
            store = SQLiteMetricStore(root / "state.db")
            store.initialize()
            window = QueryWindow(
                datetime(2026, 7, 1, tzinfo=UTC),
                datetime(2026, 7, 2, tzinfo=UTC),
                "UTC",
            )
            point = MetricPoint(
                "example-client", "example-site", "fixture",
                "umami.pageviews", "count", window.start, window.end,
                TimeGrain.DAY, Decimal(12), (), Completeness.FINAL,
                datetime(2026, 7, 3, tzinfo=UTC),
            )
            batch = AcquisitionBatch(
                AcquisitionSlice(
                    "fixture.first", "fixture-traffic", window.start, window.end,
                    Completeness.FINAL, "fixture", "fixture", (), "fixture",
                    1, 1, 1, 0, "fixture-complete",
                ),
                (point,),
            )

            class FailingConnector:
                def collect_batches(self, connection, credential, request):
                    yield batch
                    raise ValueError("private provider detail must not enter ledger")

            with patch(
                "boho_analytics_platform.engine.build_connector",
                return_value=FailingConnector(),
            ):
                result = SyncEngine(config, store).sync(window)[0]

            self.assertEqual((result.status, result.error_category), (
                "failed", "value-error",
            ))
            with store.connect(readonly=True) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM acquisition_slices").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM metric_fact_observations"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM metric_facts").fetchone()[0],
                    0,
                )
                run = db.execute(
                    "SELECT status,error_category,error_message FROM sync_runs"
                ).fetchone()
            self.assertEqual(tuple(run), ("failed", "value-error", "ValueError"))

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

    def test_successful_empty_sync_records_authoritative_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); fixture = root / "fixture.json"
            fixture.write_text('{"points":[]}', encoding="utf-8")
            path = root / "platform.toml"
            path.write_text(config_text(root / "state.db", fixture), encoding="utf-8")
            config = load_config(path); store = SQLiteMetricStore(root / "state.db"); store.initialize()
            window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 3, tzinfo=UTC), "UTC")

            result = SyncEngine(config, store).sync(window)[0]

            self.assertEqual(result.status, "success")
            self.assertIsNone(result.error_category)
            with store.connect(readonly=True) as db:
                self.assertEqual(
                    db.execute("SELECT completed_through FROM watermarks").fetchone()[0],
                    window.end.isoformat(),
                )
                run = db.execute(
                    "SELECT status,result_kind,window_start,window_end,data_through FROM sync_runs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
            self.assertEqual(tuple(run), (
                "success", "empty", window.start.isoformat(), window.end.isoformat(), None,
            ))

    def test_sync_projects_requested_dates_into_each_site_timezone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "fixture.json"
            fixture.write_text(
                '{"points":['
                '{"resource_id":"demo","date":"2026-07-01",'
                '"metric":"umami.pageviews","value":1},'
                '{"resource_id":"tokyo","date":"2026-07-01",'
                '"metric":"umami.pageviews","value":2}]}',
                encoding="utf-8",
            )
            text = config_text(root / "state.db", fixture)
            second_site = '''[[sites]]
id = "tokyo-site"
client_id = "example-client"
name = "Tokyo Site"
canonical_url = "https://tokyo.example.com"
timezone = "Asia/Tokyo"
'''
            second_binding = '''[[bindings]]
site_id = "tokyo-site"
connection_id = "example-connection"
resource_type = "website"
resource_id = "tokyo"
metric_groups = ["traffic"]
'''
            text = text.replace(
                "[[connections]]", second_site + "[[connections]]", 1
            )
            text = text.replace(
                "[[reports]]", second_binding + "[[reports]]", 1
            )
            path = root / "platform.toml"
            path.write_text(text, encoding="utf-8")
            config = load_config(path)
            store = SQLiteMetricStore(root / "state.db")
            store.initialize()
            window = QueryWindow(
                datetime(2026, 7, 1, tzinfo=UTC),
                datetime(2026, 7, 2, tzinfo=UTC),
                "UTC",
            )

            results = SyncEngine(config, store).sync(window)

            self.assertEqual(
                [result.status for result in results], ["success", "success"]
            )
            with store.connect(readonly=True) as db:
                runs = db.execute(
                    "SELECT site_id,window_start,window_end "
                    "FROM sync_runs ORDER BY site_id"
                ).fetchall()
            self.assertEqual(
                tuple(runs[0]),
                (
                    "example-site",
                    "2026-07-01T00:00:00+00:00",
                    "2026-07-02T00:00:00+00:00",
                ),
            )
            self.assertEqual(
                tuple(runs[1]),
                (
                    "tokyo-site",
                    "2026-06-30T15:00:00+00:00",
                    "2026-07-01T15:00:00+00:00",
                ),
            )

    def test_unknown_or_unbound_connection_selection_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); fixture = root / "fixture.json"; write_fixture(fixture)
            text = config_text(root / "state.db", fixture)
            text = text.replace(
                "[[bindings]]",
                '''[[connections]]
id = "unused-connection"
provider = "fixture"
credential_ref = "none:test"
[connections.options]
path = "unused.json"
[[bindings]]''',
                1,
            )
            path = root / "platform.toml"; path.write_text(text, encoding="utf-8")
            config = load_config(path); store = SQLiteMetricStore(root / "state.db"); store.initialize()
            engine = SyncEngine(config, store)
            window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 2, tzinfo=UTC), "UTC")

            with self.assertRaisesRegex(ValueError, "unknown connection"):
                engine.sync(window, {"typo"})
            with self.assertRaisesRegex(ValueError, "no configured bindings"):
                engine.sync(window, {"unused-connection"})
            with self.assertRaisesRegex(ValueError, "unknown connection"):
                engine.probe({"typo"})

    def test_site_selection_is_validated_and_restricts_bindings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "fixture.json"
            fixture.write_text(
                '{"points":['
                '{"resource_id":"demo","date":"2026-07-01",'
                '"metric":"umami.pageviews","value":1},'
                '{"resource_id":"second","date":"2026-07-01",'
                '"metric":"umami.pageviews","value":2}]}',
                encoding="utf-8",
            )
            text = config_text(root / "state.db", fixture)
            text = text.replace(
                "[[connections]]",
                '''[[sites]]
id = "second-site"
client_id = "example-client"
name = "Second Site"
canonical_url = "https://second.example.com"
timezone = "UTC"
[[connections]]''',
                1,
            )
            text = text.replace(
                "[[reports]]",
                '''[[bindings]]
site_id = "second-site"
connection_id = "example-connection"
resource_type = "website"
resource_id = "second"
metric_groups = ["traffic"]
[[reports]]''',
                1,
            )
            path = root / "platform.toml"
            path.write_text(text, encoding="utf-8")
            config = load_config(path)
            store = SQLiteMetricStore(root / "state.db")
            store.initialize()
            engine = SyncEngine(config, store)
            window = QueryWindow(
                datetime(2026, 7, 1, tzinfo=UTC),
                datetime(2026, 7, 2, tzinfo=UTC),
                "UTC",
            )

            result = engine.sync(window, site_ids={"second-site"})

            self.assertEqual(
                [(item.site_id, item.status, item.points) for item in result],
                [("second-site", "success", 1)],
            )
            with self.assertRaisesRegex(ValueError, "unknown site"):
                engine.sync(window, site_ids={"typo"})

    def test_combined_site_and_connection_selection_rejects_unbound_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "fixture.json"
            write_fixture(fixture)
            text = config_text(root / "state.db", fixture)
            text = text.replace(
                "[[bindings]]",
                '''[[connections]]
id = "unused-connection"
provider = "fixture"
credential_ref = "none:test"
[connections.options]
path = "unused.json"
[[bindings]]''',
                1,
            )
            path = root / "platform.toml"
            path.write_text(text, encoding="utf-8")
            config = load_config(path)
            store = SQLiteMetricStore(root / "state.db")
            store.initialize()
            engine = SyncEngine(config, store)
            window = QueryWindow(
                datetime(2026, 7, 1, tzinfo=UTC),
                datetime(2026, 7, 2, tzinfo=UTC),
                "UTC",
            )

            with self.assertRaisesRegex(ValueError, "no configured bindings"):
                engine.sync(
                    window,
                    connection_ids={"unused-connection"},
                    site_ids={"example-site"},
                )

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
