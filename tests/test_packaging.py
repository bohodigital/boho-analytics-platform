from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

from boho_analytics_platform import __version__


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_project_metadata_matches_public_package(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]

        self.assertEqual(project["name"], "boho-analytics-platform")
        self.assertEqual(project["version"], __version__)
        self.assertEqual(project["scripts"]["boho-analytics"], "boho_analytics_platform.cli:main")
        self.assertEqual(project["license"], "MIT")
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(project["urls"]["Repository"], "https://github.com/bohodigital/boho-analytics-platform")

    def test_pypi_workflow_is_manual_pinned_and_oidc_only(self):
        workflow = (ROOT / ".github" / "workflows" / "pypi.yml").read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotRegex(workflow, r"(?m)^\s+(push|release):\s*$")
        self.assertIn("environment:\n      name: pypi", workflow)
        self.assertEqual(workflow.count("id-token: write"), 1)
        self.assertNotRegex(workflow, r"(?i)(PYPI_API_TOKEN|secrets\..*pypi|password:)")
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("ref: refs/tags/${{ inputs.tag }}", workflow)
        self.assertIn('pip install --disable-pip-version-check . "build>=1.2,<2"', workflow)
        self.assertNotIn("--no-deps", workflow)

        uses = re.findall(r"(?m)^\s*uses:\s+([^\s#]+)", workflow)
        self.assertGreaterEqual(len(uses), 5)
        for action in uses:
            _, separator, revision = action.rpartition("@")
            self.assertTrue(separator, action)
            self.assertRegex(revision, r"^[0-9a-f]{40}$", action)

    def test_ci_and_release_workflows_execute_the_tracked_test_suite(self):
        quality = (ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
        publish = (ROOT / ".github" / "workflows" / "pypi.yml").read_text(encoding="utf-8")
        for workflow in (quality, publish):
            self.assertIn("python -m unittest discover -s tests -v", workflow)
            self.assertIn("python scripts/verify_release.py", workflow)
            self.assertNotIn("Internal validation material is not allowed", workflow)


if __name__ == "__main__":
    unittest.main()
