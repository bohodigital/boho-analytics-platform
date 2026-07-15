from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from boho_analytics_platform.config import load_config
from boho_analytics_platform.models import Completeness, MetricPoint, QueryWindow, TimeGrain
from boho_analytics_platform.reporting import ReportService
from boho_analytics_platform.storage import SQLiteMetricStore
from support import config_text, write_fixture


def metric(name, value, day, unit, dimensions=()):
    start = datetime(2026, 7, day, tzinfo=UTC)
    return MetricPoint("example-client", "example-site", "search-console" if name.startswith("search.") else "cloudflare-forms",
        name, unit, start, start + timedelta(days=1), TimeGrain.DAY, Decimal(str(value)), tuple(sorted(dimensions)),
        Completeness.FINAL, datetime(2026, 7, day, 12, tzinfo=UTC))


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup); root = Path(self.temporary.name)
        fixture = root / "fixture.json"; write_fixture(fixture)
        text = config_text(root / "state.db", fixture).replace(
            'metric_ids = ["umami.pageviews", "forms.submissions", "forms.inbox-deliveries"]',
            'metric_ids = ["search.clicks", "search.impressions", "search.ctr", "search.position", "forms.submissions"]')
        path = root / "platform.toml"; path.write_text(text, encoding="utf-8"); self.config = load_config(path)
        self.store = SQLiteMetricStore(root / "state.db"); self.store.initialize()
        self.window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 3, tzinfo=UTC), "UTC")

    def test_ctr_and_position_are_weighted_not_summed(self):
        self.store.upsert([
            metric("search.clicks", 10, 1, "count"), metric("search.impressions", 100, 1, "count"),
            metric("search.ctr", .1, 1, "ratio"), metric("search.position", 2, 1, "position"),
            metric("search.clicks", 20, 2, "count"), metric("search.impressions", 100, 2, "count"),
            metric("search.ctr", .2, 2, "ratio"), metric("search.position", 4, 2, "position")])
        report = ReportService(self.config, self.store).render("summary", self.window)
        values = {row["metric"]: row["value"] for row in report["rows"]}
        self.assertEqual(values["search.ctr"], .15); self.assertEqual(values["search.position"], 3)

    def test_subreport_dimension_filter_is_applied(self):
        root = Path(self.temporary.name); text = (root / "platform.toml").read_text(encoding="utf-8")
        text = text.replace('metric_ids = ["forms.submissions", "forms.inbox-deliveries"]\ndefault_window_days = 30',
            'metric_ids = ["forms.submissions", "forms.inbox-deliveries"]\ndefault_window_days = 30\n[reports.subreports.filters]\nform_id = "contact"')
        (root / "filtered.toml").write_text(text, encoding="utf-8"); config = load_config(root / "filtered.toml")
        self.store.upsert([metric("forms.submissions", 2, 1, "count", (("form_id", "contact"),)), metric("forms.submissions", 8, 1, "count", (("form_id", "quote"),))])
        report = ReportService(config, self.store).render("summary", self.window, "forms")
        self.assertEqual(report["filters"], {"form_id": "contact"}); self.assertEqual(report["rows"][0]["value"], 2)


if __name__ == "__main__": unittest.main()
