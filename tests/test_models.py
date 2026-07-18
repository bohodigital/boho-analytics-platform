from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from boho_analytics_platform.models import (
    Completeness,
    MetricPoint,
    QueryWindow,
    TimeGrain,
    canonical_dimensions,
)


class ModelTests(unittest.TestCase):
    def test_query_window_requires_ordered_aware_instants(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        QueryWindow(start=start, end=start + timedelta(days=1), timezone="UTC")
        with self.assertRaisesRegex(ValueError, "earlier"):
            QueryWindow(start=start, end=start, timezone="UTC")
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            QueryWindow(
                start=datetime(2026, 1, 1),
                end=datetime(2026, 1, 2),
                timezone="UTC",
            )

    def test_dimensions_are_canonical(self) -> None:
        self.assertEqual(
            canonical_dimensions({"device": "mobile", "country": "US"}),
            (("country", "US"), ("device", "mobile")),
        )

    def test_metric_point_rejects_non_finite_values(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        with self.assertRaisesRegex(ValueError, "finite"):
            MetricPoint(
                client_id="example-client",
                site_id="example-site",
                source="umami",
                metric="visits",
                unit="count",
                start=start,
                end=start + timedelta(days=1),
                grain=TimeGrain.DAY,
                value=Decimal("NaN"),
                dimensions=(),
                completeness=Completeness.FINAL,
                observed_at=start + timedelta(days=2),
            )


if __name__ == "__main__":
    unittest.main()
