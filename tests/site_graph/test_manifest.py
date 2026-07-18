from __future__ import annotations

import unittest

from boho_analytics_platform.site_graph.manifest import ManifestError, load_manifest_text
from tests.site_graph.support import VALID_MANIFEST


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_has_deterministic_hash_and_sanitized_summary(self):
        first = load_manifest_text(VALID_MANIFEST)
        reordered = load_manifest_text(VALID_MANIFEST.replace(
            "  key: fixture-static\n  display_name: Fixture Static Site\n",
            "  display_name: Fixture Static Site\n  key: fixture-static\n",
        ))

        self.assertEqual(first.manifest_hash, reordered.manifest_hash)
        self.assertEqual(first.schema_version, 1)
        self.assertEqual(first.site.key, "fixture-static")
        summary = first.sanitized_summary()
        self.assertEqual(summary["site_key"], "fixture-static")
        self.assertEqual(summary["analysis_mode"], "source-only")
        self.assertEqual(summary["page_rule_ids"], ["homepage", "service"])
        self.assertEqual(summary["goal_ids"], ["contact-page", "service-role"])
        self.assertNotIn("local_path", str(summary))
        self.assertNotIn("expected_remote", str(summary))
        self.assertNotIn("fixture-account", str(summary))

    def test_unknown_fields_and_duplicate_yaml_keys_are_rejected(self):
        with self.assertRaisesRegex(ManifestError, "unknown field"):
            load_manifest_text(VALID_MANIFEST + "unexpected: true\n")
        duplicate = VALID_MANIFEST.replace("schema_version: 1", "schema_version: 1\nschema_version: 1")
        with self.assertRaisesRegex(ManifestError, "duplicate key"):
            load_manifest_text(duplicate)

    def test_unsafe_paths_invalid_regex_and_aliases_are_rejected(self):
        relative = VALID_MANIFEST.replace("/srv/example/fixture-static", "../fixture-static")
        with self.assertRaisesRegex(ManifestError, "absolute"):
            load_manifest_text(relative)
        invalid_regex = VALID_MANIFEST.replace("^/services/", "[")
        with self.assertRaisesRegex(ManifestError, "regular expression"):
            load_manifest_text(invalid_regex)
        unsafe_regex = VALID_MANIFEST.replace("^/services/", "^(a+)+$")
        with self.assertRaisesRegex(ManifestError, "unsafe regular expression"):
            load_manifest_text(unsafe_regex)
        alias = VALID_MANIFEST.replace("schema_version: 1", "schema_version: &version 1").replace(
            "maximum_pages: 500", "maximum_pages: *version"
        )
        with self.assertRaisesRegex(ManifestError, "aliases"):
            load_manifest_text(alias)

    def test_build_boundary_rejects_arbitrary_commands_and_inconsistent_mode(self):
        command = VALID_MANIFEST.replace("adapter_command: null", "adapter_command: npm run build")
        with self.assertRaisesRegex(ManifestError, "adapter-owned"):
            load_manifest_text(command)
        inconsistent = VALID_MANIFEST.replace("enabled: false", "enabled: true", 1).replace(
            "output_directory: null", "output_directory: dist"
        )
        with self.assertRaisesRegex(ManifestError, "analysis.mode"):
            load_manifest_text(inconsistent)

        vinext = VALID_MANIFEST.replace("adapter: static-html", "adapter: vinext")
        self.assertEqual(load_manifest_text(vinext).analysis.adapter, "vinext")

    def test_duplicate_ids_and_unresolvable_goals_are_rejected(self):
        duplicate = VALID_MANIFEST.replace("  - id: service\n", "  - id: homepage\n")
        with self.assertRaisesRegex(ManifestError, "duplicate page rule"):
            load_manifest_text(duplicate)
        missing_role = VALID_MANIFEST.replace("roles: [service]\n", "roles: [missing]\n", 1)
        with self.assertRaisesRegex(ManifestError, "unknown role"):
            load_manifest_text(missing_role)
        excluded_goal = VALID_MANIFEST.replace("paths: [/contact/]", "paths: [/admin/private/]")
        with self.assertRaisesRegex(ManifestError, "excluded route"):
            load_manifest_text(excluded_goal)

        empty_page_goal = VALID_MANIFEST.replace("paths: [/contact/]", "paths: []")
        with self.assertRaisesRegex(ManifestError, "at least one path"):
            load_manifest_text(empty_page_goal)

    def test_secret_fields_and_credentialed_remotes_are_rejected(self):
        secret = VALID_MANIFEST.replace("project_name: fixture-static", "project_name: fixture-static\n  api_token: unsafe")
        with self.assertRaisesRegex(ManifestError, "secret field"):
            load_manifest_text(secret)
        remote = VALID_MANIFEST.replace(
            "https://github.com/example/fixture-static.git",
            "https://user:password@github.com/example/fixture-static.git",
        )
        with self.assertRaisesRegex(ManifestError, "credentials"):
            load_manifest_text(remote)

        ssh_remote = VALID_MANIFEST.replace(
            "https://github.com/example/fixture-static.git",
            "ssh://git@github.com/example/fixture-static.git",
        )
        self.assertEqual(
            load_manifest_text(ssh_remote).repository.expected_remote,
            "ssh://git@github.com/example/fixture-static.git",
        )


if __name__ == "__main__":
    unittest.main()
