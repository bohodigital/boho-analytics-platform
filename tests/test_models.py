from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from boho_analytics_platform.models import (
    AcquisitionBatch,
    AcquisitionSlice,
    Completeness,
    MetricPoint,
    QueryWindow,
    TimeGrain,
    canonical_dimensions,
)


class ModelTests(unittest.TestCase):
    @staticmethod
    def _acquisition_slice(**overrides) -> AcquisitionSlice:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        values = {
            "slice_key": "gsc.web.query-page",
            "metric_family": "search-performance",
            "start": start,
            "end": start + timedelta(days=1),
            "completeness": Completeness.FINAL,
            "data_state": "final",
            "provider_scope": "web",
            "request_dimensions": ("date", "query", "page"),
            "provider_aggregation": "byProperty",
            "pages_fetched": 2,
            "raw_rows": 1,
            "accepted_rows": 1,
            "rejected_rows": 0,
            "exhaustion_reason": "unique_short_page",
        }
        values.update(overrides)
        return AcquisitionSlice(**values)

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

    def test_acquisition_slice_is_strict_bounded_and_immutable(self) -> None:
        acquisition = self._acquisition_slice()
        with self.assertRaises(FrozenInstanceError):
            acquisition.raw_rows = 2
        with self.assertRaisesRegex(ValueError, "bounded ASCII"):
            self._acquisition_slice(provider_scope="https://private.example")
        with self.assertRaisesRegex(ValueError, "unique"):
            self._acquisition_slice(request_dimensions=("date", "date"))
        with self.assertRaisesRegex(ValueError, "must equal"):
            self._acquisition_slice(raw_rows=2)
        with self.assertRaisesRegex(ValueError, "final.*rejected"):
            self._acquisition_slice(
                completeness=Completeness.FINAL,
                raw_rows=1,
                accepted_rows=0,
                rejected_rows=1,
            )

    def test_acquisition_batch_owns_matching_tuple_of_points(self) -> None:
        acquisition = self._acquisition_slice()
        point = MetricPoint(
            client_id="client",
            site_id="site",
            source="search-console",
            metric="search.clicks",
            unit="count",
            start=acquisition.start,
            end=acquisition.end,
            grain=TimeGrain.DAY,
            value=Decimal("3"),
            dimensions=(("query_cluster", "example"),),
            completeness=Completeness.FINAL,
            observed_at=acquisition.end + timedelta(days=1),
        )
        batch = AcquisitionBatch(acquisition, (point,))
        self.assertEqual(batch.points, (point,))
        with self.assertRaises(FrozenInstanceError):
            batch.points = ()
        with self.assertRaisesRegex(ValueError, "immutable tuple"):
            AcquisitionBatch(acquisition, [point])
        with self.assertRaisesRegex(ValueError, "accepted provider row"):
            AcquisitionBatch(
                self._acquisition_slice(
                    completeness=Completeness.UNKNOWN,
                    raw_rows=0,
                    accepted_rows=0,
                    rejected_rows=0,
                ),
                (point,),
            )
        self.assertEqual(
            AcquisitionBatch(acquisition, ()).points,
            (),
        )
        mixed = AcquisitionBatch(
            self._acquisition_slice(completeness=Completeness.PROVISIONAL),
            (point,),
        )
        self.assertIs(mixed.slice.completeness, Completeness.PROVISIONAL)
        self.assertIs(mixed.points[0].completeness, Completeness.FINAL)
        with self.assertRaisesRegex(ValueError, "overlap"):
            AcquisitionBatch(
                self._acquisition_slice(
                    start=acquisition.end + timedelta(days=1),
                    end=acquisition.end + timedelta(days=2),
                ),
                (point,),
            )


if __name__ == "__main__":
    unittest.main()
