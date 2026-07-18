from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from datetime import timedelta
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path

from boho_analytics_platform.config import load_config
from boho_analytics_platform.engine import SyncEngine
from boho_analytics_platform.models import Completeness, MetricPoint, QueryWindow, TimeGrain
from boho_analytics_platform.storage import SQLiteMetricStore
from boho_analytics_platform.web import _summary_cards, handler_factory
from support import config_text, write_fixture
from tests.site_graph.test_analysis import seed_site_graph


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup); root = Path(self.temporary.name)
        fixture = root / "fixture.json"; write_fixture(fixture); path = root / "platform.toml"; path.write_text(config_text(root / "state.db", fixture), encoding="utf-8")
        self.config = load_config(path); self.store = SQLiteMetricStore(root / "state.db"); self.store.initialize()
        SyncEngine(self.config, self.store).sync(QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 2, tzinfo=UTC), "UTC"))
        graph_store, self.graph_site_key = seed_site_graph(root / "state.db")
        from boho_analytics_platform.site_graph.analysis import compile_graph
        compile_graph(graph_store, site_key=self.graph_site_key, projection="contextual")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(self.config, self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown); self.addCleanup(self.thread.join, 2)

    def request(self, path, host="127.0.0.1"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.putrequest("GET", path, skip_host=True); connection.putheader("Host", host); connection.endheaders()
        response = connection.getresponse(); body = response.read().decode(); headers = dict(response.getheaders()); connection.close()
        return response.status, headers, body

    def test_dashboard_is_server_rendered_and_has_security_headers(self):
        status, headers, body = self.request("/?report=summary&start=2026-07-01&end=2026-07-02")
        self.assertEqual(status, 200); self.assertIn("Forms delivery", body); self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("camera=()", headers["Permissions-Policy"]); self.assertIn("microphone=()", headers["Permissions-Policy"])
        self.assertIn('data-chart="umami.pageviews"', body); self.assertIn("Report tools", body); self.assertIn('src="/assets/app.js"', body)
        self.assertIn('id="time-series-chart"', body); self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers); self.assertEqual(headers["Cache-Control"], "no-store")

    def test_invalid_host_is_rejected(self): self.assertEqual(self.request("/healthz", "attacker.invalid")[0], 400)

    def test_health_identifies_the_exact_build_and_schema(self):
        status, _headers, body = self.request("/healthz")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["version"], "0.1.1.dev0")
        self.assertIn("build_commit", payload)
        self.assertIn("build_tree", payload)
        self.assertGreaterEqual(payload["database_schema"], 2)

    def test_report_dates_are_strict_bounded_and_comparison_safe(self):
        invalid_paths = (
            "/api/v1/report?report=summary&start=2026-07-01T00:00:00&end=2026-07-02",
            "/api/v1/report?report=summary&start=2026-07-02&end=2026-07-01",
            "/api/v1/report?report=summary&start=2010-01-01&end=2026-07-02",
            "/api/v1/report?report=summary&start=0001-01-01&end=0001-01-02",
        )
        for path in invalid_paths:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 400)
        self.assertEqual(self.request("/healthz")[0], 200)

    def test_blank_analytical_query_values_are_rejected(self):
        for path in (
            "/api/v1/report?report=summary&start=&end=",
            "/api/v1/report?report=summary&site=",
            "/api/v1/series?report=summary&metric=",
            "/api/v1/series?report=summary&source=&metric=umami.pageviews",
            "/site-graph?site=",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 400)

    def test_json_and_csv_share_report_rows(self):
        json_body = self.request("/api/v1/report?report=summary&start=2026-07-01&end=2026-07-02")[2]
        self.assertIn('"umami.pageviews"', json_body); self.assertIn('"series"', json_body)
        status, headers, csv_body = self.request("/api/v1/report.csv?report=summary&start=2026-07-01&end=2026-07-02")
        self.assertEqual(status, 200); self.assertIn("umami.pageviews", csv_body); self.assertIn("attachment", headers["Content-Disposition"])

    def test_plot_builder_filters_series_and_exports_portable_csv(self):
        path = "/?report=summary&view=plot&source=umami&metric=umami.pageviews&style=area&compare=1&start=2026-07-01&end=2026-07-02"
        status, _headers, body = self.request(path)
        self.assertEqual(status, 200); self.assertIn("Time-series Plot Builder", body)
        self.assertIn('name="source"', body); self.assertIn('name="style"', body); self.assertIn('name="compare"', body)
        self.assertIn("Load series JSON", body); self.assertIn("Download series CSV", body)

        api = "/api/v1/series?report=summary&view=plot&source=umami&metric=umami.pageviews&style=area&start=2026-07-01&end=2026-07-02"
        status, _headers, json_body = self.request(api)
        self.assertEqual(status, 200); self.assertIn('"metric": "umami.pageviews"', json_body)
        self.assertIn('"style": "area"', json_body); self.assertIn('"value": 12', json_body)

        status, headers, csv_body = self.request(api.replace("/series?", "/series.csv?"))
        self.assertEqual(status, 200); self.assertIn("period,date,metric,site_id,source,unit,value", csv_body)
        self.assertIn("current,2026-07-01,umami.pageviews", csv_body)
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_script_asset_is_same_origin_and_invalid_plot_source_is_rejected(self):
        status, _headers, body = self.request("/assets/app.js")
        self.assertEqual(status, 200); self.assertIn("ResizeObserver", body); self.assertIn("fetch(canvas.dataset.seriesUrl", body)
        self.assertIn("contiguousSegments", body)
        self.assertIn("comparison unavailable", body)
        invalid = "/api/v1/series?report=summary&source=search-console&metric=umami.pageviews&start=2026-07-01&end=2026-07-02"
        self.assertEqual(self.request(invalid)[0], 400)

    def test_series_rejects_an_unknown_metric_instead_of_substituting_one(self):
        invalid = "/api/v1/series?report=summary&source=umami&metric=made.up&start=2026-07-01&end=2026-07-02"
        status, _headers, body = self.request(invalid)
        self.assertEqual(status, 400)
        self.assertIn("invalid report request", body)

    def test_series_discloses_when_requested_comparison_is_unavailable(self):
        path = "/api/v1/series?report=summary&source=umami&metric=umami.pageviews&compare=1&start=2026-07-01&end=2026-07-02"
        status, _headers, body = self.request(path)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertFalse(payload["comparison_available"])
        self.assertEqual(payload["comparison_status"], "unavailable")
        self.assertTrue(any("comparison" in warning.casefold() for warning in payload["warnings"]))

    def test_series_comparison_uses_selected_metric_not_unrelated_report_coverage(self):
        start = datetime(2026, 7, 2, tzinfo=UTC)
        self.store.upsert([MetricPoint(
            "example-client", "example-site", "fixture", "umami.pageviews", "count",
            start, start + timedelta(days=1), TimeGrain.DAY, Decimal(15), (),
            Completeness.FINAL, start + timedelta(hours=12),
        )])
        path = (
            "/api/v1/series?report=summary&source=umami&metric=umami.pageviews"
            "&compare=1&start=2026-07-02&end=2026-07-03"
        )
        status, _headers, body = self.request(path)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["comparison_available"])
        self.assertEqual(payload["comparison_status"], "available")

    def test_summary_cards_do_not_silently_switch_visitor_definitions(self):
        result = {
            "subreport_id": "traffic",
            "rows": [{
                "metric": "google.active-users", "site_id": "example-site",
                "source": "google-analytics", "unit": "count", "value": 7,
                "previous_value": None,
            }],
            "summary_totals": {
                "google.active-users": {
                    "metric": "google.active-users", "source": "google-analytics",
                    "unit": "count", "aggregation": "sum", "value": 7,
                    "previous_value": None, "change_percent": None,
                    "coverage_status": "complete", "comparison_available": False,
                }
            },
            "forms_pipeline": None,
        }
        html = _summary_cards(result, ("umami.visitors", "google.active-users"))
        self.assertIn("Umami visitors", html)
        self.assertIn("GA active-user days", html)
        self.assertIn('data-metric="umami.visitors" data-state="unknown"', html)
        self.assertIn('data-metric="google.active-users" data-state="observed"', html)

    def test_search_cards_use_weighted_portfolio_totals_not_summed_site_rates(self):
        result = {
            "subreport_id": "search",
            "rows": [
                {"metric": "search.ctr", "site_id": "one", "source": "search-console", "unit": "ratio", "value": .05, "previous_value": None},
                {"metric": "search.ctr", "site_id": "two", "source": "search-console", "unit": "ratio", "value": .03, "previous_value": None},
                {"metric": "search.position", "site_id": "one", "source": "search-console", "unit": "position", "value": 40, "previous_value": None},
                {"metric": "search.position", "site_id": "two", "source": "search-console", "unit": "position", "value": 92.8, "previous_value": None},
            ],
            "summary_totals": {
                "search.ctr": {"metric": "search.ctr", "source": "search-console", "unit": "ratio", "aggregation": "weighted", "value": 9 / 404, "previous_value": None, "change_percent": None, "coverage_status": "complete", "comparison_available": False},
                "search.position": {"metric": "search.position", "source": "search-console", "unit": "position", "aggregation": "weighted", "value": 36.396, "previous_value": None, "change_percent": None, "coverage_status": "complete", "comparison_available": False},
            },
            "forms_pipeline": None,
        }
        html = _summary_cards(result, ("search.clicks", "search.impressions", "search.ctr", "search.position"))
        self.assertIn("2.2%", html)
        self.assertIn("36.4", html)
        self.assertNotIn("8.0%", html)
        self.assertNotIn("132.8", html)

    def test_forms_cards_preserve_unknown_pipeline_values(self):
        body = self.request("/?report=summary&subreport=forms&start=2026-07-01&end=2026-07-02")[2]
        self.assertIn('data-metric="forms.pending" data-state="unknown"', body)
        self.assertIn('data-metric="forms.failed" data-state="unknown"', body)

    def test_subreport_navigation_and_form_preserve_scope_and_window(self):
        body = self.request("/?report=summary&subreport=forms&start=2026-07-01&end=2026-07-02")[2]
        self.assertIn('name="subreport" value="forms"', body)
        self.assertIn("subreport=forms", body); self.assertIn("start=2026-07-01", body); self.assertIn("end=2026-07-02", body)

    def test_site_scope_is_validated_and_preserved(self):
        path = "/?report=summary&site=example-site&start=2026-07-01&end=2026-07-02"
        status, _headers, body = self.request(path)
        self.assertEqual(status, 200); self.assertIn('<option value="example-site" selected>', body)
        self.assertIn("site=example-site", body)
        self.assertEqual(self.request("/?report=summary&site=unknown&start=2026-07-01&end=2026-07-02")[0], 400)

    def test_css_charts_need_no_inline_style_permission(self):
        status, _headers, body = self.request("/assets/app.css")
        self.assertEqual(status, 200); self.assertIn(".h-50{height:100%}", body)
        page_headers = self.request("/?report=summary&start=2026-07-01&end=2026-07-02")[1]
        self.assertNotIn("unsafe-inline", page_headers["Content-Security-Policy"])

    def test_site_graph_dashboard_and_api_are_read_only_accessible_and_bounded(self):
        status, headers, body = self.request(f"/site-graph?site={self.graph_site_key}&page=%2Fservices%2F")
        self.assertEqual(status, 200)
        self.assertIn("Site Graph", body)
        self.assertIn("Structural evidence", body)
        self.assertIn("Analytical basis stays compiled contextual", body)
        self.assertIn("Goal distance", body)
        self.assertIn("Strongly connected components", body)
        self.assertIn("<svg", body)
        self.assertIn("<title>", body)
        self.assertIn("Graph nodes and edges", body)
        self.assertIn('name="page" value="/services/"', body)
        self.assertNotIn("github.com/example", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

        status, _headers, api_body = self.request(f"/api/v1/site-graph?site={self.graph_site_key}&page=%2Fservices%2F")
        self.assertEqual(status, 200)
        self.assertIn('"projection": "contextual"', api_body)
        self.assertIn('"analysis_basis": "compiled-contextual"', api_body)
        self.assertIn('"edge_basis": "selected-layers"', api_body)
        self.assertIn('"selected_page": "/services/"', api_body)

    def test_site_graph_rejects_unknown_layer_without_mutating_database(self):
        before = self.store.path.stat().st_size
        status, _headers, body = self.request(f"/api/v1/site-graph?site={self.graph_site_key}&layer=bogus")
        self.assertEqual(status, 400)
        self.assertIn("invalid site graph request", body)
        self.assertEqual(self.store.path.stat().st_size, before)

    def test_site_graph_rejects_unknown_site_instead_of_returning_empty_success(self):
        status, _headers, body = self.request("/api/v1/site-graph?site=unknown")
        self.assertEqual(status, 400)
        self.assertIn("invalid site graph request", body)

    def test_site_graph_mobile_css_prevents_page_level_horizontal_overflow(self):
        css = self.request("/assets/app.css")[2]
        self.assertIn("overflow-x:hidden", css)
        self.assertIn("fieldset.field{min-width:0", css)
        self.assertIn("overflow-wrap:anywhere", css)


if __name__ == "__main__": unittest.main()
