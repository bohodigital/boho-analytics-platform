from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from boho_analytics_platform.connectors.common import daily_point, total_point
from boho_analytics_platform.config import load_config
from boho_analytics_platform.geography import GeographyService
from boho_analytics_platform.models import QueryWindow
from boho_analytics_platform.storage import SQLiteMetricStore
from support import config_text, write_fixture


class GeographyServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        fixture = root / "fixture.json"; write_fixture(fixture)
        path = root / "platform.toml"; path.write_text(config_text(root / "state.db", fixture), encoding="utf-8")
        self.config = load_config(path); self.store = SQLiteMetricStore(root / "state.db"); self.store.initialize()
        self.window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 3, tzinfo=UTC), "UTC")
        self.service = GeographyService(self.config, self.store, suppression_threshold=3)

    def test_umami_aggregates_country_and_us_region_without_exposing_row_identifiers(self):
        common = {"client_id": "example-client", "site_id": "example-site", "source": "umami", "unit": "count", "start": self.window.start, "end": self.window.end}
        self.store.upsert([
            total_point(**common, metric="umami.country-visits", value=8, dimensions={"country_code": "US", "country_code_system": "iso-alpha2"}),
            total_point(**common, metric="umami.country-visits", value=2, dimensions={"country_code": "GB", "country_code_system": "iso-alpha2"}),
            total_point(**common, metric="umami.region-visits", value=5, dimensions={"country_code": "US", "country_code_system": "iso-alpha2", "region_code": "CA"}),
            total_point(**common, metric="umami.region-visits", value=2, dimensions={"country_code": "US", "country_code_system": "iso-alpha2", "region_code": "TX"}),
        ])

        payload = self.service.render("summary", self.window, "umami")

        self.assertEqual(payload["countries"], [{"code": "US", "code_system": "iso-alpha2", "value": 8}])
        self.assertEqual(payload["us_states"], [{"code": "CA", "name": "California", "value": 5}])
        self.assertEqual(payload["suppression"], {"threshold": 3, "withheld_country_rows": 1, "withheld_us_state_rows": 1})
        self.assertEqual(payload["counties"]["status"], "unavailable")
        self.assertNotIn("points", payload)
        self.assertNotIn("visitor", str(payload).casefold())

    def test_search_console_country_only_discloses_provider_limit(self):
        self.store.upsert([daily_point(
            client_id="example-client", site_id="example-site", source="search-console",
            metric="search.country-clicks", unit="count", day="2026-07-01", value=9,
            timezone="UTC", dimensions={"country_code": "USA", "country_code_system": "iso-alpha3"},
        )])

        payload = self.service.render("summary", self.window, "search-console")

        self.assertEqual(payload["countries"][0]["code"], "USA")
        self.assertEqual(payload["us_states"], [])
        self.assertEqual(payload["region_support"]["status"], "unavailable")
        self.assertIn("country", payload["methodology"].casefold())

    def test_rejects_unknown_source_and_site(self):
        with self.assertRaisesRegex(ValueError, "geography source"):
            self.service.render("summary", self.window, "fixture")
        with self.assertRaisesRegex(ValueError, "site"):
            self.service.render("summary", self.window, "umami", site_id="unknown")

    def test_actual_provider_rows_take_precedence_over_fixture_rows(self):
        dimensions = {"country_code": "US", "country_code_system": "iso-alpha2"}
        common = {"client_id": "example-client", "site_id": "example-site",
            "metric": "umami.country-visits", "unit": "count",
            "start": self.window.start, "end": self.window.end, "dimensions": dimensions}
        self.store.upsert([
            total_point(**common, source="fixture", value=99),
            total_point(**common, source="umami", value=7),
        ])

        payload = self.service.render("summary", self.window, "umami")

        self.assertEqual(payload["countries"], [{"code": "US", "code_system": "iso-alpha2", "value": 7}])


if __name__ == "__main__":
    unittest.main()
