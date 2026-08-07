from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from boho_analytics_platform.config import (
    ConfigError,
    ReportConfig,
    SubreportConfig,
    binding_observation_boundary,
    binding_observation_start,
    load_config,
    route_analytics_options,
)
from support import config_text, write_fixture


class ConfigTests(unittest.TestCase):
    def _load(self, mutate=lambda value: value):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name); fixture = root / "fixture.json"; write_fixture(fixture)
        path = root / "platform.toml"; path.write_text(mutate(config_text(root / "state.db", fixture)), encoding="utf-8")
        return load_config(path)

    def test_valid_schema_v2_configuration(self):
        config = self._load(); self.assertEqual(config.schema_version, 2); self.assertEqual(config.reports[0].subreports[0].id, "forms")

    def test_report_default_end_lag_is_bounded_and_inherited_by_subreports(self):
        config = self._load(lambda text: text.replace(
            "default_window_days = 30\n[[reports.subreports]]",
            "default_window_days = 7\ndefault_end_lag_days = 1\n[[reports.subreports]]",
            1,
        ))
        self.assertEqual(config.reports[0].default_end_lag_days, 1)
        self.assertEqual(config.reports[0].subreports[0].default_end_lag_days, 1)

        with self.assertRaisesRegex(ConfigError, "default_end_lag_days"):
            self._load(lambda text: text.replace(
                "default_window_days = 30\n[[reports.subreports]]",
                "default_window_days = 7\ndefault_end_lag_days = -1\n[[reports.subreports]]",
                1,
            ))

    def test_report_config_positional_callers_keep_the_existing_field_order(self):
        filters = (("form", "contact"),)
        subreport = SubreportConfig("forms", "Forms", ("forms.submissions",), 30, filters)
        report = ReportConfig(
            "summary", "Summary", "example-client", ("example-site",),
            ("forms.submissions",), 30, (subreport,),
        )

        self.assertEqual(subreport.filters, filters)
        self.assertEqual(subreport.default_end_lag_days, 0)
        self.assertEqual(report.subreports, (subreport,))
        self.assertEqual(report.default_end_lag_days, 0)

    def test_inline_secret_is_rejected_anywhere(self):
        with self.assertRaisesRegex(ConfigError, "inline secret field"):
            self._load(lambda text: text.replace('path = "', 'api_key = "bad"\npath = "'))

    def test_unknown_field_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "unknown field"):
            self._load(lambda text: text.replace("default_sync_days = 30", "default_sync_days = 30\ntyop = true"))

    def test_unknown_client_reference_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "unknown client"):
            self._load(lambda text: text.replace('client_id = "example-client"', 'client_id = "missing"', 1))

    def test_unauthenticated_non_loopback_server_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "loopback"):
            self._load(lambda text: text.replace('bind_host = "127.0.0.1"', 'bind_host = "0.0.0.0"'))

    def test_basic_auth_requires_credential_reference(self):
        with self.assertRaisesRegex(ConfigError, "requires username"):
            self._load(lambda text: text.replace('auth_mode = "none"', 'auth_mode = "basic"'))

    def test_unknown_report_and_subreport_metrics_are_rejected_during_validation(self):
        with self.assertRaisesRegex(ConfigError, "unknown metric ids: made.up"):
            self._load(lambda text: text.replace('"umami.pageviews"', '"made.up"', 1))
        with self.assertRaisesRegex(ConfigError, "unknown metric ids: made.up"):
            self._load(lambda text: text.replace(
                'metric_ids = ["forms.submissions", "forms.inbox-deliveries"]',
                'metric_ids = ["made.up"]',
                1,
            ))
        with self.assertRaisesRegex(ConfigError, "acquisition-only metric ids"):
            self._load(lambda text: text.replace(
                '"umami.pageviews"', '"search.route-clicks"', 1
            ))

    def test_binding_observation_start_is_strict_and_uses_site_timezone(self):
        config = self._load(lambda text: text.replace(
            'timezone = "UTC"',
            'timezone = "America/Chicago"',
        ).replace(
            'metric_groups = ["traffic"]',
            'metric_groups = ["traffic"]\n[bindings.options]\n'
            'observation_start = "2026-07-12"',
        ))

        self.assertEqual(
            binding_observation_start(config.bindings[0]).isoformat(),
            "2026-07-12",
        )
        self.assertEqual(
            binding_observation_boundary(
                config, config.bindings[0]
            ).isoformat(),
            "2026-07-12T00:00:00-05:00",
        )

    def test_binding_observation_start_rejects_non_iso_values_at_load_time(self):
        for raw in ('"07/12/2026"', '"20260712"', "2026-07-12"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(
                    ConfigError,
                    r"bindings\[0\]\.options\.observation_start must use YYYY-MM-DD",
                ):
                    self._load(lambda text, raw=raw: text.replace(
                        'metric_groups = ["traffic"]',
                        'metric_groups = ["traffic"]\n[bindings.options]\n'
                        f'observation_start = {raw}',
                    ))

    def test_route_analytics_is_disabled_by_default_and_rejects_unbounded_options(self):
        config = self._load()
        self.assertFalse(route_analytics_options(config.bindings[0]).enabled)

        with self.assertRaisesRegex(ConfigError, "route_analytics.max_days"):
            self._load(lambda text: text.replace(
                'metric_groups = ["traffic"]',
                'metric_groups = ["traffic"]\n[bindings.options.route_analytics]\n'
                'enabled = true\nmax_days = 365',
            ))
        with self.assertRaisesRegex(ConfigError, "disallowed key"):
            self._load(lambda text: text.replace(
                'metric_groups = ["traffic"]',
                'metric_groups = ["traffic"]\n[bindings.options.route_analytics]\n'
                'allowed_query_parameters = ["email"]',
            ))
        configured = self._load(lambda text: text.replace(
            'metric_groups = ["traffic"]',
            'metric_groups = ["traffic"]\n[bindings.options.route_analytics]\n'
            'umami_dimensions = ["title", "channel", "domain", "device", "country"]',
        ))
        self.assertEqual(
            route_analytics_options(configured.bindings[0]).umami_dimensions,
            ("channel", "country", "device", "domain", "title"),
        )
        configured = self._load(lambda text: text.replace(
            'metric_groups = ["traffic"]',
            'metric_groups = ["traffic"]\n[bindings.options.route_analytics]\n'
            'ga4_dimensions = ["title", "channel", "referrer"]',
        ))
        self.assertEqual(
            route_analytics_options(configured.bindings[0]).ga4_dimensions,
            ("channel", "referrer", "title"),
        )

    def test_search_console_detail_capture_is_explicit_and_bounded(self):
        configured = self._load(lambda text: text.replace(
            'metric_groups = ["traffic"]',
            'metric_groups = ["traffic"]\n[bindings.options.route_analytics]\n'
            'enabled = true\nsearch_console_query_text = true\n'
            'search_console_page_query = true\nsearch_console_hourly = true',
        ))
        options = route_analytics_options(configured.bindings[0])
        self.assertTrue(options.search_console_query_text)
        self.assertTrue(options.search_console_page_query)
        self.assertTrue(options.search_console_hourly)

        with self.assertRaisesRegex(ConfigError, "requires search_console_query_text"):
            self._load(lambda text: text.replace(
                'metric_groups = ["traffic"]',
                'metric_groups = ["traffic"]\n[bindings.options.route_analytics]\n'
                'search_console_page_query = true',
            ))
        with self.assertRaisesRegex(ConfigError, "must be a boolean"):
            self._load(lambda text: text.replace(
                'metric_groups = ["traffic"]',
                'metric_groups = ["traffic"]\n[bindings.options.route_analytics]\n'
                'search_console_hourly = "yes"',
            ))

        all_surfaces = self._load(lambda text: text.replace(
            'metric_groups = ["traffic"]',
            'metric_groups = ["traffic"]\n[bindings.options.route_analytics]\n'
            'search_types = ["all"]\nsearch_console_dimensions = ["all"]',
        ))
        all_options = route_analytics_options(all_surfaces.bindings[0])
        self.assertEqual(all_options.search_types, (
            "web", "image", "video", "news", "discover", "googleNews",
        ))
        self.assertEqual(
            all_options.search_console_dimensions,
            ("country", "device", "searchAppearance"),
        )

        with self.assertRaisesRegex(ConfigError, "search_type or search_types"):
            self._load(lambda text: text.replace(
                'metric_groups = ["traffic"]',
                'metric_groups = ["traffic"]\n[bindings.options.route_analytics]\n'
                'search_type = "web"\nsearch_types = ["image"]',
            ))

    def test_umami_all_expands_only_to_privacy_safe_aggregates(self):
        configured = self._load(lambda text: text.replace(
            'metric_groups = ["traffic"]',
            'metric_groups = ["traffic"]\n[bindings.options.route_analytics]\n'
            'enabled = true\numami_dimensions = ["all"]',
        ))
        options = route_analytics_options(configured.bindings[0])
        self.assertEqual(
            options.umami_dimensions,
            (
                "browser", "channel", "country", "device", "domain", "event",
                "hostname", "language", "os", "referrer", "region", "screen",
                "tag", "title",
            ),
        )
        self.assertNotIn("city", options.umami_dimensions)
        self.assertNotIn("distinctId", options.umami_dimensions)

        with self.assertRaisesRegex(ConfigError, "unsupported values"):
            self._load(lambda text: text.replace(
                'metric_groups = ["traffic"]',
                'metric_groups = ["traffic"]\n[bindings.options.route_analytics]\n'
                'umami_dimensions = ["all", "device"]',
            ))


if __name__ == "__main__": unittest.main()
