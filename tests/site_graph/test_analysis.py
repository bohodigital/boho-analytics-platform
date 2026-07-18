from __future__ import annotations

import tempfile
import unittest
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from boho_analytics_platform.site_graph.analysis import _strongly_connected, compile_graph
from boho_analytics_platform.site_graph.dashboard import SiteGraphReportService
from boho_analytics_platform.site_graph.manifest import load_manifest_text
from boho_analytics_platform.site_graph.storage import LinkOccurrence, PageFact, SiteGraphStore
from boho_analytics_platform.cli import main
from tests.site_graph.support import VALID_MANIFEST


def seed_site_graph(path: Path) -> tuple[SiteGraphStore, str]:
    store = SiteGraphStore(path)
    store.initialize()
    manifest = load_manifest_text(VALID_MANIFEST)
    manifest_id = store.save_manifest(manifest)
    run_id = store.start_ingest(
        manifest_version_id=manifest_id,
        site_key=manifest.site.key,
        analysis_mode=manifest.analysis.mode,
    )
    repository_id = store.save_repository_snapshot(
        ingest_run_id=run_id,
        site_key=manifest.site.key,
        repository_identity="example/fixture-static",
        remote_url="https://github.com/example/fixture-static.git",
        revision="a" * 40,
        ref="main",
        clean=True,
        content_hash="b" * 64,
    )
    pages = [
        PageFact("home", "/", "https://fixture.example/", "index.html", {}, "1" * 64),
        PageFact("services", "/services/", "https://fixture.example/services/", "services/index.html", {}, "2" * 64),
        PageFact("contact", "/contact/", "https://fixture.example/contact/", "contact/index.html", {}, "3" * 64),
        PageFact("orphan", "/orphan/", "https://fixture.example/orphan/", "orphan/index.html", {}, "4" * 64),
    ]
    links = [
        LinkOccurrence("home-services-menu", "home", "/services/", "/services/", "Services", "", "index.html:4", "nav", "menu", 1.0, {}),
        LinkOccurrence("home-services-body", "home", "/services/", "/services/", "Explore services", "", "index.html:14", "main", "contextual", 1.0, {}),
        LinkOccurrence("services-contact", "services", "/contact/", "/contact/", "Talk to us", "", "services/index.html:18", "main", "action", 1.0, {}),
        LinkOccurrence("home-contact-menu", "home", "/contact/", "/contact/", "Contact", "", "index.html:6", "nav", "menu", 1.0, {}),
    ]
    store.save_fact_batch(repository_id, pages=pages, links=links)
    store.finish_ingest(run_id, status="succeeded")
    return store, manifest.site.key


def seed_newer_repository_snapshot(store: SiteGraphStore, site_key: str) -> str:
    manifest = load_manifest_text(VALID_MANIFEST)
    manifest_id = store.save_manifest(manifest)
    run_id = store.start_ingest(
        manifest_version_id=manifest_id,
        site_key=site_key,
        analysis_mode=manifest.analysis.mode,
    )
    repository_id = store.save_repository_snapshot(
        ingest_run_id=run_id,
        site_key=site_key,
        repository_identity="example/fixture-static",
        remote_url="https://github.com/example/fixture-static.git",
        revision="c" * 40,
        ref="main",
        clean=True,
        content_hash="d" * 64,
    )
    pages = [
        PageFact("home", "/", "https://fixture.example/", "index.html", {}, "5" * 64),
        PageFact("contact", "/contact/", "https://fixture.example/contact/", "contact/index.html", {}, "6" * 64),
    ]
    links = [
        LinkOccurrence(
            "home-contact", "home", "/contact/", "/contact/", "Contact", "", "index.html:2",
            "main", "action", 1.0, {},
        ),
    ]
    store.save_fact_batch(repository_id, pages=pages, links=links)
    store.finish_ingest(run_id, status="succeeded")
    return repository_id


class SiteGraphAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store, self.site_key = seed_site_graph(Path(self.temporary.name) / "analytics.db")

    def test_compile_is_deterministic_and_separates_contextual_links(self):
        first = compile_graph(self.store, site_key=self.site_key, projection="contextual")
        second = compile_graph(self.store, site_key=self.site_key, projection="contextual")

        self.assertEqual(first["graph_snapshot_id"], second["graph_snapshot_id"])
        self.assertEqual(first["nodes"], 4)
        self.assertEqual(first["edges"], 2)
        self.assertEqual(first["goal_distance_buckets"]["goal"], 2)
        self.assertEqual(first["goal_distance_buckets"]["1"], 1)
        self.assertEqual(first["goal_distance_buckets"]["unreachable"], 1)
        self.assertEqual(first["findings"]["orphan"], 1)

    def test_dashboard_summary_is_bounded_sanitized_and_has_neighborhood(self):
        compile_graph(self.store, site_key=self.site_key, projection="contextual")
        payload = SiteGraphReportService(self.store).summary(
            site_key=self.site_key,
            selected_page="/services/",
            layers=("contextual", "action"),
        )

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["site"]["key"], self.site_key)
        self.assertEqual(payload["coverage"]["pages"], 4)
        self.assertEqual(payload["coverage"]["link_occurrences"], 4)
        self.assertEqual(payload["overview"]["projection_edges"], 2)
        self.assertEqual(payload["neighborhood"]["selected_page"], "/services/")
        self.assertLessEqual(len(payload["visualization"]["nodes"]), 36)
        self.assertLessEqual(len(payload["visualization"]["edges"]), 60)
        rendered = repr(payload)
        self.assertNotIn(str(self.temporary.name), rendered)
        self.assertNotIn("github.com/example", rendered)

    def test_cli_compile_and_report_share_the_normalized_dashboard_summary(self):
        database = str(self.store.path)
        compiled_output = io.StringIO()
        with redirect_stdout(compiled_output):
            status = main(["site-graph", "compile", "--database", database, "--site", self.site_key])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(compiled_output.getvalue())["projection"], "contextual")

        report_output = io.StringIO()
        with redirect_stdout(report_output):
            status = main(["site-graph", "report", "--database", database, "--site", self.site_key])
        payload = json.loads(report_output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(payload["site"]["key"], self.site_key)
        self.assertEqual(payload["overview"]["projection_edges"], 2)
        self.assertNotIn(str(self.temporary.name), report_output.getvalue())

    def test_component_analysis_handles_a_large_acyclic_site_without_recursion(self):
        nodes = [f"/page-{index}/" for index in range(2_000)]
        adjacency = {node: set() for node in nodes}
        for source, destination in zip(nodes, nodes[1:]):
            adjacency[source].add(destination)

        components = _strongly_connected(nodes, adjacency)

        self.assertEqual(len(components), len(nodes))
        self.assertTrue(all(len(component) == 1 for component in components))

    def test_interrupted_compile_never_becomes_the_latest_visible_graph(self):
        first = compile_graph(self.store, site_key=self.site_key, projection="contextual")
        seed_newer_repository_snapshot(self.store, self.site_key)

        with self.assertRaisesRegex(RuntimeError, "snapshot"):
            compile_graph(
                self.store,
                site_key=self.site_key,
                projection="contextual",
                _interrupt_after="snapshot",
            )

        payload = SiteGraphReportService(self.store).summary(site_key=self.site_key)
        self.assertEqual(payload["snapshot"]["id"], first["graph_snapshot_id"])
        with self.store.connect(readonly=True) as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM site_graph_snapshots WHERE site_key=? AND projection_name='contextual'",
                    (self.site_key,),
                ).fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()
