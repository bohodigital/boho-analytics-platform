from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from boho_analytics_platform.site_graph.ingest import IngestError, ingest_repository, inspect_repository
from boho_analytics_platform.cli import main
from boho_analytics_platform.site_graph.manifest import load_manifest_text
from boho_analytics_platform.site_graph.storage import SiteGraphStore
from tests.site_graph.support import VALID_MANIFEST


class RepositoryIngestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "site"
        self.repo.mkdir()
        (self.repo / "index.html").write_text(
            """<!doctype html><html><body>
            <header><nav><a href="/contact/">Contact</a></nav></header>
            <main><a data-link-layer="action" href="/contact/">Talk to us</a></main>
            <footer><a href="/privacy/">Privacy</a></footer>
            </body></html>""",
            encoding="utf-8",
        )
        (self.repo / "contact").mkdir()
        (self.repo / "contact" / "index.html").write_text(
            '<main><a href="/">Home</a></main>', encoding="utf-8"
        )
        (self.repo / "privacy").mkdir()
        (self.repo / "privacy" / "index.html").write_text(
            '<main><a href="/">Home</a></main>', encoding="utf-8"
        )
        self._git("init", "-b", "main")
        self._git("config", "user.name", "Fixture")
        self._git("config", "user.email", "fixture@example.invalid")
        self._git("config", "core.autocrlf", "false")
        self._git("remote", "add", "origin", "https://github.com/example/fixture-static.git")
        self._git("add", ".")
        self._git("commit", "-m", "fixture")
        self.revision = self._git("rev-parse", "HEAD").strip()

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.repo, text=True, capture_output=True, check=True
        )
        return result.stdout

    def manifest(self, *, adapter: str = "static-html"):
        text = VALID_MANIFEST.replace(
            "/srv/example/fixture-static", self.repo.as_posix()
        ).replace(
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", self.revision
        ).replace("adapter: static-html", f"adapter: {adapter}")
        return load_manifest_text(text)

    def _commit_vinext(self, files: dict[str, str]) -> None:
        (self.repo / "package.json").write_text(
            json.dumps({"dependencies": {"vinext": "0.0.50"}}), encoding="utf-8"
        )
        for relative, text in files.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "vinext route fixture")
        self.revision = self._git("rev-parse", "HEAD").strip()
        self.assertEqual(self._git("status", "--porcelain=v1", "--untracked-files=all"), "")

    def _ingest_vinext(self):
        store = SiteGraphStore(self.root / "vinext-routes.sqlite3")
        store.initialize()
        self.assertEqual(self._git("status", "--porcelain=v1", "--untracked-files=all"), "")
        result = ingest_repository(store, self.manifest(adapter="auto"))
        return store, result

    @staticmethod
    def _routes(store: SiteGraphStore, repository_snapshot_id: str) -> set[str]:
        with store.connect(readonly=True) as db:
            return {
                row["route"]
                for row in db.execute(
                    "SELECT route FROM site_graph_page_facts WHERE repository_snapshot_id=?",
                    (repository_snapshot_id,),
                )
            }

    def test_inspection_records_exact_clean_provenance_without_changing_source(self):
        before = self._git("status", "--porcelain=v1")

        inspected = inspect_repository(self.manifest())

        self.assertEqual(inspected.revision, self.revision)
        self.assertEqual(inspected.adapter, "static-html")
        self.assertTrue(inspected.clean)
        self.assertEqual(inspected.repository_identity, "github.com/example/fixture-static")
        self.assertEqual(self._git("status", "--porcelain=v1"), before)

    def test_dirty_repository_is_refused_by_default(self):
        (self.repo / "index.html").write_text("dirty", encoding="utf-8")

        with self.assertRaisesRegex(IngestError, "repository is dirty"):
            inspect_repository(self.manifest())

    def test_repository_fsmonitor_configuration_is_not_executed(self):
        sentinel = self.root / "fsmonitor-ran"
        hook = self.root / "untrusted-fsmonitor.sh"
        hook.write_text(f"#!/bin/sh\nprintf unsafe > '{sentinel.as_posix()}'\n", encoding="utf-8")
        hook.chmod(0o700)
        self._git("config", "core.fsmonitor", hook.as_posix())

        inspected = inspect_repository(self.manifest())

        self.assertTrue(inspected.clean)
        self.assertFalse(sentinel.exists())

    def test_static_html_ingest_preserves_duplicate_occurrences_and_is_idempotent(self):
        database = self.root / "analytics.sqlite3"
        store = SiteGraphStore(database)
        store.initialize()
        self.assertEqual(self._git("status", "--porcelain=v1", "--untracked-files=all"), "")

        first = ingest_repository(store, self.manifest())
        second = ingest_repository(store, self.manifest())

        self.assertEqual(first.pages, 3)
        self.assertEqual(first.links, 5)
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(first.fact_hash, second.fact_hash)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM site_graph_ingest_runs").fetchone()[0], 1)
            rows = db.execute(
                "SELECT layer,canonical_destination FROM site_graph_link_occurrences "
                "WHERE canonical_destination='/contact/' ORDER BY layer"
            ).fetchall()
        self.assertEqual(rows, [("action", "/contact/"), ("menu", "/contact/")])

    def test_auto_detects_vinext_from_package_metadata(self):
        (self.repo / "package.json").write_text(
            json.dumps({"dependencies": {"vinext": "0.0.50"}, "scripts": {"build": "vinext build"}}),
            encoding="utf-8",
        )
        self._git("add", "package.json")
        self._git("commit", "-m", "vinext")
        self.revision = self._git("rev-parse", "HEAD").strip()

        inspected = inspect_repository(self.manifest(adapter="auto"))

        self.assertEqual(inspected.adapter, "vinext")

    def test_vinext_excludes_routes_declared_in_public_retirement_policy(self):
        (self.repo / "package.json").write_text(
            json.dumps({"dependencies": {"vinext": "0.0.50"}}), encoding="utf-8"
        )
        content = self.repo / "app" / "content"
        content.mkdir(parents=True)
        (content / "publicPages.ts").write_text(
            'const retiredPublicSlugs = new Set(["/retired/"]);', encoding="utf-8"
        )
        (content / "corePages.ts").write_text(
            'export const pages = [{ slug: "/active/", primaryCta: { href: "/contact/" } },'
            '{ slug: "/retired/", primaryCta: { href: "/contact/" } }];',
            encoding="utf-8",
        )
        self._git("add", ".")
        self._git("commit", "-m", "vinext routes")
        self.revision = self._git("rev-parse", "HEAD").strip()
        store = SiteGraphStore(self.root / "vinext.sqlite3")
        store.initialize()

        result = ingest_repository(store, self.manifest(adapter="auto"))

        self.assertEqual(result.coverage["retired_source_routes"], 1)
        with store.connect(readonly=True) as db:
            routes = {
                row[0] for row in db.execute(
                    "SELECT route FROM site_graph_page_facts WHERE repository_snapshot_id=?",
                    (result.repository_snapshot_id,),
                )
            }
        self.assertIn("/active/", routes)
        self.assertNotIn("/retired/", routes)

    def test_vinext_does_not_invent_home_without_an_app_page(self):
        self._commit_vinext({
            "app/content/pages.ts": 'export const pages = [{ slug: "/services/" }];\n',
        })

        store, result = self._ingest_vinext()

        self.assertEqual(self._routes(store, result.repository_snapshot_id), {"/services/"})

    def test_vinext_keeps_unresolved_internal_targets_as_links_not_pages(self):
        self._commit_vinext({
            "app/page.tsx": (
                'export default function Home() { return <main><a href="/missing/">Missing</a></main>; }\n'
            ),
        })

        store, result = self._ingest_vinext()

        self.assertEqual(self._routes(store, result.repository_snapshot_id), {"/"})
        with store.connect(readonly=True) as db:
            links = {
                (row["source_route"], row["canonical_destination"])
                for row in db.execute(
                    """SELECT p.route AS source_route,l.canonical_destination
                       FROM site_graph_link_occurrences l
                       JOIN site_graph_page_facts p ON p.id=l.source_page_fact_id
                       WHERE l.repository_snapshot_id=?""",
                    (result.repository_snapshot_id,),
                )
            }
        self.assertIn(("/", "/missing/"), links)

    def test_vinext_attributes_each_object_links_to_its_own_slug(self):
        self._commit_vinext({
            "app/content/pages.ts": (
                'export const pages = [{ slug: "/alpha/", primaryCta: { href: "/alpha-contact/" } }, '
                '{ slug: "/beta/", primaryCta: { href: "/beta-contact/" } }];\n'
            ),
        })

        store, result = self._ingest_vinext()

        self.assertEqual(self._routes(store, result.repository_snapshot_id), {"/alpha/", "/beta/"})
        with store.connect(readonly=True) as db:
            links = {
                (row["source_route"], row["canonical_destination"])
                for row in db.execute(
                    """SELECT p.route AS source_route,l.canonical_destination
                       FROM site_graph_link_occurrences l
                       JOIN site_graph_page_facts p ON p.id=l.source_page_fact_id
                       WHERE l.repository_snapshot_id=?""",
                    (result.repository_snapshot_id,),
                )
            }
        self.assertEqual(
            links,
            {("/alpha/", "/alpha-contact/"), ("/beta/", "/beta-contact/")},
        )

    def test_vinext_rejects_template_and_resource_page_candidates(self):
        self._commit_vinext({
            "app/page.tsx": (
                'export default function Home() { return <a href="/brand/icon.png">Brand</a>; }\n'
            ),
            "app/content/pages.ts": (
                'export const pages = [{ slug: `/${root}/` }, { slug: "/brand/icon.png" }, '
                '{ slug: "/valid/" }];\n'
            ),
        })

        store, result = self._ingest_vinext()

        self.assertEqual(self._routes(store, result.repository_snapshot_id), {"/", "/valid/"})

    def test_real_cli_inspect_and_ingest_emit_only_sanitized_repository_details(self):
        manifest_path = self.root / "site-graph.yaml"
        manifest_text = VALID_MANIFEST.replace(
            "/srv/example/fixture-static", self.repo.as_posix()
        ).replace("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", self.revision)
        manifest_path.write_text(manifest_text, encoding="utf-8")
        database = self.root / "analytics.sqlite3"
        output = StringIO()

        with redirect_stdout(output):
            inspect_status = main(["site-graph", "inspect-repo", "--manifest", str(manifest_path)])
            ingest_status = main([
                "site-graph", "ingest", "--manifest", str(manifest_path), "--database", str(database)
            ])

        self.assertEqual((inspect_status, ingest_status), (0, 0))
        rendered = output.getvalue()
        self.assertIn(self.revision, rendered)
        self.assertIn("github.com/example/fixture-static", rendered)
        self.assertNotIn(str(self.repo), rendered)
        self.assertNotIn("https://github.com/example/fixture-static.git", rendered)


if __name__ == "__main__":
    unittest.main()
