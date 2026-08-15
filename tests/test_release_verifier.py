from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.verify_release import verify_git_identity, verify_tree


class ReleaseVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, relative: str, content: str = "safe fixture\n") -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def initialize_git_repository(self) -> str:
        self.write("README.md", "reviewed bytes\n")
        self.write("docs/guide.md", "nested reviewed bytes\n")
        self.write("scripts/tool.sh", "#!/bin/sh\nexit 0\n")
        (self.root / "scripts/tool.sh").chmod(0o755)
        subprocess.run(
            ["git", "init", "-q", "--initial-branch=main", os.fspath(self.root)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", os.fspath(self.root), "config", "user.name", "Release Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", os.fspath(self.root), "config", "user.email", "release@example.invalid"],
            check=True,
        )
        subprocess.run(["git", "-C", os.fspath(self.root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", os.fspath(self.root), "commit", "-q", "-m", "reviewed"],
            check=True,
        )
        return subprocess.run(
            ["git", "-C", os.fspath(self.root), "rev-parse", "HEAD^{tree}"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    def test_clean_git_checkout_is_bound_to_head_tree(self):
        expected_tree = self.initialize_git_repository()

        self.assertEqual(verify_git_identity(self.root), [])
        self.assertEqual(verify_git_identity(self.root, expected_tree=expected_tree), [])

    def test_modified_allowed_tracked_file_is_rejected(self):
        self.initialize_git_repository()
        self.write("README.md", "different bytes\n")

        failures = verify_git_identity(self.root)

        self.assertIn("Git checkout differs from reviewed HEAD", failures)
        self.assertTrue(
            any(failure.startswith("public filesystem Git tree mismatch:") for failure in failures)
        )

    def test_deleted_allowed_tracked_file_is_rejected(self):
        self.initialize_git_repository()
        (self.root / "README.md").unlink()

        failures = verify_git_identity(self.root)

        self.assertIn("Git checkout differs from reviewed HEAD", failures)
        self.assertTrue(
            any(failure.startswith("public filesystem Git tree mismatch:") for failure in failures)
        )

    def test_additional_allowlisted_untracked_file_is_rejected(self):
        self.initialize_git_repository()
        self.write("docs/additional.md", "unreviewed bytes\n")

        failures = verify_git_identity(self.root)

        self.assertIn("Git checkout differs from reviewed HEAD", failures)
        self.assertTrue(
            any(failure.startswith("public filesystem Git tree mismatch:") for failure in failures)
        )

    def test_exported_tree_requires_and_matches_explicit_reviewed_tree(self):
        expected_tree = self.initialize_git_repository()
        export_root = Path(self.temporary.name) / "export"
        export_root.mkdir()
        shutil.copy2(self.root / "README.md", export_root / "README.md")
        shutil.copytree(self.root / "docs", export_root / "docs", copy_function=shutil.copy2)
        shutil.copytree(self.root / "scripts", export_root / "scripts", copy_function=shutil.copy2)

        self.assertEqual(
            verify_git_identity(export_root),
            ["exported release tree requires --expected-tree"],
        )
        self.assertEqual(
            verify_git_identity(export_root, expected_tree=expected_tree),
            [],
        )
        self.write("export/README.md", "tampered archive bytes\n")
        self.assertTrue(
            any(
                failure.startswith("public filesystem Git tree mismatch:")
                for failure in verify_git_identity(export_root, expected_tree=expected_tree)
            )
        )

    def test_invalid_or_mismatched_expected_tree_is_rejected(self):
        expected_tree = self.initialize_git_repository()

        self.assertEqual(
            verify_git_identity(self.root, expected_tree="bad"),
            ["expected Git tree must be exactly 40 lowercase hexadecimal characters"],
        )
        failures = verify_git_identity(self.root, expected_tree="0" * 40)
        self.assertIn(
            f"reviewed HEAD Git tree mismatch: expected {'0' * 40}, found {expected_tree}",
            failures,
        )

    def test_exact_site_graph_directories_are_allowed(self):
        self.write("docs/site-graph/architecture.md")
        self.write("examples/site-graph/static-site.yaml")
        self.write("src/boho_analytics_platform/site_graph/__init__.py")
        self.write("tests/site_graph/test_manifest.py")

        self.assertEqual(verify_tree(self.root), [])

    def test_exact_core21_public_surfaces_are_allowed(self):
        self.write("src/boho_analytics_platform/site_graph/adapters/source_semantic.py")
        self.write("tests/site_graph/fixtures/core21/source_semantic/app/page.tsx")
        self.write("tests/site_graph/fixtures/core21/source_semantic/src/navigation.ts")
        self.write("tests/site_graph/fixtures/core21/artifact_evidence/site/index.html")
        self.write("tests/site_graph/fixtures/core21/artifact_evidence/site/sitemap.xml")
        self.write("tests/site_graph/fixtures/core21/artifact_evidence/site/_redirects")
        self.write("tests/site_graph/fixtures/core21/rendered_crawl/replay.json", "{}")
        self.write("tests/site_graph/ground_truth/core21/source_semantic/expected.json", "{}")
        self.write("examples/site-graph/fixtures/core21/public_core21_contract.yaml")
        self.write("examples/site-graph/ground_truth/core21/public_core21_ground_truth.yaml")
        self.write("scripts/capture_site_graph_evidence.py")

        self.assertEqual(verify_tree(self.root), [])

    def test_exact_static_map_directory_and_geojson_are_allowed(self):
        self.write("src/boho_analytics_platform/static/world.geojson", '{"type":"FeatureCollection","features":[]}')
        self.write("src/boho_analytics_platform/static/US_ATLAS_LICENSE.txt", "ISC license")

        self.assertEqual(verify_tree(self.root), [])

    def test_unexpected_static_map_sibling_remains_rejected(self):
        self.write("src/boho_analytics_platform/static-private/world.geojson", "{}")

        failures = verify_tree(self.root)

        self.assertIn(
            f"unexpected directory: {Path('src') / 'boho_analytics_platform' / 'static-private'}",
            failures,
        )

    def test_unexpected_site_graph_sibling_remains_rejected(self):
        self.write("docs/site-graph-private/notes.md")

        failures = verify_tree(self.root)

        self.assertIn(f"unexpected directory: {Path('docs') / 'site-graph-private'}", failures)

    def test_core21_lookalike_sibling_directories_are_rejected(self):
        paths = (
            "src/boho_analytics_platform/site_graph/adapters-private/unsafe.py",
            "tests/site_graph/fixtures-backup/core21/unsafe.json",
            "tests/site_graph/ground_truth-old/core21/unsafe.json",
            "examples/site-graph-private/fixtures/core21/unsafe.yaml",
            "tests/site_graph/fixtures/core21/source_semantic-private/unsafe.json",
            "tests/site_graph/ground_truth/core21/private_export/unsafe.json",
        )
        for relative in paths:
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("safe fixture\n", encoding="utf-8")
                    self.assertTrue(
                        any(failure.startswith("unexpected directory:") for failure in verify_tree(root))
                    )

    def test_arbitrary_file_in_exact_adapter_directory_is_rejected(self):
        self.write("src/boho_analytics_platform/site_graph/adapters/credential_dump.py")

        self.assertIn(
            (
                "unexpected adapter file: "
                f"{Path('src/boho_analytics_platform/site_graph/adapters/credential_dump.py')}"
            ),
            verify_tree(self.root),
        )

    def test_arbitrary_file_in_exact_core21_directory_is_rejected(self):
        self.write("tests/site_graph/fixtures/core21/source_semantic/extra.json")

        self.assertIn(
            (
                "unexpected Core 2.1 fixture file: "
                f"{Path('tests/site_graph/fixtures/core21/source_semantic/extra.json')}"
            ),
            verify_tree(self.root),
        )

    def test_typescript_is_rejected_outside_semantic_fixtures(self):
        self.write("tests/site_graph/fixtures/core21/artifact_evidence/unsafe.ts")

        self.assertIn(
            f"unexpected file type: {Path('tests/site_graph/fixtures/core21/artifact_evidence/unsafe.ts')}",
            verify_tree(self.root),
        )

    def test_html_and_xml_are_rejected_outside_artifact_or_rendered_fixtures(self):
        for name in ("unsafe.html", "unsafe.xml"):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    path = root / "tests/site_graph/fixtures/core21/source_semantic" / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("safe fixture\n", encoding="utf-8")
                    self.assertIn(
                        f"unexpected file type: {path.relative_to(root)}",
                        verify_tree(root),
                    )

    def test_arbitrary_extensionless_file_is_rejected(self):
        self.write("tests/site_graph/fixtures/core21/source_semantic/NOTES")

        self.assertIn(
            f"unexpected file type: {Path('tests/site_graph/fixtures/core21/source_semantic/NOTES')}",
            verify_tree(self.root),
        )

    def test_redirects_file_is_rejected_outside_exact_approved_path(self):
        self.write("tests/site_graph/fixtures/core21/rendered_crawl/_redirects")

        self.assertIn(
            f"unexpected file type: {Path('tests/site_graph/fixtures/core21/rendered_crawl/_redirects')}",
            verify_tree(self.root),
        )

    def test_arbitrary_new_script_is_rejected(self):
        self.write("scripts/capture_anything.py")

        self.assertIn(
            f"unexpected script: {Path('scripts/capture_anything.py')}",
            verify_tree(self.root),
        )

    def test_arbitrary_script_inside_core21_fixture_tree_is_rejected(self):
        self.write("tests/site_graph/fixtures/core21/source_semantic/run-me.sh")

        self.assertIn(
            (
                "unexpected Core 2.1 fixture file: "
                f"{Path('tests/site_graph/fixtures/core21/source_semantic/run-me.sh')}"
            ),
            verify_tree(self.root),
        )

    def test_archives_databases_environment_files_and_bytecode_are_rejected(self):
        paths = (
            "tests/site_graph/fixtures/core21/source_semantic/archive.zip",
            "tests/site_graph/fixtures/core21/source_semantic/private.sqlite3",
            "tests/site_graph/fixtures/core21/source_semantic/.env",
            "tests/site_graph/fixtures/core21/source_semantic/module.pyc",
        )
        for relative in paths:
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"not public source")
                    self.assertIn(f"unexpected file type: {path.relative_to(root)}", verify_tree(root))

    def test_generated_build_output_is_rejected(self):
        for name in ("build", ".next", "out", "node_modules"):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    path = (
                        root
                        / "tests/site_graph/fixtures/core21/rendered_crawl"
                        / name
                        / "output.json"
                    )
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("{}\n", encoding="utf-8")
                    self.assertIn(
                        (
                            "generated/private directory is not allowed: "
                            f"{path.parent.relative_to(root)}"
                        ),
                        verify_tree(root),
                    )

    def test_symlink_path_traversal_is_rejected(self):
        outside = Path(self.temporary.name).parent / f"{self.root.name}-outside.txt"
        outside.write_text("safe fixture\n", encoding="utf-8")
        self.addCleanup(outside.unlink, missing_ok=True)
        link = self.root / "tests/site_graph/fixtures/core21/source_semantic/outside.ts"
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, link)

        self.assertIn(f"symbolic link is not allowed: {link.relative_to(self.root)}", verify_tree(self.root))

    def test_every_secret_scan_class_applies_to_every_new_textual_type(self):
        markers = {
            "private key": "-----BEGIN " + "PRIVATE KEY-----",
            "AWS access key": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
            "Cloudflare token": "cf" + "xx_" + "abcdefghijklmnopqrst",
            "GitHub token": "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz1234567890ABCD",
            "Google client secret": "GOC" + "SPX-" + "abcdefghijklmnopqrst",
            "Slack token": "xox" + "b-" + "abcdefghijklmnopqrst",
            "Windows user path": "C:" + r"\Users\person\secret.txt",
            "POSIX user path": "/Users/" + "person/private/secret.txt",
            "private deployment path": "/srv/" + "local1/private",
            "credentialed URL": "https://operator:" + "password@internal.invalid/repo",
            "password-bearing SSH URL": (
                "ssh://operator:" + "password@internal.invalid/repo"
            ),
            "credentialed remote target": "ssh -i key " + "operator" + "@" + "internal.invalid",
            "internal coordination identifier": "W" + "O-2026-07-18-PRIVATE-001",
        }
        paths = (
            "tests/site_graph/fixtures/core21/source_semantic/src/navigation.ts",
            "tests/site_graph/fixtures/core21/source_semantic/app/page.tsx",
            "tests/site_graph/fixtures/core21/artifact_evidence/site/index.html",
            "tests/site_graph/fixtures/core21/artifact_evidence/site/sitemap.xml",
            "tests/site_graph/fixtures/core21/artifact_evidence/site/_redirects",
        )
        for label, marker in markers.items():
            for relative in paths:
                with self.subTest(label=label, relative=relative):
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        path = root / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(marker + "\n", encoding="utf-8")
                        self.assertIn(
                            f"{label} pattern found: {path.relative_to(root)}",
                            verify_tree(root),
                        )

    def test_binary_content_is_rejected_in_new_textual_types(self):
        for relative in (
            "tests/site_graph/fixtures/core21/source_semantic/src/navigation.ts",
            "tests/site_graph/fixtures/core21/source_semantic/app/page.tsx",
            "tests/site_graph/fixtures/core21/artifact_evidence/site/index.html",
            "tests/site_graph/fixtures/core21/artifact_evidence/site/sitemap.xml",
            "tests/site_graph/fixtures/core21/artifact_evidence/site/_redirects",
        ):
            for payload, label in (
                (b"\xff\xfe\x00", "non-UTF-8 file"),
                (b"abc\x00def", "binary/control content found"),
                (b"abc\x7fdef", "binary/control content found"),
                ("abc\u0080def".encode(), "binary/control content found"),
            ):
                with self.subTest(relative=relative, label=label):
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        path = root / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(payload)
                        self.assertIn(
                            f"{label}: {path.relative_to(root)}",
                            verify_tree(root),
                        )

    def test_lookalike_public_git_host_is_not_exempted(self):
        marker = "ssh://" + "operator" + "@" + "github.com.evil.invalid/repo"
        self.write("tests/site_graph/fixtures/core21/source_semantic/app/page.tsx", marker)

        self.assertIn(
            (
                "credentialed remote target pattern found: "
                f"{Path('tests/site_graph/fixtures/core21/source_semantic/app/page.tsx')}"
            ),
            verify_tree(self.root),
        )

    def test_existing_secret_scan_remains_active_in_allowed_directory(self):
        token_marker = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz1234567890ABCD"
        self.write("docs/site-graph/unsafe.md", token_marker + "\n")

        failures = verify_tree(self.root)

        relative = Path("docs") / "site-graph" / "unsafe.md"
        self.assertIn(f"GitHub token pattern found: {relative}", failures)

    def test_internal_work_order_identifiers_are_rejected(self):
        marker = "W" + "O-2026-07-18-PRIVATE-001"
        self.write("docs/site-graph/unsafe.md", marker + "\n")

        failures = verify_tree(self.root)

        relative = Path("docs") / "site-graph" / "unsafe.md"
        self.assertIn(f"internal coordination identifier pattern found: {relative}", failures)

    def test_credentialed_remote_targets_are_rejected_without_named_hosts(self):
        marker = "ssh -i key " + "operator" + "@" + "internal.invalid"
        self.write("docs/site-graph/unsafe.md", marker + "\n")

        failures = verify_tree(self.root)

        relative = Path("docs") / "site-graph" / "unsafe.md"
        self.assertIn(f"credentialed remote target pattern found: {relative}", failures)


if __name__ == "__main__":
    unittest.main()
