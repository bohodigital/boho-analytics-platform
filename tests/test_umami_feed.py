from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from boho_analytics_platform.config import load_config, route_analytics_options
from boho_analytics_platform.connectors.umami import UmamiConnector
from boho_analytics_platform.contracts import SyncRequest
from boho_analytics_platform.credentials import MemoryCredentialLease
from boho_analytics_platform.models import Completeness, QueryWindow
from support import config_text, write_fixture


class QueueHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers=None, body=None):
        self.calls.append((method, url, headers, body))
        if not self.responses:
            raise AssertionError(f"unexpected Umami request: {method} {url}")
        return self.responses.pop(0)


def stats_response(*, pageviews=0, visitors=0, visits=0, bounces=0, totaltime=0):
    return {
        "pageviews": pageviews,
        "visitors": visitors,
        "visits": visits,
        "bounces": bounces,
        "totaltime": totaltime,
    }


class UmamiFeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = self.root / "fixture.json"
        write_fixture(self.fixture)
        self.window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 3, tzinfo=UTC),
            "UTC",
        )
        self.one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        self.credential = MemoryCredentialLease({"token": b"test-token"})

    def config(self, route_analytics=None, *, timezone="UTC"):
        path = self.root / "analytics.toml"
        text = config_text(
                self.root / "state.db",
                self.fixture,
                provider="umami",
                options='base_url = "https://analytics.example.invalid"',
        )
        text = text.replace('timezone = "UTC"', f'timezone = "{timezone}"')
        path.write_text(text, encoding="utf-8")
        config = load_config(path)
        if route_analytics is not None:
            object.__setattr__(
                config.bindings[0], "options", {"route_analytics": route_analytics}
            )
        return config

    def collect(self, config, http, window=None):
        return list(
            UmamiConnector(config, http).collect(
                config.connections[0],
                self.credential,
                SyncRequest(config.bindings[0], window or self.window, ()),
            )
        )

    def test_headline_dates_and_all_supplied_counts_are_strict(self):
        config = self.config()
        for pageviews, sessions, message in (
            (
                [
                    {"x": "2026-07-01T00:00:00Z", "y": 1},
                    {"x": "2026-07-01", "y": 2},
                ],
                [],
                "repeated a date",
            ),
            (
                [{"x": "2026-06-30T00:00:00Z", "y": 1}],
                [],
                "outside the request",
            ),
            (
                [],
                [{"x": "2026-07-01T00:00:00Z", "y": 1.5}],
                "daily visitor count",
            ),
            (
                [{"x": "2026-07-01 12:00:00", "y": 1}],
                [],
                "midnight bucket",
            ),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self.collect(
                        config,
                        QueueHttp([{"pageviews": pageviews, "sessions": sessions}]),
                    )

        pacific = ZoneInfo("America/Los_Angeles")
        local_config = self.config(timezone="America/Los_Angeles")
        local_window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=pacific),
            datetime(2026, 7, 3, tzinfo=pacific),
            "America/Los_Angeles",
        )
        points = self.collect(
            local_config,
            QueueHttp(
                [
                    {
                        "pageviews": [{"x": "2026-07-01 00:00:00", "y": 2}],
                        "sessions": [{"x": "2026-07-01 00:00:00", "y": 1}],
                    },
                    stats_response(pageviews=2),
                    [],
                    [],
                ]
            ),
            local_window,
        )
        self.assertEqual(
            {
                point.metric: point.start.date().isoformat()
                for point in points
                if point.metric in {"umami.pageviews", "umami.daily-visitors"}
            },
            {
                "umami.pageviews": "2026-07-01",
                "umami.daily-visitors": "2026-07-01",
            },
        )

        for key in ("pageviews", "visitors", "visits", "bounces", "totaltime"):
            with self.subTest(stats_key=key):
                with self.assertRaisesRegex(ValueError, "count was invalid"):
                    self.collect(
                        config,
                        QueueHttp(
                            [
                                {"pageviews": [], "sessions": []},
                                {key: True},
                            ]
                        ),
                    )

    def test_stats_pageviews_reconcile_with_daily_series(self):
        config = self.config()
        with self.assertRaisesRegex(ValueError, "did not match the daily series"):
            self.collect(
                config,
                QueueHttp(
                    [
                        {
                            "pageviews": [
                                {"x": "2026-07-01", "y": 2},
                                {"x": "2026-07-02", "y": 3},
                            ],
                            "sessions": [],
                        },
                        {"pageviews": {"value": 6}},
                    ]
                ),
            )

        with self.assertRaisesRegex(ValueError, "stats visitors count"):
            self.collect(
                config,
                QueueHttp(
                    [
                        {"pageviews": [], "sessions": []},
                        {"pageviews": 0},
                    ]
                ),
            )

    def test_headline_series_is_chunked_without_changing_daily_grain(self):
        config = self.config()
        window = QueryWindow(
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 6, 10, tzinfo=UTC),
            "UTC",
        )
        http = QueueHttp(
            [
                {"pageviews": [], "sessions": []},
                {"pageviews": [], "sessions": []},
                stats_response(),
                [],
                [],
            ]
        )
        self.collect(config, http, window)

        pageview_urls = [call[1] for call in http.calls if "/pageviews?" in call[1]]
        self.assertEqual(len(pageview_urls), 2)
        queries = [parse_qs(urlsplit(url).query) for url in pageview_urls]
        self.assertEqual([query["unit"] for query in queries], [["day"], ["day"]])
        self.assertEqual(
            [query["startAt"][0] for query in queries],
            [
                str(int(datetime(2026, 5, 1, tzinfo=UTC).timestamp() * 1000)),
                str(int(datetime(2026, 6, 1, tzinfo=UTC).timestamp() * 1000)),
            ],
        )
        self.assertEqual(
            [query["endAt"][0] for query in queries],
            [
                str(int(datetime(2026, 6, 1, tzinfo=UTC).timestamp() * 1000) - 1),
                str(int(datetime(2026, 6, 10, tzinfo=UTC).timestamp() * 1000) - 1),
            ],
        )
        self.assertEqual(
            int(queries[0]["endAt"][0]) + 1,
            int(queries[1]["startAt"][0]),
        )

    def test_probe_paginates_websites_and_advertises_real_metrics(self):
        config = self.config()
        first = [{"id": f"site-{index:03d}"} for index in range(100)]
        http = QueueHttp(
            [
                {"data": first, "count": 101},
                {"data": [{"id": "site-100"}], "count": 101},
            ]
        )
        snapshot = UmamiConnector(config, http).probe(
            config.connections[0], self.credential
        )

        self.assertEqual(len(snapshot.resources), 101)
        self.assertIn("umami.pageviews", snapshot.metric_groups)
        self.assertIn("umami.total-time", snapshot.metric_groups)
        self.assertNotIn("umami.summary", snapshot.metric_groups)
        self.assertEqual(
            [parse_qs(urlsplit(call[1]).query)["page"] for call in http.calls],
            [["1"], ["2"]],
        )

    def test_exact_geography_paginates_and_only_short_page_is_final(self):
        config = self.config(
            {"enabled": False, "page_size": 1, "max_pages": 2}
        )
        http = QueueHttp(
            [
                {"pageviews": [], "sessions": []},
                stats_response(),
                [{"name": "us", "visits": 2}],
                [],
                [{"country": "us", "name": "CA", "visits": 2}],
                [],
            ]
        )
        points = self.collect(config, http)
        geography = [
            point
            for point in points
            if point.metric in {"umami.country-visits", "umami.region-visits"}
        ]

        self.assertEqual(len(geography), 2)
        self.assertTrue(
            all(point.completeness is Completeness.FINAL for point in geography)
        )
        expanded_urls = [call[1] for call in http.calls if "/metrics/expanded?" in call[1]]
        self.assertEqual(len(expanded_urls), 4)
        self.assertTrue(any("offset=1" in url for url in expanded_urls))
        self.assertTrue(all("field=" not in url for url in expanded_urls))

        capped_config = self.config(
            {"enabled": False, "page_size": 1, "max_pages": 1}
        )
        capped = self.collect(
            capped_config,
            QueueHttp(
                [
                    {"pageviews": [], "sessions": []},
                    stats_response(),
                    [{"name": "us", "visits": 2}],
                    [{"country": "us", "name": "CA", "visits": 2}],
                ]
            ),
        )
        self.assertTrue(
            all(
                point.completeness is Completeness.UNKNOWN
                for point in capped
                if point.metric
                in {"umami.country-visits", "umami.region-visits"}
            )
        )

    def test_geography_rejects_invalid_counts_and_aggregates_safe_collisions(self):
        config = self.config()
        points = self.collect(
            config,
            QueueHttp(
                [
                    {"pageviews": [], "sessions": []},
                    stats_response(),
                    [
                        {"name": "us", "visits": 2},
                        {"name": "US", "visits": 3},
                        {"name": "gb", "visits": 1.5},
                    ],
                    [],
                ]
            ),
        )
        country = next(
            point
            for point in points
            if point.metric == "umami.country-visits"
            and dict(point.dimensions)["country_code"] == "US"
        )
        self.assertEqual(country.value, 5)
        self.assertIs(country.completeness, Completeness.UNKNOWN)
        self.assertFalse(
            any(
                dict(point.dimensions).get("country_code") == "GB"
                for point in points
            )
        )

    def test_expanded_dimensions_emit_all_measures_from_one_safe_request(self):
        config = self.config(
            {
                "enabled": True,
                "max_days": 1,
                "page_size": 10,
                "max_pages": 1,
                "approved_referrer_domains": ["outside.example"],
                "umami_dimensions": ["browser", "region", "referrer"],
            }
        )
        measure_row = {
            "pageviews": 2,
            "visitors": 1,
            "visits": 1,
            "bounces": 0,
            "totaltime": 12,
        }
        http = QueueHttp(
            [
                {"pageviews": [], "sessions": []},
                stats_response(),
                [],
                [],
                {
                    "startDate": "2026-01-01T00:00:00Z",
                    "endDate": "2026-07-01T18:00:00Z",
                },
                [
                    {"name": "/story/", **measure_row},
                    {
                        "name": "/story",
                        "pageviews": 3,
                        "visitors": 2,
                        "visits": 2,
                        "bounces": 1,
                        "totaltime": 15,
                    },
                ],
                [],
                [],
                [{"name": "Firefox", **measure_row}],
                [
                    {
                        "name": "https://outside.example/article?person=a@example.com",
                        **measure_row,
                    }
                ],
                [
                    {"country": "US", "name": "CA", **measure_row},
                    {"country": "CA", "name": "CA", **measure_row},
                ],
            ]
        )
        points = self.collect(config, http, self.one_day)

        path_points = {
            point.metric: point
            for point in points
            if dict(point.dimensions).get("dimension_type") == "path"
        }
        self.assertEqual(
            set(path_points),
            {
                "umami.dimension-pageviews",
                "umami.dimension-visitors",
                "umami.dimension-visits",
                "umami.dimension-bounces",
                "umami.dimension-total-time",
            },
        )
        self.assertEqual(path_points["umami.dimension-pageviews"].value, 5)
        self.assertEqual(path_points["umami.dimension-total-time"].value, 27)
        self.assertEqual(path_points["umami.dimension-total-time"].unit, "seconds")
        self.assertTrue(
            all(point.completeness is Completeness.FINAL for point in path_points.values())
        )
        route_pageviews = next(
            point for point in points if point.metric == "umami.route-pageviews"
        )
        route_visits = next(
            point for point in points if point.metric == "umami.route-visits"
        )
        self.assertEqual(route_pageviews.value, 5)
        self.assertEqual(route_visits.value, 3)

        referrer = next(
            point
            for point in points
            if dict(point.dimensions).get("dimension_type") == "referrer"
        )
        self.assertEqual(
            dict(referrer.dimensions),
            {
                "dimension_type": "referrer",
                "dimension_value": "outside.example",
                "dimension_value_kind": "referrer_domain",
            },
        )
        self.assertNotIn("person", repr(points))

        region_values = {
            dict(point.dimensions)["dimension_value"]
            for point in points
            if point.metric == "umami.dimension-visits"
            and dict(point.dimensions).get("dimension_type") == "region"
        }
        self.assertEqual(region_values, {"US-CA", "CA-CA"})

        expanded_urls = [call[1] for call in http.calls if "/metrics/expanded?" in call[1]]
        daily_urls = expanded_urls[2:]
        self.assertEqual(len(daily_urls), 6)
        self.assertEqual(
            {parse_qs(urlsplit(url).query)["type"][0] for url in daily_urls},
            {"path", "entry", "exit", "browser", "referrer", "region"},
        )
        self.assertTrue(all("field=" not in url for url in daily_urls))

    def test_partial_first_observed_day_is_retained_but_not_called_final(self):
        config = self.config(
            {
                "enabled": True,
                "max_days": 1,
                "page_size": 10,
                "max_pages": 1,
            }
        )
        http = QueueHttp(
            [
                {"pageviews": [], "sessions": []},
                stats_response(),
                [],
                [],
                {
                    "startDate": "2026-07-01T12:00:00Z",
                    "endDate": "2026-07-02T00:00:00Z",
                },
                [{
                    "name": "/story",
                    "pageviews": 3,
                    "visitors": 2,
                    "visits": 2,
                    "bounces": 0,
                    "totaltime": 10,
                }],
                [],
                [],
            ]
        )
        points = self.collect(config, http, self.one_day)

        route = next(
            point for point in points if point.metric == "umami.route-pageviews"
        )
        self.assertEqual(route.value, 3)
        self.assertIs(route.completeness, Completeness.UNKNOWN)

    def test_configured_event_series_is_window_bounded_and_aggregates_duplicates(self):
        config = self.config(
            {
                "enabled": True,
                "max_days": 2,
                "page_size": 10,
                "max_pages": 1,
                "umami_event_names": ["signup"],
            }
        )
        http = QueueHttp(
            [
                {"pageviews": [], "sessions": []},
                stats_response(),
                [],
                [],
                {
                    "startDate": "2026-01-01T00:00:00Z",
                    "endDate": "2026-07-02T18:00:00Z",
                },
                [],
                [],
                [],
                [],
                [],
                [],
                [
                    {"x": "signup", "t": "2026-07-01T00:00:00Z", "y": 2},
                    {"x": "signup", "t": "2026-07-01", "y": 3},
                    {"x": "signup", "t": "2026-07-02T00:00:00Z", "y": 4},
                ],
            ]
        )
        points = self.collect(config, http)
        events = [
            point
            for point in points
            if point.metric == "umami.configured-event-count"
        ]
        self.assertEqual(
            [(point.start.date().isoformat(), point.value) for point in events],
            [("2026-07-01", 5), ("2026-07-02", 4)],
        )
        event_urls = [call[1] for call in http.calls if "/events/series?" in call[1]]
        self.assertEqual(len(event_urls), 1)
        event_query = parse_qs(urlsplit(event_urls[0]).query)
        self.assertEqual(event_query["unit"], ["day"])
        self.assertEqual(event_query["timezone"], ["UTC"])
        self.assertEqual(event_query["event"], ["signup"])
        self.assertEqual(event_query["startAt"], ["1782864000000"])
        self.assertEqual(event_query["endAt"], ["1783036799999"])

        options = route_analytics_options(config.bindings[0])
        site = config.sites[0]
        for row, message in (
            ({"x": "other", "t": "2026-07-01", "y": 1}, "identity"),
            ({"x": "signup", "t": "2026-06-30", "y": 1}, "outside"),
            ({"x": "signup", "t": "2026-07-01", "y": 1.5}, "count"),
        ):
            with self.subTest(message=message):
                connector = UmamiConnector(config, QueueHttp([[row]]))
                with self.assertRaisesRegex(ValueError, message):
                    list(
                        connector._configured_event_points(
                            "https://analytics.example.invalid/api/websites/demo",
                            {},
                            self.one_day.start,
                            self.one_day.end,
                            site,
                            options,
                            "signup",
                        )
                    )


if __name__ == "__main__":
    unittest.main()
