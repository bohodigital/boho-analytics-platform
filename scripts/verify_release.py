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
    ("scripts",),
    ("src",),
    ("src", "boho_analytics_platform"),
    ("src", "boho_analytics_platform", "connectors"),
    ("src", "boho_analytics_platform", "migrations"),
    ("src", "boho_analytics_platform", "site_graph"),
    ("src", "boho_analytics_platform", "static"),
    ("tests",),
    ("tests", "site_graph"),
}
ALLOWED_SUFFIXES = {".css", ".geojson", ".json", ".md", ".py", ".sh", ".sql", ".toml", ".txt", ".yaml", ".yml"}
ALLOWED_BINARY_SUFFIXES = {".png"}
ALLOWED_NAMES = {"CODEOWNERS", "LICENSE"}
GENERATED_NAMES = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
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
        r"(?!(?:github|gitlab|bitbucket)\.com\b)|"
        r"\b(?:ssh|scp|sftp)\b[^\r\n]{0,200}\s"
        r"[A-Za-z_][A-Za-z0-9._-]*@(?!(?:github|gitlab|bitbucket)\.com\b)"
        r"[A-Za-z0-9.-]+\b)",
        re.IGNORECASE,
    ),
    "internal coordination identifier": re.compile(r"\b(?:W" + r"O|C" + r"R)-\d{4}-[A-Z0-9-]+\b"),
}


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
            elif relative.parts not in ALLOWED_DIRECTORIES:
                failures.append(f"unexpected directory: {relative}")
            continue

        allowed_root_file = len(relative.parts) == 1 and path.name in ALLOWED_ROOT_FILES
        if len(relative.parts) == 1 and not allowed_root_file:
            failures.append(f"unexpected root file: {relative}")
            continue
        if (
            not allowed_root_file
            and path.name not in ALLOWED_NAMES
            and path.suffix not in ALLOWED_SUFFIXES | ALLOWED_BINARY_SUFFIXES
        ):
            failures.append(f"unexpected file type: {relative}")
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
        for label, pattern in SECRET_PATTERNS.items():
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
