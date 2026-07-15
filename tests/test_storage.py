from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from boho_analytics_platform.models import Completeness, MetricPoint, QueryWindow, TimeGrain
from boho_analytics_platform.storage import LockBusy, SQLiteMetricStore


def point(value="4"):
    start = datetime(2026, 7, 1, tzinfo=UTC)
    return MetricPoint("client", "site", "fixture", "test.views", "count", start,
        start + timedelta(days=1), TimeGrain.DAY, Decimal(value), (), Completeness.FINAL, datetime.now(UTC))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.store = SQLiteMetricStore(Path(self.temporary.name) / "state.db"); self.store.initialize()

    def test_initialize_enables_wal_and_integrity(self):
        with self.store.connect(readonly=True) as db: self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.store.integrity_check(), "ok")

    def test_upsert_is_idempotent_and_updates_value(self):
        self.store.upsert([point("4")]); self.store.upsert([point("7")])
        window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 2, tzinfo=UTC), "UTC")
        rows = self.store.query(client_id="client", site_ids=["site"], metric_ids=["test.views"], window=window)
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0].value, Decimal("7"))

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
