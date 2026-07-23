from __future__ import annotations

import unittest
from datetime import UTC, datetime

from boho_analytics_platform.time_window import report_window


class ReportWindowTests(unittest.TestCase):
    def test_default_window_can_end_before_immature_provider_days(self):
        window = report_window(
            timezone="America/Chicago",
            default_days=7,
            default_end_lag_days=1,
            now=datetime(2026, 7, 21, 14, 0, tzinfo=UTC),
        )

        self.assertEqual(window.start.isoformat(), "2026-07-13T00:00:00-05:00")
        self.assertEqual(window.end.isoformat(), "2026-07-20T00:00:00-05:00")

    def test_explicit_window_is_not_shifted_by_default_end_lag(self):
        window = report_window(
            timezone="UTC",
            default_days=7,
            default_end_lag_days=3,
            start="2026-07-01",
            end="2026-07-08",
            now=datetime(2026, 7, 21, tzinfo=UTC),
        )

        self.assertEqual(window.start.isoformat(), "2026-07-01T00:00:00+00:00")
        self.assertEqual(window.end.isoformat(), "2026-07-08T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
