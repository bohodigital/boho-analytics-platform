from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from boho_analytics_platform.config import load_config
from boho_analytics_platform.connectors.cloudflare import CloudflareAnalyticsConnector
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

    def test_umami_parses_daily_series_and_summary(self):
        config = self.config("umami", 'base_url = "https://analytics.example.invalid"')
        http = QueueHttp([{"pageviews": [{"x": "2026-07-01T00:00:00Z", "y": 10}], "sessions": [{"x": 1782864000000, "y": 7}]}, {"visitors": 5, "visits": 7, "bounces": 2, "totaltime": 120}])
        points = list(UmamiConnector(config, http).collect(config.connections[0], MemoryCredentialLease({"token": b"test"}), SyncRequest(config.bindings[0], self.window, ())))
        self.assertEqual(len(points), 6); self.assertEqual({point.start.date().isoformat() for point in points[:2]}, {"2026-07-01"})
        self.assertIn("startAt=", http.calls[0][1]); self.assertTrue(all("test" not in call[1] for call in http.calls))

    def test_cloudflare_parses_adaptive_groups_without_rescaling(self):
        config = self.config("cloudflare")
        response = {"data": {"viewer": {"zones": [{"httpRequestsAdaptiveGroups": [{"dimensions": {"date": "2026-07-01"}, "count": 100, "sum": {"visits": 8, "edgeResponseBytes": 2048}}]}]}}}
        points = list(CloudflareAnalyticsConnector(config, QueueHttp([response])).collect(config.connections[0], MemoryCredentialLease({"api_token": b"test"}), SyncRequest(config.bindings[0], self.window, ())))
        self.assertEqual({point.metric for point in points}, {"cloudflare.requests", "cloudflare.visits", "cloudflare.bytes"}); self.assertEqual(points[0].value, 100)
        self.assertTrue(all(point.completeness is Completeness.PROVISIONAL for point in points))
        self.assertTrue(all(point.dimensions == () for point in points))

        store = SQLiteMetricStore(self.root / "cloudflare-identity.db"); store.initialize()
        historical = replace(points[0], completeness=Completeness.FINAL)
        store.upsert([historical]); store.upsert([points[0]])
        stored = store.query(client_id=points[0].client_id, site_ids=(points[0].site_id,),
            metric_ids=(points[0].metric,), window=self.window)
        self.assertEqual(len(stored), 1)
        self.assertIs(stored[0].completeness, Completeness.PROVISIONAL)

    def test_cloudflare_probe_queries_the_configured_zone_and_discloses_sampling(self):
        config = self.config("cloudflare")
        http = QueueHttp([{"data": {"viewer": {"zones": [{"httpRequestsAdaptiveGroups": []}]}}}])
        snapshot = CloudflareAnalyticsConnector(config, http).probe(
            config.connections[0], MemoryCredentialLease({"api_token": b"test"}))
        self.assertEqual(snapshot.resources, ("demo",))
        self.assertTrue(any("adaptive" in warning.casefold() for warning in snapshot.warnings))
        self.assertEqual(http.calls[0][0], "POST")
        self.assertEqual(http.calls[0][3]["variables"]["zone"], "demo")

    def test_cloudflare_probe_fails_when_the_configured_zone_is_not_accessible(self):
        config = self.config("cloudflare")
        connector = CloudflareAnalyticsConnector(
            config, QueueHttp([{"data": {"viewer": {"zones": []}}}]))
        with self.assertRaisesRegex(ValueError, "configured zone"):
            connector.probe(config.connections[0], MemoryCredentialLease({"api_token": b"test"}))

    def test_ga4_uses_exclusive_window_as_inclusive_api_end(self):
        config = self.config("google-analytics")
        response = {"metricHeaders": [{"name": "sessions"}], "rows": [{"dimensionValues": [{"value": "20260701"}], "metricValues": [{"value": "9"}]}]}
        http = QueueHttp([response]); points = list(GoogleAnalyticsConnector(config, http).collect(config.connections[0], MemoryCredentialLease({"access_token": b"test"}), SyncRequest(config.bindings[0], self.window, ())))
        self.assertEqual(points[0].metric, "google.sessions"); self.assertEqual(http.calls[0][3]["dateRanges"][0]["endDate"], "2026-07-02")

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

    def test_search_console_parses_metrics_and_uses_encoded_site(self):
        config = self.config("search-console"); binding = config.bindings[0]
        object.__setattr__(binding, "resource_id", "sc-domain:example.com")
        response = {"rows": [{"keys": ["2026-07-01"], "clicks": 4, "impressions": 20, "ctr": 0.2, "position": 3.1}]}
        http = QueueHttp([response]); points = list(SearchConsoleConnector(config, http).collect(config.connections[0], MemoryCredentialLease({"access_token": b"test"}), SyncRequest(binding, self.window, ())))
        self.assertEqual(len(points), 4); self.assertIn("sc-domain%3Aexample.com", http.calls[0][1])

    def test_search_console_queries_pacific_dates_without_changing_fact_identity(self):
        config = self.config("search-console", timezone="America/Chicago")
        zone = ZoneInfo("America/Chicago")
        window = QueryWindow(datetime(2026, 7, 1, tzinfo=zone), datetime(2026, 7, 2, tzinfo=zone), "America/Chicago")
        response = {"rows": [{"keys": ["2026-07-01"], "clicks": 1}]}
        http = QueueHttp([response])
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
            {"rows": [{"keys": ["2026-07-01"], "clicks": 1}]}])).collect(
                config.connections[0], MemoryCredentialLease({"access_token": b"test"}),
                SyncRequest(config.bindings[0], window, ())))
        store = SQLiteMetricStore(self.root / "search-console-report.db"); store.initialize(); store.upsert(points)
        report = ReportService(config, store).render("summary", window)
        self.assertEqual(report["summary_totals"]["search.clicks"]["value"], 1)

    def test_search_console_probe_queries_the_configured_property(self):
        config = self.config("search-console")
        binding = config.bindings[0]
        object.__setattr__(binding, "resource_id", "sc-domain:example.com")
        http = QueueHttp([{"rows": []}])
        snapshot = SearchConsoleConnector(config, http).probe(
            config.connections[0], MemoryCredentialLease({"access_token": b"test"}))
        self.assertEqual(snapshot.resources, ("sc-domain:example.com",))
        self.assertTrue(any("America/Los_Angeles" in warning for warning in snapshot.warnings))
        self.assertIn("sc-domain%3Aexample.com", http.calls[0][1])
        self.assertEqual(http.calls[0][3]["rowLimit"], 1)


if __name__ == "__main__": unittest.main()
