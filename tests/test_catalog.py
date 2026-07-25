from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from boho_analytics_platform.catalog import METRICS, SOURCE_SEMANTICS, validate_points
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

    def test_route_metrics_reject_ambiguous_or_unapproved_dimension_sets(self):
        valid = self.point(
            metric="search.route-clicks",
            source="search-console",
            dimensions=(
                ("data_state", "final"),
                ("observation_scope", "page"),
                ("route", "/about"),
            ),
        )
        validate_points([valid])
        with self.assertRaisesRegex(ValueError, "invalid dimensions"):
            validate_points([self.point(
                metric="search.route-clicks",
                source="search-console",
                dimensions=(("route", "/about"),),
            )])

    def test_source_semantics_do_not_claim_unknown_provider_details(self):
        self.assertEqual(
            SOURCE_SEMANTICS["search-console"].time_basis,
            "America/Los_Angeles-provider-date-mapped-to-site-day",
        )
        self.assertEqual(SOURCE_SEMANTICS["cloudflare"].sampling, "adaptive")
        self.assertEqual(
            SOURCE_SEMANTICS["google-analytics"].time_basis,
            "response-validated-property-timezone-or-unverified",
        )


if __name__ == "__main__": unittest.main()
