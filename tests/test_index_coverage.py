from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from boho_analytics_platform.config import load_config
from boho_analytics_platform.credentials import MemoryCredentialLease
from boho_analytics_platform.index_coverage import (
    IndexCoverageEngine,
    SitemapInventoryClient,
)
from boho_analytics_platform.storage import SQLiteMetricStore
from support import config_text, write_fixture


class _Credentials:
    def acquire(self, _reference):
        return MemoryCredentialLease({"access_token": b"test-token"})


class _Sitemaps:
    def fetch(self, _canonical_url):
        return ("https://example.com/a/", "https://example.com/b/")


class _InspectionHttp:
    def __init__(self):
        self.urls = []

    def request(self, method, url, *, headers=None, body=None):
        self.urls.append(body["inspectionUrl"])
        verdict = "PASS" if body["inspectionUrl"].endswith("/a/") else "FAIL"
        return {"inspectionResult": {"indexStatusResult": {"verdict": verdict}}}


class _ConcurrentInspectionHttp:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.maximum_active = 0

    def request(self, method, url, *, headers=None, body=None):
        with self.lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        time.sleep(0.02)
        with self.lock:
            self.active -= 1
        return {"inspectionResult": {"indexStatusResult": {"verdict": "PASS"}}}


class _ManySitemaps:
    def fetch(self, _canonical_url):
        return tuple(f"https://example.com/page-{index}/" for index in range(8))


class _MemorySitemapClient(SitemapInventoryClient):
    def __init__(self, documents):
        super().__init__()
        self.documents = documents

    def _read(self, url, host):
        return self.documents[url]


class IndexCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        fixture = root / "fixture.json"
        write_fixture(fixture)
        state = root / "state.db"
        config_path = root / "platform.toml"
        text = config_text(
            state,
            fixture,
            provider="search-console",
            credential_ref="none:test",
            options="",
        )
        config_path.write_text(text, encoding="utf-8")
        self.config = load_config(config_path)
        self.store = SQLiteMetricStore(state)
        self.store.initialize()
        self.now = datetime(2026, 8, 12, 12, tzinfo=UTC)

    def engine(self, http=None):
        return IndexCoverageEngine(
            self.config,
            self.store,
            credential_provider=_Credentials(),
            http=http or _InspectionHttp(),
            sitemaps=_Sitemaps(),
            sleeper=lambda _seconds: None,
            now=lambda: self.now,
        )

    def test_complete_census_publishes_exact_percentage_without_urls_at_rest(self):
        result = self.engine().sync(per_property_limit=2, pause_seconds=0)[0]
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.published_pages, 2)
        self.assertEqual(result.indexed_pages, 1)
        self.assertEqual(result.indexed_percentage, 50.0)
        with self.store.connect(readonly=True) as db:
            columns = {row[1] for row in db.execute(
                "PRAGMA table_info(index_coverage_url_status)"
            )}
            rows = db.execute(
                "SELECT url_hash,verdict,indexed FROM index_coverage_url_status"
            ).fetchall()
        self.assertNotIn("url", columns)
        self.assertEqual(len(rows), 2)
        self.assertNotIn("example.com", repr(rows))

    def test_partial_census_withholds_indexed_total_and_percentage(self):
        result = self.engine().sync(per_property_limit=1, pause_seconds=0)[0]
        self.assertEqual(result.status, "partial")
        self.assertIsNone(result.indexed_pages)
        self.assertIsNone(result.indexed_percentage)
        summary = self.store.query_index_coverage(["example-site"])[0]
        self.assertEqual(summary["inspection_progress"], {"inspected": 1, "total": 2})

    def test_census_uses_bounded_concurrency(self):
        http = _ConcurrentInspectionHttp()
        engine = IndexCoverageEngine(
            self.config,
            self.store,
            credential_provider=_Credentials(),
            http=http,
            sitemaps=_ManySitemaps(),
            sleeper=lambda _seconds: None,
            now=lambda: self.now,
        )
        result = engine.sync(
            per_property_limit=8, pause_seconds=0, workers=4
        )[0]
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.inspected_this_run, 8)
        self.assertGreater(http.maximum_active, 1)
        self.assertLessEqual(http.maximum_active, 4)

    def test_sitemap_index_recurses_and_deduplicates_same_host_urls(self):
        root = "https://example.com/sitemap.xml"
        child = "https://example.com/posts.xml"
        client = _MemorySitemapClient({
            root: b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://example.com/posts.xml</loc></sitemap></sitemapindex>',
            child: b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/a/</loc></url><url><loc>https://example.com/a/#fragment</loc></url><url><loc>https://example.com/b/</loc></url></urlset>',
        })
        self.assertEqual(client.fetch("https://example.com"), (
            "https://example.com/a/", "https://example.com/b/",
        ))

    def test_sitemap_rejects_cross_host_page_membership(self):
        client = _MemorySitemapClient({
            "https://example.com/sitemap.xml": b'<urlset><url><loc>https://other.example/a/</loc></url></urlset>'
        })
        with self.assertRaisesRegex(ValueError, "outside"):
            client.fetch("https://example.com")

    def test_interrupted_run_is_closed_before_a_new_census(self):
        run_id = self.store.start_index_coverage_run(
            "example-site", "example-connection", self.now
        )
        self.assertEqual(self.store.mark_abandoned_index_coverage_runs(self.now), 1)
        with self.store.connect(readonly=True) as db:
            row = db.execute(
                "SELECT status,error_category,finished_at FROM index_coverage_runs WHERE id=?",
                (run_id,),
            ).fetchone()
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["error_category"], "interrupted")
        self.assertEqual(row["finished_at"], self.now.isoformat())


if __name__ == "__main__":
    unittest.main()
