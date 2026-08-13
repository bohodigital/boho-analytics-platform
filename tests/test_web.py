from __future__ import annotations

import csv
import http.client
import io
import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from datetime import timedelta
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from boho_analytics_platform.config import load_config
from boho_analytics_platform.connectors.common import total_point
from boho_analytics_platform.engine import SyncEngine
from boho_analytics_platform.models import CapabilitySnapshot, Completeness, MetricPoint, QueryWindow, TimeGrain
from boho_analytics_platform.reporting import ReportService
from boho_analytics_platform.storage import SQLiteMetricStore
from boho_analytics_platform.web import (
    PORTFOLIO_SUMMARY,
    SEARCH_SUMMARY,
    _attention_html,
    _chart_html,
    _coverage_summary_html,
    _decision_badge,
    _dashboard_visuals_html,
    _forms_html,
    _metrics_table,
    _overview_cards_html,
    _performance_by_site_html,
    _provider_comparisons_html,
    _snapshot_status_text,
    _search_type_label,
    _site_option_enabled,
    _summary_cards,
    handler_factory,
)
from support import config_text, write_fixture
from tests.site_graph.test_analysis import seed_site_graph


class WebTests(unittest.TestCase):
    def test_search_type_label_preserves_google_news_wordmark(self):
        self.assertEqual(_search_type_label("googleNews"), "Google News")
        self.assertEqual(_search_type_label("discover"), "Discover")

    def test_search_surface_only_filters_search_console_site_options(self):
        available = {
            "umami": {"umami-only"},
            "search-console": {"umami-only", "search-site"},
        }
        surfaces = {"umami-only": [], "search-site": ["web"]}

        self.assertTrue(_site_option_enabled(
            "umami-only", "umami", "web", available, surfaces
        ))
        self.assertFalse(_site_option_enabled(
            "umami-only", "search-console", "web", available, surfaces
        ))
        self.assertTrue(_site_option_enabled(
            "search-site", "search-console", "web", available, surfaces
        ))

    def test_forms_panel_never_claims_agreement_for_incomplete_coverage(self):
        partial = _forms_html({
            "submissions": 1,
            "inbox_deliveries": 1,
            "delivery_gap": None,
            "delivery_comparable": False,
            "pending": 0,
            "failed": 0,
        })
        self.assertIn("Coverage is incomplete or differs", partial)
        self.assertNotIn("evidence agree", partial)

        complete = _forms_html({
            "submissions": 1,
            "inbox_deliveries": 1,
            "delivery_gap": 0,
            "delivery_comparable": True,
            "pending": 0,
            "failed": 0,
        })
        self.assertIn("evidence agree for the complete selected scope", complete)

    def test_provider_comparison_html_discloses_discontinuous_date_ranges(self):
        dates = {
            "count": 2,
            "first": "2026-07-01",
            "last": "2026-07-03",
            "ranges": [
                {"start": "2026-07-01", "end": "2026-07-01"},
                {"start": "2026-07-03", "end": "2026-07-03"},
            ],
        }
        route = {
            "status": "withheld",
            "reason": "route_analytics_not_enabled",
        }
        semantics = {
            "pageview_definition": "pageviews",
            "time_basis": "UTC",
            "sampling": "none",
            "data_state": "final",
        }
        provider = {
            "complete_dates": dates,
            "first_available_date": "2026-07-01",
            "data_through": "2026-07-03",
            "route_reconciliation": route,
            "semantics": semantics,
        }
        comparison = {
            "site_id": "example-site",
            "evidence_state": "aligned",
            "low_volume_warning": False,
            "google_only_dates": {"count": 0, "first": None, "last": None, "ranges": []},
            "umami_only_dates": {"count": 0, "first": None, "last": None, "ranges": []},
            "paired_dates": dates,
            "first_paired_date": "2026-07-01",
            "last_paired_date": "2026-07-03",
            "totals": {
                "google_pageviews": 20,
                "umami_pageviews": 20,
                "absolute_difference": 0,
                "google_to_umami_ratio": 1,
            },
            "providers": {
                "google-analytics": provider,
                "umami": provider,
            },
            "semantics": [],
            "coverage_limits": [],
        }

        html = _provider_comparisons_html(
            {"provider_comparisons": [comparison]},
            {"example-site": "Example Site"},
        )

        self.assertNotIn("2 (2026-07-01 to 2026-07-03)", html)
        self.assertIn("2026-07-01 to 2026-07-01", html)
        self.assertIn("2026-07-03 to 2026-07-03", html)

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
        self.assertEqual(status, 200); self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("camera=()", headers["Permissions-Policy"]); self.assertIn("microphone=()", headers["Permissions-Policy"])
        self.assertIn('data-chart="umami.pageviews"', body); self.assertIn('src="/assets/app.js"', body)
        self.assertIn('id="time-series-chart"', body); self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers); self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn('class="dashboard-app"', body)
        self.assertIn('<h1>All properties</h1>', body)
        self.assertIn('id="property-selector" name="site"', body)
        self.assertIn('<option value="all" selected>All properties</option>', body)
        self.assertNotIn('class="report-nav"', body)
        self.assertNotIn('class="subnav"', body)
        self.assertNotIn("Forms delivery", body)
        self.assertNotIn('id="geography-map"', body)
        self.assertIn("Provider pageview comparison", body)
        self.assertIn('aria-label="GA4 and Umami pageview comparability"', body)
        self.assertIn("Non comparable", body)
        self.assertNotIn(">0 paired dates<", body)
        self.assertIn("Core feeds 100%", body)
        self.assertIn("Page views over time", body)
        self.assertGreaterEqual(body.count('data-area-fill="true"'), 1)
        self.assertEqual(
            body.count('data-area-fill="true"'),
            body.count('<canvas class="time-series-chart"'),
        )
        self.assertEqual(
            body.count('tabindex="0"'),
            body.count('<canvas class="time-series-chart"'),
        )
        self.assertIn("Traffic by property", body)
        self.assertIn("Index coverage", body)
        self.assertIn("Data details", body)
        self.assertIn('data-metric="umami.visits" data-state="unavailable"', body)
        self.assertIn("No exact-window total stored", body)
        self.assertIn('name="search_type"', body)
        self.assertNotIn("source=umami&amp;search_type=", body)

    def test_dashboard_visuals_state_exact_metric_definitions(self):
        result = {
            "site_ids": ["one", "two"],
            "rows": [
                {"site_id": "one", "metric": "umami.pageviews", "value": 80, "unit": "count"},
                {"site_id": "two", "metric": "umami.pageviews", "value": 20, "unit": "count"},
                {"site_id": "one", "metric": "search.impressions", "value": 1000, "unit": "count"},
                {"site_id": "one", "metric": "search.clicks", "value": 25, "unit": "count"},
            ],
            "series": [{
                "site_id": "one", "metric": "umami.pageviews", "unit": "count",
                "points": [{"date": "2026-07-01", "value": 80}],
            }],
            "index_coverage": {"properties": [{
                "site_id": "one", "published_pages": 200,
                "indexed_pages": 150, "indexed_percentage": 75.0,
            }]},
        }

        html = _dashboard_visuals_html(result, {"one": "One", "two": "Two"})

        self.assertIn("Traffic by property", html)
        self.assertIn("80.0% share", html)
        self.assertNotIn("Daily attention", html)
        self.assertIn("Search performance", html)
        self.assertIn("<strong>1,000</strong>", html)
        self.assertIn("<b>2.50%</b> CTR", html)
        self.assertIn("Index coverage", html)
        self.assertIn("150 indexed / 200 pages", html)
        self.assertIn(">Indexed</span>", html)
        self.assertIn(">Not indexed</span>", html)

    def test_overview_cards_do_not_confuse_missing_prior_with_missing_current_value(self):
        result = {
            "summary_totals": {
                "umami.visits": {
                    "metric": "umami.visits", "source": "umami", "unit": "count",
                    "value": 215, "previous_value": None, "change_percent": None,
                    "coverage_status": "complete", "comparison_available": False,
                },
            },
            "index_coverage": {"properties": []},
        }

        html = _overview_cards_html(result)

        self.assertIn('data-metric="umami.visits" data-state="observed"', html)
        self.assertIn('<strong>215</strong><small>Umami</small>', html)
        self.assertNotIn("Prior unavailable", html)
        self.assertNotIn("Unknown", html)

    def test_geography_source_switch_refreshes_claims_and_limits_live_region_noise(self):
        self.store.upsert([total_point(
            client_id="example-client", site_id="example-site", source="umami",
            metric="umami.country-visits", unit="count",
            start=datetime(2026, 7, 1, tzinfo=UTC), end=datetime(2026, 7, 2, tzinfo=UTC),
            value=7, dimensions={"country_code": "US", "country_code_system": "iso-alpha2"},
        )])
        status, _headers, body = self.request(
            "/?report=summary&start=2026-07-01&end=2026-07-02"
        )

        self.assertEqual(status, 200)
        self.assertIn("Audience by country", body)
        self.assertIn("Umami visits", body)
        self.assertIn('aria-label="US: 7 Umami visits"', body)
        self.assertNotIn('id="geography-map"', body)

        script = self.request("/assets/app.js")[2]
        self.assertIn("function updateGeographyDisclosures(payload)", script)
        self.assertIn("title.textContent = `${payload.label} by geography", script)
        self.assertIn("sourceMetric.textContent = `${payload.label}; metric ${payload.metric}", script)
        self.assertIn("suppression.textContent = `Buckets below ${payload.suppression.threshold}", script)
        self.assertIn("methodology.textContent = payload.methodology", script)
        self.assertIn("county.textContent = payload.counties.reason", script)
        self.assertIn("region.textContent = payload.region_support.status", script)
        self.assertIn("updateGeographyDisclosures(payload);", script)
        self.assertIn("if (payload.source !== requestedSource)", script)
        self.assertIn("function setGeographyLoadingState(label, status, announcement)", script)
        self.assertIn("announcement.textContent = `${payload.label} geography loaded.`", script)

    def test_geography_api_and_local_map_assets_are_privacy_bounded(self):
        self.store.upsert([total_point(
            client_id="example-client", site_id="example-site", source="umami",
            metric="umami.country-visits", unit="count",
            start=datetime(2026, 7, 1, tzinfo=UTC), end=datetime(2026, 7, 2, tzinfo=UTC),
            value=7, dimensions={"country_code": "US", "country_code_system": "iso-alpha2"},
        )])
        status, _headers, body = self.request(
            "/api/v1/geography?report=summary&source=umami&start=2026-07-01&end=2026-07-02"
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["countries"][0]["code"], "US")
        self.assertEqual(payload["counties"]["status"], "unavailable")
        encoded = json.dumps(payload).casefold()
        self.assertNotIn('"ip":', encoded)
        self.assertNotIn('"visitor_id":', encoded)

        world_status, world_headers, world = self.request("/assets/maps/world-countries.geojson")
        us_status, us_headers, us = self.request("/assets/maps/us-counties.json")
        self.assertEqual((world_status, us_status), (200, 200))
        self.assertIn("application/geo+json", world_headers["Content-Type"])
        self.assertIn("application/json", us_headers["Content-Type"])
        self.assertIn('"FeatureCollection"', world)
        self.assertIn('"counties"', us)

    def test_dashboard_renders_decision_support_and_measurement_roadmap(self):
        status, _headers, body = self.request(
            "/?report=summary&start=2026-07-01&end=2026-07-02"
        )

        self.assertEqual(status, 200)
        self.assertNotIn("Decision summary", body)
        self.assertNotIn("What needs attention", body)
        self.assertNotIn("Measurement roadmap", body)
        self.assertNotIn("Qualified leads and revenue", body)
        self.assertIn("All properties", body)
        self.assertIn("Current window summary", body)
        self.assertIn("Page views over time", body)
        self.assertIn("Traffic by property", body)
        self.assertIn('<th scope="row" class="metric-name">Example Site</th>', body)
        self.assertNotIn('<caption class="sr-only">Per-site decision metrics and data coverage</caption>', body)
        self.assertNotIn('class="attention-severity">Review</p>', body)
        self.assertNotIn("no capability snapshot", body)
        self.assertNotIn('class="dashboard-primary"', body)
        self.assertNotIn('class="panel control-panel"', body)
        self.assertIn('class="panel data-details"', body)
        self.assertIn('style=line', body)
        self.assertIn('id="chart-live-status" role="status"', body)
        self.assertNotIn('id="chart-status" role="status"', body)
        payload = json.loads(self.request(
            "/api/v1/report?report=summary&start=2026-07-01&end=2026-07-02"
        )[2])
        self.assertIn("decision_support", payload)
        self.assertTrue(payload["decision_support"]["measurement_gaps"])
        self.assertEqual(
            payload["provider_comparisons"][0]["evidence_state"],
            "non_comparable",
        )
        csv_body = self.request(
            "/api/v1/report.csv?report=summary&start=2026-07-01&end=2026-07-02"
        )[2]
        self.assertIn("provider_comparison", csv_body)

    def test_report_api_projects_safe_operational_capabilities(self):
        self.store.save_capability(CapabilitySnapshot(
            "example-connection", "fixture",
            datetime(2026, 7, 2, tzinfo=UTC), True,
            ("private-resource",), ("traffic",), 30,
            ("Private provider detail",),
        ))

        payload = json.loads(self.request(
            "/api/v1/report?report=summary&start=2026-07-01&end=2026-07-02"
        )[2])
        support = payload["decision_support"]

        self.assertNotIn("connection_id", repr(support))
        self.assertNotIn("example-connection", repr(support))
        self.assertNotIn("private-resource", repr(support))
        self.assertNotIn("Private provider detail", repr(support))
        self.assertEqual(support["capabilities"][0]["warning_count"], 1)

    def test_portfolio_summary_does_not_pull_metrics_outside_report_definition(self):
        self.store.upsert([MetricPoint(
            "example-client", "example-site", "fixture", "umami.visits",
            "count", datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC), TimeGrain.DAY,
            Decimal("7"), (), Completeness.FINAL,
            datetime(2026, 7, 2, tzinfo=UTC),
        )])

        status, _headers, body = self.request(
            "/?report=summary&start=2026-07-01&end=2026-07-02"
        )

        self.assertEqual(status, 200)
        self.assertIn(
            '<article class="overview-metric" data-metric="umami.visits" data-state="unavailable"',
            body,
        )
        self.assertIn("No exact-window total stored", body)

    def test_complete_kpi_discloses_configured_subset_of_report_sites(self):
        result = {
            "subreport_id": None,
            "site_ids": ["one", "two"],
            "rows": [{
                "metric": "umami.pageviews", "site_id": "one",
                "source": "umami", "unit": "count", "value": 7,
            }],
            "summary_totals": {
                "umami.pageviews": {
                    "metric": "umami.pageviews", "source": "umami",
                    "unit": "count", "value": 7, "previous_value": None,
                    "change_percent": None, "coverage_status": "complete",
                    "covered_cells": 2, "expected_cells": 2,
                    "comparison_available": False,
                },
            },
            "coverage": {"by_site_source": [
                {
                    "site_id": "one", "source": "umami",
                    "metric_status": {"umami.pageviews": "complete"},
                },
                {
                    "site_id": "two", "source": "umami",
                    "metric_status": {"umami.pageviews": "not_configured"},
                },
            ]},
            "forms_pipeline": None,
            "decision_support": {"supporting_metrics": {}},
        }

        html = _summary_cards(result, ("umami.pageviews",))

        self.assertIn('<strong class="kpi-value">7</strong>', html)
        self.assertIn("Umami; scope 1 contributing site of 1 configured site", html)
        self.assertIn("1 of 2 report sites configured", html)

    def test_portfolio_summary_withholds_mixed_decision_sources(self):
        result = {
            "subreport_id": None,
            "summary_totals": {},
            "rows": [],
            "forms_pipeline": None,
            "decision_support": {"supporting_metrics": {
                "search.clicks": {
                    "metric": "search.clicks", "source": "mixed",
                    "unit": "count", "value": None, "previous_value": None,
                    "change_percent": None, "coverage_status": "complete",
                    "covered_cells": 2, "expected_cells": 2,
                    "observed": False,
                },
            }},
        }

        html = _summary_cards(result, ())

        self.assertIn('data-metric="search.clicks" data-state="withheld"', html)
        self.assertIn("Source conflict", html)
        self.assertIn("multiple actual provider sources", html)
        self.assertNotIn("search.clicks\" data-state=\"unknown", html)

    def test_decision_trends_are_neutral_unless_direction_has_meaning(self):
        neutral = _decision_badge({
            "id": "ga_events_per_session", "state": "observed",
            "change_percent": 12.5,
        })
        bounce_new = _decision_badge({
            "id": "umami_bounce_rate", "state": "observed",
            "change_state": "new", "change_percent": None,
        })

        self.assertIn('class="trend flat"', neutral)
        self.assertIn("+12.5%", neutral)
        self.assertIn('class="trend down"', bounce_new)

    def test_claim_integrity_helpers_keep_unknown_zero_and_mixed_source_distinct(self):
        unknown_coverage = _coverage_summary_html({
            "coverage": {"covered_cells": 0, "expected_cells": 0},
            "complete": True,
        })
        self.assertIn("Not measurable for this selection", unknown_coverage)
        self.assertNotIn("100%", unknown_coverage)
        self.assertEqual(
            _snapshot_status_text({"source_health": []}),
            "Stored snapshot · no feed evidence in this selection",
        )

        detail = _metrics_table({"rows": [
            {
                "metric": "search.clicks", "site_id": "one",
                "source": "search-console", "unit": "count", "value": 5,
                "previous_value": 0, "change_percent": None,
                "comparison_available": True, "coverage_status": "complete",
            },
            {
                "metric": "forms.submissions", "site_id": "one",
                "source": "cloudflare-forms", "unit": "count", "value": 0,
                "previous_value": 0, "change_percent": None,
                "comparison_available": True, "coverage_status": "complete",
            },
            {
                "metric": "umami.pageviews", "site_id": "one",
                "source": "umami", "unit": "count", "value": 4,
                "previous_value": None, "change_percent": None,
                "comparison_available": False, "coverage_status": "partial",
            },
        ]}, {"one": "One"})
        self.assertIn("New vs 0 prior", detail)
        self.assertIn("No change (0 vs 0)", detail)
        self.assertIn("Prior unavailable (coverage or source not comparable)", detail)

        mixed = {
            "site_id": None,
            "rows": [
                {"metric": "search.clicks", "site_id": "one", "source": "fixture", "unit": "count", "value": 2},
                {"metric": "search.clicks", "site_id": "one", "source": "search-console", "unit": "count", "value": 3},
            ],
            "coverage": {"by_site_source": [{
                "site_id": "one", "source": "search-console",
                "metric_status": {"search.clicks": "complete"},
                "configured_providers": ["fixture", "search-console"],
                "metric_evidence_providers": {"search.clicks": ["fixture", "search-console"]},
            }]},
        }
        performance = _performance_by_site_html(mixed, {"one": "One"})
        self.assertIn("Withheld", performance)
        self.assertIn("Multiple provider sources", performance)

        missing_observation = {
            "site_id": None,
            "site_ids": ["one"],
            "rows": [],
            "summary_totals": {"umami.visits": {"value": None}},
            "coverage": {"by_site_source": [{
                "site_id": "one", "source": "umami",
                "metric_status": {"umami.visits": "unavailable"},
                "configured_providers": ["umami"],
            }]},
        }
        missing_html = _performance_by_site_html(
            missing_observation, {"one": "One", "outside": "Outside"}
        )
        self.assertIn("No stored observation", missing_html)
        self.assertNotIn("Outside", missing_html)

    def test_search_console_click_copy_does_not_claim_visits(self):
        notes = [item[2] for item in (*PORTFOLIO_SUMMARY, *SEARCH_SUMMARY)]

        self.assertIn("Clicks recorded by Google Search Console", notes)
        self.assertFalse(any("visit" in note.casefold() for note in notes if "Click" in note))

    def test_decision_support_mobile_css_collapses_without_page_overflow(self):
        css = self.request("/assets/app.css")[2]

        self.assertIn(".dashboard-header", css)
        self.assertIn(".overview-metrics", css)
        self.assertIn(".trend-grid", css)
        self.assertIn(".source-reading-grid", css)
        self.assertIn(".property-field select", css)
        self.assertIn("@media(max-width:620px)", css)
        self.assertIn("@media(prefers-reduced-motion:reduce)", css)
        self.assertIn("overflow-x:auto", css)
        self.assertIn("align-items:start", css)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", css)

    def test_attention_panel_prioritizes_three_items_and_collapses_the_rest(self):
        body = _attention_html({"attention_items": [
            {
                "severity": "review",
                "title": f"Issue {index}",
                "evidence": f"Evidence {index}",
                "action": f"Action {index}",
            }
            for index in range(1, 5)
        ]})

        self.assertIn('class="attention-more"', body)
        self.assertIn("Show 2 more items", body)
        self.assertIn('<ol class="attention-list" start="3">', body)
        self.assertLess(body.index("Issue 2"), body.index('class="attention-more"'))
        self.assertGreater(body.index("Issue 3"), body.index('class="attention-more"'))

    def test_plot_builder_keeps_visual_controls_open(self):
        status, _headers, body = self.request(
            "/?report=summary&view=plot&source=umami&metric=umami.pageviews&start=2026-07-01&end=2026-07-02"
        )

        self.assertEqual(status, 200)
        self.assertIn('class="panel control-panel" open', body)
        self.assertNotIn('class="dashboard-primary"', body)
        self.assertIn('style=line', body)

    def test_series_and_plot_requests_skip_provider_route_comparison_work(self):
        route_metrics = {"google.page-path-views", "umami.route-pageviews"}
        paths = (
            "/api/v1/series?report=summary&source=umami&metric=umami.pageviews"
            "&start=2026-07-01&end=2026-07-02",
            "/?report=summary&view=plot&source=umami&metric=umami.pageviews"
            "&start=2026-07-01&end=2026-07-02",
        )
        for path in paths:
            calls = []
            original_query = self.store.query

            def query_spy(**kwargs):
                calls.append(tuple(kwargs["metric_ids"]))
                return original_query(**kwargs)

            with self.subTest(path=path), patch.object(
                self.store, "query", side_effect=query_spy
            ), patch.object(
                ReportService,
                "_provider_comparisons",
                side_effect=AssertionError("provider comparison executed"),
            ):
                self.assertEqual(self.request(path)[0], 200)
            self.assertFalse(
                route_metrics.intersection(
                    metric for call in calls for metric in call
                )
            )

        report_payload = json.loads(self.request(
            "/api/v1/report?report=summary&start=2026-07-01&end=2026-07-02"
        )[2])
        self.assertEqual(len(report_payload["provider_comparisons"]), 1)

    def test_invalid_host_is_rejected(self): self.assertEqual(self.request("/healthz", "attacker.invalid")[0], 400)

    def test_health_identifies_the_exact_build_and_schema(self):
        status, _headers, body = self.request("/healthz")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["version"], "0.2.0")
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

    def test_default_dashboard_uses_configured_maturity_lag(self):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                current = cls(2026, 7, 21, 14, 0, tzinfo=UTC)
                return current.astimezone(tz) if tz is not None else current.replace(tzinfo=None)

        root = Path(self.temporary.name)
        fixture = root / "lag-fixture.json"
        write_fixture(fixture)
        text = config_text(root / "lag-state.db", fixture).replace(
            "default_window_days = 30\n[[reports.subreports]]",
            "default_window_days = 7\ndefault_end_lag_days = 1\n[[reports.subreports]]",
            1,
        )
        path = root / "lag-platform.toml"
        path.write_text(text, encoding="utf-8")
        config = load_config(path)
        store = SQLiteMetricStore(root / "lag-state.db")
        store.initialize()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(config, store))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("boho_analytics_platform.time_window.datetime", FrozenDateTime):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3
                )
                connection.request("GET", "/")
                response = connection.getresponse()
                body = response.read().decode()
                connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn('name="start" value="2026-07-13"', body)
            self.assertIn('name="end" value="2026-07-20"', body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

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

    def test_generated_all_sites_forms_submit_without_a_blank_query(self):
        status, _headers, body = self.request("/?report=summary&start=2026-07-01&end=2026-07-02")
        self.assertEqual(status, 200)
        self.assertIn('<option value="all" selected>All properties</option>', body)
        self.assertEqual(
            self.request("/?report=summary&start=2026-07-01&end=2026-07-02&metric=umami.pageviews&site=all")[0],
            200,
        )

    def test_site_controls_and_series_api_follow_configured_source_bindings(self):
        root = Path(self.temporary.name)
        fixture = root / "availability.json"; write_fixture(fixture)
        text = config_text(
            root / "availability.db", fixture, provider="cloudflare-forms",
            options='account_id = "account"\ndatabase_id = "database"',
        )
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
            "[[bindings]]",
            f'''[[connections]]
id = "umami-connection"
provider = "umami"
credential_ref = "none:test"
[connections.options]
base_url = "https://analytics.example.invalid"
[[bindings]]''',
            1,
        )
        text = text.replace(
            "[[reports]]",
            '''[[bindings]]
site_id = "second-site"
connection_id = "umami-connection"
resource_type = "website"
resource_id = "second-demo"
metric_groups = ["traffic"]
[[reports]]''',
            1,
        )
        text = text.replace('site_ids = ["example-site"]', 'site_ids = ["example-site", "second-site"]')
        text = text.replace(
            'metric_ids = ["umami.pageviews", "forms.submissions", "forms.inbox-deliveries"]',
            'metric_ids = ["umami.pageviews", "forms.submissions"]',
            1,
        )
        config_path = root / "availability.toml"; config_path.write_text(text, encoding="utf-8")
        config = load_config(config_path)
        store = SQLiteMetricStore(root / "availability.db"); store.initialize()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(config, store))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()

        def request(path):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", path, headers={"Host": "127.0.0.1"})
            response = connection.getresponse(); body = response.read().decode(); status = response.status
            connection.close(); return status, body

        try:
            status, forms_body = request("/?report=summary&subreport=forms&start=2026-07-01&end=2026-07-02")
            self.assertEqual(status, 200)
            self.assertIn('<option value="second-site">Second Site</option>', forms_body)
            self.assertNotIn('name="subreport"', forms_body)
            status, overview_body = request(
                "/?report=summary&metric=forms.submissions&start=2026-07-01&end=2026-07-02"
            )
            self.assertEqual(status, 200)
            self.assertIn('<option value="second-site">Second Site</option>', overview_body)
            unsupported_dashboard = (
                "/?report=summary&metric=forms.submissions&site=second-site"
                "&start=2026-07-01&end=2026-07-02"
            )
            status, error_body = request(unsupported_dashboard)
            self.assertEqual(status, 400)
            self.assertIn("invalid report request", error_body)
            supported_dashboard = (
                "/?report=summary&metric=umami.pageviews&site=second-site"
                "&start=2026-07-01&end=2026-07-02"
            )
            self.assertEqual(request(supported_dashboard)[0], 200)
            status, plot_body = request("/?report=summary&view=plot&source=cloudflare-forms&metric=forms.submissions&start=2026-07-01&end=2026-07-02")
            self.assertEqual(status, 200)
            self.assertIn('value="second-site" data-sources="umami" hidden disabled', plot_body)
            invalid = "/api/v1/series?report=summary&view=plot&source=cloudflare-forms&metric=forms.submissions&site=second-site&start=2026-07-01&end=2026-07-02"
            self.assertEqual(request(invalid)[0], 400)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)
        self.assertEqual(
            self.request("/?report=summary&view=plot&source=umami&metric=umami.pageviews&start=2026-07-01&end=2026-07-02&site=all")[0],
            200,
        )

    def test_unknown_and_duplicate_analytics_query_parameters_are_rejected(self):
        for path in (
            "/?report=summary&siet=example-site",
            "/api/v1/report?report=summary&report=summary",
            "/api/v1/series?report=summary&metric=umami.pageviews&metirc=umami.pageviews",
            "/api/v1/series?report=summary&metric=umami.pageviews&view=bogus",
            "/?report=summary&compare=perhaps",
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
        series_payload = json.loads(json_body)
        self.assertEqual(series_payload["unit"], "count")
        self.assertEqual(series_payload["available_search_types"], ["web"])
        self.assertIsNone(series_payload["search_type"])

        status, headers, csv_body = self.request(api.replace("/series?", "/series.csv?"))
        self.assertEqual(status, 200); self.assertIn("period,date,metric,site_id,source,unit,value", csv_body)
        self.assertIn("current,2026-07-01,umami.pageviews", csv_body)
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_script_asset_is_same_origin_and_invalid_plot_source_is_rejected(self):
        status, _headers, body = self.request("/assets/app.js")
        self.assertEqual(status, 200); self.assertIn("ResizeObserver", body); self.assertIn("fetch(canvas.dataset.seriesUrl", body)
        self.assertIn("contiguousSegments", body)
        self.assertIn("comparison unavailable", body)
        self.assertIn("formatMetricValue", body)
        self.assertIn("formatTooltipValue", body)
        self.assertIn("tooltip.replaceChildren", body)
        self.assertIn('event.key === "ArrowLeft"', body)
        self.assertIn('event.key === "Escape"', body)
        self.assertIn("formatCountValue", body)
        self.assertIn("niceCountStep", body)
        self.assertIn('if (unit === "count") return formatCountValue(number)', body)
        self.assertIn('unit === "count" ? Math.max(1, Math.round(max / niceCountStep(max))) : 4', body)
        self.assertIn("maximumFractionDigits: 0", body)
        self.assertIn('[1e9, "B"]', body)
        self.assertIn('[1e6, "M"]', body)
        self.assertIn('[1e3, "K"]', body)
        self.assertIn('unit === "ratio"', body)
        self.assertIn('unit === "seconds"', body)
        self.assertIn('unit === "bytes"', body)
        self.assertIn("lowerIsBetter", body)
        self.assertIn('const effectiveStyle = lowerIsBetter ? "line" : payload.style', body)
        self.assertIn('canvas.dataset.areaFill === "true"', body)
        self.assertIn("function fillArea(item, color)", body)
        self.assertIn('configuredColor("--chart-grid", "#e4e4de")', body)
        self.assertIn('const defaultColors = ["#e86d3d"', body)
        css_body = self.request("/assets/app.css")[2]
        self.assertIn("--chart-area-alpha:.1", css_body)
        self.assertIn("--series-1:#37e6ff", css_body)
        self.assertIn(".chart-tooltip{", css_body)
        self.assertIn(".dashboard-app .chart-tooltip{", css_body)
        self.assertIn('selectedSource !== "search-console"', body)
        self.assertIn("updateStyleOptions", body)
        self.assertIn("canvas.onpointermove = null", body)
        self.assertIn("if (legend) legend.replaceChildren()", body)
        self.assertNotIn("createLinearGradient", body)
        self.assertIn('requestedSource === "search-console"', body)
        self.assertIn('url.searchParams.delete("search_type")', body)
        invalid = "/api/v1/series?report=summary&source=search-console&metric=umami.pageviews&start=2026-07-01&end=2026-07-02"
        self.assertEqual(self.request(invalid)[0], 400)

        unsafe_position_style = (
            "/api/v1/series?report=summary&view=plot&source=search-console"
            "&metric=search.position&style=area&search_type=web"
            "&start=2026-07-01&end=2026-07-02"
        )
        self.assertEqual(self.request(unsafe_position_style)[0], 400)

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
        self.assertEqual(payload["comparison_series"], [])
        self.assertTrue(any("comparison" in warning.casefold() for warning in payload["warnings"]))

        csv_path = path.replace("/series?", "/series.csv?")
        self.assertNotIn("comparison,", self.request(csv_path)[2])

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

    def test_series_materializes_only_query_proven_zero_dates(self):
        window = QueryWindow(
            datetime(2026, 7, 2, tzinfo=UTC),
            datetime(2026, 7, 3, tzinfo=UTC),
            "UTC",
        )
        run = self.store.start_run(
            "example-connection",
            "example-site",
            binding_key="example-site:example-connection:website:demo",
            source="fixture",
            window=window,
        )
        self.store.finish_run(run, "success", result_kind="empty")

        path = (
            "/api/v1/series?report=summary&source=umami&metric=umami.pageviews"
            "&compare=1&start=2026-07-02&end=2026-07-03"
        )
        status, _headers, body = self.request(path)
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(payload["series"][0]["points"], [{"date": "2026-07-02", "value": 0}])
        self.assertTrue(payload["comparison_available"])
        self.assertTrue(any("query-proven" in warning for warning in payload["warnings"]))

    def test_summary_cards_do_not_silently_switch_visitor_definitions(self):
        result = {
            "subreport_id": "traffic",
            "rows": [{
                "metric": "google.sessions", "site_id": "example-site",
                "source": "google-analytics", "unit": "count", "value": 7,
                "previous_value": None,
            }],
            "summary_totals": {
                "google.sessions": {
                    "metric": "google.sessions", "source": "google-analytics",
                    "unit": "count", "aggregation": "sum", "value": 7,
                    "previous_value": None, "change_percent": None,
                    "coverage_status": "complete", "comparison_available": False,
                }
            },
            "forms_pipeline": None,
        }
        html = _summary_cards(result, ("umami.visitors", "google.sessions"))
        self.assertIn("Umami visitors", html)
        self.assertIn("GA4 sessions", html)
        self.assertIn('data-metric="umami.visitors" data-state="unknown"', html)
        self.assertIn('data-metric="google.sessions" data-state="observed"', html)

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

    def test_discover_position_is_explicitly_unavailable(self):
        result = {
            "subreport_id": "search",
            "search_type": "discover",
            "rows": [],
            "summary_totals": {
                "search.position": {
                    "metric": "search.position",
                    "source": "search-console",
                    "unit": "position",
                    "aggregation": "weighted",
                    "value": None,
                    "previous_value": None,
                    "change_percent": None,
                    "coverage_status": "unavailable",
                    "comparison_available": False,
                }
            },
            "forms_pipeline": None,
        }
        html = _summary_cards(result, ("search.position",))
        self.assertIn("Not available", html)
        self.assertIn("Unsupported surface", html)
        self.assertIn("does not define average position", html)

        search_binding = self.config.bindings[0]
        object.__setattr__(search_binding, "options", {
            "route_analytics": {"search_types": ["all"]},
        })
        object.__setattr__(
            self.config.reports[0],
            "metric_ids",
            (*self.config.reports[0].metric_ids, "search.position"),
        )
        status, _headers, body = self.request(
            "/api/v1/series?report=summary&view=plot"
            "&source=search-console&metric=search.position"
            "&style=line&search_type=discover"
            "&start=2026-07-01&end=2026-07-02"
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["series"], [])
        self.assertIn(
            "does not define average position", payload["availability_note"]
        )
        self.assertIn(payload["availability_note"], payload["warnings"])

    def test_route_observations_hide_legacy_discover_position_zeroes(self):
        start = datetime(2026, 7, 1, tzinfo=UTC)

        def route_position(search_type, value):
            return MetricPoint(
                "example-client", "example-site", "search-console",
                "search.route-position", "position", start,
                start + timedelta(days=1), TimeGrain.DAY, Decimal(value),
                tuple(sorted((
                    ("aggregation", "byPage"),
                    ("data_state", "final"),
                    ("observation_scope", "page"),
                    ("provider_date", "2026-07-01"),
                    ("provider_timezone", "America/Los_Angeles"),
                    ("route", "/story/"),
                    ("search_type", search_type),
                ))),
                Completeness.FINAL, start + timedelta(hours=12),
            )

        self.store.upsert([
            route_position("discover", "0"),
            route_position("web", "4"),
        ])
        status, _headers, body = self.request(
            "/api/v1/route-observations?report=summary&site=example-site"
            "&source=search-console&metric=search.route-position"
            "&start=2026-07-01&end=2026-07-02"
        )

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["total_rows"], 1)
        self.assertEqual(payload["rows"][0]["value"], "4")
        self.assertEqual(
            payload["rows"][0]["dimensions"]["search_type"], "web"
        )

        status, _headers, body = self.request(
            "/api/v1/route-observations.csv?report=summary&site=example-site"
            "&source=search-console&metric=search.route-position"
            "&start=2026-07-01&end=2026-07-02"
        )
        self.assertEqual(status, 200)
        exported = list(csv.DictReader(io.StringIO(body)))
        self.assertEqual(len(exported), 1)
        self.assertEqual(
            json.loads(exported[0]["dimensions"])["search_type"], "web"
        )
        self.assertNotIn("discover", body)

    def test_forms_cards_preserve_unknown_pipeline_values(self):
        body = self.request("/?report=summary&subreport=forms&start=2026-07-01&end=2026-07-02")[2]
        self.assertNotIn('data-metric="forms.pending"', body)
        self.assertNotIn('data-metric="forms.failed"', body)
        self.assertIn('data-metric="umami.pageviews"', body)

    def test_partial_card_and_metric_table_disclose_coverage(self):
        body = self.request("/?report=summary&start=2026-06-30&end=2026-07-02")[2]
        self.assertIn('data-metric="umami.pageviews" data-state="partial"', body)
        self.assertIn("partial coverage", body)
        self.assertIn("<th>Coverage</th>", body)

    def test_weighted_accessible_chart_uses_window_aggregate_not_sum_of_daily_rates(self):
        result = {
            "series": [{
                "metric": "search.ctr", "site_id": "example-site", "source": "search-console", "unit": "ratio",
                "points": [{"date": "2026-07-01", "value": .1}, {"date": "2026-07-02", "value": .2}],
            }],
            "rows": [{
                "metric": "search.ctr", "site_id": "example-site", "source": "search-console", "unit": "ratio",
                "value": .15, "coverage_status": "complete",
            }],
        }
        body = _chart_html(result, "search.ctr", {"example-site": "Example Site"})
        self.assertIn("15.0% window aggregate", body)
        self.assertNotIn("30.0% total", body)
        daily_unique = {
            "series": [{
                "metric": "umami.daily-visitors", "site_id": "example-site",
                "source": "umami", "unit": "count",
                "points": [
                    {"date": "2026-07-01", "value": 8},
                    {"date": "2026-07-02", "value": 7},
                ],
            }],
            "rows": [],
        }
        unique_body = _chart_html(
            daily_unique, "umami.daily-visitors", {"example-site": "Example Site"}
        )
        self.assertIn("Daily uniques · no window-unique total", unique_body)
        self.assertNotIn("15 window total", unique_body)

    def test_subreport_navigation_and_form_preserve_scope_and_window(self):
        body = self.request("/?report=summary&subreport=forms&start=2026-07-01&end=2026-07-02")[2]
        self.assertNotIn('name="subreport"', body)
        self.assertNotIn("subreport=forms", body)
        self.assertIn('name="start" value="2026-07-01"', body)
        self.assertIn('name="end" value="2026-07-02"', body)
        self.assertIn("All properties", body)

    def test_site_scope_is_validated_and_preserved(self):
        path = "/?report=summary&site=example-site&start=2026-07-01&end=2026-07-02"
        status, _headers, body = self.request(path)
        self.assertEqual(status, 200); self.assertIn('<option value="example-site" selected>Example Site</option>', body)
        self.assertIn('<h1>Example Site</h1>', body)
        self.assertIn("site=example-site", body)
        self.assertNotIn("Traffic by property", body)
        self.assertIn("Page views over time", body)
        self.assertIn("Index coverage", body)
        self.assertEqual(self.request("/?report=summary&site=unknown&start=2026-07-01&end=2026-07-02")[0], 400)

    def test_early_valid_dashboard_window_does_not_underflow_quick_presets(self):
        status, _headers, body = self.request("/?report=summary&start=0001-01-02&end=0001-01-03")
        self.assertEqual(status, 200)
        self.assertNotIn(">365 days</a>", body)

    def test_favicon_is_same_origin_and_available(self):
        status, headers, body = self.request("/favicon.svg")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/svg+xml")
        self.assertIn("<svg", body)

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
        self.assertIn('data-graph-zoom-out aria-label="Zoom out"', body)
        self.assertIn('data-graph-zoom-in aria-label="Zoom in"', body)
        self.assertIn("data-graph-zoom-reset", body)
        self.assertIn("data-graph-viewport", body)
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

    def test_site_graph_script_preserves_bounded_pointer_and_keyboard_interactions(self):
        status, _headers, script = self.request("/assets/app.js")

        self.assertEqual(status, 200)
        self.assertIn("svg.getScreenCTM?.()", script)
        self.assertIn("svg.createSVGPoint()", script)
        self.assertIn('zoomInButton?.addEventListener("click"', script)
        self.assertIn('zoomOutButton?.addEventListener("click"', script)
        self.assertIn('zoomResetButton?.addEventListener("click", fitView)', script)
        self.assertIn('addEventListener("wheel"', script)
        self.assertIn("Math.min(Math.max(scale", script)
        self.assertIn("svg.setPointerCapture?.(event.pointerId)", script)
        self.assertIn("if (!dragState.moved)", script)
        self.assertIn('addEventListener("lostpointercapture", finishDrag)', script)
        self.assertIn('addEventListener("pointerleave", event =>', script)
        self.assertIn("svg.releasePointerCapture(pointerId)", script)
        self.assertIn('stage.dataset.graphSuppressClick = "true"', script)
        self.assertIn('event.key === "Enter" || event.key === " "', script)
        self.assertIn('event.key === "Escape" && (isPinned()', script)
        self.assertIn("if (!stage) return", script)
        self.assertIn("if (!inspector || !svg || (!nodes.length && !edges.length)) return", script)

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
