from __future__ import annotations

import tempfile
import unittest
import csv
import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

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
        self.store.upsert([
            MetricPoint(
                "example-client", "example-site", "fixture", "umami.pageviews", "count",
                start, start + timedelta(days=1), TimeGrain.DAY, Decimal(100), (),
                Completeness.FINAL, start + timedelta(hours=8),
            ),
            MetricPoint(
                "example-client", "example-site", "umami", "umami.pageviews", "count",
                start, start + timedelta(days=1), TimeGrain.DAY, Decimal(3), (),
                Completeness.FINAL, start + timedelta(hours=9),
            ),
        ])

        report = ReportService(config, self.store).render(
            "summary", QueryWindow(start, start + timedelta(days=1), "UTC")
        )

        self.assertTrue(report["complete"])
        self.assertEqual(report["summary_totals"]["umami.pageviews"]["value"], 3)
        self.assertEqual({row["source"] for row in report["rows"]}, {"umami"})

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
        fixture_observed = start + timedelta(hours=8)
        umami_observed = start + timedelta(hours=10)
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
        report = ReportService(config, self.store).render(
            "summary", QueryWindow(start, start + timedelta(days=1), "UTC")
        )
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

        def native(metric_id, value, *, source, unit="count", day=None):
            point_start = start if day is None else start + timedelta(days=day)
            point_end = self.window.end if day is None else point_start + timedelta(days=1)
            return MetricPoint(
                "example-client", "example-site", source, metric_id, unit,
                point_start, point_end, TimeGrain.DAY, Decimal(str(value)), (),
                Completeness.FINAL, point_end,
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
        points = []
        for site_id, source in (
            ("example-site", "fixture"), ("second-site", "umami")
        ):
            points.append(MetricPoint(
                "example-client", site_id, source, "umami.visits", "count",
                self.window.start, self.window.end, TimeGrain.DAY,
                Decimal("10"), (), Completeness.FINAL, self.window.end,
            ))
            for day in (1, 2):
                start = datetime(2026, 7, day, tzinfo=UTC)
                points.append(MetricPoint(
                    "example-client", site_id, source, "umami.pageviews", "count",
                    start, start + timedelta(days=1), TimeGrain.DAY,
                    Decimal("20"), (), Completeness.FINAL,
                    start + timedelta(hours=12),
                ))
        self.store.upsert(points)

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
        report = ReportService(self.config, self.store).render(
            "summary", self.window, include_decision_support=False
        )

        self.assertIsNone(report["decision_support"])
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


if __name__ == "__main__": unittest.main()
