from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from boho_analytics_platform.config import load_config, route_analytics_options
from boho_analytics_platform.catalog import validate_points
from boho_analytics_platform.connectors.google import (
    GoogleAnalyticsConnector,
    SEARCH_CONSOLE_REDACTED_QUERY,
    SearchConsoleConnector,
    _collect_search_console_slice,
)
from boho_analytics_platform.contracts import SyncRequest
from boho_analytics_platform.credentials import MemoryCredentialLease
from boho_analytics_platform.engine import SyncEngine
from boho_analytics_platform.models import Completeness, QueryWindow, TimeGrain
from boho_analytics_platform.storage import SQLiteMetricStore
from support import config_text, write_fixture


class QueueHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers=None, body=None):
        self.calls.append((method, url, headers, body))
        return self.responses.pop(0)


class MemoryCredentialProvider:
    def acquire(self, _reference):
        return MemoryCredentialLease({"access_token": b"test"})


def search_row(keys, *, clicks=2, impressions=10, ctr=0.2, position=3.0):
    return {
        "keys": list(keys),
        "clicks": clicks,
        "impressions": impressions,
        "ctr": ctr,
        "position": position,
    }


class SearchConsoleFeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = self.root / "fixture.json"
        write_fixture(self.fixture)
        zone = ZoneInfo("America/Los_Angeles")
        self.window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=zone),
            datetime(2026, 7, 2, tzinfo=zone),
            "America/Los_Angeles",
        )

    def config(self, route_lines: str = "", *, provider="search-console", timezone="America/Los_Angeles"):
        text = config_text(
            self.root / f"{provider}.db", self.fixture,
            provider=provider, options="",
        ).replace('timezone = "UTC"', f'timezone = "{timezone}"')
        if route_lines:
            text = text.replace(
                'metric_groups = ["traffic"]',
                'metric_groups = ["traffic"]\n[bindings.options.route_analytics]\n'
                + route_lines,
            )
        path = self.root / f"{provider}.toml"
        path.write_text(text, encoding="utf-8")
        return load_config(path)

    def collect(self, config, responses):
        http = QueueHttp(responses)
        points = list(SearchConsoleConnector(config, http).collect(
            config.connections[0],
            MemoryCredentialLease({"access_token": b"test"}),
            SyncRequest(config.bindings[0], self.window, ()),
        ))
        return points, http

    def test_every_request_has_explicit_scope_and_geography_stays_unknown(self):
        config = self.config('search_type = "news"\npage_size = 10\nmax_pages = 4')
        points, http = self.collect(config, [
            {
                "responseAggregationType": "byProperty",
                "rows": [search_row(["2026-07-01"], clicks=4, impressions=20)],
            },
            {
                "responseAggregationType": "byProperty",
                "rows": [search_row(["2026-07-01", "usa"], clicks=4, impressions=20)],
            },
            {"responseAggregationType": "byProperty", "rows": []},
        ])

        for index, call in enumerate(http.calls):
            body = call[3]
            self.assertEqual(body["type"], "news")
            self.assertEqual(
                body["dataState"], "all" if index == 0 else "final"
            )
            self.assertEqual(body["aggregationType"], "byProperty")
        self.assertEqual(http.calls[0][3]["dimensions"], ["date"])
        self.assertEqual(http.calls[1][3]["dimensions"], ["date", "country"])
        self.assertEqual(http.calls[2][3]["startRow"], 1)

        headline = next(point for point in points if point.metric == "search.clicks")
        self.assertEqual(dict(headline.dimensions), {
            "aggregation": "byProperty",
            "data_state": "all",
            "provider_date": "2026-07-01",
            "provider_timezone": "America/Los_Angeles",
            "search_type": "news",
        })
        countries = [point for point in points if point.metric.startswith("search.country-")]
        self.assertEqual({point.metric for point in countries}, {
            "search.country-clicks", "search.country-impressions",
            "search.country-ctr", "search.country-position",
        })
        self.assertTrue(all(point.completeness is Completeness.UNKNOWN for point in countries))
        self.assertTrue(all(dict(point.dimensions)["country_code"] == "USA" for point in countries))
        self.assertTrue(all(
            dict(point.dimensions)["provider_date"] == "2026-07-01"
            for point in countries
        ))

    def test_batches_record_exact_request_evidence_and_rejections(self):
        config = self.config("page_size = 10\nmax_pages = 4")
        http = QueueHttp([
            {
                "responseAggregationType": "byProperty",
                "rows": [search_row(
                    ["2026-07-01"], clicks=2, impressions=10, ctr=0.9
                )],
            },
            {
                "responseAggregationType": "byProperty",
                "rows": [
                    search_row(["2026-07-01", "usa"]),
                    search_row(["2026-07-01", "not-a-country"]),
                ],
            },
            {"responseAggregationType": "byProperty", "rows": []},
        ])
        connector = SearchConsoleConnector(config, http)
        batches = list(connector.collect_batches(
            config.connections[0],
            MemoryCredentialLease({"access_token": b"test"}),
            SyncRequest(config.bindings[0], self.window, ()),
        ))

        self.assertEqual(len(batches), 2)
        control, country = batches
        self.assertEqual(
            (
                control.slice.metric_family,
                control.slice.pages_fetched,
                control.slice.raw_rows,
                control.slice.accepted_rows,
                control.slice.rejected_rows,
                control.slice.exhaustion_reason,
                control.slice.request_dimensions,
            ),
            ("search.control", 1, 1, 1, 0, "bounded-control", ("date",)),
        )
        self.assertIs(control.slice.completeness, Completeness.FINAL)
        self.assertEqual(control.slice.data_state, "all")
        ctr = next(point for point in control.points if point.metric == "search.ctr")
        self.assertEqual(ctr.value, Decimal("0.2"))
        self.assertEqual(
            (
                country.slice.pages_fetched,
                country.slice.raw_rows,
                country.slice.accepted_rows,
                country.slice.rejected_rows,
                country.slice.exhaustion_reason,
                country.slice.request_dimensions,
            ),
            (2, 2, 1, 1, "empty-page", ("date", "country")),
        )
        self.assertIs(country.slice.completeness, Completeness.UNKNOWN)
        self.assertEqual(
            len({batch.slice.slice_key for batch in batches}), len(batches)
        )
        self.assertTrue(all(len(batch.slice.slice_key) <= 128 for batch in batches))
        validate_points([
            point for batch in batches for point in batch.points
        ])

    def test_control_rows_require_all_metrics(self):
        config = self.config()
        connector = SearchConsoleConnector(config, QueueHttp([{
            "responseAggregationType": "byProperty",
            "rows": [{"keys": ["2026-07-01"], "clicks": 1}],
        }]))
        with self.assertRaisesRegex(ValueError, "omitted required metrics"):
            list(connector.collect_batches(
                config.connections[0],
                MemoryCredentialLease({"access_token": b"test"}),
                SyncRequest(config.bindings[0], self.window, ()),
            ))

    def test_incomplete_daily_marker_keeps_fresh_headline_but_trims_settled_detail(self):
        config = self.config()
        points, http = self.collect(config, [
            {
                "responseAggregationType": "byProperty",
                "metadata": {"first_incomplete_date": "2026-07-01"},
                "rows": [search_row(["2026-07-01"], clicks=3, impressions=12)],
            },
        ])

        headline = next(point for point in points if point.metric == "search.clicks")
        self.assertEqual(headline.value, 3)
        self.assertIs(headline.completeness, Completeness.PROVISIONAL)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0][3]["dataState"], "all")
        self.assertFalse(any(
            point.metric.startswith("search.country-") for point in points
        ))

    def test_all_search_surfaces_remain_separate_provider_scopes(self):
        config = self.config(
            'search_types = ["all"]\npage_size = 10\nmax_pages = 2'
        )
        search_types = (
            "web", "image", "video", "news", "discover", "googleNews",
        )
        responses = []
        for search_type in search_types:
            aggregation = (
                "byPage"
                if search_type in {"discover", "googleNews"}
                else "byProperty"
            )
            responses.extend([
                {
                    "responseAggregationType": aggregation,
                    "rows": [search_row(["2026-07-01"])],
                },
                {"responseAggregationType": aggregation, "rows": []},
            ])

        points, http = self.collect(config, responses)
        clicks = [point for point in points if point.metric == "search.clicks"]
        self.assertEqual(
            {dict(point.dimensions)["search_type"] for point in clicks},
            set(search_types),
        )
        self.assertEqual(len(clicks), len(search_types))
        self.assertEqual(
            [http.calls[index][3]["type"] for index in range(0, 12, 2)],
            list(search_types),
        )
        self.assertEqual(
            [http.calls[index][3]["aggregationType"] for index in (8, 10)],
            ["byPage", "byPage"],
        )

    def test_non_search_surfaces_omit_position_and_query_wording(self):
        config = self.config(
            'search_types = ["discover", "googleNews"]\n'
            'page_size = 10\nmax_pages = 2\n'
            'search_console_query_text = true\n'
            'search_console_page_query = true'
        )
        responses = []
        for search_type in ("discover", "googleNews"):
            control = search_row(["2026-07-01"])
            country = search_row(["2026-07-01", "usa"])
            if search_type == "discover":
                control["position"] = 0
                country["position"] = 0
            else:
                control.pop("position")
                country.pop("position")
            responses.extend([
                {"responseAggregationType": "byPage", "rows": [control]},
                {"responseAggregationType": "byPage", "rows": [country]},
                {"responseAggregationType": "byPage", "rows": []},
            ])

        points, http = self.collect(config, responses)
        self.assertFalse(any(point.metric.endswith("position") for point in points))
        self.assertEqual(
            {point.metric for point in points},
            {
                "search.clicks", "search.impressions", "search.ctr",
                "search.country-clicks", "search.country-impressions",
                "search.country-ctr",
            },
        )
        self.assertFalse(any(
            "query" in call[3]["dimensions"] for call in http.calls
        ))
        self.assertTrue(all(
            call[3]["aggregationType"] == "byPage" for call in http.calls
        ))

    def test_query_and_page_query_capture_redacts_and_aggregates_unsafe_text(self):
        config = self.config(
            'page_size = 2\nmax_pages = 4\n'
            'search_console_query_text = true\n'
            'search_console_page_query = true'
        )
        points, http = self.collect(config, [
            {"responseAggregationType": "byProperty", "rows": []},
            {"responseAggregationType": "byProperty", "rows": []},
            {
                "responseAggregationType": "byProperty",
                "rows": [
                    search_row(["2026-07-01", "best biscuit"], clicks=3, impressions=10),
                    search_row(["2026-07-01", "person@example.com"], clicks=1, impressions=5),
                ],
            },
            {
                "responseAggregationType": "byProperty",
                "rows": [search_row(
                    ["2026-07-01", "https://outside.example/private"],
                    clicks=2, impressions=5,
                )],
            },
            {"responseAggregationType": "byProperty", "rows": []},
            {
                "responseAggregationType": "byPage",
                "rows": [
                    search_row([
                        "2026-07-01", "https://example.com/story/", "person@example.com"
                    ], clicks=2, impressions=8),
                ],
            },
            {"responseAggregationType": "byPage", "rows": []},
        ])

        query_clicks = [point for point in points if point.metric == "search.query-clicks"]
        by_query = {
            dict(point.dimensions)["query_text"]: point.value
            for point in query_clicks
        }
        self.assertEqual(by_query["best biscuit"], 3)
        self.assertEqual(by_query[SEARCH_CONSOLE_REDACTED_QUERY], 3)
        visible = {
            dict(point.dimensions)["query_text"]:
                dict(point.dimensions)["query_visibility"]
            for point in query_clicks
        }
        self.assertEqual(visible, {
            "best biscuit": "safe",
            SEARCH_CONSOLE_REDACTED_QUERY: "redacted",
        })
        page_query = next(
            point for point in points if point.metric == "search.page-query-clicks"
        )
        self.assertEqual(dict(page_query.dimensions)["route"], "/story")
        self.assertEqual(
            dict(page_query.dimensions)["query_text"],
            SEARCH_CONSOLE_REDACTED_QUERY,
        )
        self.assertEqual(
            dict(page_query.dimensions)["query_visibility"], "redacted"
        )
        validate_points(points)
        self.assertNotIn("person@example.com", repr(points))
        self.assertNotIn("outside.example", repr(points))

        query_bodies = [
            call[3] for call in http.calls if call[3]["dimensions"] == ["date", "query"]
        ]
        self.assertEqual([body["startRow"] for body in query_bodies], [0, 2, 3])
        page_body = next(
            call[3] for call in http.calls
            if call[3]["dimensions"] == ["date", "page", "query"]
        )
        self.assertEqual(page_body["aggregationType"], "byPage")

    def test_slice_requires_empty_page_and_rejects_duplicate_raw_keys(self):
        options = SimpleNamespace(search_type="web", page_size=2, max_pages=3)
        first = search_row(["2026-07-01", "alpha"])
        http = QueueHttp([
            {"responseAggregationType": "byProperty", "rows": [first]},
            {"responseAggregationType": "byProperty", "rows": []},
        ])
        result = _collect_search_console_slice(
            http, "token", "encoded", date(2026, 7, 1), date(2026, 7, 1),
            ["date", "query"], options,
            data_state="final", aggregation="byProperty",
        )
        self.assertEqual((result.pages, result.raw_rows, result.exhaustion), (2, 1, "empty-page"))

        duplicate_http = QueueHttp([
            {"responseAggregationType": "byProperty", "rows": [
                first, search_row(["2026-07-01", "beta"]),
            ]},
            {"responseAggregationType": "byProperty", "rows": [first]},
        ])
        with self.assertRaisesRegex(ValueError, "duplicate row keys"):
            _collect_search_console_slice(
                duplicate_http, "token", "encoded",
                date(2026, 7, 1), date(2026, 7, 1),
                ["date", "query"], options,
                data_state="final", aggregation="byProperty",
            )

    def test_slice_reaches_non_divisible_provider_cap_without_skipping_rows(self):
        options = SimpleNamespace(search_type="web", page_size=3, max_pages=3)
        http = QueueHttp([
            {
                "responseAggregationType": "byProperty",
                "rows": [
                    search_row(["2026-07-01", "alpha"]),
                    search_row(["2026-07-01", "beta"]),
                    search_row(["2026-07-01", "gamma"]),
                ],
            },
            {
                "responseAggregationType": "byProperty",
                "rows": [
                    search_row(["2026-07-01", "delta"]),
                    search_row(["2026-07-01", "epsilon"]),
                ],
            },
            {"responseAggregationType": "byProperty", "rows": []},
        ])
        with patch(
            "boho_analytics_platform.connectors.google._SEARCH_CONSOLE_DAILY_ROW_CAP",
            5,
        ):
            result = _collect_search_console_slice(
                http, "token", "encoded",
                date(2026, 7, 1), date(2026, 7, 1),
                ["date", "query"], options,
                data_state="final", aggregation="byProperty",
            )

        self.assertEqual(
            [call[3]["startRow"] for call in http.calls], [0, 3, 5]
        )
        self.assertEqual(
            (result.raw_rows, result.pages, result.exhaustion),
            (5, 3, "provider-cap-empty"),
        )

    def test_search_appearance_uses_discovery_then_filtered_page_query(self):
        config = self.config(
            'enabled = true\npage_size = 10\nmax_pages = 4\n'
            'search_console_dimensions = ["searchAppearance"]'
        )
        options = route_analytics_options(config.bindings[0])
        http = QueueHttp([
            {"responseAggregationType": "byPage", "rows": []},
            {
                "responseAggregationType": "byPage",
                "rows": [search_row(["AMP_BLUE_LINK"])],
            },
            {"responseAggregationType": "byPage", "rows": []},
            {
                "responseAggregationType": "byPage",
                "rows": [search_row([
                    "2026-07-01", "https://example.com/article/"
                ])],
            },
            {"responseAggregationType": "byPage", "rows": []},
        ])
        batches = list(SearchConsoleConnector(config, http)._collect_route_batches(
            "token", "encoded", date(2026, 7, 1), date(2026, 7, 1),
            config.sites[0], options,
        ))
        points = [point for batch in batches for point in batch.points]

        self.assertEqual(http.calls[1][3]["dimensions"], ["searchAppearance"])
        filtered = http.calls[3][3]
        self.assertEqual(filtered["dimensions"], ["date", "page"])
        self.assertEqual(filtered["dimensionFilterGroups"], [{
            "groupType": "and",
            "filters": [{
                "dimension": "searchAppearance",
                "operator": "equals",
                "expression": "AMP_BLUE_LINK",
            }],
        }])
        route = next(point for point in points if point.metric == "search.route-clicks")
        self.assertEqual(dict(route.dimensions)["search_appearance"], "AMP_BLUE_LINK")
        self.assertEqual(dict(route.dimensions)["aggregation"], "byPage")
        self.assertIs(route.completeness, Completeness.UNKNOWN)
        discovery = next(
            batch for batch in batches
            if batch.slice.metric_family == "search.discovery"
        )
        self.assertEqual(discovery.points, ())
        self.assertEqual(
            (
                discovery.slice.pages_fetched,
                discovery.slice.raw_rows,
                discovery.slice.accepted_rows,
                discovery.slice.exhaustion_reason,
            ),
            (2, 1, 1, "empty-page"),
        )
        filtered_batch = next(
            batch for batch in batches
            if any(
                dict(point.dimensions).get("search_appearance") == "AMP_BLUE_LINK"
                for point in batch.points
            )
        )
        self.assertTrue(
            filtered_batch.slice.provider_scope.startswith("web:appearance:")
        )
        self.assertNotIn("AMP_BLUE_LINK", filtered_batch.slice.slice_key)

    def test_query_cluster_provenance_uses_stable_configured_identifier(self):
        config = self.config(
            'enabled = true\npage_size = 10\nmax_pages = 4\n'
            '[bindings.options.route_analytics.query_clusters]\n'
            'brand_terms = "boho|biscuit"'
        )
        options = route_analytics_options(config.bindings[0])
        http = QueueHttp([
            {"responseAggregationType": "byPage", "rows": []},
            {
                "responseAggregationType": "byPage",
                "rows": [search_row([
                    "2026-07-01", "https://example.com/story/"
                ])],
            },
            {"responseAggregationType": "byPage", "rows": []},
        ])
        batches = list(SearchConsoleConnector(config, http)._collect_route_batches(
            "token", "encoded", date(2026, 7, 1), date(2026, 7, 1),
            config.sites[0], options,
        ))

        cluster = next(
            batch for batch in batches
            if batch.slice.provider_scope == "web:route:query-cluster-brand_terms"
        )
        self.assertIn("query-cluster-brand_terms", cluster.slice.slice_key)
        self.assertEqual(
            dict(cluster.points[0].dimensions)["query_cluster"], "brand_terms"
        )

    def test_query_clusters_are_not_requested_for_non_search_surfaces(self):
        for search_type in ("discover", "googleNews"):
            with self.subTest(search_type=search_type):
                config = self.config(
                    f'enabled = true\nsearch_type = "{search_type}"\n'
                    'page_size = 10\nmax_pages = 4\n'
                    '[bindings.options.route_analytics.query_clusters]\n'
                    'brand_terms = "boho|biscuit"'
                )
                options = route_analytics_options(config.bindings[0])
                http = QueueHttp([
                    {"responseAggregationType": "byPage", "rows": []},
                ])

                batches = list(
                    SearchConsoleConnector(config, http)._collect_route_batches(
                        "token", "encoded", date(2026, 7, 1),
                        date(2026, 7, 1), config.sites[0], options,
                    )
                )

                self.assertEqual(len(batches), 1)
                self.assertEqual(len(http.calls), 1)
                self.assertNotIn("dimensionFilterGroups", http.calls[0][3])

    def test_discover_appearance_routes_do_not_require_position(self):
        config = self.config(
            'enabled = true\nsearch_type = "discover"\n'
            'search_console_dimensions = ["searchAppearance"]\n'
            'page_size = 10\nmax_pages = 4'
        )
        options = route_analytics_options(config.bindings[0])
        http = QueueHttp([
            {"responseAggregationType": "byPage", "rows": []},
            {
                "responseAggregationType": "byPage",
                "rows": [search_row(["DISCOVER_CARD"], position=None)],
            },
            {"responseAggregationType": "byPage", "rows": []},
            {
                "responseAggregationType": "byPage",
                "rows": [search_row([
                    "2026-07-01", "https://example.com/story/"
                ], position=None)],
            },
            {"responseAggregationType": "byPage", "rows": []},
        ])
        for response in http.responses:
            for row in response.get("rows", []):
                row.pop("position", None)

        batches = list(SearchConsoleConnector(config, http)._collect_route_batches(
            "token", "encoded", date(2026, 7, 1), date(2026, 7, 1),
            config.sites[0], options,
        ))

        route_points = [
            point for batch in batches for point in batch.points
            if point.metric.startswith("search.route-")
        ]
        self.assertTrue(route_points)
        self.assertFalse(any(
            point.metric == "search.route-position" for point in route_points
        ))

    def test_discover_skips_the_unsupported_device_route_dimension(self):
        config = self.config(
            'enabled = true\nsearch_type = "discover"\n'
            'search_console_dimensions = ["country", "device"]\n'
            'page_size = 10\nmax_pages = 4'
        )
        options = route_analytics_options(config.bindings[0])
        http = QueueHttp([
            {"responseAggregationType": "byPage", "rows": []},
            {"responseAggregationType": "byPage", "rows": []},
        ])

        list(SearchConsoleConnector(config, http)._collect_route_batches(
            "token", "encoded", date(2026, 7, 1), date(2026, 7, 1),
            config.sites[0], options,
        ))

        self.assertEqual(
            [call[3]["dimensions"] for call in http.calls],
            [["date", "page"], ["date", "page", "country"]],
        )

    def test_hourly_feed_is_bounded_and_uses_incomplete_marker(self):
        config = self.config('search_console_hourly = true')
        options = route_analytics_options(config.bindings[0])
        http = QueueHttp([{
            "responseAggregationType": "byProperty",
            "metadata": {"first_incomplete_hour": "2026-07-03T01:00:00-07:00"},
            "rows": [
                search_row(["2026-07-03T00:00:00-07:00"], clicks=1, impressions=10, ctr=.9),
                search_row(["2026-07-03T01:00:00-07:00"], clicks=2, impressions=10, ctr=.9),
            ],
        }])
        batches = list(SearchConsoleConnector(config, http)._collect_hourly_batches(
            "token", "encoded", date(2026, 7, 1), date(2026, 7, 12),
            config.sites[0], options,
        ))
        points = [point for batch in batches for point in batch.points]

        body = http.calls[0][3]
        self.assertEqual((body["startDate"], body["endDate"]), ("2026-07-03", "2026-07-12"))
        self.assertEqual(body["dataState"], "hourly_all")
        self.assertEqual(body["dimensions"], ["hour"])
        self.assertTrue(all(point.grain is TimeGrain.HOUR for point in points))
        by_hour = {}
        for point in points:
            by_hour.setdefault(point.start.hour, set()).add(point.completeness)
        self.assertEqual(by_hour[0], {Completeness.FINAL})
        self.assertEqual(by_hour[1], {Completeness.PROVISIONAL})
        first_ctr = next(
            point for point in points
            if point.start.hour == 0 and point.metric == "search.hourly-ctr"
        )
        self.assertEqual(first_ctr.value, Decimal("0.1"))
        self.assertEqual(len(batches), 1)
        self.assertIs(batches[0].slice.completeness, Completeness.PROVISIONAL)
        self.assertEqual(
            (
                batches[0].slice.pages_fetched,
                batches[0].slice.raw_rows,
                batches[0].slice.accepted_rows,
            ),
            (1, 2, 2),
        )

    def test_empty_hourly_incomplete_interval_keeps_slice_provisional(self):
        config = self.config('search_console_hourly = true')
        options = route_analytics_options(config.bindings[0])
        http = QueueHttp([{
            "responseAggregationType": "byProperty",
            "metadata": {
                "first_incomplete_hour": "2026-07-03T01:00:00-07:00"
            },
            "rows": [],
        }])
        batches = list(SearchConsoleConnector(config, http)._collect_hourly_batches(
            "token", "encoded", date(2026, 7, 1), date(2026, 7, 3),
            config.sites[0], options,
        ))

        self.assertEqual(batches[0].points, ())
        self.assertIs(
            batches[0].slice.completeness, Completeness.PROVISIONAL
        )

    def test_chicago_sync_preserves_requested_label_and_pacific_request_slice(self):
        config = self.config(timezone="America/Chicago")
        zone = ZoneInfo("America/Chicago")
        window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=zone),
            datetime(2026, 7, 2, tzinfo=zone),
            "America/Chicago",
        )
        http = QueueHttp([
            {
                "responseAggregationType": "byProperty",
                "rows": [search_row(["2026-07-01"])],
            },
            {"responseAggregationType": "byProperty", "rows": []},
            {"responseAggregationType": "byProperty", "rows": []},
        ])
        store = SQLiteMetricStore(self.root / "search-console.db")
        store.initialize()
        result = SyncEngine(
            config, store, credential_provider=MemoryCredentialProvider(), http=http
        ).sync(window)[0]

        self.assertEqual((result.status, result.points), ("success", 4))
        point = store.query(
            client_id=config.clients[0].id,
            site_ids=(config.sites[0].id,),
            metric_ids=("search.clicks",),
            window=window,
        )[0]
        self.assertEqual(dict(point.dimensions)["provider_date"], "2026-07-01")
        self.assertEqual(
            dict(point.dimensions)["provider_timezone"],
            "America/Los_Angeles",
        )
        with store.connect(readonly=True) as database:
            slices = database.execute(
                "SELECT start_at,end_at FROM acquisition_slices ORDER BY slice_key"
            ).fetchall()
        self.assertEqual(len(slices), 2)
        self.assertTrue(any(
            row["start_at"] == "2026-07-01T07:00:00+00:00"
            for row in slices
        ))
        self.assertEqual(
            (http.calls[0][3]["startDate"], http.calls[0][3]["endDate"]),
            ("2026-07-01", "2026-07-01"),
        )

    def test_ga4_country_rows_are_summed_across_regions(self):
        config = self.config(provider="google-analytics", timezone="UTC")
        window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        http = QueueHttp([
            {
                "metadata": {"timeZone": "UTC"},
                "metricHeaders": [{"name": "screenPageViews"}],
                "rows": [],
            },
            {
                "metadata": {"timeZone": "UTC"},
                "metricHeaders": [{"name": "sessions"}],
                "rows": [
                    {"dimensionValues": [
                        {"value": "20260701"}, {"value": "US"}, {"value": "Illinois"},
                    ], "metricValues": [{"value": "2"}]},
                    {"dimensionValues": [
                        {"value": "20260701"}, {"value": "US"}, {"value": "Texas"},
                    ], "metricValues": [{"value": "3"}]},
                    {"dimensionValues": [
                        {"value": "20260701"}, {"value": "CA"}, {"value": "Ontario"},
                    ], "metricValues": [{"value": "4"}]},
                ],
            },
        ])
        points = list(GoogleAnalyticsConnector(config, http).collect(
            config.connections[0], MemoryCredentialLease({"access_token": b"test"}),
            SyncRequest(config.bindings[0], window, ()),
        ))
        countries = {
            dict(point.dimensions)["country_code"]: point.value
            for point in points if point.metric == "google.country-sessions"
        }
        self.assertEqual(countries, {"CA": 4, "US": 5})
        self.assertEqual(
            len([point for point in points if point.metric == "google.region-sessions"]),
            3,
        )


if __name__ == "__main__":
    unittest.main()
