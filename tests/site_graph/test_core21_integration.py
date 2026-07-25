from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from boho_analytics_platform.site_graph.contracts import AdapterResult
from boho_analytics_platform.site_graph.manifest import load_manifest_text
from boho_analytics_platform.site_graph.ingest import ingest_evidence_core21
from boho_analytics_platform.site_graph.reconciliation import (
    artifact_evidence_to_contract,
    publish_reconciled_evidence,
    reconcile_adapter_results,
    rendered_evidence_to_contract,
    source_semantic_to_contract,
)
from boho_analytics_platform.site_graph.adapters.artifact_evidence import (
    collect_artifact_evidence,
)
from boho_analytics_platform.site_graph.adapters.rendered_crawl import (
    CrawlAuthorization,
    crawl_rendered_evidence,
)
from boho_analytics_platform.site_graph.adapters.source_semantic import (
    extract_source_semantic_evidence,
)
from boho_analytics_platform.site_graph.storage import SiteGraphStore
from boho_analytics_platform.site_graph.analysis import analyze_structural_semantics
from boho_analytics_platform.site_graph.reporting import core21_evidence_report
from tests.site_graph.support import VALID_MANIFEST
from tests.site_graph.test_core21_reconciliation import ORIGIN, REVISION, lane
from tests.site_graph.test_core21_rendered_crawl import _Factory


class Core21IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = SiteGraphStore(self.root / "graph.db")
        self.store.initialize()
        self.manifest = load_manifest_text(VALID_MANIFEST)
        self.manifest_id = self.store.save_manifest(self.manifest)
        run_id = self.store.start_ingest(
            manifest_version_id=self.manifest_id,
            site_key=self.manifest.site.key,
            analysis_mode=self.manifest.analysis.mode,
        )
        self.repository_id = self.store.save_repository_snapshot(
            ingest_run_id=run_id,
            site_key=self.manifest.site.key,
            repository_identity="fixture/public-site",
            remote_url="https://github.com/example/public-site.git",
            revision=REVISION,
            ref="main",
            clean=True,
            content_hash="b" * 64,
        )

    def lane_inputs(self):
        source = lane(
            "source-semantic",
            {"/": "source-only", "/about/": "source-only"},
            links=(("/", "/about/", "source-only", "contextual"),),
        )
        artifact = lane(
            "artifact-evidence",
            {"/": "artifact-only", "/about/": "artifact-only"},
        )
        unavailable = AdapterResult(
            "failed",
            None,
            (
                {
                    "severity": "warning",
                    "code": "rendered-unavailable",
                    "message": "Rendered evidence was unavailable.",
                },
            ),
        )
        source = AdapterResult(
            source.status,
            replace(source.batch, site_key=self.manifest.site.key),
            source.diagnostics,
        )
        artifact = AdapterResult(
            artifact.status,
            replace(artifact.batch, site_key=self.manifest.site.key),
            artifact.diagnostics,
        )
        return (source, artifact, unavailable)

    def reconciled(self):
        return reconcile_adapter_results(
            self.lane_inputs(),
            site_key=self.manifest.site.key,
            repository_revision=REVISION,
            canonical_origin=ORIGIN,
        )

    def test_reconciled_batch_publishes_atomically_and_idempotently(self) -> None:
        result = self.reconciled()
        first = publish_reconciled_evidence(
            self.store,
            result,
            repository_snapshot_id=self.repository_id,
            manifest_version_id=self.manifest_id,
            goal_definition_hash="c" * 64,
        )
        second = publish_reconciled_evidence(
            self.store,
            result,
            repository_snapshot_id=self.repository_id,
            manifest_version_id=self.manifest_id,
            goal_definition_hash="c" * 64,
        )
        self.assertEqual(first, second)
        with self.store.connect(readonly=True) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM site_graph_snapshots").fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM site_graph_page_facts").fetchone()[0],
                2,
            )

        integrated = ingest_evidence_core21(
            self.store,
            self.lane_inputs(),
            site_key=self.manifest.site.key,
            repository_revision=REVISION,
            canonical_origin=ORIGIN,
            repository_snapshot_id=self.repository_id,
            manifest_version_id=self.manifest_id,
            goal_definition_hash="c" * 64,
        )
        self.assertEqual(integrated.graph_snapshot_id, first)
        self.assertTrue(integrated.coverage["complete_totals"])

    def test_interrupted_publication_never_becomes_visible(self) -> None:
        result = self.reconciled()
        with patch.object(
            self.store, "save_graph_snapshot", side_effect=RuntimeError("interrupted")
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                publish_reconciled_evidence(
                    self.store,
                    result,
                    repository_snapshot_id=self.repository_id,
                    manifest_version_id=self.manifest_id,
                    goal_definition_hash="c" * 64,
                )
        with self.store.connect(readonly=True) as db:
            for table in (
                "site_graph_page_facts",
                "site_graph_link_occurrences",
                "site_graph_page_entities",
                "site_graph_snapshots",
            ):
                self.assertEqual(
                    db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    0,
                )

    def test_versioned_lane_outputs_convert_to_the_frozen_contract(self) -> None:
        fixture_root = Path(__file__).parent / "fixtures" / "core21"
        source_root = fixture_root / "source_semantic"
        sources = {
            path.relative_to(source_root).as_posix(): path.read_text(encoding="utf-8")
            for path in source_root.rglob("*")
            if path.is_file()
        }
        source = source_semantic_to_contract(
            extract_source_semantic_evidence(
                sources, repository_revision=REVISION
            ),
            site_key="fixture-site",
            repository_revision=REVISION,
            canonical_origin=ORIGIN,
        )
        artifact = artifact_evidence_to_contract(
            collect_artifact_evidence(
                (fixture_root / "artifact_evidence" / "site",),
                revision=REVISION,
                canonical_hosts=("fixture.example",),
            ),
            site_key="fixture-site",
            repository_revision=REVISION,
            canonical_origin=ORIGIN,
        )
        rendered_origin = "https://example.test"
        rendered = rendered_evidence_to_contract(
            crawl_rendered_evidence(
                CrawlAuthorization(rendered_origin, REVISION, REVISION),
                ("/",),
                _Factory(),
            ),
            site_key="fixture-site",
            repository_revision=REVISION,
            canonical_origin=rendered_origin,
        )
        for result, adapter in (
            (source, "source-semantic"),
            (artifact, "artifact-evidence"),
            (rendered, "rendered-crawl"),
        ):
            with self.subTest(adapter=adapter):
                self.assertIsNotNone(result.batch)
                assert result.batch is not None
                self.assertEqual(result.batch.adapter, adapter)
                self.assertEqual(result.batch.repository_revision, REVISION)
                self.assertTrue(result.batch.content_hash)

    def test_coverage_report_discloses_complete_lane_and_projection_totals(self) -> None:
        result = self.reconciled()
        structural = analyze_structural_semantics(
            result.batch, selected_layers=("contextual",)
        )
        report = core21_evidence_report(result, structural=structural)
        self.assertTrue(report["coverage"]["complete_totals"])
        self.assertFalse(report["coverage"]["display_cap_applied"])
        self.assertEqual(report["coverage"]["failed_lanes"], 1)
        self.assertEqual(
            report["coverage"]["candidates"], len(result.batch.candidates)
        )
        self.assertTrue(report["structural_metrics"]["available"])
        withheld = core21_evidence_report(result)
        self.assertFalse(withheld["structural_metrics"]["available"])
        self.assertEqual(
            withheld["structural_metrics"]["reason"],
            "no-compatible-selected-projection",
        )


if __name__ == "__main__":
    unittest.main()
