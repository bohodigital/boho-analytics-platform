"""Fail closed when the public tree contains unexpected or secret-like content."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ALLOWED_ROOT_FILES = {
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "pyproject.toml",
}
ALLOWED_DIRECTORIES = {
    (".github",),
    (".github", "workflows"),
    ("docs",),
    ("docs", "adr"),
    ("docs", "images"),
    ("docs", "site-graph"),
    ("examples",),
    ("examples", "fixtures"),
    ("examples", "site-graph"),
    ("examples", "site-graph", "fixtures"),
    ("examples", "site-graph", "ground_truth"),
    ("scripts",),
    ("src",),
    ("src", "boho_analytics_platform"),
    ("src", "boho_analytics_platform", "connectors"),
    ("src", "boho_analytics_platform", "migrations"),
    ("src", "boho_analytics_platform", "site_graph"),
    ("src", "boho_analytics_platform", "site_graph", "adapters"),
    ("src", "boho_analytics_platform", "static"),
    ("tests",),
    ("tests", "site_graph"),
    ("tests", "site_graph", "fixtures"),
    ("tests", "site_graph", "ground_truth"),
    ("examples", "site-graph", "fixtures", "core21"),
    ("examples", "site-graph", "ground_truth", "core21"),
    ("tests", "site_graph", "fixtures", "core21"),
    ("tests", "site_graph", "fixtures", "core21", "artifact_evidence"),
    ("tests", "site_graph", "fixtures", "core21", "artifact_evidence", "site"),
    ("tests", "site_graph", "fixtures", "core21", "artifact_evidence", "site", "about"),
    ("tests", "site_graph", "fixtures", "core21", "rendered_crawl"),
    ("tests", "site_graph", "fixtures", "core21", "source_semantic"),
    ("tests", "site_graph", "fixtures", "core21", "source_semantic", "app"),
    ("tests", "site_graph", "fixtures", "core21", "source_semantic", "src"),
    ("tests", "site_graph", "ground_truth", "core21"),
    ("tests", "site_graph", "ground_truth", "core21", "artifact_evidence"),
    ("tests", "site_graph", "ground_truth", "core21", "rendered_crawl"),
    ("tests", "site_graph", "ground_truth", "core21", "source_semantic"),
}
CORE21_ROOTS = {
    ("examples", "site-graph", "fixtures", "core21"),
    ("examples", "site-graph", "ground_truth", "core21"),
    ("tests", "site_graph", "fixtures", "core21"),
    ("tests", "site_graph", "ground_truth", "core21"),
}
ALLOWED_CORE21_FILES = {
    ("examples", "site-graph", "fixtures", "core21", "public_core21_contract.yaml"),
    ("examples", "site-graph", "ground_truth", "core21", "public_core21_ground_truth.yaml"),
    ("tests", "site_graph", "fixtures", "core21", "artifact_evidence", "deployment.json"),
    ("tests", "site_graph", "fixtures", "core21", "artifact_evidence", "site", "_redirects"),
    ("tests", "site_graph", "fixtures", "core21", "artifact_evidence", "site", "about", "index.html"),
    ("tests", "site_graph", "fixtures", "core21", "artifact_evidence", "site", "index.html"),
    ("tests", "site_graph", "fixtures", "core21", "artifact_evidence", "site", "routes-manifest.json"),
    ("tests", "site_graph", "fixtures", "core21", "artifact_evidence", "site", "sitemap.xml"),
    ("tests", "site_graph", "fixtures", "core21", "public_core21_contract.yaml"),
    ("tests", "site_graph", "fixtures", "core21", "rendered_crawl", "replay.json"),
    ("tests", "site_graph", "fixtures", "core21", "rendered_crawl", "routes.json"),
    ("tests", "site_graph", "fixtures", "core21", "source_semantic", "app", "page.tsx"),
    ("tests", "site_graph", "fixtures", "core21", "source_semantic", "src", "navigation.ts"),
    ("tests", "site_graph", "ground_truth", "core21", "artifact_evidence", "routes.json"),
    ("tests", "site_graph", "ground_truth", "core21", "public_core21_ground_truth.yaml"),
    ("tests", "site_graph", "ground_truth", "core21", "rendered_crawl", "summary.json"),
    ("tests", "site_graph", "ground_truth", "core21", "source_semantic", "expected.json"),
}
ALLOWED_ADAPTER_FILES = {
    ("src", "boho_analytics_platform", "site_graph", "adapters", "__init__.py"),
    ("src", "boho_analytics_platform", "site_graph", "adapters", "artifact_evidence.py"),
    ("src", "boho_analytics_platform", "site_graph", "adapters", "deployment_metadata.py"),
    ("src", "boho_analytics_platform", "site_graph", "adapters", "rendered_crawl.py"),
    ("src", "boho_analytics_platform", "site_graph", "adapters", "source_semantic.py"),
}
ALLOWED_SUFFIXES = {".css", ".geojson", ".json", ".md", ".py", ".sh", ".sql", ".toml", ".txt", ".yaml", ".yml"}
ALLOWED_BINARY_SUFFIXES = {".png"}
ALLOWED_NAMES = {"CODEOWNERS", "LICENSE"}
ALLOWED_SCRIPT_FILES = {
    ("scripts", "backup_runtime.sh"),
    ("scripts", "capture_dashboard_headless.py"),
    ("scripts", "capture_dashboard_headless.sh"),
    ("scripts", "capture_site_graph_evidence.py"),
    ("scripts", "verify_release.py"),
    ("scripts", "verify_site_graph_browser.py"),
}
SCOPED_TEXT_SUFFIX_TREES = {
    ".html": {
        ("tests", "site_graph", "fixtures", "core21", "artifact_evidence"),
        ("tests", "site_graph", "fixtures", "core21", "rendered_crawl"),
    },
    ".ts": {
        ("tests", "site_graph", "fixtures", "core21", "source_semantic"),
    },
    ".tsx": {
        ("tests", "site_graph", "fixtures", "core21", "source_semantic"),
    },
    ".xml": {
        ("tests", "site_graph", "fixtures", "core21", "artifact_evidence"),
        ("tests", "site_graph", "fixtures", "core21", "rendered_crawl"),
    },
}
ALLOWED_EXTENSIONLESS_FILES = {
    ("tests", "site_graph", "fixtures", "core21", "artifact_evidence", "site", "_redirects"),
}
GENERATED_NAMES = {
    ".mypy_cache",
    ".next",
    ".nyc_output",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "out",
}
MAX_FILE_BYTES = 1_000_000
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "Cloudflare token": re.compile(r"\bcf[a-z]{2}_[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "Google client secret": re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "Windows user path": re.compile(r"[A-Z]:\\Users\\[^\r\n]+", re.IGNORECASE),
    "private deployment path": re.compile(re.escape("/srv/" + "local1/")),
    "credentialed remote target": re.compile(
        r"(?:\b(?:ssh|sftp)://[A-Za-z_][A-Za-z0-9._-]*@"
        r"(?!(?:github|gitlab|bitbucket)\.com(?=[:/\s]|$))|"
        r"\b(?:ssh|scp|sftp)\b[^\r\n]{0,200}\s"
        r"[A-Za-z_][A-Za-z0-9._-]*@(?!(?:github|gitlab|bitbucket)\.com(?=[:/\s]|$))"
        r"[A-Za-z0-9.-]+\b)",
        re.IGNORECASE,
    ),
    "internal coordination identifier": re.compile(r"\b(?:W" + r"O|C" + r"R)-\d{4}-[A-Z0-9-]+\b"),
}
SCOPED_TEXT_SECRET_PATTERNS = {
    "POSIX user path": re.compile(r"(?:/Users|/home)/[A-Za-z0-9._-]+/"),
    "credentialed URL": re.compile(r"\bhttps?://[^/\s@]+@", re.IGNORECASE),
    "password-bearing SSH URL": re.compile(
        r"\b(?:ssh|sftp)://[^/\s@:]+:[^/\s@]*@",
        re.IGNORECASE,
    ),
}


def _is_within(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return len(parts) >= len(prefix) and parts[: len(prefix)] == prefix


def _is_allowed_directory(parts: tuple[str, ...]) -> bool:
    return parts in ALLOWED_DIRECTORIES


def _is_allowed_scoped_text_file(relative: Path) -> bool:
    prefixes = SCOPED_TEXT_SUFFIX_TREES.get(relative.suffix, set())
    return any(_is_within(relative.parts, prefix) for prefix in prefixes)


def verify_tree(root: Path) -> list[str]:
    """Return deterministic public-tree policy failures for ``root``."""

    failures: list[str] = []

    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            failures.append(f"symbolic link is not allowed: {relative}")
            continue
        if path.is_dir():
            if path.name in GENERATED_NAMES or path.name.endswith(".egg-info"):
                failures.append(f"generated/private directory is not allowed: {relative}")
            elif not _is_allowed_directory(relative.parts):
                failures.append(f"unexpected directory: {relative}")
            continue

        allowed_root_file = len(relative.parts) == 1 and path.name in ALLOWED_ROOT_FILES
        if len(relative.parts) == 1 and not allowed_root_file:
            failures.append(f"unexpected root file: {relative}")
            continue
        if relative.parts[:1] == ("scripts",) and relative.parts not in ALLOWED_SCRIPT_FILES:
            failures.append(f"unexpected script: {relative}")
            continue
        if (
            relative.parts[:4] == ("src", "boho_analytics_platform", "site_graph", "adapters")
            and relative.parts not in ALLOWED_ADAPTER_FILES
        ):
            failures.append(f"unexpected adapter file: {relative}")
            continue
        allowed_scoped_text_file = _is_allowed_scoped_text_file(relative)
        allowed_extensionless_file = relative.parts in ALLOWED_EXTENSIONLESS_FILES
        if (
            not allowed_root_file
            and path.name not in ALLOWED_NAMES
            and path.suffix not in ALLOWED_SUFFIXES | ALLOWED_BINARY_SUFFIXES
            and not allowed_scoped_text_file
            and not allowed_extensionless_file
        ):
            failures.append(f"unexpected file type: {relative}")
            continue
        if (
            any(_is_within(relative.parts, root_parts) for root_parts in CORE21_ROOTS)
            and relative.parts not in ALLOWED_CORE21_FILES
        ):
            failures.append(f"unexpected Core 2.1 fixture file: {relative}")
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            failures.append(f"file exceeds {MAX_FILE_BYTES} bytes: {relative}")
            continue
        if path.suffix in ALLOWED_BINARY_SUFFIXES:
            if path.suffix == ".png" and not path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
                failures.append(f"invalid PNG signature: {relative}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append(f"non-UTF-8 file: {relative}")
            continue
        if any(
            (ord(character) < 32 and character not in "\t\n\r")
            or 0x7F <= ord(character) <= 0x9F
            for character in text
        ):
            failures.append(f"binary/control content found: {relative}")
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                failures.append(f"{label} pattern found: {relative}")
        if (
            allowed_scoped_text_file
            or allowed_extensionless_file
            or relative.parts in ALLOWED_CORE21_FILES
        ):
            for label, pattern in SCOPED_TEXT_SECRET_PATTERNS.items():
                if pattern.search(text):
                    failures.append(f"{label} pattern found: {relative}")

    return failures


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    failures = verify_tree(root)

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print("public tree contains only expected UTF-8 source, configuration, and documentation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
