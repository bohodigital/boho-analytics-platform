from __future__ import annotations

import tempfile
import unittest
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from boho_analytics_platform.site_graph.manifest import load_manifest_text
from boho_analytics_platform.site_graph.storage import SiteGraphStore
from tests.site_graph.support import VALID_MANIFEST
from tests.site_graph.test_core21_contracts import REVISION, sample_batch
from boho_analytics_platform.site_graph.contracts import CoverageSummary, EvidenceBatch, PageCandidate


class Core21PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = load_manifest_text(VALID_MANIFEST)

    def initialized(self, name: str) -> tuple[SiteGraphStore, str, str]:
        store = SiteGraphStore(self.root / name)
        store.initialize()
        manifest_id = store.save_manifest(self.manifest)
        run_id = store.start_ingest(
            manifest_version_id=manifest_id,
            site_key=self.manifest.site.key,
            analysis_mode=self.manifest.analysis.mode,
        )
        repository_id = store.save_repository_snapshot(
            ingest_run_id=run_id,
            site_key=self.manifest.site.key,
            repository_identity="fixture/public-site",
            remote_url="https://github.com/example/public-site.git",
            revision=REVISION,
            ref="main",
            clean=True,
            content_hash="b" * 64,
        )
        return store, manifest_id, repository_id

    def publish(self, store: SiteGraphStore, manifest_id: str, repository_id: str) -> str:
        batch = replace(sample_batch(), site_key=self.manifest.site.key)
        return store.publish_evidence_batch(
            batch,
            repository_snapshot_id=repository_id,
            manifest_version_id=manifest_id,
            compiler_version="core21",
            projection_name="all-page-links",
            goal_definition_hash="c" * 64,
        )

    def test_identical_batches_have_stable_snapshot_identity_across_databases(self):
        first, first_manifest, first_repository = self.initialized("first.db")
        second, second_manifest, second_repository = self.initialized("second.db")
        first_id = self.publish(first, first_manifest, first_repository)
        second_id = self.publish(second, second_manifest, second_repository)
        self.assertEqual(first_id, second_id)
        self.assertEqual(first_id, self.publish(first, first_manifest, first_repository))
        with first.connect(readonly=True) as db:
            counts = {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "site_graph_page_facts", "site_graph_link_occurrences",
                    "site_graph_page_entities", "site_graph_snapshots",
                )
            }
            carrier = db.execute(
                "SELECT evidence_json FROM site_graph_page_facts ORDER BY fact_key LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(counts, {
            "site_graph_page_facts": 2,
            "site_graph_link_occurrences": 1,
            "site_graph_page_entities": 2,
            "site_graph_snapshots": 1,
        })
        self.assertIn('"evidence_batch_id":"sgb_', carrier)

    def test_publication_evaluates_batch_identity_once(self):
        store, manifest_id, repository_id = self.initialized("batch-id.db")
        original = EvidenceBatch.batch_id.fget
        evaluations = 0

        def counted(batch: EvidenceBatch) -> str:
            nonlocal evaluations
            evaluations += 1
            return original(batch)

        with patch.object(EvidenceBatch, "batch_id", property(counted)):
            self.publish(store, manifest_id, repository_id)

        self.assertEqual(evaluations, 1)

    def test_interrupted_publication_rolls_back_batch_and_derived_rows(self):
        store, manifest_id, repository_id = self.initialized("rollback.db")
        with patch.object(store, "save_graph_snapshot", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.publish(store, manifest_id, repository_id)
        with store.connect(readonly=True) as db:
            for table in (
                "site_graph_page_facts", "site_graph_link_occurrences",
                "site_graph_page_entities", "site_graph_snapshots",
            ):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_unresolved_candidate_is_preserved_without_becoming_a_page(self):
        store, manifest_id, repository_id = self.initialized("unresolved.db")
        batch = replace(sample_batch(), site_key=self.manifest.site.key)
        unresolved = PageCandidate(
            "slug + runtimeValue", "", "dynamic-unknown",
            "src/pages/routes.tsx", "routes.tsx:20",
        )
        batch = replace(
            batch,
            candidates=(*batch.candidates, unresolved),
            coverage=CoverageSummary(3, 2, 1, 3, 2, 1, (
                ("confirmed-page", 2), ("dynamic-unknown", 1),
            )),
        )
        store.publish_evidence_batch(
            batch,
            repository_snapshot_id=repository_id,
            manifest_version_id=manifest_id,
            compiler_version="core21",
            projection_name="all-page-links",
            goal_definition_hash="c" * 64,
        )
        with store.connect(readonly=True) as db:
            rows = db.execute("SELECT evidence_json FROM site_graph_page_facts").fetchall()
            page_count = len(rows)
        carried_ids = {
            item["candidate_id"]
            for row in rows
            for item in json.loads(row["evidence_json"])["candidate_evidence"]
        }
        self.assertEqual(page_count, 2)
        self.assertIn(unresolved.candidate_id, carried_ids)


if __name__ == "__main__":
    unittest.main()
