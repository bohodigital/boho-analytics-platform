from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from boho_analytics_platform.catalog import validate_points
from boho_analytics_platform.models import Completeness, MetricPoint, TimeGrain


class CatalogTests(unittest.TestCase):
    def point(self, metric="umami.pageviews", unit="count", source="umami"):
        start = datetime(2026, 7, 1, tzinfo=UTC)
        return MetricPoint("client", "site", source, metric, unit, start, start + timedelta(days=1),
            TimeGrain.DAY, Decimal(1), (), Completeness.FINAL, start)

    def test_unknown_metric_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown metric"): validate_points([self.point("unknown")])

    def test_wrong_unit_and_source_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "wrong unit"): validate_points([self.point(unit="bytes")])
        with self.assertRaisesRegex(ValueError, "wrong source"): validate_points([self.point(source="cloudflare")])

    def test_fixture_may_replay_cataloged_metrics(self): validate_points([self.point(source="fixture")], fixture=True)


if __name__ == "__main__": unittest.main()
