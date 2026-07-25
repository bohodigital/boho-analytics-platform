from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from boho_analytics_platform.site_graph.storage import SiteGraphStore
from boho_analytics_platform.storage import MIGRATIONS, SCHEMA_VERSION, _apply_migration


class Core21MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_schema_four_reuses_current_tables_without_replaying_old_v2_migration(self):
        path = self.root / "schema-four.db"
        with closing(sqlite3.connect(path)) as db:
            for version in range(1, 5):
                migration = files("boho_analytics_platform.migrations").joinpath(
                    MIGRATIONS[version]
                ).read_text(encoding="utf-8")
                _apply_migration(db, migration, version)
            db.execute(
                """INSERT INTO metric_facts(
                     point_key,client_id,site_id,source,metric,unit,start_at,end_at,grain,value,
                     dimensions_json,completeness,observed_at,updated_at,identity_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "kept", "client", "site", "fixture", "fixture.pageviews", "count",
                    "2026-07-01T00:00:00+00:00", "2026-07-02T00:00:00+00:00",
                    "day", "1", "{}", "final", "2026-07-02T00:00:00+00:00",
                    "2026-07-02T00:00:00+00:00", 1,
                ),
            )
            db.commit()

        store = SiteGraphStore(path)
        store.initialize()
        store.initialize()
        with store.connect(readonly=True) as db:
            version = db.execute("SELECT version FROM schema_meta").fetchone()[0]
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            point = db.execute("SELECT point_key FROM metric_facts").fetchone()[0]
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertEqual(point, "kept")
        self.assertNotIn("site_graph_evidence_batches", tables)
        self.assertNotIn("site_graph_page_candidates", tables)

    def test_backup_restore_and_old_runtime_boundary_remain_explicit(self):
        source = SiteGraphStore(self.root / "source.db")
        source.initialize()
        backup = source.backup(self.root / "backup.db")
        restored = SiteGraphStore(self.root / "restored.db")
        restored.restore(backup, confirmed=True)
        self.assertEqual(restored.integrity_check(), "ok")
        with patch("boho_analytics_platform.storage.SCHEMA_VERSION", 3):
            with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                restored.initialize()


if __name__ == "__main__":
    unittest.main()
