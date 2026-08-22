from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from boho_analytics_platform.config import load_config
from boho_analytics_platform.connectors.common import total_point
from boho_analytics_platform.models import Completeness
from boho_analytics_platform.page_intelligence import (
    PageIntelligenceService,
    SchemeValidationError,
    validate_scheme_definition,
)
from boho_analytics_platform.storage import SQLiteMetricStore
from support import config_text, write_fixture


class PageIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        fixture = root / "fixture.json"
        write_fixture(fixture)
        config_path = root / "platform.toml"
        config_path.write_text(config_text(root / "state.db", fixture), encoding="utf-8")
        self.config = load_config(config_path)
        self.store = SQLiteMetricStore(root / "state.db")
        self.store.initialize()
        self.instant = datetime(2026, 8, 21, 12, tzinfo=UTC)
        self.service = PageIntelligenceService(
            self.config, self.store, now=lambda: self.instant
        )

    def point(self, source, metric, value, route, **dimensions):
        all_dimensions = {"route": route, **dimensions}
        return total_point(
            client_id="example-client",
            site_id="example-site",
            source=source,
            metric=metric,
            unit=("position" if metric.endswith("position") else "count"),
            start=datetime(2026, 8, 10, tzinfo=UTC),
            end=datetime(2026, 8, 11, tzinfo=UTC),
            value=value,
            dimensions=all_dimensions,
            observed_at=self.instant,
            completeness=(
                Completeness.UNKNOWN if source == "search-console"
                else Completeness.FINAL
            ),
        )

    def seed(self):
        gsc = {
            "observation_scope": "page",
            "provider_date": "2026-08-10",
            "provider_timezone": "America/Los_Angeles",
            "search_type": "web",
            "data_state": "final",
        }
        points = [
            self.point("search-console", "search.route-clicks", 10, "/news/a", **gsc),
            self.point("search-console", "search.route-impressions", 100, "/news/a", **gsc),
            self.point("search-console", "search.route-position", 2.5, "/news/a", **gsc),
            self.point("search-console", "search.route-clicks", 2, "/news/b", **gsc),
            self.point("search-console", "search.route-impressions", 100, "/news/b", **gsc),
            self.point("search-console", "search.route-position", 5, "/news/b", **gsc),
            self.point(
                "search-console", "search.route-impressions", 999, "/news/a",
                **{**gsc, "observation_scope": "page-device", "device": "mobile"},
            ),
            self.point("umami", "umami.route-pageviews", 20, "/news/a"),
            self.point("umami", "umami.route-visits", 10, "/news/a"),
            self.point("google-analytics", "google.page-path-views", 15, "/news/a"),
            self.point("google-analytics", "google.landing-page-sessions", 8, "/news/a"),
        ]
        self.store.upsert(points)
        return self.service.materialize()

    def seed_index(self):
        urls = ("https://example.com/news/a", "https://example.com/news/b")
        hashes = tuple(hashlib.sha256(url.encode()).hexdigest() for url in urls)
        inventory_hash = hashlib.sha256("\n".join(sorted(hashes)).encode()).hexdigest()
        self.store.begin_index_coverage_inventory(
            "example-site", inventory_hash, hashes, self.instant
        )
        self.service.record_sitemap_inventory(
            "example-site", urls, inventory_hash, self.instant
        )
        self.store.record_index_coverage_inspection(
            "example-site", inventory_hash, hashes[0], "PASS", self.instant
        )
        self.store.record_index_coverage_inspection(
            "example-site", inventory_hash, hashes[1], "FAIL", self.instant
        )

    def test_materializer_uses_only_page_scope_and_recomputes_weighted_metrics(self):
        materialized = self.seed()
        self.assertEqual(materialized["source_facts"], 10)
        self.assertEqual(materialized["pages_seen"], 2)

        properties = self.service.properties("2026-08-10", "2026-08-11")
        item = properties["data"]["properties"][0]
        self.assertEqual(item["gsc_clicks"], 12)
        self.assertEqual(item["gsc_impressions"], 200)
        self.assertEqual(item["gsc_ctr"], 0.06)
        self.assertEqual(item["gsc_position"], 3.75)
        self.assertEqual(item["umami_pageviews"], 20)
        self.assertEqual(item["umami_visits"], 10)
        self.assertEqual(item["ga4_pageviews"], 15)
        self.assertEqual(item["ga4_sessions"], 8)
        self.assertNotIn("visits", properties["metric_definitions"]["ga4_sessions"])
        self.assertIn("not interchangeable", " ".join(properties["completeness_notes"]))

    def test_catalog_index_links_retain_routes_and_hashes_but_no_public_urls(self):
        self.seed()
        self.seed_index()
        properties = self.service.properties("2026-08-10", "2026-08-11")
        item = properties["data"]["properties"][0]
        self.assertEqual(item["published_pages"], 2)
        self.assertEqual(item["indexed_pages"], 1)
        self.assertEqual(item["indexed_percentage"], 0.5)

        pages = self.service.pages("2026-08-10", "2026-08-11")
        indexed = {item["route"]: item["indexed"] for item in pages["data"]["pages"]}
        self.assertEqual(indexed, {"/news/a": True, "/news/b": False})
        with self.store.connect(readonly=True) as db:
            stored = "\n".join(
                str(tuple(row))
                for table in (
                    "page_catalog", "page_catalog_sources", "page_catalog_index_links"
                )
                for row in db.execute(f"SELECT * FROM {table}").fetchall()
            )
        self.assertNotIn("example.com", stored)
        self.assertNotIn("https://", stored)

    def test_exclusive_cluster_totals_reconcile_to_page_controls(self):
        self.seed()
        clusters = self.service.clusters("2026-08-10", "2026-08-11")
        self.assertEqual(clusters["data"]["scheme_mode"], "exclusive")
        self.assertTrue(clusters["data"]["shares_available"])
        self.assertEqual(sum(
            item["gsc_impressions"] for item in clusters["data"]["clusters"]
        ), 200)
        self.assertEqual(sum(
            item["gsc_clicks"] for item in clusters["data"]["clusters"]
        ), 12)
        self.assertEqual(sum(
            item["impression_share"] for item in clusters["data"]["clusters"]
        ), 1.0)

    def test_partial_index_census_discloses_progress_and_withholds_percentage(self):
        self.seed()
        urls = ("https://example.com/news/a", "https://example.com/news/b")
        hashes = tuple(hashlib.sha256(url.encode()).hexdigest() for url in urls)
        inventory_hash = hashlib.sha256("\n".join(sorted(hashes)).encode()).hexdigest()
        self.store.begin_index_coverage_inventory(
            "example-site", inventory_hash, hashes, self.instant
        )
        self.service.record_sitemap_inventory(
            "example-site", urls, inventory_hash, self.instant
        )
        self.store.record_index_coverage_inspection(
            "example-site", inventory_hash, hashes[0], "PASS", self.instant
        )
        item = self.service.properties(
            "2026-08-10", "2026-08-11"
        )["data"]["properties"][0]
        self.assertEqual(item["published_pages"], 2)
        self.assertEqual(item["inspected_pages"], 1)
        self.assertEqual(item["indexed_observed_pages"], 1)
        self.assertIsNone(item["indexed_pages"])
        self.assertIsNone(item["indexed_percentage"])
        self.assertEqual(item["index_coverage_status"], "partial")

    def test_scheme_versions_are_declarative_immutable_and_rollback_capable(self):
        self.seed()
        definition = validate_scheme_definition({
            "schema_version": 1,
            "scheme_id": "editorial-map",
            "name": "Editorial map",
            "mode": "exclusive",
            "site_ids": ["example-site"],
            "fallback": {"cluster_id": "other", "label": "Other"},
            "rules": [{
                "cluster_id": "news",
                "label": "News",
                "priority": 10,
                "match": {"path_prefixes": ["/news/"]},
            }],
        })
        first = self.service.apply_scheme(definition, reason="test apply")
        self.assertEqual(first["assignments"], 2)
        changed = {**definition, "name": "Editorial map two"}
        second = self.service.apply_scheme(changed, reason="test change")
        self.assertEqual(second["version_number"], 2)
        rollback = self.service.activate_scheme_version(
            "editorial-map", 1, reason="test rollback"
        )
        self.assertEqual(rollback["version_number"], 1)
        with self.store.connect() as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                db.execute(
                    "UPDATE page_scheme_versions SET definition_json='{}' WHERE version_id=?",
                    (first["version_id"],),
                )

    def test_scheme_rejects_executable_and_unbounded_match_languages(self):
        base = {
            "schema_version": 1,
            "scheme_id": "unsafe-map",
            "name": "Unsafe map",
            "mode": "exclusive",
            "site_ids": [],
            "fallback": {"cluster_id": "other", "label": "Other"},
            "rules": [{
                "cluster_id": "bad",
                "label": "Bad",
                "priority": 1,
                "match": {"regex": ".*"},
            }],
        }
        with self.assertRaises(SchemeValidationError):
            validate_scheme_definition(base)
        with self.assertRaises(SchemeValidationError):
            validate_scheme_definition({**base, "python": "import os"})

    def test_evidence_pagination_is_bounded_and_opportunities_are_noncausal(self):
        self.seed()
        first = self.service.pages(
            "2026-08-10", "2026-08-11", limit=1, sort="route"
        )
        self.assertIsNotNone(first["pagination"]["next_cursor"])
        second = self.service.pages(
            "2026-08-10", "2026-08-11", limit=1, sort="route",
            cursor=first["pagination"]["next_cursor"],
        )
        self.assertNotEqual(
            first["data"]["pages"][0]["route"], second["data"]["pages"][0]["route"]
        )
        opportunities = self.service.opportunities(
            "2026-08-10", "2026-08-11", minimum_impressions=50,
            maximum_ctr=0.03,
        )
        self.assertEqual(
            [item["route"] for item in opportunities["data"]["opportunities"]],
            ["/news/b"],
        )
        self.assertIn("does not", opportunities["data"]["interpretation"])

    def test_application_integrity_detects_catalog_identity_tampering(self):
        self.seed()
        self.assertEqual(
            self.store.verify_page_intelligence_integrity()["pages"], 2
        )
        with self.store.connect() as db:
            db.execute(
                "UPDATE page_catalog SET route='/tampered' WHERE route='/news/a'"
            )
        self.assertEqual(self.store.integrity_check(), "application-integrity-error")


if __name__ == "__main__":
    unittest.main()
