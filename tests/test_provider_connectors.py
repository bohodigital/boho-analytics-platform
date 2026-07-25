from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from boho_analytics_platform.config import load_config
from boho_analytics_platform.connectors.cloudflare import CloudflareAnalyticsConnector
from boho_analytics_platform.connectors.common import normalize_route, sanitize_referrer
from boho_analytics_platform.connectors.google import GoogleAnalyticsConnector, SearchConsoleConnector
from boho_analytics_platform.connectors.umami import UmamiConnector
from boho_analytics_platform.contracts import SyncRequest
from boho_analytics_platform.credentials import MemoryCredentialLease
from boho_analytics_platform.models import Completeness, QueryWindow
from boho_analytics_platform.reporting import ReportService
from boho_analytics_platform.storage import SQLiteMetricStore
from support import config_text, write_fixture


class QueueHttp:
    def __init__(self, responses): self.responses = list(responses); self.calls = []
    def request(self, method, url, *, headers=None, body=None):
        self.calls.append((method, url, headers, body)); return self.responses.pop(0)


class ProviderConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup); self.root = Path(self.temporary.name)
        self.fixture = self.root / "fixture.json"; write_fixture(self.fixture)
        self.window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 3, tzinfo=UTC), "UTC")

    def config(self, provider, options="", *, timezone="UTC"):
        text = config_text(self.root / f"{provider}.db", self.fixture, provider=provider, options=options)
        text = text.replace('timezone = "UTC"', f'timezone = "{timezone}"')
        path = self.root / f"{provider}.toml"; path.write_text(text, encoding="utf-8")
        return load_config(path)

    def test_route_normalizer_keeps_only_safe_internal_route_information(self):
        self.assertEqual(
            normalize_route(
                "https://EXAMPLE.com/products/%7Eblue/?keep=one&drop=two#section",
                "https://example.com",
                allow_query_parameters=("keep",),
            ),
            "/products/~blue?keep=one",
        )
        self.assertEqual(normalize_route("/products/", "https://example.com"), "/products")
        self.assertIsNone(normalize_route("/invite/jane@example.com", "https://example.com"))
        self.assertIsNone(normalize_route("/call/415-555-1212", "https://example.com"))
        self.assertIsNone(normalize_route("/bad%ZZ", "https://example.com"))
        for unsafe in (
            "/a%00b",
            "/%2f%2foutside.example/path",
            "/%252f%252foutside.example/path",
            "/%2e%2e/private",
            "https:%2f%2foutside.example/path",
            "/https%3A%2F%2Foutside.example/path",
            r"/safe\..\private",
        ):
            with self.subTest(unsafe=unsafe):
                self.assertIsNone(normalize_route(unsafe, "https://example.com"))
        self.assertEqual(
            normalize_route("/products?variant=customer@example.com", "https://example.com", allow_query_parameters=("variant",)),
            "/products",
        )
        self.assertIsNone(normalize_route("https://outside.example/path?email=a@example.com", "https://example.com"))
        self.assertIsNone(normalize_route("https://example.com/private/form", "https://example.com", exclusions=("/private",)))
        self.assertEqual(
            sanitize_referrer("https://outside.example/form?email=a@example.com", "https://example.com", approved_domains=("outside.example",)),
            {"referrer_domain": "outside.example"},
        )

    @staticmethod
    def cloudflare_settings(*, enabled=True, max_duration=86400, not_older_than=691200):
        return {"data": {"viewer": {"zones": [{"settings": {
            "httpRequestsAdaptiveGroups": {
                "enabled": enabled,
                "maxDuration": max_duration,
                "maxNumberOfFields": 40,
                "maxPageSize": 10000,
                "notOlderThan": not_older_than,
            }
        }, "httpRequestsAdaptiveGroups": []}]}}}

    def test_umami_parses_daily_series_and_summary(self):
        config = self.config("umami", 'base_url = "https://analytics.example.invalid"')
        http = QueueHttp([
            {"pageviews": [{"x": "2026-07-01T00:00:00Z", "y": 10}], "sessions": [{"x": 1782864000000, "y": 7}]},
            {"visitors": 5, "visits": 7, "bounces": 2, "totaltime": 120},
            [{"name": "us", "visits": 6}, {"name": "gb", "visits": 1}],
            [{"country": "us", "name": "CA", "visits": 4}, {"country": "us", "name": "TX", "visits": 2}],
        ])
        points = list(UmamiConnector(config, http).collect(config.connections[0], MemoryCredentialLease({"token": b"test"}), SyncRequest(config.bindings[0], self.window, ())))
        self.assertEqual(len(points), 10); self.assertEqual({point.start.date().isoformat() for point in points[:2]}, {"2026-07-01"})
        country = next(point for point in points if point.metric == "umami.country-visits" and dict(point.dimensions)["country_code"] == "US")
        region = next(point for point in points if point.metric == "umami.region-visits" and dict(point.dimensions)["region_code"] == "CA")
        self.assertEqual(country.value, 6); self.assertEqual(dict(country.dimensions)["country_code_system"], "iso-alpha2")
        self.assertEqual(dict(region.dimensions)["country_code"], "US")
        self.assertIn("type=country", http.calls[2][1]); self.assertIn("type=region", http.calls[3][1])
        self.assertIn("startAt=", http.calls[0][1]); self.assertTrue(all("test" not in call[1] for call in http.calls))

    def test_umami_route_probe_requires_a_valid_available_date_range(self):
        config = self.config("umami", 'base_url = "https://analytics.example.invalid"')
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {"enabled": True}})
        snapshot = UmamiConnector(config, QueueHttp([
            [{"id": "demo"}], {"startDate": "2026-01-01T00:00:00Z", "endDate": "2026-07-01T00:00:00Z"},
        ])).probe(config.connections[0], MemoryCredentialLease({"token": b"test"}))
        self.assertTrue(any("available date range" in warning for warning in snapshot.warnings))
        self.assertIn("umami.route-visits", snapshot.metric_groups)
        self.assertNotIn("umami.page-title-visits", snapshot.metric_groups)

    def test_cloudflare_parses_adaptive_groups_without_rescaling(self):
        config = self.config("cloudflare")
        response = {"data": {"viewer": {"zones": [{"httpRequestsAdaptiveGroups": [{"dimensions": {"date": "2026-07-01"}, "count": 100, "sum": {"visits": 8, "edgeResponseBytes": 2048}}]}]}}}
        geography = {"data": {"viewer": {"zones": [{"httpRequestsAdaptiveGroups": [{"dimensions": {"date": "2026-07-01", "clientCountryName": "US"}, "sum": {"visits": 8}}]}]}}}
        http = QueueHttp([response, geography])
        points = list(CloudflareAnalyticsConnector(config, http).collect(config.connections[0], MemoryCredentialLease({"api_token": b"test"}), SyncRequest(config.bindings[0], self.window, ())))
        self.assertEqual({point.metric for point in points}, {"cloudflare.requests", "cloudflare.visits", "cloudflare.bytes", "cloudflare.country-visits"}); self.assertEqual(points[0].value, 100)
        self.assertTrue(all(point.completeness is Completeness.PROVISIONAL for point in points))
        geo_point = next(point for point in points if point.metric == "cloudflare.country-visits")
        self.assertEqual(dict(geo_point.dimensions), {"country_code": "US", "country_code_system": "iso-alpha2"})
        self.assertIn("clientCountryName", http.calls[1][3]["query"])

        store = SQLiteMetricStore(self.root / "cloudflare-identity.db"); store.initialize()
        historical = replace(points[0], completeness=Completeness.FINAL)
        store.upsert([historical]); store.upsert([points[0]])
        stored = store.query(client_id=points[0].client_id, site_ids=(points[0].site_id,),
            metric_ids=(points[0].metric,), window=self.window)
        self.assertEqual(len(stored), 1)
        self.assertIs(stored[0].completeness, Completeness.PROVISIONAL)

    def test_cloudflare_probe_queries_the_configured_zone_and_discloses_sampling(self):
        config = self.config("cloudflare")
        http = QueueHttp([self.cloudflare_settings()])
        snapshot = CloudflareAnalyticsConnector(config, http).probe(
            config.connections[0], MemoryCredentialLease({"api_token": b"test"}))
        self.assertEqual(snapshot.resources, ("demo",))
        self.assertEqual(snapshot.max_lookback_days, 8)
        self.assertEqual(len(snapshot.warnings), 3)
        self.assertTrue(any("adaptive" in warning.casefold() for warning in snapshot.warnings))
        self.assertTrue(any("1 day" in warning for warning in snapshot.warnings))
        self.assertNotIn("demo", " ".join(snapshot.warnings))
        self.assertEqual(http.calls[0][0], "POST")
        self.assertEqual(http.calls[0][3]["variables"]["zone"], "demo")
        self.assertIn("start", http.calls[0][3]["variables"])
        self.assertIn("end", http.calls[0][3]["variables"])
        self.assertIn("settings", http.calls[0][3]["query"])
        self.assertIn("maxNumberOfFields", http.calls[0][3]["query"])
        self.assertIn("requestSource", http.calls[0][3]["query"])

    def test_cloudflare_probe_uses_conservative_limits_across_resources(self):
        config = self.config("cloudflare")
        config = replace(config, bindings=config.bindings + (
            replace(config.bindings[0], resource_id="secondary"),
        ))
        http = QueueHttp([
            self.cloudflare_settings(max_duration=172800, not_older_than=777600),
            self.cloudflare_settings(max_duration=86400, not_older_than=176400),
        ])

        snapshot = CloudflareAnalyticsConnector(config, http).probe(
            config.connections[0], MemoryCredentialLease({"api_token": b"test"}))

        self.assertEqual(snapshot.resources, ("demo", "secondary"))
        self.assertEqual(snapshot.max_lookback_days, 2)
        self.assertTrue(any("1 day" in warning for warning in snapshot.warnings))
        self.assertEqual(
            [call[3]["variables"]["zone"] for call in http.calls],
            ["demo", "secondary"],
        )

    def test_cloudflare_probe_rejects_disabled_or_invalid_settings(self):
        config = self.config("cloudflare")
        cases = (
            (self.cloudflare_settings(enabled=False), "unavailable"),
            (self.cloudflare_settings(max_duration=True), "maxDuration"),
            (self.cloudflare_settings(not_older_than=None), "notOlderThan"),
            ({"data": {"viewer": {"zones": [{
                "settings": {}, "httpRequestsAdaptiveGroups": [],
            }]}}}, "did not report"),
        )
        for response, message in cases:
            with self.subTest(message=message):
                connector = CloudflareAnalyticsConnector(config, QueueHttp([response]))
                with self.assertRaisesRegex(ValueError, message):
                    connector.probe(
                        config.connections[0],
                        MemoryCredentialLease({"api_token": b"test"}),
                    )

    def test_cloudflare_probe_fails_when_the_configured_zone_is_not_accessible(self):
        config = self.config("cloudflare")
        connector = CloudflareAnalyticsConnector(
            config, QueueHttp([{"data": {"viewer": {"zones": []}}}]))
        with self.assertRaisesRegex(ValueError, "configured zone"):
            connector.probe(config.connections[0], MemoryCredentialLease({"api_token": b"test"}))

    def test_ga4_uses_exclusive_window_as_inclusive_api_end(self):
        config = self.config("google-analytics")
        response = {"metricHeaders": [{"name": "sessions"}], "rows": [{"dimensionValues": [{"value": "20260701"}], "metricValues": [{"value": "9"}]}]}
        geography = {"metadata": {"timeZone": "UTC"}, "metricHeaders": [{"name": "sessions"}], "rows": [{"dimensionValues": [{"value": "20260701"}, {"value": "US"}, {"value": "California"}], "metricValues": [{"value": "7"}]}]}
        http = QueueHttp([response, geography]); points = list(GoogleAnalyticsConnector(config, http).collect(config.connections[0], MemoryCredentialLease({"access_token": b"test"}), SyncRequest(config.bindings[0], self.window, ())))
        self.assertEqual(points[0].metric, "google.sessions"); self.assertEqual(http.calls[0][3]["dateRanges"][0]["endDate"], "2026-07-02")
        geo_point = next(point for point in points if point.metric == "google.region-sessions")
        self.assertEqual(dict(geo_point.dimensions)["region_name"], "California")
        self.assertEqual([item["name"] for item in http.calls[1][3]["dimensions"]], ["date", "countryId", "region"])

    def test_ga4_probe_runs_a_property_query_and_validates_timezone(self):
        config = self.config("google-analytics")
        http = QueueHttp([{"metadata": {"timeZone": "UTC"}}])
        snapshot = GoogleAnalyticsConnector(config, http).probe(
            config.connections[0], MemoryCredentialLease({"access_token": b"test"}))
        self.assertEqual(snapshot.resources, ("demo",))
        self.assertEqual(snapshot.warnings, ())
        self.assertIn("properties/demo:runReport", http.calls[0][1])

    def test_ga4_fails_closed_on_reported_property_timezone_mismatch(self):
        config = self.config("google-analytics", timezone="America/Chicago")
        response = {"metadata": {"timeZone": "America/Los_Angeles"}, "rows": []}
        connector = GoogleAnalyticsConnector(config, QueueHttp([response]))
        with self.assertRaisesRegex(ValueError, "property timezone"):
            list(connector.collect(config.connections[0], MemoryCredentialLease({"access_token": b"test"}),
                SyncRequest(config.bindings[0], self.window, ())))

    def test_ga4_probe_warns_when_property_timezone_is_not_disclosed(self):
        config = self.config("google-analytics")
        snapshot = GoogleAnalyticsConnector(config, QueueHttp([{}])).probe(
            config.connections[0], MemoryCredentialLease({"access_token": b"test"}))
        self.assertTrue(any("timezone" in warning.casefold() for warning in snapshot.warnings))

    def test_ga4_route_probe_validates_metadata_and_reports_route_capabilities(self):
        config = self.config("google-analytics")
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {"enabled": True}})
        dimensions = (
            "date", "landingPagePlusQueryString", "pagePathPlusQueryString", "pageTitle",
            "sessionDefaultChannelGroup", "fullReferrer", "eventName",
        )
        metrics = (
            "sessions", "screenPageViews", "engagedSessions", "userEngagementDuration",
            "keyEvents", "eventCount",
        )
        snapshot = GoogleAnalyticsConnector(config, QueueHttp([
            {"metadata": {"timeZone": "UTC"}},
            {
                "dimensions": [{"apiName": item} for item in dimensions],
                "metrics": [{"apiName": item} for item in metrics],
            },
        ])).probe(config.connections[0], MemoryCredentialLease({"access_token": b"test"}))
        self.assertIn("google.landing-page-sessions", snapshot.metric_groups)
        self.assertNotIn("google.page-title-views", snapshot.metric_groups)
        self.assertNotIn("google.configured-event-count", snapshot.metric_groups)

    def test_ga4_route_observations_are_paginated_and_drop_unapproved_referrer_urls(self):
        config = self.config("google-analytics")
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {
            "enabled": True, "page_size": 10, "max_pages": 2,
            "approved_referrer_domains": ["search.example"],
            "ga4_dimensions": ["title", "channel", "referrer"],
        }})
        aggregate = {"rows": []}
        geography = {"metadata": {"timeZone": "UTC"}, "metricHeaders": [{"name": "sessions"}], "rows": []}
        route_row = {"rowCount": "1", "rows": [{"dimensionValues": [{"value": "20260701"}, {"value": "/about/?email=a@example.com"}], "metricValues": [{"value": "2"}]}]}
        title_row = {"rowCount": "1", "rows": [{"dimensionValues": [{"value": "20260701"}, {"value": "About us"}], "metricValues": [{"value": "2"}]}]}
        channel_row = {"rowCount": "1", "rows": [{"dimensionValues": [{"value": "20260701"}, {"value": "Organic Search"}], "metricValues": [{"value": "2"}]}]}
        referrer_row = {"rowCount": "1", "rows": [{"dimensionValues": [{"value": "20260701"}, {"value": "https://outside.example/form?email=a@example.com"}], "metricValues": [{"value": "2"}]}]}
        http = QueueHttp([
            aggregate, geography,
            route_row, route_row, route_row, route_row, route_row,
            channel_row, referrer_row, title_row,
        ])
        points = list(GoogleAnalyticsConnector(config, http).collect(
            config.connections[0], MemoryCredentialLease({"access_token": b"test"}),
            SyncRequest(config.bindings[0], self.window, ())))
        self.assertIn("google.landing-page-sessions", {point.metric for point in points})
        self.assertNotIn("google.referrer-sessions", {point.metric for point in points})
        landing = next(point for point in points if point.metric == "google.landing-page-sessions")
        self.assertEqual(dict(landing.dimensions), {"route": "/about"})
        self.assertTrue(all("email=a@example.com" not in str(point.dimensions) for point in points))
        self.assertEqual(http.calls[2][3]["dimensions"][1]["name"], "landingPagePlusQueryString")
        self.assertEqual(http.calls[2][3]["offset"], "0")

    def test_route_observations_reject_out_of_bounds_and_partial_day_windows(self):
        config = self.config("google-analytics")
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {"enabled": True, "max_days": 1}})
        long_window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 3, tzinfo=UTC), "UTC")
        with self.assertRaisesRegex(ValueError, "max_days"):
            list(GoogleAnalyticsConnector(config, QueueHttp([])).collect(
                config.connections[0], MemoryCredentialLease({"access_token": b"test"}),
                SyncRequest(config.bindings[0], long_window, ())))
        partial = QueryWindow(datetime(2026, 7, 1, 1, tzinfo=UTC), datetime(2026, 7, 2, 1, tzinfo=UTC), "UTC")
        with self.assertRaisesRegex(ValueError, "whole site-local days"):
            list(SearchConsoleConnector(config, QueueHttp([])).collect(
                config.connections[0], MemoryCredentialLease({"access_token": b"test"}),
                SyncRequest(config.bindings[0], partial, ())))

    def test_search_console_parses_metrics_and_uses_encoded_site(self):
        config = self.config("search-console"); binding = config.bindings[0]
        object.__setattr__(binding, "resource_id", "sc-domain:example.com")
        response = {"rows": [{"keys": ["2026-07-01"], "clicks": 4, "impressions": 20, "ctr": 0.2, "position": 3.1}]}
        geography = {"rows": [{"keys": ["2026-07-01", "usa"], "clicks": 4}]}
        http = QueueHttp([response, geography]); points = list(SearchConsoleConnector(config, http).collect(config.connections[0], MemoryCredentialLease({"access_token": b"test"}), SyncRequest(binding, self.window, ())))
        self.assertEqual(len(points), 5); self.assertIn("sc-domain%3Aexample.com", http.calls[0][1])
        geo_point = next(point for point in points if point.metric == "search.country-clicks")
        self.assertEqual(dict(geo_point.dimensions), {"country_code": "USA", "country_code_system": "iso-alpha3"})
        self.assertEqual(http.calls[1][3]["dimensions"], ["date", "country"])

    def test_search_console_queries_pacific_dates_without_changing_fact_identity(self):
        config = self.config("search-console", timezone="America/Chicago")
        zone = ZoneInfo("America/Chicago")
        window = QueryWindow(datetime(2026, 7, 1, tzinfo=zone), datetime(2026, 7, 2, tzinfo=zone), "America/Chicago")
        response = {"rows": [{"keys": ["2026-07-01"], "clicks": 1}]}
        http = QueueHttp([response, {"rows": []}])
        points = list(SearchConsoleConnector(config, http).collect(
            config.connections[0], MemoryCredentialLease({"access_token": b"test"}),
            SyncRequest(config.bindings[0], window, ())))
        query = http.calls[0][3]
        self.assertEqual((query["startDate"], query["endDate"]), ("2026-06-30", "2026-07-01"))
        self.assertEqual(getattr(points[0].start.tzinfo, "key", None), "America/Chicago")
        self.assertEqual(points[0].dimensions, ())

    def test_search_console_chicago_fact_survives_store_and_report_window(self):
        config = self.config("search-console", timezone="America/Chicago")
        object.__setattr__(config.reports[0], "metric_ids", ("search.clicks",))
        zone = ZoneInfo("America/Chicago")
        window = QueryWindow(datetime(2026, 7, 1, tzinfo=zone), datetime(2026, 7, 2, tzinfo=zone), "America/Chicago")
        points = list(SearchConsoleConnector(config, QueueHttp([
            {"rows": [{"keys": ["2026-07-01"], "clicks": 1}]}, {"rows": []}])).collect(
                config.connections[0], MemoryCredentialLease({"access_token": b"test"}),
                SyncRequest(config.bindings[0], window, ())))
        store = SQLiteMetricStore(self.root / "search-console-report.db"); store.initialize(); store.upsert(points)
        report = ReportService(config, store).render("summary", window)
        self.assertEqual(report["summary_totals"]["search.clicks"]["value"], 1)

    def test_search_console_probe_queries_the_configured_property(self):
        config = self.config("search-console")
        binding = config.bindings[0]
        object.__setattr__(binding, "resource_id", "sc-domain:example.com")
        object.__setattr__(binding, "options", {"route_analytics": {"enabled": True}})
        http = QueueHttp([{"rows": []}])
        snapshot = SearchConsoleConnector(config, http).probe(
            config.connections[0], MemoryCredentialLease({"access_token": b"test"}))
        self.assertEqual(snapshot.resources, ("sc-domain:example.com",))
        self.assertTrue(any("America/Los_Angeles" in warning for warning in snapshot.warnings))
        self.assertIn("sc-domain%3Aexample.com", http.calls[0][1])
        self.assertEqual(http.calls[0][3]["rowLimit"], 1)
        self.assertIn("search.route-clicks", snapshot.metric_groups)

    def test_search_console_route_observations_use_page_pagination_and_unknown_completeness(self):
        config = self.config("search-console")
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {
            "enabled": True, "page_size": 10, "max_pages": 2,
        }})
        http = QueueHttp([
            {"rows": [{"keys": ["2026-07-01"], "clicks": 2}]}, {"rows": []},
            {"rows": [{"keys": ["2026-07-01", "https://example.com/blog/?email=a@example.com"], "clicks": 2, "impressions": 10, "ctr": 0.2, "position": 3.0}]},
        ])
        points = list(SearchConsoleConnector(config, http).collect(
            config.connections[0], MemoryCredentialLease({"access_token": b"test"}),
            SyncRequest(config.bindings[0], self.window, ())))
        route = next(point for point in points if point.metric == "search.route-clicks")
        self.assertEqual(dict(route.dimensions), {
            "data_state": "final",
            "observation_scope": "page",
            "route": "/blog",
        })
        self.assertIs(route.completeness, Completeness.UNKNOWN)
        self.assertEqual(http.calls[2][3]["dimensions"], ["date", "page"])
        self.assertEqual(http.calls[2][3]["startRow"], 0)

    def test_umami_route_observations_issue_bounded_daily_path_queries(self):
        config = self.config("umami", 'base_url = "https://analytics.example.invalid"')
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {
            "enabled": True, "max_days": 2, "page_size": 10, "max_pages": 2,
        }})
        responses = [
            {"pageviews": [], "sessions": []}, {"visitors": 0, "visits": 0, "bounces": 0, "totaltime": 0}, [], [],
            {"startDate": "2026-01-01T00:00:00Z", "endDate": "2026-07-03T00:00:00Z"},
        ]
        responses.extend([[{"name": "/pricing/?email=a@example.com", "visits": 3}]] + [[]] * 5)
        http = QueueHttp(responses)
        points = list(UmamiConnector(config, http).collect(
            config.connections[0], MemoryCredentialLease({"token": b"test"}),
            SyncRequest(config.bindings[0], self.window, ())))
        route = next(point for point in points if point.metric == "umami.route-visits")
        self.assertEqual(dict(route.dimensions), {"route": "/pricing"})
        self.assertIn("/daterange", http.calls[4][1])
        self.assertIn("type=path", http.calls[5][1])
        self.assertIn("startAt=", http.calls[5][1])
        self.assertFalse(any(
            f"type={item}" in call[1]
            for call in http.calls[5:]
            for item in ("title", "channel", "domain", "device", "country")
        ))


if __name__ == "__main__": unittest.main()
