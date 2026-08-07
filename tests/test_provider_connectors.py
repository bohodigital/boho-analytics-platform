from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from types import SimpleNamespace

from boho_analytics_platform.config import load_config, route_analytics_options
from boho_analytics_platform.connectors.cloudflare import CloudflareAnalyticsConnector
from boho_analytics_platform.connectors.common import (
    aggregate_dimension_values,
    nonnegative_integral_count,
    normalize_route,
    sanitize_referrer,
    site_local_daily_bounds,
)
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
        self.calls.append((method, url, headers, body))
        response = self.responses.pop(0)
        if (
            isinstance(response, dict)
            and isinstance(body, dict)
            and body.get("returnPropertyQuota") is True
            and "dimensionHeaders" not in response
            and "metricHeaders" not in response
        ):
            response = {
                **response,
                "dimensionHeaders": [
                    {"name": item["name"]} for item in body["dimensions"]
                ],
                "metricHeaders": [
                    {"name": item["name"]} for item in body["metrics"]
                ],
            }
        return response


def umami_stats(*, pageviews=0, visitors=0, visits=0, bounces=0, totaltime=0):
    return {
        "pageviews": pageviews,
        "visitors": visitors,
        "visits": visits,
        "bounces": bounces,
        "totaltime": totaltime,
    }


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
        for unsafe_dimension in (
            "/products?campaign=test",
            "/products#details",
            "https://example.com/products",
            "relative/path",
            "/bad\u0085route",
            "/bad%C2%85route",
        ):
            with self.subTest(path_only=unsafe_dimension):
                self.assertIsNone(
                    normalize_route(
                        unsafe_dimension,
                        "https://example.com",
                        path_only=True,
                    )
                )
        self.assertEqual(
            normalize_route("/products", "https://example.com", path_only=True),
            "/products",
        )

        for unsafe_identity_route in (
            "/session/abcdefghijklmnop",
            "/session/abcdefghijkl.mnop",
            "/session/abc.def.ghi.jkl",
            "/sessions/0123456789abcdef",
            "/resource/550e8400e29b41d4a716446655440000",
            "/token/abcdefghijklmnop==",
            "/token/abcd%2Fefghijklmnopq==",
        ):
            with self.subTest(unsafe_identity_route=unsafe_identity_route):
                self.assertIsNone(
                    normalize_route(
                        unsafe_identity_route,
                        "https://example.com",
                        path_only=True,
                    )
                )
        self.assertEqual(
            normalize_route(
                "/guides/abcdefghijklmnop",
                "https://example.com",
                path_only=True,
            ),
            "/guides/abcdefghijklmnop",
        )
        self.assertEqual(
            normalize_route(
                "/resources/article-alpha",
                "https://example.com",
                path_only=True,
            ),
            "/resources/article-alpha",
        )
        for lexical_route in (
            "/session/appointment-booking",
            "/resource/article-alpha",
        ):
            with self.subTest(lexical_route=lexical_route):
                self.assertEqual(
                    normalize_route(
                        lexical_route,
                        "https://example.com",
                        path_only=True,
                    ),
                    lexical_route,
                )
        for opaque_route in (
            "/session/550e8400-e29b-41d4-a716-446655440000",
            "/session/0123456789abcdef",
            "/session/deadbeef-deadbeef",
            "/token/eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.signature",
            "/token/YWJjZGVmZ2hpamtsbW5vcA==",
            "/token/YWJjZGVmZ2hpamtsbW5vcA",
            "/token/abcdefghijk-lmnopqrs",
        ):
            with self.subTest(opaque_route=opaque_route):
                self.assertIsNone(
                    normalize_route(
                        opaque_route,
                        "https://example.com",
                        path_only=True,
                    )
                )

    def test_pageview_count_domain_is_nonnegative_and_integral(self):
        accepted = (
            (0, "0"),
            (1, "1"),
            (1.0, "1.0"),
            ("2", "2"),
            ("1e3", "1E+3"),
        )
        for value, expected in accepted:
            with self.subTest(accepted=value):
                self.assertEqual(str(nonnegative_integral_count(value)), expected)
        for value in (
            True,
            False,
            -1,
            -0.5,
            1.5,
            "-1",
            "1.5",
            "malformed",
            "NaN",
            "Infinity",
            "-Infinity",
            None,
        ):
            with self.subTest(rejected=value):
                self.assertIsNone(nonnegative_integral_count(value))
        for amplified in (
            "1e1000000",
            "9" * 5000,
            "1." + "0" * 5000,
            10 ** 1000,
        ):
            with self.subTest(amplified=type(amplified).__name__):
                self.assertIsNone(nonnegative_integral_count(amplified))

    def test_normalized_count_collisions_are_exact_and_rebounded(self):
        maximum = Decimal("9" * 38)
        rows, rejected = aggregate_dimension_values(
            [(date(2026, 7, 1), {"route": "/pricing"}, maximum)],
            integral=True,
        )
        self.assertFalse(rejected)
        self.assertEqual(rows[0][2], maximum)

        rows, rejected = aggregate_dimension_values(
            [
                (date(2026, 7, 1), {"route": "/pricing"}, Decimal("9e37")),
                (date(2026, 7, 1), {"route": "/pricing"}, Decimal("9e37")),
            ],
            integral=True,
        )
        self.assertEqual(rows, [])
        self.assertTrue(rejected)

    def test_headline_pageview_series_must_be_explicit(self):
        ga_config = self.config("google-analytics")
        with self.assertRaisesRegex(ValueError, "pageview series"):
            list(GoogleAnalyticsConnector(
                ga_config,
                QueueHttp([{
                    "metadata": {"timeZone": "UTC"},
                    "metricHeaders": [{"name": "sessions"}],
                    "rows": [],
                }]),
            ).collect(
                ga_config.connections[0],
                MemoryCredentialLease({"access_token": b"test"}),
                SyncRequest(ga_config.bindings[0], self.window, ()),
            ))

        umami_config = self.config(
            "umami", 'base_url = "https://analytics.example.invalid"'
        )
        with self.assertRaisesRegex(ValueError, "pageview series"):
            list(UmamiConnector(
                umami_config, QueueHttp([{"sessions": []}])
            ).collect(
                umami_config.connections[0],
                MemoryCredentialLease({"token": b"test"}),
                SyncRequest(umami_config.bindings[0], self.window, ()),
            ))

    def test_site_local_daily_bounds_reject_ambiguous_midnight_fold(self):
        timezone = "America/Havana"
        exact = QueryWindow(
            datetime(2026, 11, 1, 4, tzinfo=UTC),
            datetime(2026, 11, 2, 5, tzinfo=UTC),
            "UTC",
        )
        start, end = site_local_daily_bounds(exact, timezone)
        self.assertEqual(end.astimezone(UTC) - start.astimezone(UTC), timedelta(hours=25))
        self.assertEqual(start.fold, 0)

        clipped = QueryWindow(
            datetime(2026, 11, 1, 5, tzinfo=UTC),
            datetime(2026, 11, 2, 5, tzinfo=UTC),
            "UTC",
        )
        with self.assertRaisesRegex(ValueError, "whole site-local days"):
            site_local_daily_bounds(clipped, timezone)

        search_config = self.config("search-console", timezone=timezone)
        object.__setattr__(search_config.bindings[0], "options", {
            "route_analytics": {"enabled": True}
        })
        with self.assertRaisesRegex(ValueError, "whole site-local days"):
            list(SearchConsoleConnector(
                search_config, QueueHttp([])
            ).collect(
                search_config.connections[0],
                MemoryCredentialLease({"access_token": b"test"}),
                SyncRequest(search_config.bindings[0], clipped, ()),
            ))

    def test_provider_headlines_use_exact_site_local_daily_window(self):
        timezone = "Asia/Tokyo"
        window = QueryWindow(
            datetime(2026, 6, 30, 15, tzinfo=UTC),
            datetime(2026, 7, 1, 15, tzinfo=UTC),
            "UTC",
        )
        ga_config = self.config("google-analytics", timezone=timezone)
        ga_http = QueueHttp([
            {
                "metadata": {"timeZone": timezone},
                "metricHeaders": [{"name": "screenPageViews"}],
                "rows": [],
            },
            {
                "metadata": {"timeZone": timezone},
                "metricHeaders": [{"name": "sessions"}],
                "rows": [],
            },
        ])
        list(GoogleAnalyticsConnector(ga_config, ga_http).collect(
            ga_config.connections[0],
            MemoryCredentialLease({"access_token": b"test"}),
            SyncRequest(ga_config.bindings[0], window, ()),
        ))
        self.assertEqual(
            ga_http.calls[0][3]["dateRanges"],
            [{"startDate": "2026-07-01", "endDate": "2026-07-01"}],
        )
        self.assertEqual(
            ga_http.calls[1][3]["dateRanges"],
            [{"startDate": "2026-07-01", "endDate": "2026-07-01"}],
        )

        umami_config = self.config(
            "umami", 'base_url = "https://analytics.example.invalid"',
            timezone=timezone,
        )
        umami_http = QueueHttp([
            {"pageviews": [{"x": "2026-06-30T15:00:00Z", "y": 1}], "sessions": []},
            umami_stats(pageviews=1),
            [],
            [],
        ])
        points = list(UmamiConnector(umami_config, umami_http).collect(
            umami_config.connections[0],
            MemoryCredentialLease({"token": b"test"}),
            SyncRequest(umami_config.bindings[0], window, ()),
        ))
        self.assertIn("timezone=Asia%2FTokyo", umami_http.calls[0][1])
        pageview = next(point for point in points if point.metric == "umami.pageviews")
        self.assertEqual(pageview.start.date().isoformat(), "2026-07-01")
        self.assertEqual(getattr(pageview.start.tzinfo, "key", None), timezone)

        partial = QueryWindow(
            window.start + timedelta(hours=1),
            window.end + timedelta(hours=1),
            "UTC",
        )
        for connector, config, credential in (
            (GoogleAnalyticsConnector(ga_config, QueueHttp([])), ga_config,
             MemoryCredentialLease({"access_token": b"test"})),
            (UmamiConnector(umami_config, QueueHttp([])), umami_config,
             MemoryCredentialLease({"token": b"test"})),
        ):
            with self.subTest(provider=connector.provider):
                with self.assertRaisesRegex(ValueError, "whole site-local days"):
                    list(connector.collect(
                        config.connections[0], credential,
                        SyncRequest(config.bindings[0], partial, ()),
                    ))

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
            umami_stats(pageviews=10, visitors=5, visits=7, bounces=2, totaltime=120),
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
        response = {"metricHeaders": [{"name": "sessions"}, {"name": "screenPageViews"}], "rows": [{"dimensionValues": [{"value": "20260701"}], "metricValues": [{"value": "9"}, {"value": "11"}]}]}
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
        zone = ZoneInfo("America/Chicago")
        window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=zone),
            datetime(2026, 7, 3, tzinfo=zone),
            "America/Chicago",
        )
        response = {"metadata": {"timeZone": "America/Los_Angeles"}, "rows": []}
        connector = GoogleAnalyticsConnector(config, QueueHttp([response]))
        with self.assertRaisesRegex(ValueError, "property timezone"):
            list(connector.collect(config.connections[0], MemoryCredentialLease({"access_token": b"test"}),
                SyncRequest(config.bindings[0], window, ())))

    def test_ga4_probe_warns_when_property_timezone_is_not_disclosed(self):
        config = self.config("google-analytics")
        snapshot = GoogleAnalyticsConnector(config, QueueHttp([{}])).probe(
            config.connections[0], MemoryCredentialLease({"access_token": b"test"}))
        self.assertTrue(any("timezone" in warning.casefold() for warning in snapshot.warnings))

    def test_ga4_route_probe_validates_metadata_and_reports_route_capabilities(self):
        config = self.config("google-analytics")
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {"enabled": True}})
        dimensions = (
            "date", "landingPagePlusQueryString", "pagePath", "pageTitle",
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
        aggregate = {"metricHeaders": [{"name": "screenPageViews"}], "rows": []}
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

    def test_ga4_page_path_views_use_path_only_and_expose_incomplete_pagination(self):
        config = self.config("google-analytics")
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {
            "enabled": True, "page_size": 1, "max_pages": 1,
        }})
        aggregate = {
            "metadata": {"timeZone": "UTC"},
            "metricHeaders": [{"name": "screenPageViews"}],
            "rows": [{
                "dimensionValues": [{"value": "20260701"}],
                "metricValues": [{"value": "7"}],
            }],
        }
        geography = {
            "metadata": {"timeZone": "UTC"},
            "metricHeaders": [{"name": "sessions"}],
            "rows": [],
        }
        route_row = {
            "rowCount": "2",
            "rows": [{
                "dimensionValues": [
                    {"value": "20260701"},
                    {"value": "/safe"},
                ],
                "metricValues": [{"value": "7"}],
            }],
        }
        http = QueueHttp([
            aggregate,
            geography,
            {"rowCount": 0, "rows": []},
            route_row,
            {"rowCount": 0, "rows": []},
            {"rowCount": 0, "rows": []},
            {"rowCount": 0, "rows": []},
        ])

        points = list(GoogleAnalyticsConnector(config, http).collect(
            config.connections[0],
            MemoryCredentialLease({"access_token": b"test"}),
            SyncRequest(config.bindings[0], self.window, ()),
        ))

        route = next(
            point for point in points
            if point.metric == "google.page-path-views"
        )
        page_path_call = http.calls[3]
        self.assertEqual(
            page_path_call[3]["dimensions"][1]["name"],
            "pagePath",
        )
        self.assertEqual(
            page_path_call[3]["metrics"][0]["name"],
            "screenPageViews",
        )
        self.assertEqual(dict(route.dimensions), {"route": "/safe"})
        self.assertIs(route.completeness, Completeness.UNKNOWN)

    def test_ga4_short_nonterminal_page_never_skips_to_final_coverage(self):
        config = self.config("google-analytics")
        http = QueueHttp([{
            "rowCount": 3,
            "rows": [{
                "dimensionValues": [
                    {"value": "20260701"},
                    {"value": "/first"},
                ],
                "metricValues": [{"value": "1"}],
            }],
        }])
        connector = GoogleAnalyticsConnector(config, http)

        rows, exhaustive = connector._ga_rows(
            "test",
            "demo",
            self.window.start.date(),
            self.window.end.date(),
            "pagePath",
            "screenPageViews",
            SimpleNamespace(page_size=2, max_pages=3),
        )

        self.assertEqual(len(rows), 1)
        self.assertFalse(exhaustive)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0][3]["offset"], "0")

    def test_ga4_missing_initial_row_count_never_proves_exhaustion(self):
        config = self.config("google-analytics")
        http = QueueHttp([{
            "rows": [{
                "dimensionValues": [
                    {"value": "20260701"},
                    {"value": "/safe"},
                ],
                "metricValues": [{"value": "1"}],
            }],
        }])
        connector = GoogleAnalyticsConnector(config, http)

        rows, exhaustive = connector._ga_rows(
            "test",
            "demo",
            self.window.start.date(),
            self.window.end.date(),
            "pagePath",
            "screenPageViews",
            SimpleNamespace(page_size=10, max_pages=2),
        )

        self.assertEqual(len(rows), 1)
        self.assertFalse(exhaustive)
        self.assertEqual(len(http.calls), 1)

    def test_ga4_page_contract_mismatches_never_prove_exhaustion(self):
        valid_row = {
            "dimensionValues": [
                {"value": "20260701"},
                {"value": "/safe"},
            ],
            "metricValues": [{"value": "1"}],
        }
        valid_headers = {
            "dimensionHeaders": [{"name": "date"}, {"name": "pagePath"}],
            "metricHeaders": [{"name": "screenPageViews"}],
        }
        mismatches = (
            {
                **valid_headers,
                "metricHeaders": [{"name": "sessions"}],
                "rowCount": 1,
                "rows": [valid_row],
            },
            {
                **valid_headers,
                "dimensionHeaders": [{"name": "date"}, {"name": "pageTitle"}],
                "rowCount": 1,
                "rows": [valid_row],
            },
            {
                **valid_headers,
                "rowCount": 1,
                "rows": [{
                    **valid_row,
                    "dimensionValues": [{"value": "20260701"}],
                }],
            },
            {
                **valid_headers,
                "rowCount": 1,
                "rows": [{
                    **valid_row,
                    "metricValues": [{"value": "1"}, {"value": "2"}],
                }],
            },
            {
                **valid_headers,
                "rowCount": 1,
                "rows": [{
                    **valid_row,
                    "metricValues": [{"value": "1e1000000"}],
                }],
            },
        )
        for response in mismatches:
            with self.subTest(response=response):
                rows, exhaustive = GoogleAnalyticsConnector(
                    self.config("google-analytics"),
                    QueueHttp([response]),
                )._ga_rows(
                    "test",
                    "demo",
                    self.window.start.date(),
                    self.window.end.date(),
                    "pagePath",
                    "screenPageViews",
                    SimpleNamespace(page_size=10, max_pages=1),
                )
                self.assertEqual(rows, [])
                self.assertFalse(exhaustive)


    def test_ga4_established_row_count_cannot_disappear_into_final_coverage(self):
        config = self.config("google-analytics")
        http = QueueHttp([{
            "rowCount": 3,
            "rows": [
                {
                    "dimensionValues": [
                        {"value": "20260701"},
                        {"value": "/first"},
                    ],
                    "metricValues": [{"value": "1"}],
                },
                {
                    "dimensionValues": [
                        {"value": "20260701"},
                        {"value": "/second"},
                    ],
                    "metricValues": [{"value": "1"}],
                },
            ],
        }, {
            "rows": [],
        }])
        connector = GoogleAnalyticsConnector(config, http)

        rows, exhaustive = connector._ga_rows(
            "test",
            "demo",
            self.window.start.date(),
            self.window.end.date(),
            "pagePath",
            "screenPageViews",
            SimpleNamespace(page_size=2, max_pages=3),
        )

        self.assertEqual(len(rows), 2)
        self.assertFalse(exhaustive)
        self.assertEqual(
            [call[3]["offset"] for call in http.calls],
            ["0", "2"],
        )

    def test_ga4_repeated_page_dimensions_never_prove_exhaustion(self):
        config = self.config("google-analytics")
        repeated_rows = [
            {
                "dimensionValues": [
                    {"value": "20260701"},
                    {"value": "/first"},
                ],
                "metricValues": [{"value": "1"}],
            },
            {
                "dimensionValues": [
                    {"value": "20260701"},
                    {"value": "/second"},
                ],
                "metricValues": [{"value": "1"}],
            },
        ]
        http = QueueHttp([
            {"rowCount": 4, "rows": repeated_rows},
            {"rowCount": 4, "rows": repeated_rows},
        ])

        rows, exhaustive = GoogleAnalyticsConnector(
            config, http
        )._ga_rows(
            "test",
            "demo",
            self.window.start.date(),
            self.window.end.date(),
            "pagePath",
            "screenPageViews",
            SimpleNamespace(page_size=2, max_pages=2),
        )

        self.assertEqual(len(rows), 2)
        self.assertFalse(exhaustive)
        self.assertEqual(
            [call[3]["offset"] for call in http.calls],
            ["0", "2"],
        )


    def test_ga4_page_path_rejections_make_returned_safe_facts_incomplete(self):
        config = self.config("google-analytics")
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {
            "enabled": True, "page_size": 20, "max_pages": 1,
        }})
        route_rows = {
            "rowCount": 8,
            "rows": [
                {
                    "dimensionValues": [
                        {"value": "20260701"},
                        {"value": "/safe"},
                    ],
                    "metricValues": [{"value": "2"}],
                },
                {
                    "dimensionValues": [
                        {"value": "20260701"},
                        {"value": "/safe/"},
                    ],
                    "metricValues": [{"value": "3"}],
                },
                {
                    "dimensionValues": [
                        {"value": "20260701"},
                        {"value": "/query?campaign=test"},
                    ],
                    "metricValues": [{"value": "3"}],
                },
                {
                    "dimensionValues": [
                        {"value": "20260701"},
                        {"value": "/fragment#details"},
                    ],
                    "metricValues": [{"value": "4"}],
                },
                {
                    "dimensionValues": [
                        {"value": "20260701"},
                        {"value": "https://outside.example/path"},
                    ],
                    "metricValues": [{"value": "5"}],
                },
                {
                    "dimensionValues": [
                        {"value": "20260701"},
                        {"value": "relative/path"},
                    ],
                    "metricValues": [{"value": "6"}],
                },
                {
                    "dimensionValues": [
                        {"value": "20260701"},
                        {"value": "/bad\u0085route"},
                    ],
                    "metricValues": [{"value": "7"}],
                },
                {
                    "dimensionValues": [
                        {"value": "20260701"},
                        {"value": "/bad%C2%85route"},
                    ],
                    "metricValues": [{"value": "8"}],
                },
            ],
        }
        http = QueueHttp([
            {"metadata": {"timeZone": "UTC"}, "metricHeaders": [{"name": "screenPageViews"}], "rows": []},
            {"metadata": {"timeZone": "UTC"}, "metricHeaders": [], "rows": []},
            {"rowCount": 0, "rows": []},
            route_rows,
            {"rowCount": 0, "rows": []},
            {"rowCount": 0, "rows": []},
            {"rowCount": 0, "rows": []},
        ])

        points = list(GoogleAnalyticsConnector(config, http).collect(
            config.connections[0],
            MemoryCredentialLease({"access_token": b"test"}),
            SyncRequest(config.bindings[0], self.window, ()),
        ))

        routes = [
            point for point in points
            if point.metric == "google.page-path-views"
        ]
        self.assertEqual(
            [dict(point.dimensions)["route"] for point in routes],
            ["/safe"],
        )
        self.assertTrue(
            all(point.completeness is Completeness.UNKNOWN for point in routes)
        )
        self.assertEqual(routes[0].value, 5)

    def test_ga4_page_paths_reject_out_of_window_counts_and_opaque_identity(self):
        config = self.config("google-analytics")
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {
            "enabled": True, "page_size": 20, "max_pages": 1,
        }})
        route_rows = {
            "rowCount": 5,
            "rows": [
                {
                    "dimensionValues": [
                        {"value": "20260701"},
                        {"value": "/safe"},
                    ],
                    "metricValues": [{"value": "2"}],
                },
                {
                    "dimensionValues": [
                        {"value": "20260703"},
                        {"value": "/outside"},
                    ],
                    "metricValues": [{"value": "9"}],
                },
                {
                    "dimensionValues": [
                        {"value": "20260701"},
                        {"value": "/negative"},
                    ],
                    "metricValues": [{"value": "-1"}],
                },
                {
                    "dimensionValues": [
                        {"value": "20260701"},
                        {"value": "/fractional"},
                    ],
                    "metricValues": [{"value": "1.5"}],
                },
                {
                    "dimensionValues": [
                        {"value": "20260701"},
                        {"value": "/session/abcdefghijklmnop"},
                    ],
                    "metricValues": [{"value": "4"}],
                },
            ],
        }
        http = QueueHttp([
            {"metadata": {"timeZone": "UTC"}, "metricHeaders": [{"name": "screenPageViews"}], "rows": []},
            {"metadata": {"timeZone": "UTC"}, "metricHeaders": [], "rows": []},
            {"rowCount": 0, "rows": []},
            route_rows,
            {"rowCount": 0, "rows": []},
            {"rowCount": 0, "rows": []},
            {"rowCount": 0, "rows": []},
        ])

        points = list(GoogleAnalyticsConnector(config, http).collect(
            config.connections[0],
            MemoryCredentialLease({"access_token": b"test"}),
            SyncRequest(config.bindings[0], self.window, ()),
        ))
        routes = [
            point for point in points
            if point.metric == "google.page-path-views"
        ]

        self.assertEqual(len(routes), 1)
        self.assertEqual(dict(routes[0].dimensions), {"route": "/safe"})
        self.assertEqual(routes[0].value, 2)
        self.assertIs(routes[0].completeness, Completeness.UNKNOWN)
        self.assertNotIn("abcdefghijklmnop", repr(points))

    def test_provider_headline_rows_must_be_complete_and_in_window(self):
        ga_config = self.config("google-analytics")
        malformed = {
            "metadata": {"timeZone": "UTC"},
            "metricHeaders": [
                {"name": "activeUsers"}, {"name": "screenPageViews"},
            ],
            "rows": [{
                "dimensionValues": [{"value": "20260701"}],
                "metricValues": [{"value": "7"}],
            }],
        }
        with self.assertRaisesRegex(ValueError, "headline row"):
            list(GoogleAnalyticsConnector(
                ga_config, QueueHttp([malformed])
            ).collect(
                ga_config.connections[0],
                MemoryCredentialLease({"access_token": b"test"}),
                SyncRequest(ga_config.bindings[0], self.window, ()),
            ))

        out_of_window = {
            "metadata": {"timeZone": "UTC"},
            "metricHeaders": [{"name": "screenPageViews"}],
            "rows": [{
                "dimensionValues": [{"value": "20260703"}],
                "metricValues": [{"value": "1"}],
            }],
        }
        with self.assertRaisesRegex(ValueError, "outside the request"):
            list(GoogleAnalyticsConnector(
                ga_config, QueueHttp([out_of_window])
            ).collect(
                ga_config.connections[0],
                MemoryCredentialLease({"access_token": b"test"}),
                SyncRequest(ga_config.bindings[0], self.window, ()),
            ))

        umami_config = self.config(
            "umami", 'base_url = "https://analytics.example.invalid"'
        )
        with self.assertRaisesRegex(ValueError, "outside the request"):
            list(UmamiConnector(
                umami_config,
                QueueHttp([{
                    "pageviews": [{"x": "2026-07-03T00:00:00Z", "y": 1}],
                    "sessions": [],
                }]),
            ).collect(
                umami_config.connections[0],
                MemoryCredentialLease({"token": b"test"}),
                SyncRequest(umami_config.bindings[0], self.window, ()),
            ))

    def test_search_console_route_dates_outside_provider_window_are_rejected(self):
        config = self.config("search-console")
        object.__setattr__(config.bindings[0], "options", {
            "route_analytics": {"enabled": True}
        })
        connector = SearchConsoleConnector(config, QueueHttp([{
            "rows": [{
                "keys": ["2026-07-03", "https://example.com/outside"],
                "clicks": 1,
                "impressions": 2,
                "ctr": 0.5,
                "position": 3,
            }],
        }, {"rows": []}, {"rows": []}]))
        points = list(connector._collect_route_observations(
            "token", "encoded", date(2026, 7, 1), date(2026, 7, 2),
            config.sites[0], route_analytics_options(config.bindings[0]),
        ))
        self.assertEqual(points, [])

    def test_ga4_rejects_invalid_headline_pageview_counts(self):
        config = self.config("google-analytics")
        for value in (
            True, -1, "-1", 1.5, "1.5", "malformed",
            "NaN", "Infinity", "-Infinity",
        ):
            with self.subTest(value=value):
                response = {
                    "metadata": {"timeZone": "UTC"},
                    "metricHeaders": [{"name": "screenPageViews"}],
                    "rows": [{
                        "dimensionValues": [{"value": "20260701"}],
                        "metricValues": [{"value": value}],
                    }],
                }
                geography = {
                    "metadata": {"timeZone": "UTC"},
                    "metricHeaders": [{"name": "sessions"}],
                    "rows": [],
                }
                connector = GoogleAnalyticsConnector(
                    config, QueueHttp([response, geography])
                )
                with self.assertRaisesRegex(ValueError, "pageview count"):
                    list(connector.collect(
                        config.connections[0],
                        MemoryCredentialLease({"access_token": b"test"}),
                        SyncRequest(config.bindings[0], self.window, ()),
                    ))

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
        geography = {"rows": [{
            "keys": ["2026-07-01", "usa"],
            "clicks": 4,
            "impressions": 20,
            "ctr": 0.2,
            "position": 3.1,
        }]}
        http = QueueHttp([
            response, geography, {"rows": []}, {"rows": []},
        ]); points = list(SearchConsoleConnector(config, http).collect(config.connections[0], MemoryCredentialLease({"access_token": b"test"}), SyncRequest(binding, self.window, ())))
        self.assertEqual(len(points), 8); self.assertIn("sc-domain%3Aexample.com", http.calls[0][1])
        geo_point = next(point for point in points if point.metric == "search.country-clicks")
        self.assertEqual(dict(geo_point.dimensions), {
            "aggregation": "byProperty",
            "country_code": "USA",
            "country_code_system": "iso-alpha3",
            "data_state": "final",
            "provider_date": "2026-07-01",
            "provider_timezone": "America/Los_Angeles",
            "search_type": "web",
        })
        self.assertEqual(http.calls[1][3]["dimensions"], ["date", "country"])

    def test_search_console_queries_pacific_dates_without_changing_fact_identity(self):
        config = self.config("search-console", timezone="America/Chicago")
        zone = ZoneInfo("America/Chicago")
        window = QueryWindow(datetime(2026, 7, 1, tzinfo=zone), datetime(2026, 7, 2, tzinfo=zone), "America/Chicago")
        response = {"rows": [{
            "keys": ["2026-07-01"],
            "clicks": 1,
            "impressions": 2,
            "ctr": 0.5,
            "position": 3,
        }]}
        http = QueueHttp([response, {"rows": []}])
        points = list(SearchConsoleConnector(config, http).collect(
            config.connections[0], MemoryCredentialLease({"access_token": b"test"}),
            SyncRequest(config.bindings[0], window, ())))
        query = http.calls[0][3]
        self.assertEqual((query["startDate"], query["endDate"]), ("2026-07-01", "2026-07-01"))
        self.assertEqual(getattr(points[0].start.tzinfo, "key", None), "America/Chicago")
        self.assertEqual(dict(points[0].dimensions)["provider_date"], "2026-07-01")
        self.assertEqual(
            dict(points[0].dimensions)["provider_timezone"],
            "America/Los_Angeles",
        )

    def test_search_console_chicago_fact_survives_store_and_report_window(self):
        config = self.config("search-console", timezone="America/Chicago")
        object.__setattr__(config.reports[0], "metric_ids", ("search.clicks",))
        zone = ZoneInfo("America/Chicago")
        window = QueryWindow(datetime(2026, 7, 1, tzinfo=zone), datetime(2026, 7, 2, tzinfo=zone), "America/Chicago")
        points = list(SearchConsoleConnector(config, QueueHttp([
            {"rows": [{
                "keys": ["2026-07-01"],
                "clicks": 1,
                "impressions": 2,
                "ctr": 0.5,
                "position": 3,
            }]}, {"rows": []}])).collect(
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
            {"rows": [{
                "keys": ["2026-07-01"], "clicks": 2,
                "impressions": 10, "ctr": 0.2, "position": 3.0,
            }]},
            {"rows": []}, {"rows": []},
            {"rows": [{"keys": ["2026-07-01", "https://example.com/blog/?email=a@example.com"], "clicks": 2, "impressions": 10, "ctr": 0.2, "position": 3.0}]},
            {"rows": []}, {"rows": []},
        ])
        points = list(SearchConsoleConnector(config, http).collect(
            config.connections[0], MemoryCredentialLease({"access_token": b"test"}),
            SyncRequest(config.bindings[0], self.window, ())))
        route = next(point for point in points if point.metric == "search.route-clicks")
        self.assertEqual(dict(route.dimensions), {
            "aggregation": "byPage",
            "data_state": "final",
            "observation_scope": "page",
            "provider_date": "2026-07-01",
            "provider_timezone": "America/Los_Angeles",
            "route": "/blog",
            "search_type": "web",
        })
        self.assertIs(route.completeness, Completeness.UNKNOWN)
        self.assertEqual(http.calls[3][3]["dimensions"], ["date", "page"])
        self.assertEqual(http.calls[3][3]["startRow"], 0)

    def test_umami_route_observations_issue_bounded_daily_path_queries(self):
        config = self.config("umami", 'base_url = "https://analytics.example.invalid"')
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {
            "enabled": True, "max_days": 2, "page_size": 10, "max_pages": 2,
        }})
        responses = [
            {"pageviews": [], "sessions": []}, umami_stats(), [], [],
            {"startDate": "2026-01-01T00:00:00Z", "endDate": "2026-07-03T00:00:00Z"},
        ]
        responses.extend([
            [{
                "name": "/pricing/?email=a@example.com",
                "pageviews": 4,
                "visitors": 2,
                "visits": 3,
                "bounces": 1,
                "totaltime": 12,
            }],
            [], [], [], [], [],
        ])
        http = QueueHttp(responses)
        points = list(UmamiConnector(config, http).collect(
            config.connections[0], MemoryCredentialLease({"token": b"test"}),
            SyncRequest(config.bindings[0], self.window, ())))
        route = next(point for point in points if point.metric == "umami.route-visits")
        self.assertEqual(dict(route.dimensions), {"route": "/pricing"})
        self.assertIn("/daterange", http.calls[4][1])
        self.assertIn("type=path", http.calls[5][1])
        self.assertNotIn("field=", http.calls[5][1])
        self.assertIn("startAt=", http.calls[5][1])
        self.assertFalse(any(
            f"type={item}" in call[1]
            for call in http.calls[5:]
            for item in ("title", "channel", "domain", "device", "country")
        ))


    def test_umami_route_pageviews_are_distinct_from_visits_and_field_bound(self):
        config = self.config("umami", 'base_url = "https://analytics.example.invalid"')
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {
            "enabled": True, "max_days": 1, "page_size": 10, "max_pages": 1,
        }})
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        http = QueueHttp([
            {"pageviews": [], "sessions": []},
            umami_stats(),
            [],
            [],
            {
                "startDate": "2026-01-01T00:00:00Z",
                "endDate": "2026-07-02T00:00:00Z",
            },
            [{
                "name": "/pricing",
                "pageviews": 3,
                "visitors": 1,
                "visits": 2,
                "bounces": 0,
                "totaltime": 9,
            }],
            [], [],
        ])

        points = list(UmamiConnector(config, http).collect(
            config.connections[0],
            MemoryCredentialLease({"token": b"test"}),
            SyncRequest(config.bindings[0], one_day, ()),
        ))

        route_pageviews = next(
            point for point in points
            if point.metric == "umami.route-pageviews"
        )
        route_visits = next(
            point for point in points
            if point.metric == "umami.route-visits"
        )
        self.assertEqual(route_pageviews.value, 3)
        self.assertEqual(route_visits.value, 2)
        self.assertIs(route_pageviews.completeness, Completeness.FINAL)
        self.assertIn("type=path", http.calls[5][1])
        self.assertNotIn("field=", http.calls[5][1])

    def test_umami_route_availability_retains_partial_first_day_as_unknown(self):
        config = self.config("umami", 'base_url = "https://analytics.example.invalid"')
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {
            "enabled": True, "max_days": 1, "page_size": 10, "max_pages": 1,
        }})
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        connector = UmamiConnector(config, QueueHttp([
            {"pageviews": [], "sessions": []},
            umami_stats(),
            [],
            [],
            {
                "startDate": "2026-07-01T12:00:00Z",
                "endDate": "2026-07-02T00:00:00Z",
            },
            [{"name": "/safe", "pageviews": 3}],
            [],
            [],
            [],
        ]))

        points = list(connector.collect(
            config.connections[0],
            MemoryCredentialLease({"token": b"test"}),
            SyncRequest(config.bindings[0], one_day, ()),
        ))

        route = next(
            point for point in points
            if point.metric == "umami.route-pageviews"
        )
        self.assertEqual(route.value, 3)
        self.assertIs(route.completeness, Completeness.UNKNOWN)
        self.assertEqual(len(connector.http.calls), 8)

    def test_umami_route_availability_accepts_quiet_trailing_hours(self):
        config = self.config("umami", 'base_url = "https://analytics.example.invalid"')
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {
            "enabled": True, "max_days": 1, "page_size": 10, "max_pages": 1,
        }})
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        connector = UmamiConnector(config, QueueHttp([
            {"pageviews": [], "sessions": []},
            umami_stats(),
            [],
            [],
            {
                "startDate": "2026-01-01T00:00:00Z",
                "endDate": "2026-07-01T18:00:00Z",
            },
            [{
                "name": "/safe",
                "pageviews": 3,
                "visitors": 2,
                "visits": 2,
                "bounces": 0,
                "totaltime": 10,
            }],
            [],
            [],
            [],
        ]))

        points = list(connector.collect(
            config.connections[0],
            MemoryCredentialLease({"token": b"test"}),
            SyncRequest(config.bindings[0], one_day, ()),
        ))

        route = next(
            point for point in points
            if point.metric == "umami.route-pageviews"
        )
        self.assertEqual(route.value, 3)
        self.assertIs(route.completeness, Completeness.FINAL)

    def test_umami_rejects_invalid_headline_pageview_counts(self):
        config = self.config("umami", 'base_url = "https://analytics.example.invalid"')
        for value in (
            True, -1, "-1", 1.5, "1.5", "malformed",
            "NaN", "Infinity", "-Infinity",
        ):
            with self.subTest(value=value):
                connector = UmamiConnector(config, QueueHttp([
                    {
                        "pageviews": [{
                            "x": "2026-07-01T00:00:00Z",
                            "y": value,
                        }],
                        "sessions": [],
                    },
                    {},
                    [],
                    [],
                ]))
                with self.assertRaisesRegex(ValueError, "pageview count"):
                    list(connector.collect(
                        config.connections[0],
                        MemoryCredentialLease({"token": b"test"}),
                        SyncRequest(config.bindings[0], self.window, ()),
                    ))

    def test_umami_invalid_route_pageview_downgrades_retained_safe_rows(self):
        config = self.config("umami", 'base_url = "https://analytics.example.invalid"')
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {
            "enabled": True, "max_days": 1, "page_size": 10, "max_pages": 1,
        }})
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        connector = UmamiConnector(config, QueueHttp([
            {"pageviews": [], "sessions": []},
            umami_stats(),
            [],
            [],
            {
                "startDate": "2026-01-01T00:00:00Z",
                "endDate": "2026-07-02T00:00:00Z",
            },
            [
                {"name": "/safe", "pageviews": 2},
                {"name": "/fractional", "pageviews": 1.5},
            ],
            [],
            [],
            [],
        ]))

        points = list(connector.collect(
            config.connections[0],
            MemoryCredentialLease({"token": b"test"}),
            SyncRequest(config.bindings[0], one_day, ()),
        ))
        pageviews = [
            point for point in points
            if point.metric == "umami.route-pageviews"
        ]

        self.assertEqual(len(pageviews), 1)
        self.assertEqual(dict(pageviews[0].dimensions), {"route": "/safe"})
        self.assertEqual(pageviews[0].value, 2)
        self.assertIs(pageviews[0].completeness, Completeness.UNKNOWN)

    def test_umami_full_last_page_preserves_safe_pageviews_as_unknown(self):
        config = self.config("umami", 'base_url = "https://analytics.example.invalid"')
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {
            "enabled": True, "max_days": 1, "page_size": 1, "max_pages": 1,
        }})
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        http = QueueHttp([
            {"pageviews": [], "sessions": []},
            umami_stats(),
            [],
            [],
            {
                "startDate": "2026-01-01T00:00:00Z",
                "endDate": "2026-07-02T00:00:00Z",
            },
            [{"name": "/pricing", "pageviews": 3}],
            [],
            [],
            [],
        ])

        points = list(UmamiConnector(config, http).collect(
            config.connections[0],
            MemoryCredentialLease({"token": b"test"}),
            SyncRequest(config.bindings[0], one_day, ()),
        ))

        route = next(
            point for point in points
            if point.metric == "umami.route-pageviews"
        )
        self.assertEqual(route.value, 3)
        self.assertIs(route.completeness, Completeness.UNKNOWN)

    def test_umami_overlapping_raw_pages_are_not_double_counted_or_exhaustive(self):
        responses = [
            [
                {"name": "/first", "pageviews": 2},
                {"name": "/overlap", "pageviews": 3},
            ],
            [
                {"name": "/overlap", "pageviews": 4},
            ],
        ]
        rows, exhaustive = UmamiConnector(
            None, QueueHttp(responses)
        )._expanded_rows(
            "https://analytics.example.invalid",
            {},
            "startAt=1&endAt=2",
            "path",
            "pageviews",
            SimpleNamespace(page_size=2, max_pages=2),
        )

        self.assertFalse(exhaustive)
        self.assertEqual(
            rows,
            [
                {"name": "/first", "pageviews": 2},
                {"name": "/overlap", "pageviews": 3},
            ],
        )

    def test_umami_unique_short_final_page_proves_exhaustion(self):
        rows, exhaustive = UmamiConnector(
            None,
            QueueHttp([
                [
                    {"name": "/first", "pageviews": 2},
                    {"name": "/second", "pageviews": 3},
                ],
                [{"name": "/third", "pageviews": 4}],
            ]),
        )._expanded_rows(
            "https://analytics.example.invalid",
            {},
            "startAt=1&endAt=2",
            "path",
            "pageviews",
            SimpleNamespace(page_size=2, max_pages=2),
        )

        self.assertTrue(exhaustive)
        self.assertEqual([row["name"] for row in rows], [
            "/first", "/second", "/third",
        ])


    def test_umami_route_pageviews_reject_unsafe_paths_and_mark_safe_rows_unknown(self):
        config = self.config("umami", 'base_url = "https://analytics.example.invalid"')
        object.__setattr__(config.bindings[0], "options", {"route_analytics": {
            "enabled": True,
            "max_days": 1,
            "page_size": 20,
            "max_pages": 1,
            "excluded_routes": ["/private"],
        }})
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        http = QueueHttp([
            {"pageviews": [], "sessions": []},
            umami_stats(),
            [],
            [],
            {
                "startDate": "2026-01-01T00:00:00Z",
                "endDate": "2026-07-02T00:00:00Z",
            },
            [
                {"name": "/safe", "pageviews": 2},
                {"name": "/safe/", "pageviews": 9},
                {"name": "/query?campaign=test", "pageviews": 3},
                {"name": "/fragment#details", "pageviews": 4},
                {"name": "https://outside.example/path", "pageviews": 5},
                {"name": r"/safe\..\private", "pageviews": 6},
                {"name": "/bad%00route", "pageviews": 7},
                {"name": "/private/form", "pageviews": 8},
                {"name": "relative/path", "pageviews": 10},
                {"name": "/bad\u0085route", "pageviews": 11},
                {"name": "/bad%C2%85route", "pageviews": 12},
            ],
            [],
            [],
            [],
        ])

        points = list(UmamiConnector(config, http).collect(
            config.connections[0],
            MemoryCredentialLease({"token": b"test"}),
            SyncRequest(config.bindings[0], one_day, ()),
        ))
        pageviews = [
            point for point in points
            if point.metric == "umami.route-pageviews"
        ]

        self.assertEqual(len(pageviews), 1)
        self.assertEqual(dict(pageviews[0].dimensions), {"route": "/safe"})
        self.assertEqual(pageviews[0].value, 11)
        self.assertIs(pageviews[0].completeness, Completeness.UNKNOWN)

if __name__ == "__main__": unittest.main()
