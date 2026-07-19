from __future__ import annotations

import tempfile
import unittest
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from boho_analytics_platform.models import (
    CapabilitySnapshot,
    Completeness,
    MetricPoint,
    QueryWindow,
    TimeGrain,
)
from boho_analytics_platform.storage import SCHEMA_VERSION, LockBusy, SQLiteMetricStore


def point(value="4"):
    start = datetime(2026, 7, 1, tzinfo=UTC)
    return MetricPoint("client", "site", "fixture", "test.views", "count", start,
        start + timedelta(days=1), TimeGrain.DAY, Decimal(value), (), Completeness.FINAL, datetime.now(UTC))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.store = SQLiteMetricStore(Path(self.temporary.name) / "state.db"); self.store.initialize()

    def test_initialize_enables_wal_and_integrity(self):
        with self.store.connect(readonly=True) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("SELECT version FROM schema_meta").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT version FROM schema_meta").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(self.store.integrity_check(), "ok")

    def test_sync_run_records_binding_window_and_result_metadata(self):
        window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 3, tzinfo=UTC), "UTC")
        run_id = self.store.start_run(
            "connection", "site", binding_key="site:connection:website:demo",
            source="fixture", window=window,
        )
        data_through = datetime(2026, 7, 2, tzinfo=UTC)
        self.store.finish_run(run_id, "success", points=1, result_kind="data", data_through=data_through)
        with self.store.connect(readonly=True) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(sync_runs)")}
            row = db.execute(
                "SELECT binding_key,source,window_start,window_end,result_kind,data_through FROM sync_runs WHERE id=?",
                (run_id,),
            ).fetchone()
        self.assertTrue({"binding_key", "source", "window_start", "window_end", "result_kind", "data_through"}.issubset(columns))
        self.assertEqual(tuple(row), (
            "site:connection:website:demo", "fixture", window.start.isoformat(),
            window.end.isoformat(), "data", data_through.isoformat(),
        ))

    def test_query_sync_coverage_returns_only_successful_current_bindings(self):
        window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 4, tzinfo=UTC), "UTC")
        key = "site:connection:website:demo"
        good = self.store.start_run(
            "connection", "site", binding_key=key, source="fixture", window=window,
        )
        self.store.finish_run(good, "success", result_kind="empty")
        failed = self.store.start_run(
            "connection", "site", binding_key=key, source="fixture", window=window,
        )
        self.store.finish_run(failed, "failed", result_kind="failed")

        rows = self.store.query_sync_coverage(
            site_ids=["site"], sources=["fixture"], binding_keys=[key], window=window,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site_id"], "site")
        self.assertEqual(rows[0]["source"], "fixture")
        self.assertEqual(rows[0]["window_start"], window.start)
        self.assertEqual(rows[0]["window_end"], window.end)
        self.assertEqual(rows[0]["result_kind"], "empty")

    def test_latest_sync_status_is_bounded_to_current_bindings_and_safe_fields(self):
        window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 3, tzinfo=UTC),
            "UTC",
        )
        current_key = "site:connection:website:current-resource"
        stale_key = "site:connection:website:removed-resource"
        older = self.store.start_run(
            "connection", "site", binding_key=current_key,
            source="umami", window=window,
        )
        self.store.finish_run(older, "success", points=8, result_kind="data")
        latest = self.store.start_run(
            "connection", "site", binding_key=current_key,
            source="umami", window=window,
        )
        self.store.finish_run(
            latest, "failed", category="provider_http",
            message="secret-bearing provider response must not escape",
            result_kind="failed",
        )
        removed = self.store.start_run(
            "connection", "site", binding_key=stale_key,
            source="umami", window=window,
        )
        self.store.finish_run(removed, "success", points=99, result_kind="data")
        with self.store.connect() as db:
            db.execute(
                "UPDATE sync_runs SET started_at=? WHERE id=?",
                ("2026-07-01T00:00:00+00:00", older),
            )
            db.execute(
                "UPDATE sync_runs SET started_at=? WHERE id=?",
                ("2026-07-02T00:00:00+00:00", latest),
            )

        rows = self.store.query_latest_sync_status(binding_keys=[current_key])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["binding_index"], 0)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[0]["error_category"], "provider_http")
        self.assertEqual(rows[0]["site_id"], "site")
        self.assertNotIn("binding_key", rows[0])
        self.assertNotIn("error_message", rows[0])
        self.assertNotIn("resource", repr(rows[0]))

    def test_capability_summary_omits_resource_identifiers(self):
        self.store.save_capability(CapabilitySnapshot(
            "connection", "cloudflare", datetime(2026, 7, 2, tzinfo=UTC),
            True, ("private-zone-id",), ("traffic",), 8,
            ("Resource private-zone-id is plan-limited.",),
        ))

        rows = self.store.query_capability_summaries(connection_ids=["connection"])

        self.assertEqual(rows, [{
            "connection_id": "connection",
            "provider": "cloudflare",
            "probed_at": "2026-07-02T00:00:00+00:00",
            "metric_groups": ["traffic"],
            "max_lookback_days": 8,
            "warnings": ["Resource [resource] is plan-limited."],
        }])
        self.assertNotIn("authentication_ok", rows[0])
        self.assertNotIn("private-zone-id", repr(rows))

    def test_upsert_is_idempotent_and_updates_value(self):
        self.store.upsert([point("4")]); self.store.upsert([point("7")])
        window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 2, tzinfo=UTC), "UTC")
        rows = self.store.query(client_id="client", site_ids=["site"], metric_ids=["test.views"], window=window)
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0].value, Decimal("7"))

    def test_forms_identity_cutover_preserves_but_hides_prior_untrusted_facts(self):
        path = Path(self.temporary.name) / "legacy-forms.db"
        initial = files("boho_analytics_platform.migrations").joinpath("001_initial.sql").read_text(encoding="utf-8")
        graph = files("boho_analytics_platform.migrations").joinpath("002_site_graph.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(path)) as db:
            db.executescript(initial)
            db.executescript(graph)
            db.execute("INSERT INTO schema_meta(version) VALUES (2)")
            db.execute(
                "INSERT INTO metric_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("legacy", "client", "site", "cloudflare-forms", "forms.sent", "count",
                 "2026-07-02T05:00:00+00:00", "2026-07-03T05:00:00+00:00", "day", "1",
                 '{"form_id":"contact"}', "final", "2026-07-02T12:00:00+00:00",
                 "2026-07-02T12:00:00+00:00"),
            )
            db.commit()
        store = SQLiteMetricStore(path)
        store.initialize()
        with store.connect() as db:
            db.execute("""INSERT INTO metric_facts(
              point_key,client_id,site_id,source,metric,unit,start_at,end_at,grain,value,
              dimensions_json,completeness,observed_at,updated_at,identity_version
            ) SELECT ?,client_id,site_id,source,metric,unit,start_at,end_at,grain,?,
              dimensions_json,completeness,observed_at,updated_at,2
              FROM metric_facts WHERE point_key=?""", ("untrusted-v2", "999", "legacy"))
        corrected_start = datetime(2026, 7, 1, 5, tzinfo=UTC)
        corrected = MetricPoint(
            "client", "site", "cloudflare-forms", "forms.sent", "count",
            corrected_start, corrected_start + timedelta(days=1), TimeGrain.DAY,
            Decimal(1), (("form_id", "contact"),), Completeness.FINAL,
            datetime(2026, 7, 3, tzinfo=UTC),
        )
        store.upsert([corrected])
        window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 4, tzinfo=UTC),
            "UTC",
        )
        visible = store.query(
            client_id="client", site_ids=["site"], metric_ids=["forms.sent"], window=window,
        )
        with store.connect(readonly=True) as db:
            lineage = db.execute(
                "SELECT identity_version,start_at FROM metric_facts WHERE source='cloudflare-forms' ORDER BY identity_version"
            ).fetchall()
        self.assertEqual([(row["identity_version"], row["start_at"]) for row in lineage], [
            (1, "2026-07-02T05:00:00+00:00"),
            (2, "2026-07-02T05:00:00+00:00"),
            (3, "2026-07-01T05:00:00+00:00"),
        ])
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0].start, corrected_start)

    def test_forms_v3_schema_marker_blocks_schema_v3_runtime_rollback(self):
        with patch("boho_analytics_platform.storage.SCHEMA_VERSION", 3):
            with self.assertRaisesRegex(RuntimeError, "database schema 4 is newer than supported 3"):
                SQLiteMetricStore(self.store.path).initialize()

    def test_active_lock_fails_and_stale_lock_is_recovered(self):
        self.store.acquire_lock("sync", "one", 60)
        with self.assertRaises(LockBusy): self.store.acquire_lock("sync", "two", 60)
        with self.store.connect() as db: db.execute("UPDATE sync_locks SET expires_at=?", (datetime(2000, 1, 1, tzinfo=UTC).isoformat(),))
        self.store.acquire_lock("sync", "two", 60)

    def test_backup_and_guarded_restore(self):
        self.store.upsert([point("4")]); backup = Path(self.temporary.name) / "backup.db"; self.store.backup(backup)
        self.store.upsert([point("8")])
        with self.assertRaisesRegex(ValueError, "confirmation"): self.store.restore(backup)
        self.store.restore(backup, confirmed=True)
        window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 2, tzinfo=UTC), "UTC")
        self.assertEqual(self.store.query(client_id="client", site_ids=["site"], metric_ids=["test.views"], window=window)[0].value, Decimal("4"))

    def test_retention_deletes_old_daily_points(self):
        self.store.upsert([point()]); self.assertEqual(self.store.enforce_retention(hourly_days=1, daily_days=1), 1)


if __name__ == "__main__": unittest.main()
