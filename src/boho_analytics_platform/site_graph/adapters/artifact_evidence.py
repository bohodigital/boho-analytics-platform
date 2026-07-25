"""Bounded, read-only evidence extraction from owner-authorized build artifacts.

The adapter never imports target code, runs a build, or writes to a provider.  Its
lane-local result is intentionally serializable so the reconciliation layer can
convert it to the shared schema without coupling this parser to storage.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tarfile
import zipfile
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from itertools import islice
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit, urlunsplit
from xml.etree import ElementTree


ADAPTER_VERSION = "site-graph-artifact-evidence-core-2.1.0"
MAX_ARTIFACT_FILE_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_ROOTS = 100
MAX_ENTRIES = 20_000
MAX_JSON_NODES = 200_000
MAX_XML_LOCATIONS = 100_000
MAX_COMPRESSION_RATIO = 1_000
SUPPORTED_SUFFIXES = frozenset({".html", ".htm", ".json", ".xml", ".txt"})
ROUTE_MANIFEST_NAMES = frozenset(
    {
        "routes-manifest.json",
        "prerender-manifest.json",
        "app-path-routes-manifest.json",
        "app-paths-manifest.json",
        "build-manifest.json",
    }
)
_ROUTE_FIELDS = frozenset(
    {
        "routes",
        "staticRoutes",
        "dynamicRoutes",
        "sortedPages",
        "pages",
        "appPaths",
        "dataRoutes",
        "redirects",
        "rewrites",
        "source",
        "destination",
        "page",
        "route",
        "path",
    }
)
_REVISION_FIELDS = frozenset(
    {"revision", "commit", "commitHash", "commitSha", "commit_hash", "commit_sha"}
)
_REVISION = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_INVALID_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_SECRET_PARTS = (
    "password",
    "secret",
    "token",
    "credential",
    "private_key",
    "api_key",
    "authorization",
    "cookie",
)


class ArtifactEvidenceError(ValueError):
    """Artifact input violated a safety, resource, or determinism invariant."""


@dataclass(frozen=True)
class ArtifactFileEvidence:
    relative_path: str
    artifact_type: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ArtifactRouteEvidence:
    route: str
    source_path: str
    route_kind: str
    title: str
    h1: str
    canonical_url: str
    indexable: bool
    schema_types: tuple[str, ...]
    content_hash: str
    revision_state: str


@dataclass(frozen=True)
class ArtifactLinkEvidence:
    source_route: str
    destination: str
    source_path: str
    source_location: str
    anchor_text: str
    element: str
    content_hash: str


@dataclass(frozen=True)
class ArtifactAdapterResult:
    adapter: str
    adapter_version: str
    revision: str
    revision_state: str
    files: tuple[ArtifactFileEvidence, ...]
    routes: tuple[ArtifactRouteEvidence, ...]
    links: tuple[ArtifactLinkEvidence, ...]
    coverage: dict[str, int | str]
    diagnostics: tuple[str, ...]
    evidence_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "revision": self.revision,
            "revision_state": self.revision_state,
            "files": [asdict(item) for item in self.files],
            "routes": [asdict(item) for item in self.routes],
            "links": [asdict(item) for item in self.links],
            "coverage": dict(self.coverage),
            "diagnostics": list(self.diagnostics),
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True)
class _LoadedFile:
    relative_path: str
    artifact_type: str
    content: bytes
    sha256: str


@dataclass
class _Route:
    route: str
    source_path: str
    route_kind: str
    title: str = ""
    h1: str = ""
    canonical_url: str = ""
    indexable: bool = True
    schema_types: set[str] = field(default_factory=set)
    content_hash: str = ""
    revision_state: str = "unchecked"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return _sha256(raw.encode("ascii"))


def _artifact_type(path: str) -> str:
    pure = PurePosixPath(path)
    name = pure.name.lower()
    if name == "_redirects":
        return "redirects"
    if name in ROUTE_MANIFEST_NAMES:
        return "route-manifest"
    if pure.suffix.lower() in {".html", ".htm"}:
        return "html"
    if name == "sitemap.xml" or pure.suffix.lower() == ".xml":
        return "sitemap"
    if pure.suffix.lower() == ".json":
        return "json"
    return "other"


def _safe_member_name(raw: str) -> str:
    if not raw or "\x00" in raw or "\\" in raw:
        raise ArtifactEvidenceError("artifact entry has an unsafe path")
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise ArtifactEvidenceError(f"artifact entry has an unsafe path: {raw[:200]}")
    return posix.as_posix()


def _supported(path: str) -> bool:
    pure = PurePosixPath(path)
    return pure.name == "_redirects" or pure.suffix.lower() in SUPPORTED_SUFFIXES


def _loaded(label: str, relative: str, raw: bytes) -> _LoadedFile:
    if len(raw) > MAX_ARTIFACT_FILE_BYTES:
        raise ArtifactEvidenceError(f"artifact file exceeds the per-file limit: {relative[:200]}")
    path = f"{label}/{_safe_member_name(relative)}"
    return _LoadedFile(path, _artifact_type(relative), raw, _sha256(raw))


def _load_directory(root: Path, label: str) -> tuple[list[_LoadedFile], int]:
    if root.is_symlink():
        raise ArtifactEvidenceError("artifact root must not be a symlink")
    if not root.is_dir():
        raise ArtifactEvidenceError("artifact root must be a directory or supported archive")
    output: list[_LoadedFile] = []
    entry_count = 0

    def walk(directory: Path, relative: PurePosixPath) -> None:
        nonlocal entry_count
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise ArtifactEvidenceError("artifact directory cannot be read") from exc
        for entry in entries:
            entry_count += 1
            if entry_count > MAX_ENTRIES:
                raise ArtifactEvidenceError("artifact input exceeds the entry-count limit")
            if entry.is_symlink():
                raise ArtifactEvidenceError("artifact symlinks are not allowed")
            child_relative = relative / entry.name
            safe_relative = _safe_member_name(child_relative.as_posix())
            if entry.is_dir(follow_symlinks=False):
                walk(Path(entry.path), PurePosixPath(safe_relative))
            elif entry.is_file(follow_symlinks=False) and _supported(safe_relative):
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError as exc:
                    raise ArtifactEvidenceError("artifact file cannot be inspected") from exc
                if size > MAX_ARTIFACT_FILE_BYTES:
                    raise ArtifactEvidenceError(
                        f"artifact file exceeds the per-file limit: {safe_relative[:200]}"
                    )
                try:
                    raw = Path(entry.path).read_bytes()
                except OSError as exc:
                    raise ArtifactEvidenceError("artifact file cannot be read") from exc
                if len(raw) != size:
                    raise ArtifactEvidenceError("artifact file changed while it was being read")
                output.append(_loaded(label, safe_relative, raw))

    walk(root, PurePosixPath())
    return output, entry_count


def _zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _load_zip(path: Path, label: str) -> tuple[list[_LoadedFile], int]:
    output: list[_LoadedFile] = []
    total = 0
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ENTRIES:
                raise ArtifactEvidenceError("artifact archive exceeds the entry-count limit")
            for info in sorted(infos, key=lambda item: item.filename):
                relative = _safe_member_name(info.filename.rstrip("/"))
                if relative in seen:
                    raise ArtifactEvidenceError("artifact archive contains duplicate paths")
                seen.add(relative)
                if _zip_is_symlink(info):
                    raise ArtifactEvidenceError("artifact archive links are not allowed")
                if info.flag_bits & 0x1:
                    raise ArtifactEvidenceError("encrypted artifact archives are not allowed")
                if info.is_dir():
                    continue
                if info.file_size > MAX_ARTIFACT_FILE_BYTES:
                    raise ArtifactEvidenceError("artifact archive member exceeds the per-file limit")
                if (
                    (info.file_size > 0 and info.compress_size == 0)
                    or (
                        info.compress_size > 0
                        and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
                    )
                ):
                    raise ArtifactEvidenceError("artifact archive exceeds the compression-ratio limit")
                total += info.file_size
                if total > MAX_ARTIFACT_TOTAL_BYTES:
                    raise ArtifactEvidenceError("artifact archive exceeds the expanded-size limit")
                if not _supported(relative):
                    continue
                with archive.open(info) as source:
                    raw = source.read(MAX_ARTIFACT_FILE_BYTES + 1)
                    if source.read(1):
                        raise ArtifactEvidenceError("artifact archive member exceeds its declared size")
                if len(raw) != info.file_size:
                    raise ArtifactEvidenceError("artifact archive member size does not match metadata")
                output.append(_loaded(label, relative, raw))
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ArtifactEvidenceError("artifact zip is invalid or unreadable") from exc
    return output, len(seen)


def _load_tar(path: Path, label: str) -> tuple[list[_LoadedFile], int]:
    output: list[_LoadedFile] = []
    total = 0
    count = 0
    seen: set[str] = set()
    suffixes = "".join(path.suffixes[-2:]).lower()
    compressed_size = path.stat().st_size
    try:
        with tarfile.open(path, mode="r|*") as archive:
            for member in archive:
                count += 1
                if count > MAX_ENTRIES:
                    raise ArtifactEvidenceError("artifact archive exceeds the entry-count limit")
                relative = _safe_member_name(member.name.rstrip("/"))
                if relative in seen:
                    raise ArtifactEvidenceError("artifact archive contains duplicate paths")
                seen.add(relative)
                if member.issym() or member.islnk():
                    raise ArtifactEvidenceError("artifact archive links are not allowed")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ArtifactEvidenceError("artifact archive contains a special file")
                if member.size > MAX_ARTIFACT_FILE_BYTES:
                    raise ArtifactEvidenceError("artifact archive member exceeds the per-file limit")
                total += member.size
                if total > MAX_ARTIFACT_TOTAL_BYTES:
                    raise ArtifactEvidenceError("artifact archive exceeds the expanded-size limit")
                if (
                    suffixes in {".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz"}
                    and compressed_size > 0
                    and total / compressed_size > MAX_COMPRESSION_RATIO
                ):
                    raise ArtifactEvidenceError(
                        "artifact archive exceeds the compression-ratio limit"
                    )
                if not _supported(relative):
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise ArtifactEvidenceError("artifact archive member cannot be read")
                raw = source.read(MAX_ARTIFACT_FILE_BYTES + 1)
                if len(raw) != member.size:
                    raise ArtifactEvidenceError("artifact archive member size does not match metadata")
                output.append(_loaded(label, relative, raw))
    except (OSError, tarfile.TarError) as exc:
        raise ArtifactEvidenceError("artifact tar is invalid or unreadable") from exc
    return output, count


def _load_artifacts(paths: Iterable[Path]) -> tuple[list[_LoadedFile], int, int]:
    requested = list(islice(paths, MAX_ARTIFACT_ROOTS + 1))
    if not requested:
        raise ArtifactEvidenceError("at least one artifact root is required")
    if len(requested) > MAX_ARTIFACT_ROOTS:
        raise ArtifactEvidenceError(
            f"at most {MAX_ARTIFACT_ROOTS} artifact roots may be inspected"
        )
    files: list[_LoadedFile] = []
    total_bytes = 0
    total_entries = 0
    for index, raw_path in enumerate(requested, 1):
        if raw_path.is_symlink():
            raise ArtifactEvidenceError("artifact inputs must not be symlinks")
        try:
            size = raw_path.stat().st_size
        except OSError as exc:
            raise ArtifactEvidenceError("artifact input does not exist or cannot be inspected") from exc
        if raw_path.is_file() and size > MAX_ARCHIVE_BYTES:
            raise ArtifactEvidenceError("artifact archive exceeds the compressed-size limit")
        label = f"artifact{index}"
        suffixes = "".join(raw_path.suffixes[-2:]).lower()
        if raw_path.is_dir():
            loaded, entries = _load_directory(raw_path, label)
        elif raw_path.suffix.lower() == ".zip":
            loaded, entries = _load_zip(raw_path, label)
        elif raw_path.suffix.lower() in {".tar", ".tgz", ".tbz2", ".txz"} or suffixes in {
            ".tar.gz",
            ".tar.bz2",
            ".tar.xz",
        }:
            loaded, entries = _load_tar(raw_path, label)
        else:
            raise ArtifactEvidenceError("artifact input must be a directory, zip, or tar archive")
        files.extend(loaded)
        total_entries += entries
        total_bytes += sum(len(item.content) for item in loaded)
        if total_entries > MAX_ENTRIES:
            raise ArtifactEvidenceError("artifact inputs exceed the entry-count limit")
        if len(files) > MAX_ENTRIES:
            raise ArtifactEvidenceError("artifact inputs exceed the supported-file limit")
        if total_bytes > MAX_ARTIFACT_TOTAL_BYTES:
            raise ArtifactEvidenceError("artifact inputs exceed the expanded-size limit")
    paths_seen = [item.relative_path for item in files]
    if len(paths_seen) != len(set(paths_seen)):
        raise ArtifactEvidenceError("artifact inputs produce duplicate evidence paths")
    return sorted(files, key=lambda item: item.relative_path), total_entries, total_bytes


def _decode(file: _LoadedFile) -> str:
    try:
        text = file.content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ArtifactEvidenceError(f"artifact is not UTF-8: {file.relative_path}") from exc
    if any(
        (ord(character) < 32 and character not in "\t\n\r")
        or 0x7F <= ord(character) <= 0x9F
        for character in text
    ):
        raise ArtifactEvidenceError(
            f"artifact contains binary/control content: {file.relative_path}"
        )
    return text


def _normalize_route(raw: str) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        raise ArtifactEvidenceError("artifact route is missing or oversized")
    if "\\" in raw or _INVALID_PERCENT.search(raw) or any(ord(char) < 32 for char in raw):
        raise ArtifactEvidenceError("artifact route contains unsafe characters")
    decoded = unquote(raw)
    if (
        len(decoded) > 4096
        or "%" in decoded
        or "\\" in decoded
        or any(ord(char) < 32 for char in decoded)
    ):
        raise ArtifactEvidenceError("artifact route has ambiguous encoded characters")
    split = urlsplit(decoded)
    path = split.path or "/"
    if split.scheme or split.netloc or not path.startswith("/") or path.startswith("//"):
        raise ArtifactEvidenceError("artifact route must be a root-relative path")
    pure = PurePosixPath(path)
    if any(part in {".", ".."} for part in pure.parts):
        raise ArtifactEvidenceError("artifact route contains traversal")
    normalized = re.sub(r"/{2,}", "/", path)
    if normalized != "/" and not normalized.endswith("/") and "." not in PurePosixPath(normalized).name:
        normalized += "/"
    return normalized


def _safe_destination(raw: str) -> str:
    if not raw or len(raw) > 4096:
        raise ArtifactEvidenceError("artifact link destination is missing or oversized")
    if "\\" in raw or _INVALID_PERCENT.search(raw) or any(ord(char) < 32 for char in raw):
        raise ArtifactEvidenceError("artifact link destination contains unsafe characters")
    split = urlsplit(raw)
    scheme = split.scheme.lower()
    if scheme in {"javascript", "data", "file", "vbscript"}:
        raise ArtifactEvidenceError("artifact link destination uses an unsafe scheme")
    if scheme in {"mailto", "tel"}:
        return f"{scheme}:"
    if scheme and scheme not in {"http", "https"}:
        raise ArtifactEvidenceError("artifact link destination uses an unsupported scheme")
    if scheme and not split.hostname:
        raise ArtifactEvidenceError("artifact link destination has no HTTP host")
    if split.username is not None or split.password is not None:
        raise ArtifactEvidenceError("artifact link destination contains credentials")
    if not scheme and not split.netloc and not split.path and split.fragment:
        return f"#{split.fragment[:500]}"
    if not scheme and (split.netloc or not split.path.startswith("/")):
        raise ArtifactEvidenceError("artifact link destination must be root-relative or absolute HTTP")
    return urlunsplit((scheme, split.netloc.lower(), split.path, "", split.fragment[:500]))


def _route_from_html_path(relative_path: str) -> str:
    relative = relative_path.split("/", 1)[-1]
    pure = PurePosixPath(relative)
    if pure.name.lower() in {"index.html", "index.htm"}:
        parent = pure.parent.as_posix()
        return "/" if parent == "." else _normalize_route(f"/{parent}/")
    return _normalize_route(f"/{pure.with_suffix('').as_posix()}")


class _HTMLEvidenceParser(HTMLParser):
    def __init__(self, source_route: str, source_path: str):
        super().__init__(convert_charrefs=True)
        self.source_route = source_route
        self.source_path = source_path
        self.title = ""
        self.h1 = ""
        self.canonical_url = ""
        self.indexable = True
        self.schema_types: set[str] = set()
        self.links: list[tuple[str, str, str, int, int]] = []
        self._capture: str | None = None
        self._captured: list[str] = []
        self._anchor: tuple[str, list[str], int, int] | None = None
        self._json_ld = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag in {"title", "h1"}:
            self._capture = tag
            self._captured = []
        elif tag == "script" and attributes.get("type", "").lower() == "application/ld+json":
            self._json_ld = True
            self._captured = []
        if tag == "meta" and attributes.get("name", "").lower() in {"robots", "googlebot"}:
            directives = {part.strip().lower() for part in attributes.get("content", "").split(",")}
            if "noindex" in directives or "none" in directives:
                self.indexable = False
        if tag == "link" and "canonical" in attributes.get("rel", "").lower().split():
            self.canonical_url = attributes.get("href", "")[:4096]
        if tag == "a" and attributes.get("href"):
            line, column = self.getpos()
            self._anchor = (attributes["href"][:4096], [], line, column)
        if tag == "form" and attributes.get("action"):
            line, column = self.getpos()
            self.links.append((attributes["action"][:4096], "", "form", line, column))
        if tag == "button" and attributes.get("formaction"):
            line, column = self.getpos()
            self.links.append((attributes["formaction"][:4096], "", "button", line, column))

    def handle_data(self, data: str) -> None:
        if self._capture or self._json_ld:
            self._captured.append(data)
        if self._anchor is not None:
            self._anchor[1].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._anchor is not None:
            destination, words, line, column = self._anchor
            label = " ".join("".join(words).split())[:500]
            self.links.append((destination, label, "anchor", line, column))
            self._anchor = None
        if self._capture == tag:
            text = " ".join("".join(self._captured).split())[:500]
            if tag == "title":
                self.title = text
            elif tag == "h1" and not self.h1:
                self.h1 = text
            self._capture = None
            self._captured = []
        if tag == "script" and self._json_ld:
            self._read_json_ld("".join(self._captured))
            self._json_ld = False
            self._captured = []

    def _read_json_ld(self, raw: str) -> None:
        if len(raw.encode("utf-8")) > 200_000:
            return
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            return
        stack = [payload]
        nodes = 0
        while stack and nodes < 10_000:
            value = stack.pop()
            nodes += 1
            if isinstance(value, dict):
                item_type = value.get("@type")
                values = item_type if isinstance(item_type, list) else [item_type]
                self.schema_types.update(
                    str(item)[:120] for item in values if isinstance(item, str)
                )
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactEvidenceError(f"JSON contains duplicate key: {key[:100]}")
        result[key] = value
    return result


def _load_json(file: _LoadedFile) -> Any:
    try:
        return json.loads(
            _decode(file),
            object_pairs_hook=_json_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ArtifactEvidenceError(f"JSON contains non-finite value: {value}")
            ),
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ArtifactEvidenceError(f"artifact JSON is invalid: {file.relative_path}") from exc


def _manifest_routes_and_revisions(payload: Any) -> tuple[set[str], set[str], int]:
    routes: set[str] = set()
    revisions: set[str] = set()
    nodes = 0
    stack: list[tuple[str | None, Any]] = [(None, payload)]
    while stack:
        field, value = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise ArtifactEvidenceError("route manifest exceeds the JSON-node limit")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 500:
                    raise ArtifactEvidenceError("route manifest contains an invalid key")
                normalized_key = key.lower().replace("-", "_")
                if any(part in normalized_key for part in _SECRET_PARTS):
                    raise ArtifactEvidenceError("route manifest contains a sensitive field")
                stack.append((key, child))
        elif isinstance(value, list):
            if len(value) > MAX_ENTRIES:
                raise ArtifactEvidenceError("route manifest list exceeds the entry-count limit")
            stack.extend((field, child) for child in value)
        elif isinstance(value, str):
            if len(value) > 4096:
                raise ArtifactEvidenceError("route manifest string exceeds the size limit")
            if field in _REVISION_FIELDS:
                if not _REVISION.fullmatch(value):
                    raise ArtifactEvidenceError("route manifest contains an invalid revision")
                revisions.add(value.lower())
            elif field in _ROUTE_FIELDS and value.startswith("/") and not value.startswith("//"):
                routes.add(_normalize_route(value))
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise ArtifactEvidenceError("route manifest contains an unsupported value")
    return routes, revisions, nodes


def _add_route(routes: dict[str, _Route], incoming: _Route) -> None:
    rank = {"html": 4, "redirect": 3, "sitemap": 2, "manifest": 1}
    existing = routes.get(incoming.route)
    if existing is None or (rank[incoming.route_kind], incoming.source_path) > (
        rank[existing.route_kind],
        existing.source_path,
    ):
        routes[incoming.route] = incoming


def collect_artifact_evidence(
    artifact_roots: Iterable[Path],
    *,
    revision: str,
    canonical_hosts: Iterable[str] = (),
) -> ArtifactAdapterResult:
    """Extract deterministic evidence without executing or persisting target code."""
    if not _REVISION.fullmatch(revision):
        raise ArtifactEvidenceError("revision must be a full 40- or 64-character hex digest")
    revision = revision.lower()
    hosts = frozenset(host.lower() for host in canonical_hosts)
    if any(not host or "/" in host or "@" in host for host in hosts):
        raise ArtifactEvidenceError("canonical hosts contain an invalid hostname")
    files, entry_count, total_bytes = _load_artifacts(artifact_roots)
    if not files:
        raise ArtifactEvidenceError("artifact input contains no supported evidence files")
    # All selected text inputs must be valid even when their specific evidence
    # type is not interpreted by this release.
    for file in files:
        _decode(file)

    routes: dict[str, _Route] = {}
    links: list[ArtifactLinkEvidence] = []
    diagnostics: set[str] = set()
    manifest_nodes = 0
    manifest_revision_states: list[str] = []
    for file in files:
        if file.artifact_type == "html":
            route = _route_from_html_path(file.relative_path)
            parser = _HTMLEvidenceParser(route, file.relative_path)
            try:
                parser.feed(_decode(file))
                parser.close()
            except ArtifactEvidenceError:
                raise
            except (RecursionError, ValueError) as exc:
                raise ArtifactEvidenceError(f"artifact HTML is invalid: {file.relative_path}") from exc
            canonical_url = parser.canonical_url
            if canonical_url:
                split = urlsplit(canonical_url)
                if (
                    split.scheme not in {"http", "https"}
                    or not split.hostname
                    or split.username is not None
                    or split.password is not None
                    or (hosts and split.hostname.lower() not in hosts)
                ):
                    diagnostics.add(f"canonical-rejected:{file.relative_path}")
                    canonical_url = ""
                else:
                    canonical_route = _normalize_route(split.path or "/")
                    if canonical_route != route:
                        diagnostics.add(f"canonical-route-conflict:{file.relative_path}")
                    canonical_url = urlunsplit(
                        (split.scheme.lower(), split.netloc.lower(), split.path or "/", "", "")
                    )
            _add_route(
                routes,
                _Route(
                    route=route,
                    source_path=file.relative_path,
                    route_kind="html",
                    title=parser.title,
                    h1=parser.h1,
                    canonical_url=canonical_url,
                    indexable=parser.indexable,
                    schema_types=parser.schema_types,
                    content_hash=file.sha256,
                    revision_state="associated",
                ),
            )
            for destination, label, element, line, column in parser.links:
                try:
                    destination = _safe_destination(destination)
                except ArtifactEvidenceError:
                    diagnostics.add(f"unsafe-link-rejected:{file.relative_path}")
                    continue
                source_location = f"{file.relative_path}:{line}:{column}"
                links.append(
                    ArtifactLinkEvidence(
                        source_route=route,
                        destination=destination,
                        source_path=file.relative_path,
                        source_location=source_location,
                        anchor_text=label,
                        element=element,
                        content_hash=_stable_hash(
                            [route, destination, source_location, label, element]
                        ),
                    )
                )
        elif file.artifact_type == "sitemap":
            text = _decode(file)
            if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
                raise ArtifactEvidenceError("sitemap declarations and entities are not allowed")
            try:
                root = ElementTree.fromstring(text)
            except ElementTree.ParseError as exc:
                raise ArtifactEvidenceError(f"sitemap XML is invalid: {file.relative_path}") from exc
            locations = 0
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1] != "loc" or not element.text:
                    continue
                locations += 1
                if locations > MAX_XML_LOCATIONS:
                    raise ArtifactEvidenceError("sitemap exceeds the location-count limit")
                split = urlsplit(element.text.strip())
                if split.scheme not in {"http", "https"} or not split.hostname:
                    raise ArtifactEvidenceError("sitemap location must be an absolute HTTP URL")
                if hosts and split.hostname.lower() not in hosts:
                    diagnostics.add(f"sitemap-external-host:{file.relative_path}")
                    continue
                route = _normalize_route(split.path or "/")
                _add_route(
                    routes,
                    _Route(
                        route,
                        file.relative_path,
                        "sitemap",
                        content_hash=file.sha256,
                        revision_state="associated",
                    ),
                )
        elif file.artifact_type == "route-manifest":
            found_routes, revisions, nodes = _manifest_routes_and_revisions(_load_json(file))
            manifest_nodes += nodes
            state = (
                "unchecked"
                if not revisions
                else "matched"
                if revisions == {revision}
                else "mismatched"
            )
            manifest_revision_states.append(state)
            if state == "mismatched":
                diagnostics.add(f"revision-mismatch:{file.relative_path}")
            for route in sorted(found_routes):
                _add_route(
                    routes,
                    _Route(
                        route,
                        file.relative_path,
                        "manifest",
                        content_hash=file.sha256,
                        revision_state=state,
                    ),
                )
        elif file.artifact_type == "redirects":
            for line_number, raw_line in enumerate(_decode(file).splitlines(), 1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 2 or not parts[0].startswith("/") or parts[0].startswith("//"):
                    diagnostics.add(f"redirect-unparsed:{file.relative_path}:{line_number}")
                    continue
                source = _normalize_route(parts[0])
                _add_route(
                    routes,
                    _Route(
                        source,
                        file.relative_path,
                        "redirect",
                        content_hash=file.sha256,
                        revision_state="associated",
                    ),
                )
                try:
                    destination = _safe_destination(parts[1][:4096])
                except ArtifactEvidenceError:
                    diagnostics.add(f"unsafe-redirect-rejected:{file.relative_path}:{line_number}")
                    continue
                links.append(
                    ArtifactLinkEvidence(
                        source,
                        destination,
                        file.relative_path,
                        f"{file.relative_path}:{line_number}:1",
                        "redirect",
                        "redirect",
                        _stable_hash([source, destination, file.relative_path, line_number]),
                    )
                )

    links.sort(
        key=lambda item: (
            item.source_path,
            item.source_location,
            item.source_route,
            item.destination,
            item.element,
            item.anchor_text,
            item.content_hash,
        )
    )
    route_items = tuple(
        ArtifactRouteEvidence(
            route=item.route,
            source_path=item.source_path,
            route_kind=item.route_kind,
            title=item.title,
            h1=item.h1,
            canonical_url=item.canonical_url,
            indexable=item.indexable,
            schema_types=tuple(sorted(item.schema_types)),
            content_hash=item.content_hash,
            revision_state=item.revision_state,
        )
        for _, item in sorted(routes.items())
    )
    file_items = tuple(
        ArtifactFileEvidence(
            item.relative_path,
            item.artifact_type,
            len(item.content),
            item.sha256,
        )
        for item in files
    )
    overall_revision_state = (
        "mismatched"
        if "mismatched" in manifest_revision_states
        else "matched"
        if "matched" in manifest_revision_states
        else "unchecked"
    )
    coverage: dict[str, int | str] = {
        "input_entries": entry_count,
        "supported_files": len(file_items),
        "artifact_bytes": total_bytes,
        "html_files": sum(item.artifact_type == "html" for item in file_items),
        "route_manifest_files": sum(
            item.artifact_type == "route-manifest" for item in file_items
        ),
        "sitemap_files": sum(item.artifact_type == "sitemap" for item in file_items),
        "redirect_files": sum(item.artifact_type == "redirects" for item in file_items),
        "manifest_nodes": manifest_nodes,
        "routes": len(route_items),
        "links": len(links),
        "max_file_bytes": MAX_ARTIFACT_FILE_BYTES,
        "max_total_bytes": MAX_ARTIFACT_TOTAL_BYTES,
        "max_entries": MAX_ENTRIES,
        "local_build_execution": "disabled",
        "provider_mutation": "disabled",
    }
    evidence_payload = {
        "adapter": ADAPTER_VERSION,
        "revision": revision,
        "revision_state": overall_revision_state,
        "files": [asdict(item) for item in file_items],
        "routes": [asdict(item) for item in route_items],
        "links": [asdict(item) for item in links],
        "coverage": coverage,
        "diagnostics": sorted(diagnostics),
    }
    return ArtifactAdapterResult(
        adapter="artifact-evidence",
        adapter_version=ADAPTER_VERSION,
        revision=revision,
        revision_state=overall_revision_state,
        files=file_items,
        routes=route_items,
        links=tuple(links),
        coverage=coverage,
        diagnostics=tuple(sorted(diagnostics)),
        evidence_hash=_stable_hash(evidence_payload),
    )
