from __future__ import annotations

import tempfile
import unittest
import csv
import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from boho_analytics_platform.config import load_config
from boho_analytics_platform.models import Completeness, MetricPoint, QueryWindow, TimeGrain
from boho_analytics_platform.reporting import ReportService, to_csv, to_series_csv
from boho_analytics_platform.storage import SQLiteMetricStore
from support import config_text, write_fixture


def metric(name, value, day, unit, dimensions=(), *, site_id="example-site", observed_hour=12):
    start = datetime(2026, 7, day, tzinfo=UTC)
    source = "search-console" if name.startswith("search.") else "cloudflare-forms"
    return MetricPoint("example-client", site_id, source,
        name, unit, start, start + timedelta(days=1), TimeGrain.DAY, Decimal(str(value)), tuple(sorted(dimensions)),
        Completeness.FINAL, datetime(2026, 7, day, observed_hour, tzinfo=UTC))


def pageview_metric(
    name,
    value,
    day,
    *,
    completeness=Completeness.FINAL,
    route=None,
    site_id="example-site",
    observed_at=None,
):
    start = datetime(2026, 7, day, tzinfo=UTC)
    source = "google-analytics" if name.startswith("google.") else "umami"
    dimensions = () if route is None else (("route", route),)
    return MetricPoint(
        "example-client", site_id, source, name, "count", start,
        start + timedelta(days=1), TimeGrain.DAY, Decimal(str(value)),
        dimensions, completeness, observed_at or datetime.now(UTC),
    )


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup); root = Path(self.temporary.name)
        fixture = root / "fixture.json"; write_fixture(fixture)
        text = config_text(root / "state.db", fixture).replace(
            'metric_ids = ["umami.pageviews", "forms.submissions", "forms.inbox-deliveries"]',
            'metric_ids = ["search.clicks", "search.impressions", "search.ctr", "search.position", "forms.submissions"]')
        native_connections = '''[[connections]]
id = "native-umami"
provider = "umami"
credential_ref = "none:test"
[[connections]]
id = "native-search"
provider = "search-console"
credential_ref = "none:test"
[[connections]]
id = "native-cloudflare"
provider = "cloudflare"
credential_ref = "none:test"
[[connections]]
id = "native-google"
provider = "google-analytics"
credential_ref = "none:test"
[[connections]]
id = "native-forms"
provider = "cloudflare-forms"
credential_ref = "none:test"
[[connections]]
id = "native-inbox"
provider = "forms-inbox"
credential_ref = "none:test"
'''
        native_bindings = '''[[bindings]]
site_id = "example-site"
connection_id = "native-umami"
resource_type = "website"
resource_id = "native-demo"
metric_groups = ["traffic"]
[[bindings]]
site_id = "example-site"
connection_id = "native-search"
resource_type = "site"
resource_id = "sc-domain:example.com"
metric_groups = ["search"]
[[bindings]]
site_id = "example-site"
connection_id = "native-cloudflare"
resource_type = "zone"
resource_id = "example-zone"
metric_groups = ["traffic"]
[[bindings]]
site_id = "example-site"
connection_id = "native-google"
resource_type = "property"
resource_id = "123456"
metric_groups = ["traffic"]
[[bindings]]
site_id = "example-site"
connection_id = "native-forms"
resource_type = "forms-site"
resource_id = "example-site"
metric_groups = ["forms"]
[[bindings]]
site_id = "example-site"
connection_id = "native-inbox"
resource_type = "mailbox"
resource_id = "example"
metric_groups = ["forms"]
'''
        text = text.replace("[[bindings]]", native_connections + "[[bindings]]", 1)
        text = text.replace("[[reports]]", native_bindings + "[[reports]]", 1)
        path = root / "platform.toml"; path.write_text(text, encoding="utf-8"); self.config = load_config(path)
        self.store = SQLiteMetricStore(root / "state.db"); self.store.initialize()
        self.window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 3, tzinfo=UTC), "UTC")

    def _multi_site_config(
        self, *, include_second_inbox=True, include_second_fixture=True
    ):
        root = Path(self.temporary.name)
        text = (root / "platform.toml").read_text(encoding="utf-8")
        second_site = '''[[sites]]
id = "second-site"
client_id = "example-client"
name = "Second Site"
canonical_url = "https://second.example.com"
timezone = "UTC"
'''
        second_binding = '''[[bindings]]
site_id = "second-site"
connection_id = "example-connection"
resource_type = "website"
resource_id = "second-demo"
metric_groups = ["traffic"]
[[bindings]]
site_id = "second-site"
connection_id = "native-umami"
resource_type = "website"
resource_id = "second-native-demo"
metric_groups = ["traffic"]
[[bindings]]
site_id = "second-site"
connection_id = "native-search"
resource_type = "site"
resource_id = "sc-domain:second.example.com"
metric_groups = ["search"]
[[bindings]]
site_id = "second-site"
connection_id = "native-cloudflare"
resource_type = "zone"
resource_id = "second-zone"
metric_groups = ["traffic"]
[[bindings]]
site_id = "second-site"
connection_id = "native-forms"
resource_type = "forms-site"
resource_id = "second-site"
metric_groups = ["forms"]
[[bindings]]
site_id = "second-site"
connection_id = "native-inbox"
resource_type = "mailbox"
resource_id = "second-example"
metric_groups = ["forms"]
'''
        if not include_second_inbox:
            second_binding = second_binding.replace('''[[bindings]]
site_id = "second-site"
connection_id = "native-inbox"
resource_type = "mailbox"
resource_id = "second-example"
metric_groups = ["forms"]
''', "")
        if not include_second_fixture:
            second_binding = second_binding.replace('''[[bindings]]
site_id = "second-site"
connection_id = "example-connection"
resource_type = "website"
resource_id = "second-demo"
metric_groups = ["traffic"]
''', "")
        text = text.replace("[[connections]]", second_site + "[[connections]]", 1)
        text = text.replace("[[reports]]", second_binding + "[[reports]]", 1)
        text = text.replace('site_ids = ["example-site"]', 'site_ids = ["example-site", "second-site"]')
        path = root / "multi.toml"
        path.write_text(text, encoding="utf-8")
        return load_config(path)

    def _enable_pageview_routes(self):
        providers = {
            connection.id: connection.provider
            for connection in self.config.connections
        }
        for binding in self.config.bindings:
            if providers[binding.connection_id] in {"google-analytics", "umami"}:
                object.__setattr__(binding, "options", {
                    "route_analytics": {"enabled": True},
                })

    def _record_current_provider_runs(self, window, providers=(
        "google-analytics", "umami",
    )):
        run_ids = []
        connection_sources = {
            connection.id: connection.provider
            for connection in self.config.connections
        }
        for binding in self.config.bindings:
            source = connection_sources[binding.connection_id]
            if source not in providers or binding.site_id != "example-site":
                continue
            binding_key = (
                f"{binding.site_id}:{binding.connection_id}:"
                f"{binding.resource_type}:{binding.resource_id}"
            )
            run_id = self.store.start_run(
                binding.connection_id, binding.site_id,
                binding_key=binding_key, source=source, window=window,
            )
            run_ids.append(run_id)
        observed_at = datetime.now(UTC)
        for run_id in run_ids:
            self.store.finish_run(
                run_id, "success", points=1, result_kind="data",
                data_through=window.end,
            )
        return observed_at

    def test_provider_comparison_pairs_only_complete_dates_and_reconciles_routes(self):
        self._enable_pageview_routes()
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        observed_at = self._record_current_provider_runs(one_day)
        self.store.upsert([
            pageview_metric("google.pageviews", 120, 1, observed_at=observed_at),
            pageview_metric(
                "google.page-path-views", 70, 1, route="/",
                observed_at=observed_at,
            ),
            pageview_metric(
                "google.page-path-views", 50, 1, route="/about",
                observed_at=observed_at,
            ),
            pageview_metric("umami.pageviews", 100, 1, observed_at=observed_at),
            pageview_metric("umami.route-pageviews", 100, 1, route="/", observed_at=observed_at),
        ])

        report = ReportService(self.config, self.store).render("summary", one_day)
        comparison = report["provider_comparisons"][0]

        self.assertTrue(comparison["comparable"])
        self.assertEqual(
            comparison["evidence_state"],
            "within_expected_variation",
        )
        self.assertEqual(comparison["paired_dates"]["count"], 1)
        self.assertEqual(comparison["first_paired_date"], "2026-07-01")
        self.assertEqual(comparison["last_paired_date"], "2026-07-01")
        self.assertEqual(comparison["totals"]["google_pageviews"], 120)
        self.assertEqual(comparison["totals"]["umami_pageviews"], 100)
        self.assertEqual(comparison["totals"]["absolute_difference"], 20)
        self.assertEqual(comparison["totals"]["google_to_umami_ratio"], 1.2)
        self.assertFalse(comparison["low_volume_warning"])
        for provider in ("google-analytics", "umami"):
            reconciliation = comparison["providers"][provider][
                "route_reconciliation"
            ]
            self.assertEqual(reconciliation["status"], "reconciled")
            self.assertEqual(
                reconciliation["headline_total"],
                reconciliation["route_total"],
            )

        exported = list(csv.DictReader(io.StringIO(to_csv(report))))
        comparison_row = next(
            row for row in exported
            if row["record_type"] == "provider_comparison"
        )
        self.assertEqual(comparison_row["evidence_state"], "within_expected_variation")
        self.assertEqual(comparison_row["paired_date_count"], "1")

    def test_provider_route_queries_are_bounded_to_current_window(self):
        calls = []
        original_query = self.store.query

        def capture_query(**kwargs):
            calls.append(kwargs)
            return original_query(**kwargs)

        with patch.object(self.store, "query", side_effect=capture_query):
            ReportService(self.config, self.store).render("summary", self.window)

        self.assertEqual(len(calls), 3)
        self.assertIn("google.page-path-views", calls[0]["metric_ids"])
        for call in calls[1:]:
            self.assertNotIn("google.page-path-views", call["metric_ids"])
            self.assertNotIn("umami.route-pageviews", call["metric_ids"])
        self.assertEqual(
            tuple(calls[2]["metric_ids"]),
            (
                "google.pageviews",
                "umami.pageviews",
            ),
        )


    def test_provider_comparison_qualifies_unequal_coverage_and_uses_paired_totals(self):
        self._enable_pageview_routes()
        google_observed_at = self._record_current_provider_runs(
            self.window, providers=("google-analytics",)
        )
        umami_observed_at = self._record_current_provider_runs(QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC), "UTC",
        ), providers=("umami",))
        self.store.upsert([
            pageview_metric("google.pageviews", 10, 1, observed_at=google_observed_at),
            pageview_metric("google.pageviews", 500, 2, observed_at=google_observed_at),
            pageview_metric("umami.pageviews", 8, 1, observed_at=umami_observed_at),
        ])

        report = ReportService(self.config, self.store).render(
            "summary", self.window
        )
        comparison = report["provider_comparisons"][0]

        self.assertTrue(comparison["comparable"])
        self.assertEqual(comparison["evidence_state"], "coverage_mismatch")
        self.assertEqual(comparison["paired_dates"]["count"], 1)
        self.assertEqual(comparison["google_only_dates"]["count"], 1)
        self.assertEqual(comparison["umami_only_dates"]["count"], 0)
        self.assertEqual(comparison["totals"]["google_pageviews"], 10)
        self.assertEqual(comparison["totals"]["umami_pageviews"], 8)
        self.assertNotEqual(comparison["totals"]["google_pageviews"], 510)

    def test_opposing_daily_differences_do_not_cancel_into_expected_variation(self):
        observed_at = self._record_current_provider_runs(self.window)
        self.store.upsert([
            pageview_metric("google.pageviews", 200, 1, observed_at=observed_at),
            pageview_metric("umami.pageviews", 100, 1, observed_at=observed_at),
            pageview_metric("google.pageviews", 100, 2, observed_at=observed_at),
            pageview_metric("umami.pageviews", 200, 2, observed_at=observed_at),
        ])

        comparison = ReportService(self.config, self.store).render(
            "summary", self.window
        )["provider_comparisons"][0]

        self.assertEqual(comparison["totals"]["google_pageviews"], 300)
        self.assertEqual(comparison["totals"]["umami_pageviews"], 300)
        self.assertEqual(comparison["evidence_state"], "persistent_divergence")

    def test_aligned_zero_day_keeps_exact_half_divergence_unknown(self):
        state = ReportService._comparison_evidence_state(
            {
                "2026-07-01": Decimal(),
                "2026-07-02": Decimal("200"),
            },
            {
                "2026-07-01": Decimal(),
                "2026-07-02": Decimal("100"),
            },
            ("2026-07-01", "2026-07-02"),
            (),
            (),
            low_volume=False,
        )

        self.assertEqual(state, "unknown")

    def test_divergence_minority_majority_and_single_date_contracts_remain_stable(self):
        cases = (
            (
                {"2026-07-01": Decimal("200")},
                {"2026-07-01": Decimal("100")},
                ("2026-07-01",),
                "isolated_divergence",
            ),
            (
                {
                    "2026-07-01": Decimal("200"),
                    "2026-07-02": Decimal("100"),
                    "2026-07-03": Decimal("100"),
                },
                {day: Decimal("100") for day in (
                    "2026-07-01", "2026-07-02", "2026-07-03"
                )},
                ("2026-07-01", "2026-07-02", "2026-07-03"),
                "isolated_divergence",
            ),
            (
                {
                    "2026-07-01": Decimal("200"),
                    "2026-07-02": Decimal("200"),
                    "2026-07-03": Decimal("100"),
                },
                {day: Decimal("100") for day in (
                    "2026-07-01", "2026-07-02", "2026-07-03"
                )},
                ("2026-07-01", "2026-07-02", "2026-07-03"),
                "persistent_divergence",
            ),
        )
        for google, umami, paired_dates, expected in cases:
            with self.subTest(expected=expected):
                state = ReportService._comparison_evidence_state(
                    google, umami, paired_dates, (), (), low_volume=False
                )
                self.assertEqual(state, expected)

    def test_large_counts_use_exact_ratio_thresholds(self):
        umami = Decimal("1" + "0" * 37)
        below = Decimal("7" + "9" * 36)
        above = Decimal("125" + "0" * 34 + "1")

        self.assertEqual(
            ReportService._comparison_evidence_state(
                {"2026-07-01": below},
                {"2026-07-01": umami},
                ("2026-07-01",), (), (), low_volume=False,
            ),
            "isolated_divergence",
        )
        self.assertEqual(
            ReportService._comparison_evidence_state(
                {"2026-07-01": above},
                {"2026-07-01": umami},
                ("2026-07-01",), (), (), low_volume=False,
            ),
            "isolated_divergence",
        )

    def test_exact_half_multi_date_divergence_is_unknown(self):
        state = ReportService._comparison_evidence_state(
            {
                "2026-07-01": Decimal("200"),
                "2026-07-02": Decimal("200"),
                "2026-07-03": Decimal("100"),
                "2026-07-04": Decimal("100"),
            },
            {
                "2026-07-01": Decimal("100"),
                "2026-07-02": Decimal("100"),
                "2026-07-03": Decimal("100"),
                "2026-07-04": Decimal("100"),
            },
            (
                "2026-07-01",
                "2026-07-02",
                "2026-07-03",
                "2026-07-04",
            ),
            (),
            (),
            low_volume=False,
        )

        self.assertEqual(state, "unknown")

    def test_legacy_provider_runs_cannot_prove_quiet_pageview_dates(self):
        start = datetime(2026, 7, 1, tzinfo=UTC)
        end = datetime(2026, 7, 2, tzinfo=UTC)
        legacy_run = {
            "site_id": "example-site",
            "source": "google-analytics",
            "binding_key": "example-site:native-google:property:123456",
            "window_start": start,
            "window_end": end,
            "result_kind": "data",
            "data_through": end,
            "started_at": end,
            "finished_at": end,
        }
        service = ReportService(
            self.config,
            SimpleNamespace(query_sync_coverage=lambda **_kwargs: [legacy_run]),
        )

        dates = service._successful_provider_dates(
            "example-site",
            "google-analytics",
            QueryWindow(start, end, "UTC"),
        )

        self.assertEqual(dates, set())

    def test_invalid_retained_pageview_fact_makes_cell_incomplete(self):
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        observed_at = self._record_current_provider_runs(
            one_day, providers=("google-analytics",)
        )
        self.store.upsert([
            pageview_metric(
                "google.pageviews",
                Decimal("-0.5"),
                1,
                observed_at=observed_at,
            ),
        ])

        service = ReportService(self.config, self.store)
        retained = self.store.query(
            client_id="example-client",
            site_ids=("example-site",),
            metric_ids=("google.pageviews",),
            window=one_day,
        )
        coverage, _health = service._coverage(
            ("example-site",), ("google.pageviews",), retained, one_day
        )
        report = service.render("summary", one_day)
        comparison = report["provider_comparisons"][0]

        self.assertNotEqual(
            coverage["by_metric"]["google.pageviews"], "complete"
        )
        google = comparison["providers"]["google-analytics"]
        self.assertEqual(google["complete_dates"]["count"], 0)
        self.assertIsNone(comparison["totals"]["google_pageviews"])
        self.assertEqual(comparison["evidence_state"], "non_comparable")


    def test_provider_comparison_without_overlap_is_non_comparable_not_zero(self):
        self._enable_pageview_routes()
        google_observed_at = self._record_current_provider_runs(QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC), "UTC",
        ), providers=("google-analytics",))
        umami_observed_at = self._record_current_provider_runs(QueryWindow(
            datetime(2026, 7, 2, tzinfo=UTC),
            datetime(2026, 7, 3, tzinfo=UTC), "UTC",
        ), providers=("umami",))
        self.store.upsert([
            pageview_metric("google.pageviews", 10, 1, observed_at=google_observed_at),
            pageview_metric("umami.pageviews", 8, 2, observed_at=umami_observed_at),
        ])

        report = ReportService(self.config, self.store).render(
            "summary", self.window
        )
        comparison = report["provider_comparisons"][0]

        self.assertFalse(comparison["comparable"])
        self.assertEqual(comparison["evidence_state"], "non_comparable")
        self.assertEqual(comparison["paired_dates"]["count"], 0)
        self.assertIsNone(comparison["first_paired_date"])
        self.assertIsNone(comparison["last_paired_date"])
        self.assertEqual(comparison["totals"], {
            "google_pageviews": None,
            "umami_pageviews": None,
            "absolute_difference": None,
            "google_to_umami_ratio": None,
        })

    def test_provider_comparison_marks_small_paired_populations_low_volume(self):
        self._enable_pageview_routes()
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        observed_at = self._record_current_provider_runs(one_day)
        self.store.upsert([
            pageview_metric("google.pageviews", 3, 1, observed_at=observed_at),
            pageview_metric("umami.pageviews", 1, 1, observed_at=observed_at),
        ])

        report = ReportService(self.config, self.store).render("summary", one_day)
        comparison = report["provider_comparisons"][0]

        self.assertTrue(comparison["comparable"])
        self.assertTrue(comparison["low_volume_warning"])
        self.assertEqual(comparison["evidence_state"], "low_volume")

    def test_provider_route_reconciliation_withholds_incomplete_and_mismatched_sums(self):
        self._enable_pageview_routes()
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        observed_at = self._record_current_provider_runs(one_day)
        self.store.upsert([
            pageview_metric("google.pageviews", 10, 1, observed_at=observed_at),
            pageview_metric(
                "google.page-path-views",
                10,
                1,
                route="/",
                completeness=Completeness.UNKNOWN,
                observed_at=observed_at,
            ),
            pageview_metric("umami.pageviews", 8, 1, observed_at=observed_at),
            pageview_metric("umami.route-pageviews", 7, 1, route="/", observed_at=observed_at),
        ])

        comparison = ReportService(self.config, self.store).render(
            "summary", one_day
        )["provider_comparisons"][0]
        google = comparison["providers"]["google-analytics"][
            "route_reconciliation"
        ]
        umami = comparison["providers"]["umami"]["route_reconciliation"]

        self.assertEqual(google["status"], "withheld")
        self.assertEqual(google["reason"], "route_coverage_incomplete")
        self.assertEqual(umami["status"], "withheld")
        self.assertEqual(umami["reason"], "route_sum_differs_from_headline")
        self.assertEqual(umami["headline_total"], 8)
        self.assertEqual(umami["route_total"], 7)

    def test_partial_day_route_fact_cannot_reconcile_a_complete_local_day(self):
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        route = replace(
            pageview_metric(
                "google.page-path-views", 10, 1, route="/"
            ),
            end=datetime(2026, 7, 1, 12, tzinfo=UTC),
        )

        reconciliation = ReportService(
            self.config, self.store
        )._route_reconciliation(
            points=[route],
            site_id="example-site",
            provider="google-analytics",
            headline_metric="google.pageviews",
            route_metric="google.page-path-views",
            complete_dates={"2026-07-01"},
            headline_values={"2026-07-01": Decimal("10")},
            route_enabled=True,
            window=one_day,
        )

        self.assertEqual(reconciliation["status"], "withheld")
        self.assertEqual(
            reconciliation["reason"], "route_coverage_incomplete"
        )

    def test_provider_cells_use_site_timezone_not_report_timezone(self):
        self._enable_pageview_routes()
        chicago_config = replace(
            self.config,
            sites=tuple(
                replace(site, timezone="America/Chicago")
                if site.id == "example-site" else site
                for site in self.config.sites
            ),
        )
        chicago_day = QueryWindow(
            datetime(2026, 7, 1, 5, tzinfo=UTC),
            datetime(2026, 7, 2, 5, tzinfo=UTC),
            "UTC",
        )
        observed_at = self._record_current_provider_runs(chicago_day)
        points = [
            replace(
                pageview_metric(name, value, 1, route=route, observed_at=observed_at),
                start=chicago_day.start,
                end=chicago_day.end,
            )
            for name, value, route in (
                ("google.pageviews", 10, None),
                ("google.page-path-views", 10, "/"),
                ("umami.pageviews", 8, None),
                ("umami.route-pageviews", 8, "/"),
            )
        ]
        self.store.upsert(points)

        comparison = ReportService(chicago_config, self.store).render(
            "summary", chicago_day
        )["provider_comparisons"][0]

        self.assertEqual(
            comparison["paired_dates"]["ranges"],
            [{"start": "2026-07-01", "end": "2026-07-01"}],
        )
        self.assertEqual(
            comparison["providers"]["google-analytics"]["route_reconciliation"]["status"],
            "reconciled",
        )
        self.assertEqual(
            comparison["providers"]["umami"]["route_reconciliation"]["status"],
            "reconciled",
        )

    def test_route_reconciliation_accepts_exact_dst_site_calendar_day(self):
        chicago_config = replace(
            self.config,
            sites=tuple(
                replace(site, timezone="America/Chicago")
                if site.id == "example-site" else site
                for site in self.config.sites
            ),
        )
        window = QueryWindow(
            datetime(2026, 11, 1, 5, tzinfo=UTC),
            datetime(2026, 11, 2, 6, tzinfo=UTC),
            "UTC",
        )
        route = MetricPoint(
            "example-client",
            "example-site",
            "google-analytics",
            "google.page-path-views",
            "count",
            window.start,
            window.end,
            TimeGrain.DAY,
            Decimal("10"),
            (("route", "/"),),
            Completeness.FINAL,
            datetime.now(UTC),
        )

        reconciliation = ReportService(
            chicago_config, self.store
        )._route_reconciliation(
            points=[route],
            site_id="example-site",
            provider="google-analytics",
            headline_metric="google.pageviews",
            route_metric="google.page-path-views",
            complete_dates={"2026-11-01"},
            headline_values={"2026-11-01": Decimal("10")},
            route_enabled=True,
            window=window,
        )

        self.assertEqual(window.end - window.start, timedelta(hours=25))
        self.assertEqual(reconciliation["status"], "reconciled")

    def test_route_reconciliation_rejects_clipped_midnight_fold(self):
        havana_config = replace(
            self.config,
            sites=tuple(
                replace(site, timezone="America/Havana")
                if site.id == "example-site" else site
                for site in self.config.sites
            ),
        )
        exact_window = QueryWindow(
            datetime(2026, 11, 1, 4, tzinfo=UTC),
            datetime(2026, 11, 2, 5, tzinfo=UTC),
            "UTC",
        )
        route = MetricPoint(
            "example-client",
            "example-site",
            "google-analytics",
            "google.page-path-views",
            "count",
            datetime(2026, 11, 1, 5, tzinfo=UTC),
            datetime(2026, 11, 2, 5, tzinfo=UTC),
            TimeGrain.DAY,
            Decimal("10"),
            (("route", "/"),),
            Completeness.FINAL,
            datetime.now(UTC),
        )

        reconciliation = ReportService(
            havana_config, self.store
        )._route_reconciliation(
            points=[route],
            site_id="example-site",
            provider="google-analytics",
            headline_metric="google.pageviews",
            route_metric="google.page-path-views",
            complete_dates={"2026-11-01"},
            headline_values={"2026-11-01": Decimal("10")},
            route_enabled=True,
            window=exact_window,
        )

        self.assertEqual(reconciliation["status"], "withheld")
        self.assertEqual(
            reconciliation["reason"], "route_coverage_incomplete"
        )

    def test_replaced_binding_facts_require_current_run_identity_evidence(self):
        old_key = "example-site:native-google:property:property-A"
        old_run = self.store.start_run(
            "native-google",
            "example-site",
            binding_key=old_key,
            source="google-analytics",
            window=self.window,
        )
        self.store.finish_run(
            old_run,
            "success",
            points=1,
            result_kind="data",
            data_through=self.window.end,
        )
        old_fact = pageview_metric("google.pageviews", 12, 1)
        self.store.upsert([old_fact])

        service = ReportService(self.config, self.store)
        comparison = service.render(
            "summary", self.window
        )["provider_comparisons"][0]

        self.assertEqual(
            comparison["providers"]["google-analytics"]["complete_dates"]["count"],
            0,
        )
        self.assertIsNone(
            comparison["providers"]["google-analytics"]["first_available_date"]
        )
        self.assertEqual(
            ReportService(self.config, None)._currently_supported_points(
                [old_fact]
            ),
            [],
        )
        self.assertEqual(
            self.store.query(
                client_id="example-client",
                site_ids=("example-site",),
                metric_ids=("google.pageviews",),
                window=self.window,
            ),
            [old_fact],
        )

    def test_current_binding_run_restores_eligibility_after_replacement(self):
        old_key = "example-site:native-google:property:property-A"
        current_key = "example-site:native-google:property:123456"
        old_run = self.store.start_run(
            "native-google",
            "example-site",
            binding_key=old_key,
            source="google-analytics",
            window=self.window,
        )
        self.store.finish_run(
            old_run,
            "success",
            points=1,
            result_kind="data",
            data_through=self.window.end,
        )
        current_run = self.store.start_run(
            "native-google",
            "example-site",
            binding_key=current_key,
            source="google-analytics",
            window=self.window,
        )
        current_fact = replace(
            pageview_metric("google.pageviews", 14, 1),
            observed_at=datetime.now(UTC),
        )
        self.store.upsert([current_fact])
        self.store.finish_run(
            current_run,
            "success",
            points=1,
            result_kind="data",
            data_through=self.window.end,
        )

        comparison = ReportService(self.config, self.store).render(
            "summary", self.window
        )["provider_comparisons"][0]

        self.assertEqual(
            comparison["providers"]["google-analytics"]["complete_dates"]["count"],
            2,
        )
        self.assertEqual(
            comparison["providers"]["google-analytics"]["complete_dates"]["ranges"],
            [{"start": "2026-07-01", "end": "2026-07-02"}],
        )
        self.assertEqual(
            comparison["providers"]["google-analytics"]["route_reconciliation"]["headline_total"],
            14,
        )

    def test_later_removed_binding_fact_cannot_match_an_earlier_current_run(self):
        current_key = "example-site:native-google:property:123456"
        removed_key = "example-site:native-google:property:property-A"
        current_run = self.store.start_run(
            "native-google",
            "example-site",
            binding_key=current_key,
            source="google-analytics",
            window=self.window,
        )
        self.store.finish_run(
            current_run,
            "success",
            points=1,
            result_kind="data",
            data_through=self.window.end,
        )
        removed_run = self.store.start_run(
            "native-google",
            "example-site",
            binding_key=removed_key,
            source="google-analytics",
            window=self.window,
        )
        removed_fact = pageview_metric(
            "google.pageviews", 99, 1, observed_at=datetime.now(UTC)
        )
        self.store.upsert([removed_fact])
        self.store.finish_run(
            removed_run,
            "success",
            points=1,
            result_kind="data",
            data_through=self.window.end,
        )

        attributed = ReportService(
            self.config, self.store
        )._current_binding_attributed_points([removed_fact])

        self.assertEqual(attributed, [])
        self.assertEqual(
            self.store.query(
                client_id="example-client",
                site_ids=("example-site",),
                metric_ids=("google.pageviews",),
                window=self.window,
            ),
            [removed_fact],
        )

    def test_overlapping_removed_binding_run_makes_attribution_ambiguous(self):
        current_key = "example-site:native-google:property:123456"
        removed_key = "example-site:native-google:property:property-A"
        current_run = self.store.start_run(
            "native-google", "example-site", binding_key=current_key,
            source="google-analytics", window=self.window,
        )
        removed_run = self.store.start_run(
            "native-google", "example-site", binding_key=removed_key,
            source="google-analytics", window=self.window,
        )
        removed_fact = pageview_metric(
            "google.pageviews", 77, 1, observed_at=datetime.now(UTC)
        )
        self.store.upsert([removed_fact])
        self.store.finish_run(
            current_run, "success", points=1, result_kind="data",
            data_through=self.window.end,
        )
        self.store.finish_run(
            removed_run, "success", points=1, result_kind="data",
            data_through=self.window.end,
        )

        self.assertEqual(
            ReportService(self.config, self.store)
            ._current_binding_attributed_points([removed_fact]),
            [],
        )
        self.assertEqual(
            self.store.query(
                client_id="example-client", site_ids=("example-site",),
                metric_ids=("google.pageviews",), window=self.window,
            ),
            [removed_fact],
        )

    def test_adjacent_partial_runs_cannot_form_a_complete_daily_cell(self):
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        binding_key = "example-site:native-google:property:123456"
        for start, end in (
            (one_day.start, one_day.start + timedelta(hours=12)),
            (one_day.start + timedelta(hours=12), one_day.end),
        ):
            partial = QueryWindow(start, end, "UTC")
            run_id = self.store.start_run(
                "native-google", "example-site", binding_key=binding_key,
                source="google-analytics", window=partial,
            )
            self.store.finish_run(
                run_id, "success", result_kind="empty"
            )

        self.assertEqual(
            ReportService(self.config, self.store)._successful_provider_dates(
                "example-site", "google-analytics", one_day
            ),
            set(),
        )

    def test_open_current_or_future_day_is_not_mature_provider_coverage(self):
        tomorrow = (
            datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        )
        future_day = QueryWindow(
            tomorrow, tomorrow + timedelta(days=1), "UTC"
        )
        binding_key = "example-site:native-google:property:123456"
        run_id = self.store.start_run(
            "native-google", "example-site", binding_key=binding_key,
            source="google-analytics", window=future_day,
        )
        self.store.finish_run(run_id, "success", result_kind="empty")

        self.assertEqual(
            ReportService(self.config, self.store)._successful_provider_dates(
                "example-site", "google-analytics", future_day
            ),
            set(),
        )

    def test_route_reconciliation_uses_only_latest_complete_run_snapshot(self):
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        binding_key = "example-site:native-google:property:123456"
        first_run = self.store.start_run(
            "native-google", "example-site", binding_key=binding_key,
            source="google-analytics", window=one_day,
        )
        first_points = [
            pageview_metric("google.pageviews", 10, 1),
            pageview_metric("google.page-path-views", 6, 1, route="/kept"),
            pageview_metric("google.page-path-views", 4, 1, route="/removed"),
        ]
        self.store.upsert(first_points)
        self.store.finish_run(
            first_run, "success", points=3, result_kind="data",
            data_through=one_day.end,
        )

        second_run = self.store.start_run(
            "native-google", "example-site", binding_key=binding_key,
            source="google-analytics", window=one_day,
        )
        second_points = [
            pageview_metric("google.pageviews", 7, 1),
            pageview_metric("google.page-path-views", 7, 1, route="/kept"),
        ]
        self.store.upsert(second_points)
        self.store.finish_run(
            second_run, "success", points=2, result_kind="data",
            data_through=one_day.end,
        )

        stored = self.store.query(
            client_id="example-client", site_ids=("example-site",),
            metric_ids=("google.pageviews", "google.page-path-views"),
            window=one_day,
        )
        service = ReportService(self.config, self.store)
        attributed = service._current_binding_attributed_points(stored)
        self.assertEqual(
            {
                dict(point.dimensions).get("route")
                for point in attributed if point.dimensions
            },
            {"/kept"},
        )
        reconciliation = service._route_reconciliation(
            points=attributed,
            site_id="example-site",
            provider="google-analytics",
            headline_metric="google.pageviews",
            route_metric="google.page-path-views",
            complete_dates={"2026-07-01"},
            headline_values={"2026-07-01": Decimal("7")},
            route_enabled=True,
            window=one_day,
        )
        self.assertEqual(reconciliation["status"], "reconciled")
        self.assertEqual(reconciliation["route_total"], 7)

    def test_disjoint_overlapping_run_does_not_create_false_ambiguity(self):
        day_one = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        day_two = QueryWindow(
            datetime(2026, 7, 2, tzinfo=UTC),
            datetime(2026, 7, 3, tzinfo=UTC),
            "UTC",
        )
        current_run = self.store.start_run(
            "native-google", "example-site",
            binding_key="example-site:native-google:property:123456",
            source="google-analytics", window=day_one,
        )
        removed_run = self.store.start_run(
            "native-google", "example-site",
            binding_key="example-site:native-google:property:property-A",
            source="google-analytics", window=day_two,
        )
        point = pageview_metric(
            "google.pageviews", 9, 1, observed_at=datetime.now(UTC)
        )
        self.store.upsert([point])
        self.store.finish_run(
            current_run, "success", points=1, result_kind="data",
            data_through=day_one.end,
        )
        self.store.finish_run(
            removed_run, "success", points=1, result_kind="data",
            data_through=day_two.end,
        )

        self.assertEqual(
            ReportService(self.config, self.store)
            ._current_binding_attributed_points([point]),
            [point],
        )

    def test_later_removed_binding_run_cannot_leave_current_empty_coverage(self):
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        for binding_key in (
            "example-site:native-google:property:123456",
            "example-site:native-google:property:property-A",
        ):
            run_id = self.store.start_run(
                "native-google", "example-site", binding_key=binding_key,
                source="google-analytics", window=one_day,
            )
            self.store.finish_run(
                run_id, "success", result_kind="empty"
            )

        self.assertEqual(
            ReportService(self.config, self.store)._successful_provider_dates(
                "example-site", "google-analytics", one_day
            ),
            set(),
        )

    def test_route_metric_coverage_is_not_inferred_from_headline_sync_success(self):
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        route = pageview_metric(
            "google.page-path-views",
            10,
            1,
            route="/",
            completeness=Completeness.UNKNOWN,
        )
        binding_key = "example-site:native-google:property:123456"
        run_id = self.store.start_run(
            "native-google",
            "example-site",
            binding_key=binding_key,
            source="google-analytics",
            window=one_day,
        )
        self.store.finish_run(
            run_id,
            "success",
            points=1,
            result_kind="data",
            data_through=one_day.end,
        )

        coverage, _health = ReportService(self.config, self.store)._coverage(
            ("example-site",),
            ("google.page-path-views",),
            [route],
            one_day,
        )

        self.assertEqual(
            coverage["by_metric"]["google.page-path-views"],
            "partial",
        )
        self.assertEqual(
            coverage["by_metric_cells"]["google.page-path-views"]["covered"],
            0,
        )

    def test_ctr_and_position_are_weighted_not_summed(self):
        self.store.upsert([
            metric("search.clicks", 10, 1, "count"), metric("search.impressions", 100, 1, "count"),
            metric("search.ctr", .1, 1, "ratio"), metric("search.position", 2, 1, "position"),
            metric("search.clicks", 20, 2, "count"), metric("search.impressions", 100, 2, "count"),
            metric("search.ctr", .2, 2, "ratio"), metric("search.position", 4, 2, "position")])
        report = ReportService(self.config, self.store).render("summary", self.window)
        values = {row["metric"]: row["value"] for row in report["rows"]}
        self.assertEqual(values["search.ctr"], .15); self.assertEqual(values["search.position"], 3)
        ctr = next(series for series in report["series"] if series["metric"] == "search.ctr")
        self.assertEqual([point["value"] for point in ctr["points"]], [.1, .2])

    def test_ctr_is_unknown_when_any_impression_cell_lacks_click_evidence(self):
        self.store.upsert([
            metric("search.clicks", 2, 1, "count"),
            metric("search.impressions", 8, 1, "count"),
            metric("search.ctr", .25, 1, "ratio"),
            metric("search.impressions", 12, 2, "count"),
            metric("search.ctr", 0, 2, "ratio"),
        ])
        report = ReportService(self.config, self.store).render("summary", self.window)
        self.assertIsNone(report["summary_totals"]["search.ctr"]["value"])
        self.assertFalse(any(row["metric"] == "search.ctr" for row in report["rows"]))
        ctr_series = next(series for series in report["series"] if series["metric"] == "search.ctr")
        self.assertEqual(ctr_series["points"], [{"date": "2026-07-01", "value": .25}])
        self.assertFalse(report["complete"])

    def test_portfolio_summary_totals_weight_across_sites(self):
        config = self._multi_site_config()
        self.store.upsert([
            metric("search.clicks", 1, 1, "count"),
            metric("search.impressions", 4, 1, "count"),
            metric("search.position", 10, 1, "position"),
            metric("search.clicks", 8, 1, "count", site_id="second-site"),
            metric("search.impressions", 400, 1, "count", site_id="second-site"),
            metric("search.position", 36.66, 1, "position", site_id="second-site"),
        ])
        one_day = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        report = ReportService(config, self.store).render("summary", one_day)
        totals = report["summary_totals"]
        self.assertAlmostEqual(totals["search.ctr"]["value"], 9 / 404)
        self.assertAlmostEqual(
            totals["search.position"]["value"],
            (10 * 4 + 36.66 * 400) / 404,
        )

    def test_partial_multisite_inputs_withhold_weighted_portfolio_totals(self):
        config = self._multi_site_config()
        window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        self.store.upsert([
            metric("search.clicks", 10, 1, "count"),
            metric("search.impressions", 100, 1, "count"),
            metric("search.position", 10, 1, "position"),
            metric("search.impressions", 100, 1, "count", site_id="second-site"),
            metric("search.position", 20, 1, "position", site_id="second-site"),
        ])
        report = ReportService(config, self.store).render("summary", window)
        self.assertIsNone(report["summary_totals"]["search.ctr"]["value"])
        self.assertEqual(report["summary_totals"]["search.ctr"]["coverage_status"], "partial")

    def test_partial_multisite_position_inputs_withhold_portfolio_position(self):
        config = self._multi_site_config()
        window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        self.store.upsert([
            metric("search.clicks", 1, 1, "count"),
            metric("search.impressions", 10, 1, "count"),
            metric("search.position", 10, 1, "position"),
            metric("search.clicks", 2, 1, "count", site_id="second-site"),
            metric("search.impressions", 90, 1, "count", site_id="second-site"),
        ])
        report = ReportService(config, self.store).render("summary", window)
        self.assertIsNone(report["summary_totals"]["search.position"]["value"])
        self.assertEqual(report["summary_totals"]["search.position"]["coverage_status"], "partial")

    def test_weighted_summary_keeps_internal_inputs_when_report_requests_only_ctr(self):
        root = Path(self.temporary.name)
        text = (root / "platform.toml").read_text(encoding="utf-8").replace(
            'metric_ids = ["search.clicks", "search.impressions", "search.ctr", "search.position", "forms.submissions"]',
            'metric_ids = ["search.ctr"]',
            1,
        )
        path = root / "ctr-only.toml"; path.write_text(text, encoding="utf-8")
        config = load_config(path)
        self.store.upsert([
            metric("search.clicks", 2, 1, "count"),
            metric("search.impressions", 8, 1, "count"),
            metric("search.clicks", 1, 2, "count"),
            metric("search.impressions", 12, 2, "count"),
        ])
        report = ReportService(config, self.store).render("summary", self.window)
        self.assertEqual(report["summary_totals"]["search.ctr"]["value"], .15)
        self.assertEqual({row["metric"] for row in report["rows"]}, {"search.ctr"})

    def test_coverage_is_per_site_source_and_date(self):
        config = self._multi_site_config()
        self.store.upsert([
            metric("search.clicks", 1, 1, "count"),
            metric("search.impressions", 4, 1, "count"),
            metric("search.position", 10, 1, "position"),
            metric("search.clicks", 1, 2, "count"),
            metric("search.impressions", 4, 2, "count"),
            metric("search.position", 10, 2, "position"),
            metric("search.clicks", 2, 1, "count", site_id="second-site"),
            metric("search.impressions", 8, 1, "count", site_id="second-site"),
            metric("search.position", 20, 1, "position", site_id="second-site"),
        ])
        report = ReportService(config, self.store).render("summary", self.window)
        self.assertFalse(report["complete"])
        bucket = next(
            item for item in report["coverage"]["by_site_source"]
            if item["site_id"] == "second-site" and item["source"] == "search-console"
        )
        self.assertEqual(bucket["status"], "partial")
        self.assertEqual(bucket["missing_cells_count"], 4)
        self.assertTrue(any(item["start"] == "2026-07-02" for item in bucket["missing_ranges"]))

    def test_unconfigured_site_source_cells_do_not_degrade_coverage(self):
        root = Path(self.temporary.name)
        text = (root / "platform.toml").read_text(encoding="utf-8")
        second_site = '''[[sites]]
id = "second-site"
client_id = "example-client"
name = "Second Site"
canonical_url = "https://second.example.com"
timezone = "UTC"
'''
        text = text.replace("[[connections]]", second_site + "[[connections]]", 1)
        text = text.replace('site_ids = ["example-site"]', 'site_ids = ["example-site", "second-site"]')
        text = text.replace(
            'metric_ids = ["search.clicks", "search.impressions", "search.ctr", "search.position", "forms.submissions"]',
            'metric_ids = ["umami.pageviews"]',
            1,
        )
        path = root / "partial-bindings.toml"; path.write_text(text, encoding="utf-8")
        config = load_config(path)
        start = datetime(2026, 7, 1, tzinfo=UTC)
        self.store.upsert([
            MetricPoint(
                "example-client", "example-site", "fixture", "umami.pageviews", "count",
                start, start + timedelta(days=1), TimeGrain.DAY, Decimal(12), (),
                Completeness.FINAL, start + timedelta(hours=8),
            ),
            MetricPoint(
                "example-client", "second-site", "fixture", "umami.pageviews", "count",
                start, start + timedelta(days=1), TimeGrain.DAY, Decimal(100), (),
                Completeness.FINAL, start + timedelta(hours=8),
            ),
        ])

        report = ReportService(config, self.store).render(
            "summary", QueryWindow(start, start + timedelta(days=1), "UTC")
        )

        self.assertTrue(report["complete"])
        self.assertEqual(report["coverage"]["expected_cells"], 1)
        self.assertEqual(report["coverage"]["covered_cells"], 1)
        self.assertEqual(report["summary_totals"]["umami.pageviews"]["value"], 12)
        self.assertFalse(any(row["site_id"] == "second-site" for row in report["rows"]))
        unsupported = next(
            item for item in report["coverage"]["by_site_source"]
            if item["site_id"] == "second-site" and item["source"] == "umami"
        )
        self.assertEqual(unsupported["status"], "not_configured")
        self.assertEqual(unsupported["expected_cells"], 0)
        self.assertEqual(report["coverage"]["by_metric"]["umami.pageviews"], "complete")

    def test_fixture_fact_cannot_satisfy_a_current_native_provider_binding(self):
        root = Path(self.temporary.name)
        text = config_text(
            root / "native-state.db",
            root / "unused.json",
            provider="umami",
            credential_ref="none:test",
            options='base_url = "http://127.0.0.1:3000"',
        ).replace(
            'metric_ids = ["umami.pageviews", "forms.submissions", "forms.inbox-deliveries"]',
            'metric_ids = ["umami.pageviews"]',
            1,
        )
        path = root / "native-provider.toml"
        path.write_text(text, encoding="utf-8")
        config = load_config(path)
        start = datetime(2026, 7, 1, tzinfo=UTC)
        window = QueryWindow(start, start + timedelta(days=1), "UTC")
        run = self.store.start_run(
            "example-connection", "example-site",
            binding_key="example-site:example-connection:website:demo",
            source="umami", window=window,
        )
        observed_at = datetime.now(UTC)
        self.store.upsert([
            MetricPoint(
                "example-client", "example-site", "fixture", "umami.pageviews", "count",
                start, start + timedelta(days=1), TimeGrain.DAY, Decimal(100), (),
                Completeness.FINAL, observed_at,
            ),
            MetricPoint(
                "example-client", "example-site", "umami", "umami.pageviews", "count",
                start, start + timedelta(days=1), TimeGrain.DAY, Decimal(3), (),
                Completeness.FINAL, observed_at,
            ),
        ])
        self.store.finish_run(
            run, "success", points=1, result_kind="data",
            data_through=window.end,
        )

        report = ReportService(config, self.store).render("summary", window)

        self.assertTrue(report["complete"])
        self.assertEqual(report["summary_totals"]["umami.pageviews"]["value"], 3)
        self.assertEqual({row["source"] for row in report["rows"]}, {"umami"})

    def test_legacy_native_fact_without_explicit_run_is_not_reported_as_zero(self):
        root = Path(self.temporary.name)
        text = config_text(
            root / "legacy-native.db",
            root / "unused.json",
            provider="umami",
            credential_ref="none:test",
            options='base_url = "http://127.0.0.1:3000"',
        ).replace(
            'metric_ids = ["umami.pageviews", "forms.submissions", "forms.inbox-deliveries"]',
            'metric_ids = ["umami.pageviews"]',
            1,
        )
        path = root / "legacy-native.toml"
        path.write_text(text, encoding="utf-8")
        config = load_config(path)
        start = datetime(2026, 7, 1, tzinfo=UTC)
        self.store.upsert([
            MetricPoint(
                "example-client", "example-site", "umami",
                "umami.pageviews", "count",
                start, start + timedelta(days=1), TimeGrain.DAY,
                Decimal(0), (), Completeness.FINAL, datetime.now(UTC),
            ),
        ])

        report = ReportService(config, self.store).render(
            "summary", QueryWindow(start, start + timedelta(days=1), "UTC")
        )

        self.assertEqual(report["coverage"]["status"], "unavailable")
        self.assertEqual(
            report["summary_totals"]["umami.pageviews"]["value"], None
        )
        self.assertEqual(report["rows"], [])
        self.assertEqual(report["series"], [])

    def test_site_local_sync_fact_is_visible_for_requested_calendar_date(self):
        root = Path(self.temporary.name)
        text = config_text(
            root / "tokyo-native.db",
            root / "unused.json",
            provider="umami",
            credential_ref="none:test",
            options='base_url = "http://127.0.0.1:3000"',
        ).replace(
            'metric_ids = ["umami.pageviews", "forms.submissions", "forms.inbox-deliveries"]',
            'metric_ids = ["umami.pageviews"]',
            1,
        ).replace(
            'canonical_url = "https://example.com"\ntimezone = "UTC"',
            'canonical_url = "https://example.com"\ntimezone = "Asia/Tokyo"',
            1,
        )
        path = root / "tokyo-native.toml"
        path.write_text(text, encoding="utf-8")
        config = load_config(path)
        zone = ZoneInfo("Asia/Tokyo")
        site_window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=zone),
            datetime(2026, 7, 2, tzinfo=zone),
            "Asia/Tokyo",
        )
        run = self.store.start_run(
            "example-connection", "example-site",
            binding_key="example-site:example-connection:website:demo",
            source="umami", window=site_window,
        )
        observed_at = datetime.now(UTC)
        self.store.upsert([
            MetricPoint(
                "example-client", "example-site", "umami",
                "umami.pageviews", "count",
                site_window.start, site_window.end, TimeGrain.DAY,
                Decimal(5), (), Completeness.FINAL, observed_at,
            ),
        ])
        self.store.finish_run(
            run, "success", points=1, result_kind="data",
            data_through=site_window.end,
        )

        report = ReportService(config, self.store).render(
            "summary",
            QueryWindow(
                datetime(2026, 7, 1, tzinfo=UTC),
                datetime(2026, 7, 2, tzinfo=UTC),
                "UTC",
            ),
        )

        self.assertEqual(report["coverage"]["status"], "complete")
        self.assertEqual(
            report["summary_totals"]["umami.pageviews"]["value"], 5
        )
        self.assertEqual(report["series"][0]["points"], [
            {"date": "2026-07-01", "value": 5},
        ])

    def test_site_local_empty_run_authorizes_requested_calendar_zero(self):
        root = Path(self.temporary.name)
        text = config_text(
            root / "chicago-native.db",
            root / "unused.json",
            provider="umami",
            credential_ref="none:test",
            options='base_url = "http://127.0.0.1:3000"',
        ).replace(
            'metric_ids = ["umami.pageviews", "forms.submissions", "forms.inbox-deliveries"]',
            'metric_ids = ["umami.pageviews"]',
            1,
        ).replace(
            'canonical_url = "https://example.com"\ntimezone = "UTC"',
            'canonical_url = "https://example.com"\ntimezone = "America/Chicago"',
            1,
        )
        path = root / "chicago-native.toml"
        path.write_text(text, encoding="utf-8")
        config = load_config(path)
        store = SQLiteMetricStore(root / "chicago-native.db")
        store.initialize()
        zone = ZoneInfo("America/Chicago")
        site_window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=zone),
            datetime(2026, 7, 2, tzinfo=zone),
            "America/Chicago",
        )
        run = store.start_run(
            "example-connection", "example-site",
            binding_key="example-site:example-connection:website:demo",
            source="umami", window=site_window,
        )
        store.finish_run(run, "success", result_kind="empty")

        report = ReportService(config, store).render(
            "summary",
            QueryWindow(
                datetime(2026, 7, 1, tzinfo=UTC),
                datetime(2026, 7, 2, tzinfo=UTC),
                "UTC",
            ),
        )

        self.assertEqual(report["coverage"]["status"], "complete")
        self.assertEqual(
            report["summary_totals"]["umami.pageviews"]["value"], 0
        )

    def test_exact_multi_date_pageview_total_can_exceed_fact_digit_bound(self):
        root = Path(self.temporary.name)
        text = config_text(
            root / "large-total.db",
            root / "unused.json",
            provider="umami",
            credential_ref="none:test",
            options='base_url = "http://127.0.0.1:3000"',
        ).replace(
            'metric_ids = ["umami.pageviews", "forms.submissions", "forms.inbox-deliveries"]',
            'metric_ids = ["umami.pageviews"]',
            1,
        )
        path = root / "large-total.toml"
        path.write_text(text, encoding="utf-8")
        config = load_config(path)
        window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 3, tzinfo=UTC),
            "UTC",
        )
        run = self.store.start_run(
            "example-connection", "example-site",
            binding_key="example-site:example-connection:website:demo",
            source="umami", window=window,
        )
        observed_at = datetime.now(UTC)
        value = Decimal("9e37")
        self.store.upsert([
            pageview_metric(
                "umami.pageviews", value, day,
                observed_at=observed_at,
            )
            for day in (1, 2)
        ])
        self.store.finish_run(
            run, "success", points=2, result_kind="data",
            data_through=window.end,
        )

        report = ReportService(config, self.store).render("summary", window)

        self.assertEqual(report["rows"][0]["value"], 18 * 10 ** 37)
        self.assertEqual(
            report["summary_totals"]["umami.pageviews"]["value"],
            18 * 10 ** 37,
        )

    def test_provider_history_floor_is_calendar_stable_in_negative_offset_zone(self):
        chicago_config = replace(
            self.config,
            sites=tuple(
                replace(site, timezone="America/Chicago")
                if site.id == "example-site" else site
                for site in self.config.sites
            ),
        )
        zone = ZoneInfo("America/Chicago")
        history_window = QueryWindow(
            datetime(1999, 12, 31, tzinfo=zone),
            datetime(2000, 1, 1, tzinfo=zone),
            "America/Chicago",
        )
        run = self.store.start_run(
            "native-google", "example-site",
            binding_key="example-site:native-google:property:123456",
            source="google-analytics", window=history_window,
        )
        observed_at = datetime.now(UTC)
        self.store.upsert([
            MetricPoint(
                "example-client", "example-site", "google-analytics",
                "google.pageviews", "count",
                history_window.start, history_window.end, TimeGrain.DAY,
                Decimal(9), (), Completeness.FINAL, observed_at,
            ),
        ])
        self.store.finish_run(
            run, "success", points=1, result_kind="data",
            data_through=history_window.end,
        )
        report_window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=zone),
            datetime(2026, 7, 2, tzinfo=zone),
            "America/Chicago",
        )

        comparison = ReportService(chicago_config, self.store).render(
            "summary", report_window
        )["provider_comparisons"][0]

        self.assertIsNone(
            comparison["providers"]["google-analytics"]["first_available_date"]
        )
        self.assertIsNone(
            comparison["providers"]["google-analytics"]["data_through"]
        )

    def test_oversized_pageview_summary_is_unknown_not_authoritative_zero(self):
        coverage = {
            "by_metric": {"umami.pageviews": "complete"},
            "by_metric_cells": {
                "umami.pageviews": {"expected": 2, "covered": 2},
            },
            "by_site_source": [],
        }
        unavailable = {
            "by_metric": {"umami.pageviews": "unavailable"},
            "by_metric_cells": {
                "umami.pageviews": {"expected": 0, "covered": 0},
            },
            "by_site_source": [],
        }
        maximum = 10 ** 64 - 1
        accepted = ReportService._summary_totals(
            [
                {"metric": "umami.pageviews", "site_id": "a", "source": "umami", "value": maximum - 1},
                {"metric": "umami.pageviews", "site_id": "b", "source": "umami", "value": 1},
            ],
            [],
            ("umami.pageviews",),
            coverage,
            unavailable,
        )["umami.pageviews"]
        rejected = ReportService._summary_totals(
            [
                {"metric": "umami.pageviews", "site_id": "a", "source": "umami", "value": maximum},
                {"metric": "umami.pageviews", "site_id": "b", "source": "umami", "value": maximum},
            ],
            [],
            ("umami.pageviews",),
            coverage,
            unavailable,
        )["umami.pageviews"]

        self.assertEqual(accepted["value"], maximum)
        self.assertIsNone(rejected["value"])
        self.assertFalse(rejected["comparison_available"])

    def test_native_fact_cannot_satisfy_a_current_fixture_only_binding(self):
        root = Path(self.temporary.name)
        text = config_text(root / "fixture-only.db", root / "unused.json").replace(
            'metric_ids = ["umami.pageviews", "forms.submissions", "forms.inbox-deliveries"]',
            'metric_ids = ["umami.pageviews"]',
            1,
        )
        path = root / "fixture-only.toml"
        path.write_text(text, encoding="utf-8")
        config = load_config(path)
        store = SQLiteMetricStore(root / "fixture-only.db")
        store.initialize()
        start = datetime(2026, 7, 1, tzinfo=UTC)
        prior = start - timedelta(days=1)
        store.upsert([
            MetricPoint(
                "example-client", "example-site", "fixture", "umami.pageviews", "count",
                start, start + timedelta(days=1), TimeGrain.DAY, Decimal(3), (),
                Completeness.FINAL, start + timedelta(hours=8),
            ),
            MetricPoint(
                "example-client", "example-site", "umami", "umami.pageviews", "count",
                start, start + timedelta(days=1), TimeGrain.DAY, Decimal(100), (),
                Completeness.FINAL, start + timedelta(hours=9),
            ),
            MetricPoint(
                "example-client", "example-site", "fixture", "umami.pageviews", "count",
                prior, start, TimeGrain.DAY, Decimal(2), (),
                Completeness.FINAL, start + timedelta(hours=8),
            ),
            MetricPoint(
                "example-client", "example-site", "umami", "umami.pageviews", "count",
                prior, start, TimeGrain.DAY, Decimal(90), (),
                Completeness.FINAL, start + timedelta(hours=9),
            ),
        ])

        report = ReportService(config, store).render(
            "summary", QueryWindow(start, start + timedelta(days=1), "UTC")
        )

        self.assertEqual(report["summary_totals"]["umami.pageviews"]["value"], 3)
        self.assertEqual(report["summary_totals"]["umami.pageviews"]["previous_value"], 2)
        self.assertEqual({row["source"] for row in report["rows"]}, {"fixture"})
        self.assertEqual({series["source"] for series in report["series"]}, {"fixture"})

    def test_successful_empty_sync_coverage_yields_an_authoritative_zero_total(self):
        root = Path(self.temporary.name)
        text = config_text(
            root / "fixture-zero.db", root / "fixture.json"
        ).replace(
            'metric_ids = ["umami.pageviews", "forms.submissions", "forms.inbox-deliveries"]',
            'metric_ids = ["umami.pageviews"]',
            1,
        )
        path = root / "zero.toml"; path.write_text(text, encoding="utf-8")
        config = load_config(path)
        key = "example-site:example-connection:website:demo"
        run = self.store.start_run(
            "example-connection", "example-site", binding_key=key, source="fixture", window=self.window,
        )
        self.store.finish_run(run, "success", result_kind="empty")

        report = ReportService(config, self.store).render("summary", self.window)

        self.assertTrue(report["complete"])
        self.assertEqual(report["summary_totals"]["umami.pageviews"]["value"], 0)
        self.assertEqual(
            report["summary_totals"]["umami.pageviews"]["source"], "fixture"
        )
        self.assertFalse(any("No stored" in warning for warning in report["warnings"]))

    def test_missing_observation_warning_is_scoped_to_selected_window(self):
        report = ReportService(self.config, self.store).render("summary", self.window)

        warning = next(
            item for item in report["warnings"]
            if item.startswith("No observations match")
        )
        self.assertIn("the selected window", warning)
        self.assertNotIn("No stored observations for:", warning)

    def test_observation_boundary_withholds_pre_start_facts_and_run_coverage(self):
        root = Path(self.temporary.name)
        text = config_text(
            root / "observation.db",
            root / "fixture.json",
            provider="umami",
            options='base_url = "https://analytics.example.invalid"',
        ).replace(
            'metric_groups = ["traffic"]',
            'metric_groups = ["traffic"]\n[bindings.options]\n'
            'observation_start = "2026-07-02"',
        ).replace(
            'metric_ids = ["umami.pageviews", "forms.submissions", "forms.inbox-deliveries"]',
            'metric_ids = ["umami.pageviews", "umami.visits"]',
            1,
        )
        path = root / "observation.toml"
        path.write_text(text, encoding="utf-8")
        config = load_config(path)
        store = SQLiteMetricStore(root / "observation.db")
        store.initialize()
        window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 4, tzinfo=UTC),
            "UTC",
        )
        key = "example-site:example-connection:website:demo"
        run = store.start_run(
            "example-connection", "example-site", binding_key=key,
            source="umami", window=window,
        )
        observed_at = datetime.now(UTC)
        store.upsert([
            MetricPoint(
                "example-client", "example-site", "umami",
                "umami.pageviews", "count", window.start,
                window.start + timedelta(days=1), TimeGrain.DAY,
                Decimal(999), (), Completeness.FINAL, observed_at,
            ),
            MetricPoint(
                "example-client", "example-site", "umami",
                "umami.pageviews", "count", window.start + timedelta(days=1),
                window.start + timedelta(days=2), TimeGrain.DAY,
                Decimal(5), (), Completeness.FINAL, observed_at,
            ),
            MetricPoint(
                "example-client", "example-site", "umami",
                "umami.visits", "count", window.start, window.end,
                TimeGrain.TOTAL, Decimal(100), (), Completeness.FINAL,
                window.end,
            ),
        ])
        store.finish_run(run, "success", 3, result_kind="data")

        report = ReportService(config, store).render("summary", window)

        pageviews = report["summary_totals"]["umami.pageviews"]
        visits = report["summary_totals"]["umami.visits"]
        self.assertEqual(pageviews["value"], 5)
        self.assertEqual(pageviews["coverage_status"], "partial")
        self.assertEqual(
            (pageviews["covered_cells"], pageviews["expected_cells"]),
            (2, 3),
        )
        self.assertEqual(visits["value"], 100)
        self.assertTrue(visits["observed"])
        self.assertEqual(visits["coverage_status"], "partial")
        self.assertFalse(report["complete"])
        engagement = {
            item["id"]: item
            for item in report["decision_support"]["engagement"]
        }
        self.assertEqual(
            engagement["umami_views_per_visit"]["state"], "withheld"
        )
        self.assertIsNone(engagement["umami_views_per_visit"]["value"])
        self.assertEqual(
            report["series"][0]["points"],
            [{"date": "2026-07-02", "value": 5}],
        )
        self.assertTrue(any(
            "pre-instrumentation" in warning
            for warning in report["warnings"]
        ))

        post_window = QueryWindow(
            datetime(2026, 7, 2, tzinfo=UTC),
            datetime(2026, 7, 4, tzinfo=UTC),
            "UTC",
        )
        store.upsert([MetricPoint(
            "example-client", "example-site", "umami",
            "umami.visits", "count", post_window.start, post_window.end,
            TimeGrain.TOTAL, Decimal(7), (), Completeness.FINAL,
            post_window.end,
        )])
        post_report = ReportService(config, store).render(
            "summary", post_window, include_decision_support=False
        )
        self.assertTrue(post_report["complete"])
        self.assertEqual(
            post_report["summary_totals"]["umami.pageviews"]["value"], 5
        )
        self.assertEqual(
            post_report["summary_totals"]["umami.visits"]["value"], 7
        )

    def test_entirely_pre_observation_window_is_unknown_not_zero(self):
        root = Path(self.temporary.name)
        text = config_text(
            root / "pre-observation.db",
            root / "fixture.json",
            provider="umami",
            options='base_url = "https://analytics.example.invalid"',
        ).replace(
            'metric_groups = ["traffic"]',
            'metric_groups = ["traffic"]\n[bindings.options]\n'
            'observation_start = "2026-07-02"',
        ).replace(
            'metric_ids = ["umami.pageviews", "forms.submissions", "forms.inbox-deliveries"]',
            'metric_ids = ["umami.pageviews", "umami.visits"]',
            1,
        )
        path = root / "pre-observation.toml"
        path.write_text(text, encoding="utf-8")
        config = load_config(path)
        store = SQLiteMetricStore(root / "pre-observation.db")
        store.initialize()
        window = QueryWindow(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            "UTC",
        )
        store.upsert([
            MetricPoint(
                "example-client", "example-site", "umami",
                "umami.pageviews", "count", window.start, window.end,
                TimeGrain.DAY, Decimal(999), (), Completeness.FINAL,
                window.end,
            ),
            MetricPoint(
                "example-client", "example-site", "umami",
                "umami.visits", "count", window.start, window.end,
                TimeGrain.TOTAL, Decimal(100), (), Completeness.FINAL,
                window.end,
            ),
        ])
        key = "example-site:example-connection:website:demo"
        run = store.start_run(
            "example-connection", "example-site", binding_key=key,
            source="umami", window=window,
        )
        store.finish_run(run, "success", 2, result_kind="data")

        report = ReportService(config, store).render(
            "summary", window, include_decision_support=False
        )

        self.assertEqual(report["rows"], [])
        self.assertEqual(report["series"], [])
        for metric_id in ("umami.pageviews", "umami.visits"):
            total = report["summary_totals"][metric_id]
            self.assertIsNone(total["value"])
            self.assertFalse(total["observed"])
            self.assertEqual(total["coverage_status"], "partial")
            self.assertEqual(total["covered_cells"], 0)
        self.assertFalse(report["complete"])

    def test_observation_boundary_warning_ignores_unrequested_sources(self):
        bindings = tuple(
            replace(
                binding,
                options={**binding.options, "observation_start": "2026-07-02"},
            )
            if binding.connection_id == "native-umami"
            else binding
            for binding in self.config.bindings
        )
        config = replace(self.config, bindings=bindings)

        report = ReportService(config, self.store).render(
            "summary", self.window, subreport_id="forms",
            include_decision_support=False,
        )

        self.assertFalse(any(
            "observation boundaries" in warning
            for warning in report["warnings"]
        ))

    def test_empty_fixture_supporting_metric_keeps_fixture_provenance(self):
        root = Path(self.temporary.name)
        path = root / "supporting-zero.toml"
        path.write_text(
            config_text(root / "supporting-zero.db", root / "fixture.json"),
            encoding="utf-8",
        )
        config = load_config(path)
        key = "example-site:example-connection:website:demo"
        run = self.store.start_run(
            "example-connection", "example-site", binding_key=key,
            source="fixture", window=self.window,
        )
        self.store.finish_run(run, "success", result_kind="empty")

        support = ReportService(config, self.store).render(
            "summary", self.window
        )["decision_support"]
        clicks = support["supporting_metrics"]["search.clicks"]

        self.assertEqual(clicks["value"], 0)
        self.assertEqual(clicks["source"], "fixture")

    def test_cloudflare_provisional_measurements_are_temporally_usable(self):
        root = Path(self.temporary.name)
        text = (root / "platform.toml").read_text(encoding="utf-8").replace(
            'metric_ids = ["search.clicks", "search.impressions", "search.ctr", "search.position", "forms.submissions"]',
            'metric_ids = ["cloudflare.requests"]',
            1,
        )
        path = root / "cloudflare-usable.toml"; path.write_text(text, encoding="utf-8")
        config = load_config(path)
        start = datetime(2026, 7, 1, tzinfo=UTC)
        self.store.upsert([MetricPoint(
            "example-client", "example-site", "cloudflare", "cloudflare.requests", "count",
            start, start + timedelta(days=1), TimeGrain.DAY, Decimal(9), (),
            Completeness.PROVISIONAL, start + timedelta(hours=8),
        )])

        report = ReportService(config, self.store).render(
            "summary", QueryWindow(start, start + timedelta(days=1), "UTC")
        )

        self.assertTrue(report["complete"])
        self.assertEqual(report["summary_totals"]["cloudflare.requests"]["value"], 9)
        health = next(item for item in report["source_health"] if item["source"] == "cloudflare")
        self.assertEqual(health["sampling"], "adaptive")
        self.assertEqual(health["data_state"], "provisional")

    def test_large_missing_windows_use_compact_ranges_not_per_cell_objects(self):
        root = Path(self.temporary.name)
        text = (root / "platform.toml").read_text(encoding="utf-8").replace(
            'metric_ids = ["search.clicks", "search.impressions", "search.ctr", "search.position", "forms.submissions"]',
            'metric_ids = ["umami.pageviews"]',
            1,
        )
        path = root / "compact.toml"; path.write_text(text, encoding="utf-8")
        config = load_config(path)
        window = QueryWindow(datetime(2016, 7, 3, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC), "UTC")

        report = ReportService(config, self.store).render("summary", window)
        bucket = report["coverage"]["by_site_source"][0]

        self.assertNotIn("missing_cells", bucket)
        self.assertEqual(bucket["missing_cells_count"], 3650)
        self.assertEqual(len(bucket["missing_ranges"]), 1)
        self.assertLess(len(json.dumps(report)), 100_000)

    def test_source_health_separates_data_through_from_ingestion_time(self):
        self.store.upsert([
            metric("search.clicks", 1, 1, "count", observed_hour=20),
            metric("search.impressions", 4, 1, "count", observed_hour=20),
            metric("search.position", 10, 1, "position", observed_hour=20),
        ])
        report = ReportService(self.config, self.store).render("summary", self.window)
        health = next(item for item in report["source_health"] if item["source"] == "search-console")
        self.assertEqual(health["data_through"], "2026-07-02T00:00:00+00:00")
        self.assertEqual(health["ingested_at"], "2026-07-01T20:00:00+00:00")

    def test_provisional_displayed_facts_retain_observational_health(self):
        root = Path(self.temporary.name)
        text = (root / "platform.toml").read_text(encoding="utf-8").replace(
            'metric_ids = ["search.clicks", "search.impressions", "search.ctr", "search.position", "forms.submissions"]',
            'metric_ids = ["cloudflare.requests"]',
            1,
        )
        path = root / "cloudflare.toml"; path.write_text(text, encoding="utf-8")
        config = load_config(path)
        start = datetime(2026, 7, 1, tzinfo=UTC)
        observed = datetime(2026, 7, 2, 6, tzinfo=UTC)
        self.store.upsert([MetricPoint(
            "example-client", "example-site", "cloudflare", "cloudflare.requests", "count",
            start, start + timedelta(days=1), TimeGrain.DAY, Decimal(9), (),
            Completeness.PROVISIONAL, observed,
        )])
        report = ReportService(config, self.store).render("summary", self.window)
        health = next(item for item in report["source_health"] if item["source"] == "cloudflare")
        self.assertEqual(health["data_through"], "2026-07-02T00:00:00+00:00")
        self.assertEqual(health["ingested_at"], observed.isoformat())
        self.assertEqual(health["data_state"], "provisional")

    def test_fixture_identity_survives_summary_health_and_csv_context(self):
        root = Path(self.temporary.name)
        text = (root / "platform.toml").read_text(encoding="utf-8").replace(
            'metric_ids = ["search.clicks", "search.impressions", "search.ctr", "search.position", "forms.submissions"]',
            'metric_ids = ["umami.pageviews"]',
            1,
        )
        path = root / "fixture-source.toml"; path.write_text(text, encoding="utf-8")
        config = load_config(path)
        start = datetime(2026, 7, 1, tzinfo=UTC)
        observed = start + timedelta(hours=8)
        self.store.upsert([MetricPoint(
            "example-client", "example-site", "fixture", "umami.pageviews", "count",
            start, start + timedelta(days=1), TimeGrain.DAY, Decimal(12), (),
            Completeness.FINAL, observed,
        )])
        one_day = QueryWindow(start, start + timedelta(days=1), "UTC")
        report = ReportService(config, self.store).render("summary", one_day)
        self.assertEqual(report["summary_totals"]["umami.pageviews"]["source"], "fixture")
        health = next(item for item in report["source_health"] if item["source"] == "fixture")
        self.assertEqual(health["time_basis"], "fixture-declared")
        exported = next(csv.DictReader(io.StringIO(to_csv(report))))
        self.assertEqual(exported["source"], "fixture")
        self.assertEqual(exported["time_basis"], "fixture-declared")
        self.assertEqual(exported["sampling"], "fixture")
        self.assertEqual(exported["data_state"], "fixture")
        self.assertEqual(exported["ingested_at"], observed.isoformat())

    def test_mixed_actual_sources_keep_distinct_health_and_csv_provenance(self):
        root = Path(self.temporary.name)
        text = (root / "platform.toml").read_text(encoding="utf-8").replace(
            'metric_ids = ["search.clicks", "search.impressions", "search.ctr", "search.position", "forms.submissions"]',
            'metric_ids = ["umami.pageviews"]',
            1,
        )
        path = root / "mixed-source.toml"; path.write_text(text, encoding="utf-8")
        config = load_config(path)
        start = datetime(2026, 7, 1, tzinfo=UTC)
        one_day = QueryWindow(start, start + timedelta(days=1), "UTC")
        run = self.store.start_run(
            "native-umami", "example-site",
            binding_key="example-site:native-umami:website:native-demo",
            source="umami", window=one_day,
        )
        fixture_observed = start + timedelta(hours=8)
        umami_observed = datetime.now(UTC)
        self.store.upsert([
            MetricPoint(
                "example-client", "example-site", "fixture", "umami.pageviews", "count",
                start, start + timedelta(days=1), TimeGrain.DAY, Decimal(12), (),
                Completeness.FINAL, fixture_observed,
            ),
            MetricPoint(
                "example-client", "example-site", "umami", "umami.pageviews", "count",
                start, start + timedelta(days=1), TimeGrain.DAY, Decimal(14), (),
                Completeness.FINAL, umami_observed,
            ),
        ])
        self.store.finish_run(
            run, "success", points=1, result_kind="data",
            data_through=one_day.end,
        )
        report = ReportService(config, self.store).render("summary", one_day)
        self.assertEqual(report["summary_totals"]["umami.pageviews"]["source"], "mixed")
        self.assertIsNone(report["summary_totals"]["umami.pageviews"]["value"])
        health = {
            item["source"]: item for item in report["source_health"]
            if item["metric_source"] == "umami"
        }
        self.assertEqual(health["fixture"]["ingested_at"], fixture_observed.isoformat())
        self.assertEqual(health["umami"]["ingested_at"], umami_observed.isoformat())
        exported = {
            row["source"]: row for row in csv.DictReader(io.StringIO(to_csv(report)))
        }
        self.assertEqual(exported["fixture"]["time_basis"], "fixture-declared")
        self.assertEqual(exported["fixture"]["ingested_at"], fixture_observed.isoformat())
        self.assertEqual(exported["umami"]["time_basis"], "request-timezone")
        self.assertEqual(exported["umami"]["ingested_at"], umami_observed.isoformat())

    def test_subreport_dimension_filter_is_applied(self):
        root = Path(self.temporary.name); text = (root / "platform.toml").read_text(encoding="utf-8")
        text = text.replace('metric_ids = ["forms.submissions", "forms.inbox-deliveries"]\ndefault_window_days = 30',
            'metric_ids = ["forms.submissions", "forms.inbox-deliveries"]\ndefault_window_days = 30\n[reports.subreports.filters]\nform_id = "contact"')
        (root / "filtered.toml").write_text(text, encoding="utf-8"); config = load_config(root / "filtered.toml")
        self.store.upsert([metric("forms.submissions", 2, 1, "count", (("form_id", "contact"),)), metric("forms.submissions", 8, 1, "count", (("form_id", "quote"),))])
        report = ReportService(config, self.store).render("summary", self.window, "forms")
        self.assertEqual(report["filters"], {"form_id": "contact"}); self.assertEqual(report["rows"][0]["value"], 2)

    def test_site_scope_must_belong_to_report(self):
        with self.assertRaisesRegex(ValueError, "site is unavailable"):
            ReportService(self.config, self.store).render("summary", self.window, site_id="unknown-site")

    def test_daily_comparison_series_and_flat_csv_are_available(self):
        previous_start = datetime(2026, 6, 30, tzinfo=UTC)
        self.store.upsert([
            MetricPoint(
                "example-client", "example-site", "search-console", "search.impressions", "count",
                previous_start, previous_start + timedelta(days=1), TimeGrain.DAY, Decimal("4"), (),
                Completeness.FINAL, previous_start + timedelta(hours=12),
            ),
            metric("search.impressions", 9, 1, "count"),
        ])
        report = ReportService(self.config, self.store).render("summary", self.window)
        self.assertFalse(any(item["metric"] == "search.impressions" for item in report["comparison_series"]))
        csv_body = to_series_csv(report, include_comparison=True)
        self.assertNotIn("comparison,2026-06-30,search.impressions", csv_body)
        self.assertIn("current,2026-07-01,search.impressions", csv_body)
        self.assertIn("coverage_status", csv_body.splitlines()[0])
        self.assertFalse(report["comparison"]["available"])

    def test_forms_pipeline_preserves_unknown_instead_of_inventing_zero(self):
        self.store.upsert([metric("forms.submissions", 1, 1, "count")])
        report = ReportService(self.config, self.store).render("summary", self.window)
        self.assertEqual(report["forms_pipeline"]["submissions"], 1)
        self.assertIsNone(report["forms_pipeline"]["inbox_deliveries"])
        self.assertIsNone(report["forms_pipeline"]["delivery_gap"])
        self.assertIsNone(report["forms_pipeline"]["pending"])
        self.assertIsNone(report["forms_pipeline"]["failed"])

    def test_forms_pipeline_requires_complete_identical_coverage_before_asserting_gap(self):
        start = self.window.start
        report_config = replace(
            self.config.reports[0],
            metric_ids=tuple(dict.fromkeys((
                *self.config.reports[0].metric_ids, "forms.inbox-deliveries",
            ))),
        )
        config = replace(self.config, reports=(report_config, *self.config.reports[1:]))

        def inbox(value, day):
            point_start = start + timedelta(days=day)
            return MetricPoint(
                "example-client", "example-site", "forms-inbox",
                "forms.inbox-deliveries", "count", point_start,
                point_start + timedelta(days=1), TimeGrain.DAY,
                Decimal(value), (), Completeness.FINAL,
                point_start + timedelta(hours=12),
            )

        self.store.upsert([
            metric("forms.submissions", 1, 1, "count"),
            metric("forms.submissions", 0, 2, "count"),
            inbox(1, 0),
        ])
        partial = ReportService(config, self.store).render("summary", self.window)
        self.assertEqual(partial["forms_pipeline"]["submissions"], 1)
        self.assertEqual(partial["forms_pipeline"]["inbox_deliveries"], 1)
        self.assertFalse(partial["forms_pipeline"]["delivery_comparable"])
        self.assertIsNone(partial["forms_pipeline"]["delivery_gap"])
        self.assertTrue(any("no delivery gap is asserted" in item for item in partial["warnings"]))

        self.store.upsert([inbox(0, 1)])
        complete = ReportService(config, self.store).render("summary", self.window)
        self.assertTrue(complete["forms_pipeline"]["delivery_comparable"])
        self.assertEqual(complete["forms_pipeline"]["delivery_gap"], 0)

    def test_prior_forms_identity_zeroes_cannot_make_current_reports_complete(self):
        inbox_start = datetime(2026, 7, 1, tzinfo=UTC)
        self.store.upsert([
            metric("forms.submissions", 0, 1, "count"),
            metric("forms.submissions", 0, 2, "count"),
            MetricPoint(
                "example-client", "example-site", "forms-inbox",
                "forms.inbox-deliveries", "count", inbox_start,
                inbox_start + timedelta(days=1), TimeGrain.DAY, Decimal(0), (),
                Completeness.FINAL, inbox_start + timedelta(hours=12),
            ),
        ])
        with self.store.connect() as db:
            db.execute(
                "UPDATE metric_facts SET identity_version=2 "
                "WHERE source IN ('cloudflare-forms','forms-inbox')"
            )

        report = ReportService(self.config, self.store).render("summary", self.window)
        self.assertIsNone(report["forms_pipeline"]["submissions"])
        self.assertIsNone(report["forms_pipeline"]["inbox_deliveries"])
        self.assertIsNone(report["forms_pipeline"]["delivery_gap"])
        self.assertNotEqual(report["coverage"]["by_metric"]["forms.submissions"], "complete")

    def test_report_csv_repeats_window_coverage_and_source_context(self):
        self.store.upsert([
            metric("search.clicks", 1, 1, "count"),
            metric("search.impressions", 4, 1, "count"),
            metric("search.position", 10, 1, "position"),
        ])
        report = ReportService(self.config, self.store).render("summary", self.window)
        header = to_csv(report).splitlines()[0]
        for field in (
            "window_start", "window_end", "timezone", "aggregation",
            "coverage_status", "comparison_available", "data_through",
            "ingested_at", "time_basis", "sampling", "data_state",
        ):
            self.assertIn(field, header)

    def test_report_csv_preserves_each_rows_comparison_availability(self):
        self.store.upsert([metric("forms.submissions", 1, 1, "count")])
        report = ReportService(self.config, self.store).render("summary", self.window)
        row = next(item for item in report["rows"] if item["metric"] == "forms.submissions")
        self.assertFalse(row["comparison_available"])
        exported = next(
            item for item in csv.DictReader(io.StringIO(to_csv(report)))
            if item["metric"] == "forms.submissions"
        )
        self.assertEqual(exported["comparison_available"], "False")

    def test_decision_support_derives_complete_metrics_without_false_attribution(self):
        start = self.window.start
        pageview_observed_at = self._record_current_provider_runs(self.window)

        def native(metric_id, value, *, source, unit="count", day=None):
            point_start = start if day is None else start + timedelta(days=day)
            point_end = self.window.end if day is None else point_start + timedelta(days=1)
            return MetricPoint(
                "example-client", "example-site", source, metric_id, unit,
                point_start, point_end, TimeGrain.DAY, Decimal(str(value)), (),
                Completeness.FINAL,
                (
                    pageview_observed_at
                    if metric_id in {"umami.pageviews", "google.pageviews"}
                    else point_end
                ),
            )

        self.store.upsert([
            native("umami.pageviews", 60, source="umami", day=0),
            native("umami.pageviews", 90, source="umami", day=1),
            native("umami.visits", 100, source="umami"),
            native("umami.bounces", 40, source="umami"),
            native("umami.total-time", 6000, source="umami", unit="seconds"),
            native("google.sessions", 40, source="google-analytics", day=0),
            native("google.sessions", 60, source="google-analytics", day=1),
            native("google.pageviews", 80, source="google-analytics", day=0),
            native("google.pageviews", 120, source="google-analytics", day=1),
            native("google.events", 120, source="google-analytics", day=0),
            native("google.events", 180, source="google-analytics", day=1),
            native("forms.submissions", 2, source="cloudflare-forms", day=0),
            native("forms.submissions", 3, source="cloudflare-forms", day=1),
            native("forms.sent", 2, source="cloudflare-forms", day=0),
            native("forms.sent", 2, source="cloudflare-forms", day=1),
            native("forms.pending", 0, source="cloudflare-forms", day=0),
            native("forms.pending", 1, source="cloudflare-forms", day=1),
            native("forms.failed", 0, source="cloudflare-forms", day=0),
            native("forms.failed", 0, source="cloudflare-forms", day=1),
        ])

        report = ReportService(self.config, self.store).render("summary", self.window)
        support = report["decision_support"]
        outcomes = {item["id"]: item for item in support["outcomes"]}
        engagement = {item["id"]: item for item in support["engagement"]}

        self.assertEqual(outcomes["durable_leads"]["value"], 5)
        self.assertEqual(outcomes["durable_leads"]["scope_site_ids"], ["example-site"])
        self.assertEqual(outcomes["visit_to_submission"]["value"], .05)
        self.assertEqual(outcomes["visit_to_submission"]["scope_site_ids"], ["example-site"])
        self.assertIn("not attribution", outcomes["visit_to_submission"]["note"])
        self.assertEqual(outcomes["notification_sent_rate"]["value"], .8)
        self.assertEqual(engagement["umami_bounce_rate"]["value"], .4)
        self.assertEqual(engagement["umami_views_per_visit"]["value"], 1.5)
        self.assertEqual(engagement["umami_average_visit_duration"]["value"], 60)
        self.assertEqual(engagement["ga_views_per_session"]["value"], 2)
        self.assertEqual(engagement["ga_events_per_session"]["value"], 3)
        self.assertEqual(
            support["supporting_metrics"]["umami.visits"]["value"], 100
        )
        self.assertFalse(any(
            "conversion rate" in item["label"].casefold()
            for item in (*support["outcomes"], *support["engagement"])
        ))
        self.assertNotIn("notification_pipeline_mismatch", {
            item["id"] for item in support["attention_items"]
        })

    def test_decision_support_withholds_partial_inputs_and_preserves_report_completeness(self):
        start = self.window.start
        self.store.upsert([
            MetricPoint(
                "example-client", "example-site", "umami", "umami.visits", "count",
                start, self.window.end, TimeGrain.DAY, Decimal("100"), (),
                Completeness.FINAL, self.window.end,
            ),
            MetricPoint(
                "example-client", "example-site", "umami", "umami.pageviews", "count",
                start, start + timedelta(days=1), TimeGrain.DAY, Decimal("40"), (),
                Completeness.FINAL, start + timedelta(days=1),
            ),
        ])

        report = ReportService(self.config, self.store).render("summary", self.window)
        engagement = {
            item["id"]: item for item in report["decision_support"]["engagement"]
        }

        self.assertEqual(engagement["umami_views_per_visit"]["state"], "withheld")
        self.assertIsNone(engagement["umami_views_per_visit"]["value"])
        self.assertIn("complete inputs", engagement["umami_views_per_visit"]["note"])
        attention_ids = {
            item["id"] for item in report["decision_support"]["attention_items"]
        }
        self.assertIn("decision_input_coverage", attention_ids)
        self.assertNotIn("no_immediate_action", attention_ids)
        self.assertEqual(report["complete"], report["coverage"]["status"] == "complete")

    def test_decision_site_metric_withholds_mixed_provider_sources(self):
        coverage = {"by_site_source": [{
            "site_id": "example-site",
            "source": "umami",
            "metric_status": {"umami.visits": "complete"},
        }]}
        rows = [
            {"site_id": "example-site", "metric": "umami.visits", "source": "fixture", "value": 1},
            {"site_id": "example-site", "metric": "umami.visits", "source": "umami", "value": 2},
        ]

        self.assertEqual(
            ReportService._site_metric_value(
                rows, coverage, "example-site", "umami.visits"
            ),
            ("withheld", None, None),
        )

    def test_decision_ratio_withholds_cross_site_provider_blending(self):
        coverage = {"by_site_source": [
            {
                "site_id": site_id,
                "source": "umami",
                "configured_providers": [source],
                "metric_status": {
                    "umami.pageviews": "complete",
                    "umami.visits": "complete",
                },
            }
            for site_id, source in (
                ("example-site", "fixture"), ("second-site", "umami")
            )
        ]}
        rows = [
            {"site_id": site_id, "metric": metric_id, "source": source, "value": value}
            for site_id, source in (
                ("example-site", "fixture"), ("second-site", "umami")
            )
            for metric_id, value in (
                ("umami.pageviews", 10), ("umami.visits", 5)
            )
        ]

        state, value, scope, source_pair = ReportService._ratio_values(
            rows,
            coverage,
            ("example-site", "second-site"),
            "umami.pageviews",
            "umami.visits",
        )

        self.assertEqual(state, "withheld")
        self.assertIsNone(value)
        self.assertEqual(scope, ("example-site", "second-site"))
        self.assertIsNone(source_pair)

    def test_decision_ratio_rejects_hybrid_provider_pairs(self):
        same_source_coverage = {"by_site_source": [{
            "site_id": "example-site",
            "source": "umami",
            "configured_providers": ["fixture", "umami"],
            "metric_status": {
                "umami.bounces": "complete", "umami.visits": "complete",
            },
        }]}
        same_source_rows = [
            {"site_id": "example-site", "metric": "umami.bounces", "source": "fixture", "value": 2},
            {"site_id": "example-site", "metric": "umami.visits", "source": "umami", "value": 10},
        ]
        cross_source_coverage = {"by_site_source": [
            {
                "site_id": "example-site", "source": "cloudflare-forms",
                "configured_providers": ["cloudflare-forms"],
                "metric_status": {"forms.submissions": "complete"},
            },
            {
                "site_id": "example-site", "source": "umami",
                "configured_providers": ["fixture"],
                "metric_status": {"umami.visits": "complete"},
            },
        ]}
        cross_source_rows = [
            {"site_id": "example-site", "metric": "forms.submissions", "source": "cloudflare-forms", "value": 1},
            {"site_id": "example-site", "metric": "umami.visits", "source": "fixture", "value": 10},
        ]

        for rows, coverage, numerator, denominator in (
            (same_source_rows, same_source_coverage, "umami.bounces", "umami.visits"),
            (cross_source_rows, cross_source_coverage, "forms.submissions", "umami.visits"),
        ):
            state, value, _scope, source_pair = ReportService._ratio_values(
                rows, coverage, ("example-site",), numerator, denominator
            )
            self.assertEqual(state, "withheld")
            self.assertIsNone(value)
            self.assertIsNone(source_pair)

    def test_decision_support_alerts_on_cross_site_source_conflict(self):
        config = self._multi_site_config()
        run = self.store.start_run(
            "native-umami", "second-site",
            binding_key=(
                "second-site:native-umami:website:second-native-demo"
            ),
            source="umami", window=self.window,
        )
        observed_at = datetime.now(UTC)
        points = []
        for site_id, source in (
            ("example-site", "fixture"), ("second-site", "umami")
        ):
            points.append(MetricPoint(
                "example-client", site_id, source, "umami.visits", "count",
                self.window.start, self.window.end, TimeGrain.DAY,
                Decimal("10"), (), Completeness.FINAL,
                observed_at if source == "umami" else self.window.end,
            ))
            for day in (1, 2):
                start = datetime(2026, 7, day, tzinfo=UTC)
                points.append(MetricPoint(
                    "example-client", site_id, source, "umami.pageviews", "count",
                    start, start + timedelta(days=1), TimeGrain.DAY,
                    Decimal("20"), (), Completeness.FINAL,
                    observed_at if source == "umami"
                    else start + timedelta(hours=12),
                ))
        self.store.upsert(points)
        self.store.finish_run(
            run, "success", points=2, result_kind="data",
            data_through=self.window.end,
        )

        support = ReportService(config, self.store).render(
            "summary", self.window
        )["decision_support"]
        card = next(
            item for item in support["engagement"]
            if item["id"] == "umami_views_per_visit"
        )
        attention_ids = {item["id"] for item in support["attention_items"]}

        self.assertEqual(card["state"], "withheld")
        self.assertEqual(card["withheld_reason"], "source_conflict")
        self.assertIn("decision_source_conflict", attention_ids)

    def test_supporting_metric_source_conflict_is_an_attention_item(self):
        config = self._multi_site_config()
        self.store.upsert([
            MetricPoint(
                "example-client", site_id, source, "search.clicks", "count",
                datetime(2026, 7, day, tzinfo=UTC),
                datetime(2026, 7, day, tzinfo=UTC) + timedelta(days=1),
                TimeGrain.DAY, Decimal("2"), (), Completeness.FINAL,
                datetime(2026, 7, day, 12, tzinfo=UTC),
            )
            for site_id, source in (
                ("example-site", "fixture"),
                ("second-site", "search-console"),
            )
            for day in (1, 2)
        ])

        support = ReportService(config, self.store).render(
            "summary", self.window
        )["decision_support"]

        self.assertEqual(
            support["supporting_metrics"]["search.clicks"]["source"],
            "mixed",
        )
        self.assertIn("decision_source_conflict", {
            item["id"] for item in support["attention_items"]
        })

    def test_fact_and_query_proven_empty_provider_are_not_blended(self):
        self.store.upsert([
            MetricPoint(
                "example-client", "example-site", "fixture", metric_id,
                "count", datetime(2026, 7, day, tzinfo=UTC),
                datetime(2026, 7, day, tzinfo=UTC) + timedelta(days=1),
                TimeGrain.DAY,
                Decimal(str(value)), (), Completeness.FINAL,
                datetime(2026, 7, day, 12, tzinfo=UTC),
            )
            for day in (1, 2)
            for metric_id, value in (
                ("search.clicks", 2),
                ("umami.pageviews", 10),
                ("umami.visits", 5),
            )
        ])
        for connection_id, source, key in (
            (
                "native-search", "search-console",
                "example-site:native-search:site:sc-domain:example.com",
            ),
            (
                "native-umami", "umami",
                "example-site:native-umami:website:native-demo",
            ),
        ):
            run = self.store.start_run(
                connection_id, "example-site", binding_key=key,
                source=source, window=self.window,
            )
            self.store.finish_run(
                run, "success", result_kind="empty",
                data_through=(
                    self.window.end if source == "search-console" else None
                ),
            )

        report = ReportService(self.config, self.store).render(
            "summary", self.window
        )
        support = report["decision_support"]
        views_per_visit = next(
            item for item in support["engagement"]
            if item["id"] == "umami_views_per_visit"
        )
        search_pulse = support["site_pulse"][0]["metrics"]["search.clicks"]

        self.assertEqual(report["summary_totals"]["search.clicks"]["source"], "mixed")
        self.assertIsNone(report["summary_totals"]["search.clicks"]["value"])
        self.assertEqual(search_pulse["state"], "withheld")
        self.assertEqual(views_per_visit["state"], "withheld")
        self.assertIn("decision_source_conflict", {
            item["id"] for item in support["attention_items"]
        })

    def test_complete_report_still_alerts_on_incomplete_decision_inputs(self):
        self.store.upsert([
            point
            for day in (1, 2)
            for point in (
                metric("search.clicks", 1, day, "count"),
                metric("search.impressions", 10, day, "count"),
                metric("search.ctr", .1, day, "ratio"),
                metric("search.position", 5, day, "position"),
                metric("forms.submissions", 0, day, "count"),
            )
        ])

        report = ReportService(self.config, self.store).render("summary", self.window)
        attention_ids = {
            item["id"] for item in report["decision_support"]["attention_items"]
        }

        self.assertEqual(report["coverage"]["status"], "complete")
        self.assertNotEqual(report["decision_support"]["coverage"]["status"], "complete")
        self.assertIn("decision_input_coverage", attention_ids)
        self.assertNotIn("no_immediate_action", attention_ids)

    def test_series_render_can_skip_decision_only_queries(self):
        calls = []
        original_query = self.store.query

        def query_spy(**kwargs):
            calls.append(tuple(kwargs["metric_ids"]))
            return original_query(**kwargs)

        with patch.object(self.store, "query", side_effect=query_spy):
            report = ReportService(self.config, self.store).render(
                "summary",
                self.window,
                include_decision_support=False,
                include_provider_comparisons=False,
            )

        self.assertIsNone(report["decision_support"])
        self.assertEqual(report["provider_comparisons"], [])
        self.assertFalse(
            {"google.page-path-views", "umami.route-pageviews"}.intersection(
                metric for call in calls for metric in call
            )
        )
        self.assertEqual(
            set(report["summary_totals"]), set(self.config.reports[0].metric_ids)
        )

    def test_decision_support_represents_never_run_and_unfinished_bindings(self):
        key = "example-site:native-umami:website:native-demo"
        self.store.start_run(
            "native-umami", "example-site", binding_key=key,
            source="umami", window=self.window,
        )

        support = ReportService(self.config, self.store).render(
            "summary", self.window
        )["decision_support"]
        statuses = {item["status"] for item in support["operations_health"]}
        attention_ids = {item["id"] for item in support["attention_items"]}
        expected_bindings = [
            binding for binding in self.config.bindings
            if binding.site_id == "example-site"
        ]

        self.assertEqual(len(support["operations_health"]), len(expected_bindings))
        self.assertIn("running", statuses)
        self.assertIn("never_run", statuses)
        self.assertIn("sync_unfinished", attention_ids)
        self.assertIn("sync_never_run", attention_ids)

    def test_decision_support_represents_connections_never_probed(self):
        support = ReportService(self.config, self.store).render(
            "summary", self.window
        )["decision_support"]
        selected_connections = {
            binding.connection_id for binding in self.config.bindings
            if binding.site_id == "example-site"
        }

        self.assertEqual(len(support["capabilities"]), len(selected_connections))
        self.assertTrue(all(
            item["state"] == "not_recorded"
            for item in support["capabilities"]
        ))
        self.assertNotIn("connection_id", repr(support["capabilities"]))
        self.assertIn("capability_never_probed", {
            item["id"] for item in support["attention_items"]
        })

    def test_mailbox_reconciliation_requires_identical_site_scope(self):
        config = self._multi_site_config(
            include_second_inbox=False, include_second_fixture=False
        )

        def form_point(metric_id, value, day, site_id, source):
            start = datetime(2026, 7, day, tzinfo=UTC)
            return MetricPoint(
                "example-client", site_id, source, metric_id, "count",
                start, start + timedelta(days=1), TimeGrain.DAY,
                Decimal(str(value)), (), Completeness.FINAL,
                start + timedelta(hours=12),
            )

        self.store.upsert([
            form_point("forms.submissions", 1, day, site_id, "cloudflare-forms")
            for day in (1, 2)
            for site_id in ("example-site", "second-site")
        ] + [
            form_point("forms.inbox-deliveries", 1, day, "example-site", "forms-inbox")
            for day in (1, 2)
        ])

        support = ReportService(config, self.store).render(
            "summary", self.window
        )["decision_support"]
        attention_ids = {item["id"] for item in support["attention_items"]}

        self.assertIn("mailbox_scope_mismatch", attention_ids)
        self.assertNotIn("mailbox_reconciliation", attention_ids)

    def test_decision_support_surfaces_safe_latest_sync_failure(self):
        key = "example-site:native-umami:website:native-demo"
        run = self.store.start_run(
            "native-umami", "example-site", binding_key=key,
            source="umami", window=self.window,
        )
        self.store.finish_run(
            run, "failed", category="provider_http",
            message="private response text", result_kind="failed",
        )

        support = ReportService(self.config, self.store).render(
            "summary", self.window
        )["decision_support"]

        operation = next(
            item for item in support["operations_health"]
            if item["site_id"] == "example-site" and item["source"] == "umami"
        )
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(operation["error_category"], "provider_http")
        self.assertNotIn("private response text", repr(support))
        self.assertNotIn("connection_id", repr(support))
        self.assertIn("sync_failure", {
            item["id"] for item in support["attention_items"]
        })
        self.assertEqual(support["attention_items"][0]["id"], "sync_failure")

    def test_search_surface_selection_filters_before_aggregation_and_exports(self):
        connection_sources = {
            connection.id: connection.provider
            for connection in self.config.connections
        }
        search_binding = next(
            binding for binding in self.config.bindings
            if connection_sources[binding.connection_id] == "search-console"
        )
        object.__setattr__(search_binding, "options", {
            "route_analytics": {"search_types": ["all"]},
        })
        base_dimensions = (
            ("aggregation", "byDate"),
            ("data_state", "final"),
            ("provider_date", "2026-07-01"),
            ("provider_timezone", "America/Los_Angeles"),
        )
        web = metric(
            "search.clicks", 10, 1, "count",
            dimensions=(*base_dimensions, ("search_type", "web")),
        )
        image = metric(
            "search.clicks", 4, 1, "count",
            dimensions=(*base_dimensions, ("search_type", "image")),
        )
        legacy_discover_position = metric(
            "search.position", 0, 1, "position",
            dimensions=(*base_dimensions, ("search_type", "discover")),
        )
        self.store.upsert([web, image, legacy_discover_position])
        service = ReportService(self.config, self.store)

        default_report = service.render(
            "summary", self.window,
            include_decision_support=False,
            include_provider_comparisons=False,
        )
        image_report = service.render(
            "summary", self.window, search_type="image",
            include_decision_support=False,
            include_provider_comparisons=False,
        )

        default_clicks = next(
            row for row in default_report["rows"]
            if row["metric"] == "search.clicks"
        )
        image_clicks = next(
            row for row in image_report["rows"]
            if row["metric"] == "search.clicks"
        )
        self.assertEqual(default_report["search_type"], "web")
        self.assertEqual(default_report["site_ids"], ["example-site"])
        self.assertEqual(default_clicks["value"], 10)
        self.assertEqual(default_clicks["search_type"], "web")
        self.assertEqual(image_clicks["value"], 4)
        self.assertEqual(image_clicks["search_type"], "image")
        self.assertEqual(
            image_report["available_search_types"],
            ["web", "image", "video", "news", "discover", "googleNews"],
        )
        self.assertEqual(
            image_report["search_types_by_site"]["example-site"],
            ["web", "image", "video", "news", "discover", "googleNews"],
        )
        self.assertEqual(
            next(
                item for item in image_report["series"]
                if item["metric"] == "search.clicks"
            )["search_type"],
            "image",
        )
        self.assertEqual(
            image_report["summary_totals"]["search.clicks"]["search_type"],
            "image",
        )
        metric_csv = next(
            row for row in csv.DictReader(io.StringIO(to_csv(image_report)))
            if row["metric"] == "search.clicks"
        )
        series_csv = next(
            row for row in csv.DictReader(
                io.StringIO(to_series_csv(image_report))
            )
            if row["metric"] == "search.clicks"
        )
        self.assertEqual(metric_csv["search_type"], "image")
        self.assertEqual(series_csv["search_type"], "image")

        discover_report = service.render(
            "summary", self.window, search_type="discover",
            include_decision_support=False,
            include_provider_comparisons=False,
        )
        discover_position = discover_report["summary_totals"]["search.position"]
        self.assertEqual(discover_position["coverage_status"], "unavailable")
        self.assertEqual(discover_position["expected_cells"], 0)
        self.assertEqual(discover_position["covered_cells"], 0)
        self.assertIsNone(discover_position["value"])
        self.assertFalse(any(
            row["metric"] == "search.position"
            for row in discover_report["rows"]
        ))
        self.assertFalse(any(
            row["metric"] == "search.position"
            for row in discover_report["series"]
        ))
        search_bucket = next(
            item for item in discover_report["coverage"]["by_site_source"]
            if item["source"] == "search-console"
        )
        self.assertEqual(
            search_bucket["metric_status"]["search.position"], "unavailable"
        )
        self.assertFalse(any(
            "No observations match" in warning
            and "search.position" in warning
            for warning in discover_report["warnings"]
        ))
        self.assertTrue(any(
            "average position is not defined" in warning
            for warning in discover_report["warnings"]
        ))

        summary_config = next(
            item for item in self.config.reports if item.id == "summary"
        )
        object.__setattr__(summary_config, "metric_ids", ("search.position",))
        for search_type in ("discover", "googleNews"):
            with self.subTest(search_type=search_type):
                unsupported_only = service.render(
                    "summary", self.window, search_type=search_type
                )
                unsupported_bucket = next(
                    item
                    for item in unsupported_only["coverage"]["by_site_source"]
                    if item["source"] == "search-console"
                )
                self.assertEqual(unsupported_bucket["status"], "unavailable")
                self.assertEqual(
                    unsupported_only["coverage"]["status"], "unavailable"
                )
                self.assertFalse(any(
                    "Coverage is incomplete" in warning
                    for warning in unsupported_only["warnings"]
                ))
                self.assertFalse(any(
                    item["id"] == "data_coverage"
                    for item
                    in unsupported_only["decision_support"]["attention_items"]
                ))
        with self.assertRaisesRegex(ValueError, "search type is unavailable"):
            service.render(
                "summary", self.window, search_type="bogus",
                include_decision_support=False,
                include_provider_comparisons=False,
            )

    def test_aggregate_rejects_unfiltered_search_surfaces(self):
        dimensions = (
            ("aggregation", "byDate"),
            ("data_state", "final"),
            ("provider_date", "2026-07-01"),
            ("provider_timezone", "America/Los_Angeles"),
        )
        points = [
            metric(
                "search.clicks", value, 1, "count",
                dimensions=(*dimensions, ("search_type", search_type)),
            )
            for search_type, value in (("web", 10), ("image", 4))
        ]

        with self.assertRaisesRegex(ValueError, "filtered to one search type"):
            ReportService(self.config, self.store)._aggregate(
                points, self.window, ("search.clicks",)
            )
        with self.assertRaisesRegex(ValueError, "filtered to one search type"):
            ReportService(self.config, self.store)._series(
                points, self.window, ("search.clicks",)
            )

    def test_daily_unique_visitors_are_series_only_and_not_reported_missing(self):
        report = next(item for item in self.config.reports if item.id == "summary")
        object.__setattr__(report, "metric_ids", ("umami.daily-visitors",))
        points = []
        for day, value in ((1, 7), (2, 9)):
            start = datetime(2026, 7, day, tzinfo=UTC)
            points.append(MetricPoint(
                "example-client", "example-site", "umami",
                "umami.daily-visitors", "count", start,
                start + timedelta(days=1), TimeGrain.DAY,
                Decimal(value), (), Completeness.FINAL,
                start + timedelta(hours=12),
            ))
        self.store.upsert(points)

        rendered = ReportService(self.config, self.store).render(
            "summary", self.window,
            include_decision_support=False,
            include_provider_comparisons=False,
        )

        self.assertFalse(any(
            "No observations" in warning for warning in rendered["warnings"]
        ))
        self.assertFalse(any(
            "Partial aggregate withheld" in warning
            for warning in rendered["warnings"]
        ))
        self.assertTrue(any(
            "not summed into window uniques" in warning
            for warning in rendered["warnings"]
        ))
        self.assertEqual(rendered["rows"], [])
        self.assertEqual(rendered["series"], [{
            "metric": "umami.daily-visitors",
            "site_id": "example-site",
            "source": "umami",
            "unit": "count",
            "points": [
                {"date": "2026-07-01", "value": 7},
                {"date": "2026-07-02", "value": 9},
            ],
        }])
        total = rendered["summary_totals"]["umami.daily-visitors"]
        self.assertIsNone(total["value"])
        self.assertTrue(total["observed"])
        self.assertEqual(total["display_mode"], "daily-series-only")
        self.assertTrue(total["non_additive_across_days"])
        self.assertFalse(total["comparison_available"])

    def test_dimensioned_window_buckets_are_not_arbitrary_scalar_totals(self):
        common = {
            "client_id": "example-client",
            "site_id": "example-site",
            "source": "umami",
            "metric": "umami.country-visits",
            "unit": "count",
            "start": self.window.start,
            "end": self.window.end,
        }
        points = [
            MetricPoint(
                common["client_id"], common["site_id"], common["source"],
                common["metric"], common["unit"], common["start"],
                common["end"], TimeGrain.TOTAL, Decimal(value),
                tuple(sorted({
                    "country_code": code,
                    "country_code_system": "iso-alpha2",
                }.items())), Completeness.FINAL, self.window.end,
            )
            for code, value in (("US", 8), ("GB", 2))
        ]

        rows, _freshness = ReportService(
            self.config, self.store
        )._aggregate(points, self.window, ("umami.country-visits",))

        self.assertEqual(rows, [])


if __name__ == "__main__": unittest.main()
