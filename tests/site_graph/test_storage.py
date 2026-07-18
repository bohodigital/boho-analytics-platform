from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from importlib.resources import files
from pathlib import Path

from boho_analytics_platform.site_graph.manifest import load_manifest_text
from boho_analytics_platform.site_graph.storage import LinkOccurrence, PageFact, SiteGraphStore
from boho_analytics_platform.storage import SCHEMA_VERSION, _apply_migration
from tests.site_graph.support import VALID_MANIFEST


EXPECTED_TABLES = {
    "site_graph_manifest_versions",
    "site_graph_ingest_runs",
    "site_graph_repository_snapshots",
    "site_graph_page_facts",
    "site_graph_link_occurrences",
    "site_graph_page_entities",
    "site_graph_page_roles",
    "site_graph_edge_aggregates",
    "site_graph_snapshots",
    "site_graph_node_metrics",
    "site_graph_edge_metrics",
    "site_graph_components",
    "site_graph_findings",
}


class SiteGraphStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "analytics.db"
        self.store = SiteGraphStore(self.path)
        self.store.initialize()
        self.manifest = load_manifest_text(VALID_MANIFEST)

    def repository_snapshot(self) -> tuple[str, str]:
        manifest_id = self.store.save_manifest(self.manifest)
        run_id = self.store.start_ingest(
            manifest_version_id=manifest_id,
            site_key=self.manifest.site.key,
            analysis_mode=self.manifest.analysis.mode,
        )
        snapshot_id = self.store.save_repository_snapshot(
            ingest_run_id=run_id,
            site_key=self.manifest.site.key,
            repository_identity="example/fixture-static",
            remote_url="https://github.com/example/fixture-static.git",
            revision="a" * 40,
            ref="main",
            clean=True,
            content_hash="b" * 64,
        )
        return manifest_id, snapshot_id

    def test_migration_creates_complete_fact_first_schema(self):
        with self.store.connect(readonly=True) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version = db.execute("SELECT version FROM schema_meta").fetchone()[0]

        self.assertEqual(version, SCHEMA_VERSION)
        self.assertTrue(EXPECTED_TABLES.issubset(tables))
        self.assertEqual(self.store.integrity_check(), "ok")

    def test_version_one_database_upgrades_without_losing_metrics(self):
        legacy = Path(self.temporary.name) / "legacy.db"
        migration = files("boho_analytics_platform.migrations").joinpath("001_initial.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(legacy)) as db:
            with db:
                db.executescript(migration)
                db.execute("INSERT INTO schema_meta(version) VALUES (1)")
                db.execute(
                    "INSERT INTO watermarks(binding_key,completed_through,updated_at) VALUES (?,?,?)",
                    ("fixture", "2026-07-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
                )
        upgraded = SiteGraphStore(legacy)

        upgraded.initialize()
        upgraded.initialize()

        with upgraded.connect(readonly=True) as db:
            self.assertEqual(db.execute("SELECT version FROM schema_meta").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT binding_key FROM watermarks").fetchone()[0], "fixture")

    def test_distinct_link_occurrences_are_preserved_and_idempotent(self):
        _, snapshot_id = self.repository_snapshot()
        pages = [
            PageFact("home", "/", "https://fixture.example/", "index.html", {"source": "fixture"}, "1" * 64),
            PageFact("contact", "/contact/", "https://fixture.example/contact/", "contact.html", {}, "2" * 64),
        ]
        links = [
            LinkOccurrence("home-contact-menu", "home", "/contact/", "/contact/", "Contact", "", "index.html:8", "navigation", "menu", 1.0, {"selector": "header nav a"}),
            LinkOccurrence("home-contact-body", "home", "/contact/", "/contact/", "Talk to us", "Ready to start", "index.html:30", "main", "action", 0.98, {"selector": "[data-cta]"}),
        ]

        self.store.save_fact_batch(snapshot_id, pages=pages, links=links)
        self.store.save_fact_batch(snapshot_id, pages=pages, links=links)

        with self.store.connect(readonly=True) as db:
            rows = db.execute(
                "SELECT occurrence_key,layer,confidence,evidence_json FROM site_graph_link_occurrences ORDER BY occurrence_key"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["layer"] for row in rows}, {"menu", "action"})
        self.assertTrue(all(json.loads(row["evidence_json"])["selector"] for row in rows))

    def test_invalid_batch_rolls_back_all_page_and_link_facts(self):
        _, snapshot_id = self.repository_snapshot()
        pages = [PageFact("new", "/new/", "https://fixture.example/new/", "new.html", {}, "3" * 64)]
        links = [
            LinkOccurrence("bad", "missing", "/new/", "/new/", "New", "", "new.html:2", "main", "contextual", 0.5, {})
        ]

        with self.assertRaisesRegex(ValueError, "unknown source fact"):
            self.store.save_fact_batch(snapshot_id, pages=pages, links=links)

        with self.store.connect(readonly=True) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM site_graph_page_facts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM site_graph_link_occurrences").fetchone()[0], 0)

    def test_graph_artifacts_join_to_exact_manifest_and_repository_provenance(self):
        manifest_id, snapshot_id = self.repository_snapshot()
        self.store.save_fact_batch(
            snapshot_id,
            pages=[PageFact("about", "/about/", "https://fixture.example/about/", "about.html", {}, "f" * 64)],
            links=[],
        )
        graph_id = self.store.save_graph_snapshot(
            site_key=self.manifest.site.key,
            repository_snapshot_id=snapshot_id,
            manifest_version_id=manifest_id,
            compiler_version="site-graph-v1",
            projection_name="contextual",
            goal_definition_hash="c" * 64,
            content_hash="d" * 64,
        )
        finding_id = self.store.save_finding(
            graph_snapshot_id=graph_id,
            finding_key="dead-end:/about/",
            finding_type="contextual_dead_end",
            severity="warning",
            algorithm="out_degree",
            parameters={"projection": "contextual"},
            affected_nodes=["/about/"],
            affected_edges=[],
            source_fact_keys=["about"],
            content_hash="e" * 64,
        )

        with self.store.connect(readonly=True) as db:
            row = db.execute(
                """SELECT f.id,g.projection_name,g.compiler_version,g.goal_definition_hash,
                          r.revision,m.manifest_hash
                   FROM site_graph_findings f
                   JOIN site_graph_snapshots g ON g.id=f.graph_snapshot_id
                   JOIN site_graph_repository_snapshots r ON r.id=g.repository_snapshot_id
                   JOIN site_graph_manifest_versions m ON m.id=g.manifest_version_id
                   WHERE f.id=?""",
                (finding_id,),
            ).fetchone()
        self.assertEqual(row["projection_name"], "contextual")
        self.assertEqual(row["revision"], "a" * 40)
        self.assertEqual(row["manifest_hash"], self.manifest.manifest_hash)

    def test_finding_rejects_evidence_outside_graph_repository_snapshot(self):
        manifest_id, snapshot_id = self.repository_snapshot()
        graph_id = self.store.save_graph_snapshot(
            site_key=self.manifest.site.key,
            repository_snapshot_id=snapshot_id,
            manifest_version_id=manifest_id,
            compiler_version="site-graph-v1",
            projection_name="contextual",
            goal_definition_hash="c" * 64,
            content_hash="d" * 64,
        )

        with self.assertRaisesRegex(ValueError, "unknown source fact"):
            self.store.save_finding(
                graph_snapshot_id=graph_id,
                finding_key="dead-end:/missing/",
                finding_type="contextual_dead_end",
                severity="warning",
                algorithm="out_degree",
                parameters={"projection": "contextual"},
                affected_nodes=["/missing/"],
                affected_edges=[],
                source_fact_keys=["missing"],
                content_hash="e" * 64,
            )

    def test_repository_snapshot_rejects_credentials_in_runtime_remote(self):
        manifest_id = self.store.save_manifest(self.manifest)
        run_id = self.store.start_ingest(
            manifest_version_id=manifest_id,
            site_key=self.manifest.site.key,
            analysis_mode=self.manifest.analysis.mode,
        )

        with self.assertRaisesRegex(ValueError, "credentials"):
            self.store.save_repository_snapshot(
                ingest_run_id=run_id,
                site_key=self.manifest.site.key,
                repository_identity="example/fixture-static",
                remote_url="https://user:password@github.com/example/fixture-static.git",
                revision="a" * 40,
                ref="main",
                clean=True,
                content_hash="b" * 64,
            )

        snapshot_id = self.store.save_repository_snapshot(
            ingest_run_id=run_id,
            site_key=self.manifest.site.key,
            repository_identity="example/fixture-static",
            remote_url="ssh://git@github.com/example/fixture-static.git",
            revision="a" * 40,
            ref="main",
            clean=True,
            content_hash="b" * 64,
        )
        self.assertTrue(snapshot_id.startswith("sgr_"))

    def test_failed_migration_rolls_back_schema_and_version_together(self):
        broken = Path(self.temporary.name) / "broken-migration.db"
        with closing(sqlite3.connect(broken)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                _apply_migration(
                    db,
                    """CREATE TABLE schema_meta(version INTEGER NOT NULL);
                       CREATE TABLE should_rollback(id TEXT PRIMARY KEY);
                       THIS IS NOT SQL;""",
                    1,
                )
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("schema_meta", tables)
        self.assertNotIn("should_rollback", tables)

    def test_ingest_lifecycle_is_explicit_and_repository_requires_running_run(self):
        manifest_id = self.store.save_manifest(self.manifest)
        run_id = self.store.start_ingest(
            manifest_version_id=manifest_id,
            site_key=self.manifest.site.key,
            analysis_mode=self.manifest.analysis.mode,
        )
        self.store.finish_ingest(run_id, status="failed")

        with self.assertRaisesRegex(ValueError, "running"):
            self.store.save_repository_snapshot(
                ingest_run_id=run_id,
                site_key=self.manifest.site.key,
                repository_identity="example/fixture-static",
                remote_url="https://github.com/example/fixture-static.git",
                revision="a" * 40,
                ref="main",
                clean=True,
                content_hash="b" * 64,
            )
        with self.store.connect(readonly=True) as db:
            row = db.execute("SELECT status,finished_at FROM site_graph_ingest_runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIsNotNone(row["finished_at"])

    def test_runtime_booleans_are_strict_and_evidence_bytes_are_bounded(self):
        manifest_id = self.store.save_manifest(self.manifest)
        run_id = self.store.start_ingest(
            manifest_version_id=manifest_id,
            site_key=self.manifest.site.key,
            analysis_mode=self.manifest.analysis.mode,
        )
        with self.assertRaisesRegex(ValueError, "clean must be true or false"):
            self.store.save_repository_snapshot(
                ingest_run_id=run_id,
                site_key=self.manifest.site.key,
                repository_identity="example/fixture-static",
                remote_url="https://github.com/example/fixture-static.git",
                revision="a" * 40,
                ref="main",
                clean="false",
                content_hash="b" * 64,
            )

        _, snapshot_id = self.repository_snapshot()
        oversized = PageFact(
            "oversized", "/oversized/", "https://fixture.example/oversized/", "oversized.html",
            {"body": "x" * 70_000}, "f" * 64,
        )
        with self.assertRaisesRegex(ValueError, "evidence exceeds"):
            self.store.save_fact_batch(snapshot_id, pages=[oversized], links=[])

    def test_graph_provenance_survives_backup_and_confirmed_restore(self):
        manifest_id, snapshot_id = self.repository_snapshot()
        backup = Path(self.temporary.name) / "site-graph.backup.sqlite3"
        restored_path = Path(self.temporary.name) / "site-graph.restored.sqlite3"

        self.store.backup(backup)
        restored = SiteGraphStore(restored_path)
        restored.restore(backup, confirmed=True)

        with restored.connect(readonly=True) as db:
            row = db.execute(
                """SELECT m.manifest_hash,r.revision
                   FROM site_graph_repository_snapshots r
                   JOIN site_graph_ingest_runs i ON i.id=r.ingest_run_id
                   JOIN site_graph_manifest_versions m ON m.id=i.manifest_version_id
                   WHERE r.id=? AND m.id=?""",
                (snapshot_id, manifest_id),
            ).fetchone()
        self.assertEqual(row["manifest_hash"], self.manifest.manifest_hash)
        self.assertEqual(row["revision"], "a" * 40)
        self.assertEqual(restored.integrity_check(), "ok")


if __name__ == "__main__":
    unittest.main()
