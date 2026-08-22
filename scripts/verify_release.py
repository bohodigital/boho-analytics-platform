"""Fail closed when the public tree contains unexpected or secret-like content."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
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
    ("src", "boho_analytics_platform", "bulk_export"),
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
    ("scripts", "sync_runtime_by_site.sh"),
    ("scripts", "verify_release.py"),
    ("scripts", "verify_runtime_storage.py"),
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
FORBIDDEN_PATH_FRAGMENTS = {
    "current-state",
    "internal-procedure",
    "internal-notes",
    "migration-plan",
    "private-release",
    "roadmap",
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
    "private deployment path": re.compile(re.escape("/srv/" + "private/")),
    "credentialed remote target": re.compile(
        r"(?:\b(?:ssh|sftp)://[A-Za-z_][A-Za-z0-9._-]*@"
        r"(?!(?:github|gitlab|bitbucket)\.com(?=[:/\s]|$))|"
        r"\b(?:ssh|scp|sftp)\b[^\r\n]{0,200}\s"
        r"[A-Za-z_][A-Za-z0-9._-]*@(?!(?:github|gitlab|bitbucket)\.com(?=[:/\s]|$))"
        r"[A-Za-z0-9.-]+\b)",
        re.IGNORECASE,
    ),
}
SCOPED_TEXT_SECRET_PATTERNS = {
    "POSIX user path": re.compile(r"(?:/Users|/home)/[A-Za-z0-9._-]+/"),
    "credentialed URL": re.compile(r"\bhttps?://[^/\s@]+@", re.IGNORECASE),
    "password-bearing SSH URL": re.compile(
        r"\b(?:ssh|sftp)://[^/\s@:]+:[^/\s@]*@",
        re.IGNORECASE,
    ),
}
GIT_TREE_SHA1 = re.compile(r"\A[0-9a-f]{40}\Z")
WORD_TOKEN = re.compile(r"[a-z0-9]+", re.IGNORECASE)
ORGANIZATION_TERM_HASHES = frozenset(
    {
        "b75f54035c33cdcfafd9bf0f899dea66a76d6b8d06d194d03231e44c0e695e17",
        "0f980d50a5e2f8caf93cd7769cff990a41b2691487119d36c381a62d7f6e8d5f",
        "0b7090060449c6be09fcf10904db252d8fe034aa36c0e76e6cb8ebd17e5970cb",
        "359f46cd674f3c4763e6f95a44e73ae9c58d9a67cf0c9af868092379bb8d0b22",
        "1edc7f394848a7557d8cfe134b4558f82d2e09d8bedf7f28a1d1bca5f453143c",
        "f4709afdbe0a3cc15a22868dc6c5523b605b26ef8068169c4d77779183ed6d83",
        "289129e14fd94d392514af59c5b19952b98d80ff82022208250ea077e8f258c1",
        "c925b831f8723ddf323897cb7db312ee25a2bdbab8124072ab460311ace588a7",
        "53873b8b960eb2fead9ce01c857bdafda50f62e76105e035bcfe3dfa945b7c99",
    }
)


def _is_within(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return len(parts) >= len(prefix) and parts[: len(prefix)] == prefix


def _is_allowed_directory(parts: tuple[str, ...]) -> bool:
    return parts in ALLOWED_DIRECTORIES


def _is_allowed_scoped_text_file(relative: Path) -> bool:
    prefixes = SCOPED_TEXT_SUFFIX_TREES.get(relative.suffix, set())
    return any(_is_within(relative.parts, prefix) for prefix in prefixes)


def _contains_organization_term(text: str) -> bool:
    """Detect configured organization terms without publishing their literal values."""

    tokens = [match.group(0).casefold() for match in WORD_TOKEN.finditer(text)]
    for start in range(len(tokens)):
        candidate = ""
        for token in tokens[start : start + 3]:
            candidate += token
            digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
            if digest in ORGANIZATION_TERM_HASHES:
                return True
    return False


def _git_object_id(kind: str, payload: bytes) -> bytes:
    header = f"{kind} {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload, usedforsecurity=False).digest()


def _filesystem_git_tree_id(directory: Path, *, root: Path) -> bytes:
    entries: list[tuple[bytes, bytes]] = []
    for path in directory.iterdir():
        relative = path.relative_to(root)
        if relative.parts == (".git",):
            continue
        try:
            name = path.name.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"non-UTF-8 path is not allowed: {relative}") from exc
        file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode):
            raise ValueError(f"symbolic link is not allowed: {relative}")
        if stat.S_ISDIR(file_stat.st_mode):
            mode = b"40000"
            object_id = _filesystem_git_tree_id(path, root=root)
            sort_name = name + b"/"
        elif stat.S_ISREG(file_stat.st_mode):
            mode = b"100755" if file_stat.st_mode & 0o111 else b"100644"
            object_id = _git_object_id("blob", path.read_bytes())
            sort_name = name
        else:
            raise ValueError(f"special file is not allowed: {relative}")
        entry = mode + b" " + name + b"\0" + object_id
        entries.append((sort_name, entry))
    payload = b"".join(entry for _, entry in sorted(entries, key=lambda item: item[0]))
    return _git_object_id("tree", payload)


def filesystem_git_tree(root: Path) -> str:
    """Return the Git SHA-1 tree ID represented by the public filesystem."""

    return _filesystem_git_tree_id(root.resolve(), root=root.resolve()).hex()


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-C",
            os.fspath(root),
            *arguments,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )


def verify_git_identity(root: Path, *, expected_tree: str | None = None) -> list[str]:
    """Bind a checkout or exported tree to one exact reviewed Git tree."""

    failures: list[str] = []
    if expected_tree is not None and not GIT_TREE_SHA1.fullmatch(expected_tree):
        return ["expected Git tree must be exactly 40 lowercase hexadecimal characters"]

    root = root.resolve()
    git_marker = root / ".git"
    trusted_tree = expected_tree
    if git_marker.exists():
        top_level = _run_git(root, "rev-parse", "--show-toplevel")
        if top_level.returncode != 0:
            return ["unable to resolve Git checkout root"]
        try:
            resolved_top_level = Path(os.fsdecode(top_level.stdout).strip()).resolve()
        except (OSError, UnicodeError, ValueError):
            return ["Git checkout root is invalid"]
        if resolved_top_level != root:
            failures.append("release root is not the Git checkout root")

        head_tree_result = _run_git(root, "rev-parse", "--verify", "HEAD^{tree}")
        if head_tree_result.returncode != 0:
            failures.append("unable to resolve the reviewed HEAD Git tree")
        else:
            head_tree = os.fsdecode(head_tree_result.stdout).strip()
            if not GIT_TREE_SHA1.fullmatch(head_tree):
                failures.append("reviewed HEAD Git tree is invalid")
            elif expected_tree is not None and head_tree != expected_tree:
                failures.append(
                    f"reviewed HEAD Git tree mismatch: expected {expected_tree}, found {head_tree}"
                )
            else:
                trusted_tree = head_tree

        status = _run_git(root, "status", "--porcelain=v1", "--untracked-files=all")
        if status.returncode != 0:
            failures.append("unable to verify Git checkout status")
        elif status.stdout:
            failures.append("Git checkout differs from reviewed HEAD")
    elif expected_tree is None:
        failures.append("exported release tree requires --expected-tree")

    try:
        actual_tree = filesystem_git_tree(root)
    except (OSError, ValueError) as exc:
        failures.append(str(exc))
    else:
        if trusted_tree is not None and actual_tree != trusted_tree:
            failures.append(
                f"public filesystem Git tree mismatch: expected {trusted_tree}, found {actual_tree}"
            )
    return failures


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
        relative_text = relative.as_posix().casefold()
        if any(fragment in relative_text for fragment in FORBIDDEN_PATH_FRAGMENTS):
            failures.append(f"internal planning document is not allowed: {relative}")
            if path.is_dir():
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
        if _contains_organization_term(text):
            failures.append(f"organization-specific term found: {relative}")
        if (
            allowed_scoped_text_file
            or allowed_extensionless_file
            or relative.parts in ALLOWED_CORE21_FILES
        ):
            for label, pattern in SCOPED_TEXT_SECRET_PATTERNS.items():
                if pattern.search(text):
                    failures.append(f"{label} pattern found: {relative}")

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the public policy boundary and exact reviewed Git tree."
    )
    parser.add_argument(
        "--expected-tree",
        help="required 40-character Git tree ID when verifying an exported tree without .git",
    )
    arguments = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    failures = verify_tree(root)
    failures.extend(verify_git_identity(root, expected_tree=arguments.expected_tree))

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print(
        "public tree matches the reviewed Git tree and contains only expected UTF-8 "
        "source, configuration, and documentation"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
