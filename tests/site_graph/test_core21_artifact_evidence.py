from __future__ import annotations

import io
import json
import os
import shutil
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from boho_analytics_platform.site_graph.adapters.artifact_evidence import (
    ADAPTER_VERSION,
    MAX_ARTIFACT_ROOTS,
    ArtifactEvidenceError,
    collect_artifact_evidence,
)
from boho_analytics_platform.site_graph.adapters.deployment_metadata import (
    DeploymentMetadataError,
    load_deployment_metadata,
)


REVISION = "a" * 40
FIXTURES = Path(__file__).parent / "fixtures" / "core21" / "artifact_evidence"
GROUND_TRUTH = (
    Path(__file__).parent / "ground_truth" / "core21" / "artifact_evidence" / "routes.json"
)


class ArtifactEvidenceCore21Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.site = self.root / "site"
        shutil.copytree(FIXTURES / "site", self.site)

    def collect(self, *roots: Path):
        return collect_artifact_evidence(
            roots or (self.site,),
            revision=REVISION,
            canonical_hosts=("fixture.example",),
        )

    def test_fixture_matches_ground_truth_and_is_deterministic(self) -> None:
        expected = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
        first = self.collect()
        second = self.collect()

        self.assertEqual(first.adapter_version, ADAPTER_VERSION)
        self.assertEqual(first.evidence_hash, second.evidence_hash)
        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertEqual([item.route for item in first.routes], expected["routes"])
        self.assertTrue(set(expected["excluded_routes"]).isdisjoint(
            item.route for item in first.routes
        ))
        self.assertEqual(first.revision_state, expected["revision_state"])
        self.assertEqual(first.coverage["local_build_execution"], "disabled")
        self.assertEqual(first.coverage["provider_mutation"], "disabled")
        self.assertIn("unsafe-link-rejected:artifact1/index.html", first.diagnostics)

        home = next(item for item in first.routes if item.route == "/")
        about = next(item for item in first.routes if item.route == "/about/")
        self.assertEqual(home.title, "Fixture Home")
        self.assertEqual(home.h1, "Artifact Fixture")
        self.assertEqual(home.schema_types, ("Organization", "WebPage"))
        self.assertFalse(about.indexable)
        destinations = {item.destination for item in first.links}
        self.assertIn("/about/#team", destinations)
        self.assertFalse(any("campaign" in value for value in destinations))

    def test_zip_and_tar_produce_bounded_content_hashes(self) -> None:
        zip_path = self.root / "site.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            for path in sorted(self.site.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(self.site).as_posix())
        zip_result = self.collect(zip_path)
        self.assertEqual(zip_result.coverage["supported_files"], 5)

        tar_path = self.root / "site.tar"
        with tarfile.open(tar_path, "w") as archive:
            for path in sorted(self.site.rglob("*")):
                archive.add(path, arcname=path.relative_to(self.site).as_posix(), recursive=False)
        tar_result = self.collect(tar_path)
        self.assertEqual(
            [(item.relative_path, item.sha256) for item in zip_result.files],
            [(item.relative_path, item.sha256) for item in tar_result.files],
        )

        tgz_path = self.root / "site.tgz"
        with tarfile.open(tgz_path, "w:gz") as archive:
            for path in sorted(self.site.rglob("*")):
                archive.add(path, arcname=path.relative_to(self.site).as_posix(), recursive=False)
        tgz_result = self.collect(tgz_path)
        self.assertEqual(
            [(item.relative_path, item.sha256) for item in zip_result.files],
            [(item.relative_path, item.sha256) for item in tgz_result.files],
        )

    def test_rejects_traversal_symlinks_archive_links_and_duplicate_paths(self) -> None:
        linked = self.root / "linked"
        linked.mkdir()
        os.symlink(self.site / "index.html", linked / "index.html")
        with self.assertRaisesRegex(ArtifactEvidenceError, "symlink"):
            self.collect(linked)

        traversal = self.root / "traversal.zip"
        with zipfile.ZipFile(traversal, "w") as archive:
            archive.writestr("../escape.html", "<main></main>")
        with self.assertRaisesRegex(ArtifactEvidenceError, "unsafe path"):
            self.collect(traversal)

        symlink_zip = self.root / "link.zip"
        info = zipfile.ZipInfo("linked.html")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(symlink_zip, "w") as archive:
            archive.writestr(info, "target.html")
        with self.assertRaisesRegex(ArtifactEvidenceError, "links"):
            self.collect(symlink_zip)

        duplicate = self.root / "duplicate.zip"
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("index.html", "<main>one</main>")
            with self.assertWarns(UserWarning):
                archive.writestr("index.html", "<main>two</main>")
        with self.assertRaisesRegex(ArtifactEvidenceError, "duplicate"):
            self.collect(duplicate)

    def test_rejects_compression_bombs_oversize_counts_encoding_and_entities(self) -> None:
        bomb = self.root / "bomb.zip"
        with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("index.html", "0" * 100_000)
        with patch(
            "boho_analytics_platform.site_graph.adapters.artifact_evidence.MAX_COMPRESSION_RATIO",
            2,
        ):
            with self.assertRaisesRegex(ArtifactEvidenceError, "compression-ratio"):
                self.collect(bomb)

        oversized = self.root / "oversized"
        oversized.mkdir()
        (oversized / "index.html").write_bytes(b"12345")
        with patch(
            "boho_analytics_platform.site_graph.adapters.artifact_evidence.MAX_ARTIFACT_FILE_BYTES",
            4,
        ):
            with self.assertRaisesRegex(ArtifactEvidenceError, "per-file"):
                self.collect(oversized)

        too_many = self.root / "too-many"
        too_many.mkdir()
        (too_many / "one.html").write_text("", encoding="utf-8")
        (too_many / "two.html").write_text("", encoding="utf-8")
        with patch(
            "boho_analytics_platform.site_graph.adapters.artifact_evidence.MAX_ENTRIES",
            1,
        ):
            with self.assertRaisesRegex(ArtifactEvidenceError, "entry-count"):
                self.collect(too_many)

        invalid = self.root / "invalid"
        invalid.mkdir()
        (invalid / "index.html").write_bytes(b"\xff")
        with self.assertRaisesRegex(ArtifactEvidenceError, "UTF-8"):
            self.collect(invalid)

        control_content = self.root / "control"
        control_content.mkdir()
        (control_content / "index.html").write_bytes(b"<p>bad\x7fcontent</p>")
        with self.assertRaisesRegex(ArtifactEvidenceError, "control content"):
            self.collect(control_content)

        with self.assertRaisesRegex(ArtifactEvidenceError, "artifact roots"):
            collect_artifact_evidence(
                (self.site for _ in range(MAX_ARTIFACT_ROOTS + 1)),
                revision=REVISION,
                canonical_hosts=("fixture.example",),
            )

        missing_host = self.root / "missing-host"
        missing_host.mkdir()
        (missing_host / "index.html").write_text(
            '<a href="https:/missing-host">bad</a><p>done</p>', encoding="utf-8"
        )
        missing_host_result = self.collect(missing_host)
        self.assertEqual(missing_host_result.links, ())
        self.assertIn(
            "unsafe-link-rejected:artifact1/index.html",
            missing_host_result.diagnostics,
        )

        entity = self.root / "entity"
        entity.mkdir()
        (entity / "sitemap.xml").write_text(
            '<!DOCTYPE x [<!ENTITY x "boom">]><urlset><loc>&x;</loc></urlset>',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ArtifactEvidenceError, "entities"):
            self.collect(entity)

    def test_route_manifest_revision_mismatch_and_duplicate_json_fail_closed(self) -> None:
        manifest = self.site / "routes-manifest.json"
        manifest.write_text(
            json.dumps({"revision": "b" * 40, "staticRoutes": ["/different/"]}),
            encoding="utf-8",
        )
        result = self.collect()
        self.assertEqual(result.revision_state, "mismatched")
        self.assertIn("revision-mismatch:artifact1/routes-manifest.json", result.diagnostics)

        manifest.write_text('{"staticRoutes":["/a/"],"staticRoutes":["/b/"]}', encoding="utf-8")
        with self.assertRaisesRegex(ArtifactEvidenceError, "duplicate key"):
            self.collect()

    def test_duplicate_occurrences_and_canonical_conflicts_remain_explicit(self) -> None:
        duplicate = self.root / "duplicate-links"
        duplicate.mkdir()
        (duplicate / "index.html").write_text(
            '<link rel="canonical" href="https://fixture.example/other/">'
            '<a href="/same/">Same</a><a href="/same/">Same</a>',
            encoding="utf-8",
        )
        result = self.collect(duplicate)
        occurrences = [item for item in result.links if item.destination == "/same/"]
        self.assertEqual(len(occurrences), 2)
        self.assertEqual(len({item.source_location for item in occurrences}), 2)
        self.assertEqual(len({item.content_hash for item in occurrences}), 2)
        self.assertIn("canonical-route-conflict:artifact1/index.html", result.diagnostics)
        self.assertEqual(result.routes[0].canonical_url, "https://fixture.example/other/")

    def test_tar_special_file_and_declared_size_attacks_fail_closed(self) -> None:
        archive_path = self.root / "special.tar"
        with tarfile.open(archive_path, "w") as archive:
            info = tarfile.TarInfo("pipe")
            info.type = tarfile.FIFOTYPE
            archive.addfile(info)
        with self.assertRaisesRegex(ArtifactEvidenceError, "special file"):
            self.collect(archive_path)

        hardlink_path = self.root / "hardlink.tar"
        with tarfile.open(hardlink_path, "w") as archive:
            info = tarfile.TarInfo("linked.html")
            info.type = tarfile.LNKTYPE
            info.linkname = "target.html"
            archive.addfile(info)
        with self.assertRaisesRegex(ArtifactEvidenceError, "links"):
            self.collect(hardlink_path)


class DeploymentMetadataCore21Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, value: object, name: str = "deployment.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_normalizes_public_fields_and_records_revision_states(self) -> None:
        matched = load_deployment_metadata(FIXTURES / "deployment.json", expected_revision=REVISION)
        repeated = load_deployment_metadata(FIXTURES / "deployment.json", expected_revision=REVISION)
        self.assertEqual(matched.revision_state, "matched")
        self.assertFalse(matched.provider_mutation)
        self.assertEqual(matched.evidence_hash, repeated.evidence_hash)
        self.assertEqual(
            matched.fields["hostnames"],
            ("fixture.example", "www.fixture.example"),
        )

        mismatch_path = self.write({"commit_sha": "b" * 40, "status": "success"})
        mismatch = load_deployment_metadata(mismatch_path, expected_revision=REVISION)
        self.assertEqual(mismatch.revision_state, "mismatched")

        unchecked_path = self.write({"status": "success"})
        unchecked = load_deployment_metadata(unchecked_path, expected_revision=REVISION)
        self.assertEqual(unchecked.revision_state, "unchecked")

        conflicting_path = self.write(
            {"commit_hash": "a" * 40, "commit_sha": "b" * 40}
        )
        conflicting = load_deployment_metadata(conflicting_path, expected_revision=REVISION)
        self.assertEqual(conflicting.revision_state, "conflicting")

    def test_rejects_secrets_symlinks_bad_urls_encoding_duplicates_and_size(self) -> None:
        secret = self.write({"api_token": "do-not-store"})
        with self.assertRaisesRegex(DeploymentMetadataError, "sensitive"):
            load_deployment_metadata(secret, expected_revision=REVISION)

        credentials = self.write({"deployment_url": "https://user:pass@example.com"})
        with self.assertRaisesRegex(DeploymentMetadataError, "public HTTPS"):
            load_deployment_metadata(credentials, expected_revision=REVISION)

        query_url = self.write(
            {"deployment_url": "https://preview.example/?token=public-looking"}
        )
        with self.assertRaisesRegex(DeploymentMetadataError, "without credentials"):
            load_deployment_metadata(query_url, expected_revision=REVISION)

        duplicate = self.root / "duplicate.json"
        duplicate.write_text('{"status":"ok","status":"bad"}', encoding="utf-8")
        with self.assertRaisesRegex(DeploymentMetadataError, "duplicate key"):
            load_deployment_metadata(duplicate, expected_revision=REVISION)

        invalid = self.root / "invalid.json"
        invalid.write_bytes(b"\xff")
        with self.assertRaisesRegex(DeploymentMetadataError, "UTF-8"):
            load_deployment_metadata(invalid, expected_revision=REVISION)

        target = self.write({"status": "ok"}, "target.json")
        linked = self.root / "linked.json"
        os.symlink(target, linked)
        with self.assertRaisesRegex(DeploymentMetadataError, "symlink"):
            load_deployment_metadata(linked, expected_revision=REVISION)

        oversized = self.root / "oversized.json"
        oversized.write_bytes(b"{}")
        with patch(
            "boho_analytics_platform.site_graph.adapters.deployment_metadata.MAX_METADATA_BYTES",
            1,
        ):
            with self.assertRaisesRegex(DeploymentMetadataError, "byte limit"):
                load_deployment_metadata(oversized, expected_revision=REVISION)

    def test_never_calls_network_or_executes_provider_code(self) -> None:
        path = self.write({"provider": "cloudflare-pages", "status": "success"})
        with patch("socket.socket", side_effect=AssertionError("network attempted")), patch(
            "subprocess.run", side_effect=AssertionError("process attempted")
        ):
            result = load_deployment_metadata(path, expected_revision=REVISION)
        self.assertEqual(result.source, "owner-supplied-json")
        self.assertFalse(result.provider_mutation)


if __name__ == "__main__":
    unittest.main()
