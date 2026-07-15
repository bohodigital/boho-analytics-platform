from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from boho_analytics_platform.config import load_config
from boho_analytics_platform.connectors.cloudflare import CloudflareAnalyticsConnector
from boho_analytics_platform.connectors.google import GoogleAnalyticsConnector, SearchConsoleConnector
from boho_analytics_platform.connectors.umami import UmamiConnector
from boho_analytics_platform.contracts import SyncRequest
from boho_analytics_platform.credentials import MemoryCredentialLease
from boho_analytics_platform.models import QueryWindow
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

    def config(self, provider, options=""):
        path = self.root / f"{provider}.toml"; path.write_text(config_text(self.root / f"{provider}.db", self.fixture, provider=provider, options=options), encoding="utf-8")
        return load_config(path)

    def test_umami_parses_daily_series_and_summary(self):
        config = self.config("umami", 'base_url = "https://analytics.example.invalid"')
        http = QueueHttp([{"pageviews": [{"x": 1782864000000, "y": 10}], "sessions": [{"x": 1782864000000, "y": 7}]}, {"visitors": 5, "visits": 7, "bounces": 2, "totaltime": 120}])
        points = list(UmamiConnector(config, http).collect(config.connections[0], MemoryCredentialLease({"token": b"test"}), SyncRequest(config.bindings[0], self.window, ())))
        self.assertEqual(len(points), 6); self.assertIn("startAt=", http.calls[0][1]); self.assertTrue(all("test" not in call[1] for call in http.calls))

    def test_cloudflare_parses_adaptive_groups_without_rescaling(self):
        config = self.config("cloudflare")
        response = {"data": {"viewer": {"zones": [{"httpRequestsAdaptiveGroups": [{"dimensions": {"date": "2026-07-01"}, "count": 100, "sum": {"visits": 8, "edgeResponseBytes": 2048}}]}]}}}
        points = list(CloudflareAnalyticsConnector(config, QueueHttp([response])).collect(config.connections[0], MemoryCredentialLease({"api_token": b"test"}), SyncRequest(config.bindings[0], self.window, ())))
        self.assertEqual({point.metric for point in points}, {"cloudflare.requests", "cloudflare.visits", "cloudflare.bytes"}); self.assertEqual(points[0].value, 100)

    def test_ga4_uses_exclusive_window_as_inclusive_api_end(self):
        config = self.config("google-analytics")
        response = {"metricHeaders": [{"name": "sessions"}], "rows": [{"dimensionValues": [{"value": "20260701"}], "metricValues": [{"value": "9"}]}]}
        http = QueueHttp([response]); points = list(GoogleAnalyticsConnector(config, http).collect(config.connections[0], MemoryCredentialLease({"access_token": b"test"}), SyncRequest(config.bindings[0], self.window, ())))
        self.assertEqual(points[0].metric, "google.sessions"); self.assertEqual(http.calls[0][3]["dateRanges"][0]["endDate"], "2026-07-02")

    def test_search_console_parses_metrics_and_uses_encoded_site(self):
        config = self.config("search-console"); binding = config.bindings[0]
        object.__setattr__(binding, "resource_id", "sc-domain:example.com")
        response = {"rows": [{"keys": ["2026-07-01"], "clicks": 4, "impressions": 20, "ctr": 0.2, "position": 3.1}]}
        http = QueueHttp([response]); points = list(SearchConsoleConnector(config, http).collect(config.connections[0], MemoryCredentialLease({"access_token": b"test"}), SyncRequest(binding, self.window, ())))
        self.assertEqual(len(points), 4); self.assertIn("sc-domain%3Aexample.com", http.calls[0][1])


if __name__ == "__main__": unittest.main()
