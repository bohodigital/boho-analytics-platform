from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from boho_analytics_platform.catalog import (
    METRICS,
    SOURCE_SEMANTICS,
    search_console_metric_supported,
    validate_points,
)
from boho_analytics_platform.models import Completeness, MetricPoint, TimeGrain


class CatalogTests(unittest.TestCase):
    def point(self, metric="umami.pageviews", unit="count", source="umami", dimensions=()):
        start = datetime(2026, 7, 1, tzinfo=UTC)
        return MetricPoint("client", "site", source, metric, unit, start, start + timedelta(days=1),
            TimeGrain.DAY, Decimal(1), dimensions, Completeness.FINAL, start)

    def test_unknown_metric_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown metric"): validate_points([self.point("unknown")])

    def test_wrong_unit_and_source_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "wrong unit"): validate_points([self.point(unit="bytes")])
        with self.assertRaisesRegex(ValueError, "wrong source"): validate_points([self.point(source="cloudflare")])

    def test_fixture_may_replay_cataloged_metrics(self): validate_points([self.point(source="fixture")], fixture=True)

    def test_weighted_metrics_declare_their_coverage_inputs(self):
        self.assertEqual(
            METRICS["search.ctr"].coverage_inputs,
            ("search.clicks", "search.impressions"),
        )
        self.assertEqual(
            METRICS["search.position"].coverage_inputs,
            ("search.impressions", "search.position"),
        )
        self.assertFalse(METRICS["search.route-ctr"].reportable)

    def test_position_support_contract_covers_every_gsc_metric_family(self):
        position_metrics = (
            "search.position",
            "search.country-position",
            "search.route-position",
            "search.query-position",
            "search.page-query-position",
            "search.hourly-position",
        )
        for metric in position_metrics:
            with self.subTest(metric=metric):
                self.assertTrue(search_console_metric_supported(metric, "web"))
                self.assertFalse(
                    search_console_metric_supported(metric, "discover")
                )
                self.assertFalse(
                    search_console_metric_supported(metric, "googleNews")
                )
        self.assertTrue(
            search_console_metric_supported("search.route-impressions", "discover")
        )

    def test_route_metrics_reject_ambiguous_or_unapproved_dimension_sets(self):
        valid = self.point(
            metric="search.route-clicks",
            source="search-console",
            dimensions=(
                ("aggregation", "byPage"),
                ("data_state", "final"),
                ("observation_scope", "page"),
                ("provider_date", "2026-07-01"),
                ("provider_timezone", "America/Los_Angeles"),
                ("route", "/about"),
                ("search_type", "web"),
            ),
        )
        validate_points([valid])
        with self.assertRaisesRegex(ValueError, "invalid dimensions"):
            validate_points([self.point(
                metric="search.route-clicks",
                source="search-console",
                dimensions=(("route", "/about"),),
            )])

    def test_umami_route_pageviews_are_a_distinct_non_reportable_metric(self):
        definition = METRICS["umami.route-pageviews"]
        self.assertEqual(definition.source, "umami")
        self.assertEqual(definition.unit, "count")
        self.assertEqual(definition.dimension_sets, (("route",),))
        self.assertFalse(definition.reportable)
        self.assertNotEqual(definition.id, METRICS["umami.route-visits"].id)

    def test_gsc_control_and_detail_metrics_carry_provider_semantics(self):
        self.assertIn(
            (
                "aggregation", "data_state", "provider_date",
                "provider_timezone", "search_type",
            ),
            METRICS["search.clicks"].dimension_sets,
        )
        self.assertIn(
            ("aggregation", "data_state", "search_type"),
            METRICS["search.hourly-clicks"].dimension_sets,
        )
        self.assertFalse(METRICS["search.query-clicks"].reportable)
        self.assertFalse(METRICS["search.page-query-clicks"].reportable)
        self.assertIn(
            (
                "aggregation", "data_state", "observation_scope",
                "provider_date", "provider_timezone", "query_text",
                "query_visibility", "route", "search_type",
            ),
            METRICS["search.page-query-clicks"].dimension_sets,
        )

    def test_umami_expanded_metrics_use_one_generic_safe_dimension_contract(self):
        definition = METRICS["umami.dimension-visits"]
        self.assertEqual(definition.source, "umami")
        self.assertEqual(
            definition.dimension_sets,
            (("dimension_type", "dimension_value", "dimension_value_kind"),),
        )
        self.assertFalse(definition.reportable)

    def test_geography_buckets_are_dimension_only_not_scalar_report_metrics(self):
        expected = {
            "umami.country-visits": (
                ("country_code", "country_code_system"),
            ),
            "umami.region-visits": (
                ("country_code", "country_code_system", "region_code"),
                ("country_code", "country_code_system", "region_name"),
            ),
            "cloudflare.country-visits": (
                ("country_code", "country_code_system"),
            ),
            "google.country-sessions": (
                ("country_code", "country_code_system"),
            ),
            "google.region-sessions": (
                ("country_code", "country_code_system", "region_code"),
                ("country_code", "country_code_system", "region_name"),
            ),
        }

        for metric, dimension_sets in expected.items():
            with self.subTest(metric=metric):
                self.assertFalse(METRICS[metric].reportable)
                self.assertEqual(METRICS[metric].dimension_sets, dimension_sets)

    def test_source_semantics_do_not_claim_unknown_provider_details(self):
        self.assertEqual(
            SOURCE_SEMANTICS["search-console"].time_basis,
            "explicit-America/Los_Angeles-provider-date-mapped-to-site-reporting-day",
        )
        self.assertEqual(
            SOURCE_SEMANTICS["search-console"].sampling,
            "control-totals-plus-provider-top-rows",
        )
        self.assertEqual(
            SOURCE_SEMANTICS["search-console"].data_state,
            "request-labeled-final-or-provisional",
        )
        self.assertEqual(SOURCE_SEMANTICS["cloudflare"].sampling, "adaptive")
        self.assertEqual(
            SOURCE_SEMANTICS["google-analytics"].time_basis,
            "response-validated-property-timezone-or-unverified",
        )


if __name__ == "__main__": unittest.main()
