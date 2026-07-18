from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.verify_release import verify_tree


class ReleaseVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, relative: str, content: str = "safe fixture\n") -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_exact_site_graph_directories_are_allowed(self):
        self.write("docs/site-graph/architecture.md")
        self.write("examples/site-graph/static-site.yaml")
        self.write("src/boho_analytics_platform/site_graph/__init__.py")
        self.write("tests/site_graph/test_manifest.py")

        self.assertEqual(verify_tree(self.root), [])

    def test_unexpected_site_graph_sibling_remains_rejected(self):
        self.write("docs/site-graph-private/notes.md")

        failures = verify_tree(self.root)

        self.assertIn(f"unexpected directory: {Path('docs') / 'site-graph-private'}", failures)

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


if __name__ == "__main__":
    unittest.main()
