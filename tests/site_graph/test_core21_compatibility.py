from __future__ import annotations

import http.client
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

from boho_analytics_platform.cli import main
from boho_analytics_platform.config import load_config
from boho_analytics_platform.connectors.common import total_point
from boho_analytics_platform.site_graph.analysis import compile_graph
from boho_analytics_platform.site_graph.contracts import AdapterResult
from boho_analytics_platform.site_graph.manifest import load_manifest_text
from boho_analytics_platform.site_graph.reconciliation import (
    publish_reconciled_evidence,
    reconcile_adapter_results,
)
from boho_analytics_platform.site_graph.storage import SiteGraphStore
from boho_analytics_platform.storage import SQLiteMetricStore
from boho_analytics_platform.web import handler_factory
from tests.support import config_text, write_fixture
from tests.site_graph.support import VALID_MANIFEST
from tests.site_graph.test_core21_reconciliation import ORIGIN, REVISION, lane


class Core21CompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        fixture = root / "fixture.json"
        write_fixture(fixture)
        config_path = root / "platform.toml"
        config_path.write_text(
            config_text(root / "state.db", fixture), encoding="utf-8"
        )
        self.config = load_config(config_path)
        self.store = SQLiteMetricStore(root / "state.db")
        self.store.initialize()
        self._seed_core21_graph()
        self._seed_route_observations()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler_factory(self.config, self.store)
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.thread.join, 2)

    def _seed_core21_graph(self) -> None:
        graph_store = SiteGraphStore(self.store.path)
        manifest = load_manifest_text(VALID_MANIFEST)
        manifest_id = graph_store.save_manifest(manifest)
        run_id = graph_store.start_ingest(
            manifest_version_id=manifest_id,
            site_key=manifest.site.key,
            analysis_mode=manifest.analysis.mode,
        )
        repository_id = graph_store.save_repository_snapshot(
            ingest_run_id=run_id,
            site_key=manifest.site.key,
            repository_identity="fixture/public-site",
            remote_url="https://github.com/example/public-site.git",
            revision=REVISION,
            ref="main",
            clean=True,
            content_hash="b" * 64,
        )
        source = lane(
            "source-semantic",
            {"/": "source-only", "/about/": "source-only", "/menu-only/": "source-only"},
            links=(
                ("/", "/about/", "source-only", "contextual"),
                ("/", "/menu-only/", "source-only", "menu"),
            ),
        )
        artifact = lane(
            "artifact-evidence",
            {"/": "artifact-only", "/about/": "artifact-only", "/menu-only/": "artifact-only"},
        )
        lanes = tuple(
            AdapterResult(
                item.status,
                replace(item.batch, site_key=manifest.site.key),
                item.diagnostics,
            )
            for item in (source, artifact)
        )
        result = reconcile_adapter_results(
            lanes,
            site_key=manifest.site.key,
            repository_revision=REVISION,
            canonical_origin=ORIGIN,
        )
        publish_reconciled_evidence(
            graph_store,
            result,
            repository_snapshot_id=repository_id,
            manifest_version_id=manifest_id,
            goal_definition_hash="c" * 64,
        )
        graph_store.finish_ingest(run_id, status="succeeded")
        compile_graph(graph_store, site_key=manifest.site.key, projection="contextual")
        self.graph_site_key = manifest.site.key

    def _seed_route_observations(self) -> None:
        common = {
            "client_id": "example-client",
            "site_id": "example-site",
            "start": datetime(2026, 7, 1, tzinfo=UTC),
            "end": datetime(2026, 7, 2, tzinfo=UTC),
        }
        self.store.upsert([
            total_point(
                **common, source="google-analytics",
                metric="google.landing-page-sessions", unit="count", value=7,
                dimensions={"route": "/about/"},
            ),
            total_point(
                **common, source="search-console",
                metric="search.route-clicks", unit="count", value=3,
                dimensions={
                    "route": "/about/", "data_state": "final",
                    "observation_scope": "page",
                },
            ),
            total_point(
                **common, source="umami",
                metric="umami.route-visits", unit="count", value=5,
                dimensions={"route": "/about/"},
            ),
        ])

    def request(self, path: str, *, host: str = "127.0.0.1"):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=3
        )
        connection.putrequest("GET", path, skip_host=True)
        connection.putheader("Host", host)
        connection.endheaders()
        response = connection.getresponse()
        body = response.read().decode()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    def test_cli_json_and_html_project_complete_core21_coverage(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            status = main([
                "site-graph", "report", "--database", str(self.store.path),
                "--site", self.graph_site_key,
            ])
        self.assertEqual(status, 0)
        cli_payload = json.loads(output.getvalue())
        evidence = cli_payload["evidence_core21"]
        self.assertTrue(evidence["available"])
        self.assertTrue(evidence["coverage"]["complete_totals"])
        self.assertFalse(evidence["coverage"]["display_cap_applied"])
        self.assertIn("true_orphans", evidence["structural_metrics"])
        self.assertNotIn("traps", cli_payload["overview"])
        self.assertNotIn("bottlenecks", cli_payload["overview"])

        status, headers, body = self.request(
            f"/site-graph?site={self.graph_site_key}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertIn("Graph Evidence Core 2.1 coverage", body)
        self.assertIn(
            "Complete route-resolution coverage by state", body
        )
        self.assertIn("Corrected structural findings", body)
        self.assertNotIn("github.com/example", body)

    def test_json_and_csv_remain_sanitized_and_complete(self) -> None:
        status, headers, body = self.request(
            f"/api/v1/site-graph?site={self.graph_site_key}"
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["evidence_core21"]["evidence_core"], "2.1")
        self.assertTrue(payload["evidence_core21"]["coverage"]["complete_totals"])
        self.assertNotIn("source_path", body)
        self.assertNotIn("remote_url", body)
        self.assertNotIn("traps", payload["overview"])

        status, headers, csv_body = self.request(
            f"/api/v1/site-graph.csv?site={self.graph_site_key}"
        )
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("source_route,destination_pretty_name", csv_body)
        self.assertNotIn("github.com/example", csv_body)

    def test_structural_metrics_are_withheld_for_a_different_display_projection(self) -> None:
        status, _headers, body = self.request(
            f"/api/v1/site-graph?site={self.graph_site_key}&layer=menu"
        )
        self.assertEqual(status, 200)
        structural = json.loads(body)["evidence_core21"]["structural_metrics"]
        self.assertFalse(structural["available"])
        self.assertIn("differ", structural["reason"])

    def test_route_observations_are_provider_separated_filtered_and_private(self) -> None:
        path = (
            "/api/v1/route-observations?report=summary&start=2026-07-01"
            "&end=2026-07-02&site=example-site&route=%2Fabout%2F"
        )
        status, headers, body = self.request(path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        payload = json.loads(body)
        self.assertEqual(payload["total_rows"], 3)
        self.assertEqual(
            {row["source"] for row in payload["rows"]},
            {"google-analytics", "search-console", "umami"},
        )
        self.assertTrue(payload["complete_totals"])
        self.assertEqual(payload["provider_aggregation"], "separate")
        self.assertFalse(payload["privacy"]["raw_queries"])
        self.assertNotIn('"visitor_id":', body)
        self.assertNotIn('"session_id":', body)
        self.assertNotIn("query_cluster", body)

        status, _headers, html_body = self.request(path.replace(
            "/api/v1/route-observations", "/route-observations"
        ))
        self.assertEqual(status, 200)
        self.assertIn("Search clicks are not sessions", html_body)
        self.assertIn(
            "Provider-separated route observations with coverage, freshness, and limitations",
            html_body,
        )
        status, _headers, csv_body = self.request(path.replace(
            "/api/v1/route-observations", "/api/v1/route-observations.csv"
        ))
        self.assertEqual(status, 200)
        self.assertIn("provider_time_basis,provider_limitation", csv_body)
        self.assertIn("search-console,search.route-clicks", csv_body)

    def test_route_observation_errors_and_host_validation_are_sanitized(self) -> None:
        status, _headers, body = self.request(
            "/api/v1/route-observations?metric=unknown"
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            json.loads(body), {"error": "invalid route observation request"}
        )
        status, _headers, body = self.request(
            "/route-observations", host="attacker.example"
        )
        self.assertEqual(status, 400)
        self.assertNotIn(str(self.store.path), body)


if __name__ == "__main__":
    unittest.main()
