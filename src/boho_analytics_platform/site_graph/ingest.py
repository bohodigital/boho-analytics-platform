"""Read-only, source-first repository inspection and structural extraction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from .manifest import SiteGraphManifest
from .storage import LinkOccurrence, PageFact, SiteGraphStore


MAX_GIT_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_SOURCE_FILE_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_TRACKED_FILES = 100_000
MAX_LINKS = 2_000_000
SOURCE_EXTENSIONS = {".html", ".htm", ".ts", ".tsx", ".js", ".jsx", ".mdx"}
ROUTE_LITERAL = re.compile(r"\b(?:slug|path|href|to|url|action|formAction)\s*:\s*([\"'`])(?P<value>[^\"'`]+)\1")
JSX_HREF = re.compile(r"\b(?:href|to|action|formAction)\s*=\s*(?:\{\s*)?([\"'`])(?P<value>[^\"'`]+)\1")
JSX_ROUTE_EXPRESSION = re.compile(
    r"\b(?:href|to|action|formAction)\s*=\s*\{\s*(?P<expr>[^}\n]{1,260})\s*\}"
)
ROUTER_LITERAL = re.compile(
    r"\b(?:router\s*\.\s*)?(?:push|replace|prefetch|navigate|redirect|permanentRedirect)\s*"
    r"\(\s*([\"'`])(?P<value>[^\"'`]+)\1"
)
ROUTER_EXPRESSION = re.compile(
    r"\b(?:router\s*\.\s*)?(?:push|replace|prefetch|navigate|redirect|permanentRedirect)\s*"
    r"\(\s*(?P<expr>[^,\)\n]{1,260})"
)
MARKDOWN_LINK = re.compile(r"\[(?P<label>[^\]]{0,500})\]\(\s*(?P<value>[^)\s]+)")
SIMPLE_ROUTE_SYMBOL = re.compile(
    r"\b(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*"
    r"([\"'`])(?P<value>/[^\"'`]+)\2"
)
OBJECT_ROUTE_BLOCK = re.compile(
    r"\b(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*\{"
    r"(?P<body>.{0,20000}?)\n\}",
    re.DOTALL,
)
OBJECT_ROUTE_PROPERTY = re.compile(
    r"(?P<key>[A-Za-z_$][\w$]*|[\"'][^\"']+[\"'])\s*:\s*([\"'`])(?P<value>/[^\"'`]+)\2"
)
LABEL_LITERAL = re.compile(r"\b(?:label|linkLabel|title|headline)\s*:\s*([\"'])(?P<value>[^\"']{1,500})\1")


class IngestError(RuntimeError):
    """Repository inspection or extraction failed closed."""


@dataclass(frozen=True)
class TrackedFile:
    mode: str
    object_id: str
    path: str


@dataclass(frozen=True)
class RepositoryInspection:
    repository_identity: str
    remote_url: str
    ref: str
    revision: str
    clean: bool
    dirty_override: bool
    adapter: str
    adapter_version: str
    manifest_hash: str
    analysis_mode: str
    content_hash: str
    tracked_files: int

    def sanitized_summary(self) -> dict[str, Any]:
        return {
            key: value for key, value in asdict(self).items()
            if key != "remote_url"
        }


@dataclass(frozen=True)
class IngestResult:
    site_key: str
    repository_snapshot_id: str
    revision: str
    adapter: str
    analysis_mode: str
    pages: int
    links: int
    layers: dict[str, int]
    fact_hash: str
    reused: bool
    coverage: dict[str, Any]

    def sanitized_summary(self) -> dict[str, Any]:
        return asdict(self)


def _bounded_process(
    command: list[str], *, cwd: Path, timeout: int = 30, binary: bool = False
) -> bytes | str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=not binary,
            timeout=timeout,
            check=False,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IngestError(f"bounded command failed: {command[0]}") from exc
    stdout = result.stdout
    stderr = result.stderr
    output_size = len(stdout if isinstance(stdout, bytes) else stdout.encode("utf-8", "replace"))
    error_size = len(stderr if isinstance(stderr, bytes) else stderr.encode("utf-8", "replace"))
    if output_size + error_size > MAX_GIT_OUTPUT_BYTES:
        raise IngestError("bounded command output exceeds the repository inspection limit")
    if result.returncode != 0:
        detail = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
        detail = detail.strip().splitlines()[-1][:500] if detail.strip() else "command returned non-zero"
        raise IngestError(f"repository inspection failed: {detail}")
    return stdout


def _git_environment() -> dict[str, str]:
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "TMP", "TEMP"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
    })
    return environment


def _git_text(repository: Path, *arguments: str) -> str:
    output = _bounded_process([
        "git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", *arguments
    ], cwd=repository)
    assert isinstance(output, str)
    return output.rstrip("\r\n")


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    output = _bounded_process([
        "git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", *arguments
    ], cwd=repository, binary=True)
    assert isinstance(output, bytes)
    return output


def _tracked_files(repository: Path, revision: str) -> tuple[list[TrackedFile], bytes]:
    raw = _git_bytes(repository, "ls-tree", "-r", "--full-tree", "-z", revision)
    entries: list[TrackedFile] = []
    for item in raw.split(b"\x00"):
        if not item:
            continue
        try:
            metadata, raw_path = item.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii").split(" ")
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise IngestError("repository tree contains an unsupported entry") from exc
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or "\x00" in path:
            raise IngestError("repository tree contains an unsafe path")
        if object_type != "blob" or mode == "120000":
            continue
        entries.append(TrackedFile(mode, object_id, path))
        if len(entries) > MAX_TRACKED_FILES:
            raise IngestError(f"repository contains more than {MAX_TRACKED_FILES} tracked files")
    return entries, raw


def _repository_identity(remote: str) -> str:
    if remote.startswith("git@"):
        host, path = remote[4:].split(":", 1)
    else:
        parsed = urlsplit(remote)
        host, path = parsed.hostname or "unknown", parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host.lower()}/{path.strip('/')}"


def _detect_adapter(repository: Path, revision: str, files: list[TrackedFile], requested: str) -> tuple[str, str]:
    paths = {entry.path for entry in files}
    package: dict[str, Any] = {}
    if "package.json" in paths:
        raw = _git_bytes(repository, "show", f"{revision}:package.json")
        if len(raw) > MAX_SOURCE_FILE_BYTES:
            raise IngestError("package.json exceeds the source file limit")
        try:
            package = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise IngestError("package.json is not valid JSON") from exc
    runtime_dependencies = package.get("dependencies", {}) if isinstance(package, dict) else {}
    development_dependencies = package.get("devDependencies", {}) if isinstance(package, dict) else {}
    if not isinstance(runtime_dependencies, dict) or not isinstance(development_dependencies, dict):
        raise IngestError("package.json dependency fields must be objects")
    dependencies = {**runtime_dependencies, **development_dependencies}
    detected = "vinext" if "vinext" in dependencies else "astro" if "astro" in dependencies else "static-html"
    if requested != "auto" and requested != detected:
        if requested == "static-html" and any(Path(path).suffix.lower() in {".html", ".htm"} for path in paths):
            detected = requested
        else:
            raise IngestError(f"requested adapter {requested} does not match detected repository adapter {detected}")
    adapter = detected if requested == "auto" else requested
    version = str(dependencies.get(adapter, "stdlib-v1")) if adapter != "static-html" else "stdlib-v1"
    return adapter, version[:100]


def inspect_repository(
    manifest: SiteGraphManifest, *, allow_dirty_snapshot: bool = False
) -> RepositoryInspection:
    repository = Path(manifest.repository.local_path).expanduser().resolve()
    if not repository.is_dir():
        raise IngestError("repository path does not exist or is not a directory")
    root = Path(_git_text(repository, "rev-parse", "--show-toplevel")).resolve()
    if root != repository:
        raise IngestError("repository.local_path must identify the Git worktree root")
    remote = _git_text(repository, "remote", "get-url", "origin")
    if remote != manifest.repository.expected_remote:
        raise IngestError("repository origin does not match the manifest")
    revision = _git_text(repository, "rev-parse", "--verify", f"{manifest.repository.ref}^{{commit}}")
    if manifest.repository.expected_commit and revision.lower() != manifest.repository.expected_commit:
        raise IngestError("repository revision does not match repository.expected_commit")
    porcelain = _git_text(repository, "status", "--porcelain=v1", "--untracked-files=all")
    status_codes = " MADRCU?!"
    dirty = any(
        len(line) >= 3 and line[0] in status_codes and line[1] in status_codes and line[2] == " "
        for line in porcelain.splitlines()
    )
    if dirty and (manifest.repository.require_clean or not allow_dirty_snapshot):
        raise IngestError("repository is dirty; use an explicitly permitted non-production snapshot override")
    files, raw_tree = _tracked_files(repository, revision)
    adapter, adapter_version = _detect_adapter(repository, revision, files, manifest.analysis.adapter)
    if manifest.analysis.mode == "build":
        raise IngestError(
            "bounded build execution is unavailable without an operating-system network-isolation runner; use source-only mode"
        )
    return RepositoryInspection(
        repository_identity=_repository_identity(remote),
        remote_url=remote,
        ref=manifest.repository.ref,
        revision=revision.lower(),
        clean=not dirty,
        dirty_override=dirty and allow_dirty_snapshot,
        adapter=adapter,
        adapter_version=adapter_version,
        manifest_hash=manifest.manifest_hash,
        analysis_mode=manifest.analysis.mode,
        content_hash=hashlib.sha256(raw_tree).hexdigest(),
        tracked_files=len(files),
    )


def _route_allowed(route: str, manifest: SiteGraphManifest) -> bool:
    return any(fnmatchcase(route, pattern) for pattern in manifest.routes.include) and not any(
        fnmatchcase(route, pattern) for pattern in manifest.routes.exclude
    )


def _normalize_route(path: str, manifest: SiteGraphManifest) -> str:
    if not path.startswith("/"):
        path = "/" + path
    path = re.sub(r"/{2,}", "/", path)
    if manifest.canonicalization.normalize_trailing_slash and path != "/" and not Path(path).suffix:
        path = path.rstrip("/") + "/"
    return path


def _destination(raw: str, source_route: str, manifest: SiteGraphManifest) -> tuple[str, bool, bool, bool, str | None]:
    split = urlsplit(raw)
    fragment = bool(split.fragment) or raw.startswith("#")
    action_kind = split.scheme.lower() if split.scheme.lower() in {"mailto", "tel"} else None
    if action_kind:
        return raw, True, fragment, False, action_kind
    base = f"https://{manifest.site.canonical_hosts[0]}{source_route}"
    resolved = urlsplit(urljoin(base, raw))
    external = bool(resolved.hostname and resolved.hostname.lower() not in manifest.site.canonical_hosts)
    if external:
        query = "" if manifest.canonicalization.remove_query_parameters else resolved.query
        frag = "" if manifest.canonicalization.strip_fragments else resolved.fragment
        return urlunsplit((resolved.scheme, resolved.netloc, resolved.path, query, frag)), True, fragment, True, None
    route = _normalize_route(resolved.path or source_route, manifest)
    query = "" if manifest.canonicalization.remove_query_parameters else resolved.query
    frag = "" if manifest.canonicalization.strip_fragments else resolved.fragment
    canonical = urlunsplit(("", "", route, query, frag))
    return canonical, True, fragment, False, None


def _page_evidence(route: str, source_path: str, manifest: SiteGraphManifest, adapter: str, confidence: float) -> dict[str, Any]:
    roles: list[str] = []
    journey_stage: int | None = None
    rule_ids: list[str] = []
    for rule in manifest.page_rules:
        if re.search(rule.path_regex, route):
            roles.extend(rule.roles)
            journey_stage = rule.journey_stage if journey_stage is None else min(journey_stage, rule.journey_stage)
            rule_ids.append(rule.id)
    return {
        "adapter": adapter,
        "source_path": source_path,
        "route_confidence": confidence,
        "page_rule_ids": sorted(set(rule_ids)),
        "roles": sorted(set(roles)),
        "journey_stage": journey_stage,
    }


class _HTMLFacts(HTMLParser):
    def __init__(self, *, route: str, source_path: str, manifest: SiteGraphManifest):
        super().__init__(convert_charrefs=True)
        self.route = route
        self.source_path = source_path
        self.manifest = manifest
        self.stack: list[tuple[str, dict[str, str]]] = []
        self.anchor: dict[str, Any] | None = None
        self.links: list[LinkOccurrence] = []
        self.ordinal = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        self.stack.append((tag.lower(), attributes))
        if tag.lower() == "a" and "href" in attributes:
            self.anchor = {"attrs": attributes, "text": [], "line": self.getpos()[0], "ancestors": list(self.stack[:-1])}

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.anchor is not None:
            self.anchor["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self.anchor is not None:
            self._finish_anchor()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag.lower():
                del self.stack[index:]
                break

    def _finish_anchor(self) -> None:
        assert self.anchor is not None
        attrs = self.anchor["attrs"]
        ancestors = self.anchor["ancestors"]
        raw = attrs["href"][:4000]
        layer, confidence, landmark = _html_layer(attrs, ancestors, self.manifest)
        canonical, crawlable, fragment, external, action_kind = _destination(raw, self.route, self.manifest)
        rel = {part.lower() for part in attrs.get("rel", "").split()}
        text = re.sub(r"\s+", " ", " ".join(self.anchor["text"])).strip()[:2000]
        self.ordinal += 1
        digest = hashlib.sha256(
            f"{self.source_path}\0{self.anchor['line']}\0{self.ordinal}\0{raw}".encode("utf-8")
        ).hexdigest()[:24]
        self.links.append(LinkOccurrence(
            occurrence_key=f"html:{digest}",
            source_fact_key=f"page:{self.route}",
            raw_destination=raw,
            canonical_destination=canonical,
            anchor_text=text,
            context_excerpt=text[:500],
            source_location=f"{self.source_path}:{self.anchor['line']}",
            landmark=landmark,
            layer=layer,
            confidence=confidence,
            repeated_template=layer in {"menu", "utility"},
            crawlable=crawlable and "nofollow" not in rel,
            nofollow="nofollow" in rel,
            external=external,
            fragment=fragment,
            action_kind=action_kind or ("cta" if layer == "action" else None),
            evidence={"source": "html", "explicit": attrs.get(self.manifest.link_layers.explicit_attribute)},
        ))
        self.anchor = None


def _html_layer(
    attrs: dict[str, str], ancestors: list[tuple[str, dict[str, str]]], manifest: SiteGraphManifest
) -> tuple[str, float, str]:
    explicit = attrs.get(manifest.link_layers.explicit_attribute)
    if explicit in {"menu", "breadcrumb", "contextual", "related", "action", "utility"}:
        return explicit, 1.0, explicit
    tags = {tag for tag, _ in ancestors}
    ancestor_attrs = [attributes for _, attributes in ancestors]
    aria = " ".join(attributes.get("aria-label", "") for attributes in ancestor_attrs).lower()
    if "breadcrumb" in aria:
        return "breadcrumb", 0.99, "navigation"
    if any("data-related-content" in attributes for attributes in ancestor_attrs):
        return "related", 0.98, "main"
    if "data-cta" in attrs or any("data-cta" in attributes for attributes in ancestor_attrs):
        return "action", 0.99, "main"
    if "footer" in tags:
        return "utility", 0.98, "footer"
    if "nav" in tags or "header" in tags:
        return "menu", 0.98, "navigation"
    return "contextual", 0.75, "main" if "main" in tags else "content"


def _html_route(path: str) -> str:
    pure = PurePosixPath(path)
    if pure.name.lower() in {"index.html", "index.htm"}:
        parent = pure.parent.as_posix()
        return "/" if parent == "." else f"/{parent.strip('/')}/"
    return f"/{pure.with_suffix('').as_posix().strip('/')}/"


def _read_source(repository: Path, revision: str, entry: TrackedFile, total: list[int]) -> bytes:
    raw = _git_bytes(repository, "show", f"{revision}:{entry.path}")
    if len(raw) > MAX_SOURCE_FILE_BYTES:
        raise IngestError(f"source file exceeds {MAX_SOURCE_FILE_BYTES} bytes: {entry.path}")
    total[0] += len(raw)
    if total[0] > MAX_SOURCE_BYTES:
        raise IngestError(f"source extraction exceeds {MAX_SOURCE_BYTES} bytes")
    return raw


def _extract_static(
    repository: Path, revision: str, entries: list[TrackedFile], manifest: SiteGraphManifest
) -> tuple[list[PageFact], list[LinkOccurrence], dict[str, Any]]:
    pages: list[PageFact] = []
    links: list[LinkOccurrence] = []
    total = [0]
    html_entries = [entry for entry in entries if Path(entry.path).suffix.lower() in {".html", ".htm"}]
    for entry in html_entries:
        route = _normalize_route(_html_route(entry.path), manifest)
        if not _route_allowed(route, manifest):
            continue
        raw = _read_source(repository, revision, entry, total)
        parser = _HTMLFacts(route=route, source_path=entry.path, manifest=manifest)
        try:
            parser.feed(raw.decode("utf-8"))
            parser.close()
        except UnicodeError as exc:
            raise IngestError(f"HTML source is not UTF-8: {entry.path}") from exc
        pages.append(PageFact(
            f"page:{route}", route, f"https://{manifest.site.canonical_hosts[0]}{route}", entry.path,
            _page_evidence(route, entry.path, manifest, "static-html", 1.0), hashlib.sha256(raw).hexdigest(),
        ))
        links.extend(parser.links)
    if not pages:
        raise IngestError("static-html adapter found no included HTML pages")
    return pages, links, {"source_files": len(html_entries), "source_bytes": total[0], "route_evidence": "exact-html"}


def _source_route(path: str) -> str | None:
    pure = PurePosixPath(path)
    parts = list(pure.parts)
    if not parts or parts[0] not in {"app", "src"} or pure.name not in {"page.ts", "page.tsx", "page.js", "page.jsx", "page.mdx"}:
        return None
    segments = [part for part in parts[1:-1] if not (part.startswith("(") and part.endswith(")"))]
    if any(part.startswith("[") for part in segments):
        return None
    return "/" if not segments else "/" + "/".join(segments) + "/"


def _typescript_layer(path: str, context: str) -> tuple[str, float, str, bool]:
    lowered_path = path.lower()
    lowered = context.lower()
    if "breadcrumb" in lowered_path or "breadcrumb" in lowered:
        return "breadcrumb", 0.9, "navigation", False
    if "navigation" in lowered_path or any(name in lowered_path for name in ("desktopnavigation", "mobilemenu", "sectionnavigation")):
        return "menu", 0.94, "navigation", True
    if "footer" in lowered or "sitechrome" in lowered_path and "footer" in lowered:
        return "utility", 0.88, "footer", True
    if "primarycta" in lowered or "secondarycta" in lowered or "linklabel" in lowered or "data-cta" in lowered:
        return "action", 0.88, "main", False
    if "related" in lowered:
        return "related", 0.82, "main", False
    return "contextual", 0.7, "source", False


def _typescript_route_symbols(text: str) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    symbols: dict[str, str] = {}
    object_routes: dict[str, dict[str, str]] = {}
    for match in SIMPLE_ROUTE_SYMBOL.finditer(text):
        symbols.setdefault(match.group("name"), match.group("value")[:4000])
    for block in OBJECT_ROUTE_BLOCK.finditer(text):
        routes: dict[str, str] = {}
        for prop in OBJECT_ROUTE_PROPERTY.finditer(block.group("body")):
            key = prop.group("key").strip("\"'")
            routes.setdefault(key, prop.group("value")[:4000])
        if routes:
            object_routes.setdefault(block.group("name"), routes)
    return symbols, object_routes


def _resolve_route_expression(
    expression: str, symbols: dict[str, str], object_routes: dict[str, dict[str, str]]
) -> list[str]:
    stripped = expression.strip().strip("()")
    if not stripped:
        return []
    if stripped[0] in {"'", '"', "`"} and stripped[-1:] == stripped[0]:
        return [stripped[1:-1]]
    if stripped in symbols:
        return [symbols[stripped]]
    values: list[str] = []
    for object_name, key in re.findall(r"\b([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\b", stripped):
        value = object_routes.get(object_name, {}).get(key)
        if value:
            values.append(value)
    for object_name, key in re.findall(r"\b([A-Za-z_$][\w$]*)\[['\"]([^'\"]+)['\"]\]", stripped):
        value = object_routes.get(object_name, {}).get(key)
        if value:
            values.append(value)
    for token in re.split(r"\s*(?:\?\?|&&|\|\||,)\s*", stripped):
        if token in symbols:
            values.append(symbols[token])
    return values


def _typescript_link_candidates(
    line: str, symbols: dict[str, str], object_routes: dict[str, dict[str, str]]
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []

    def add(value: str, source: str, label: str = "") -> None:
        cleaned = value.strip().strip("\"'").strip()
        if cleaned:
            candidates.append({"value": cleaned[:4000], "source": source, "label": label[:500]})

    for source, pattern in (
        ("object-field", ROUTE_LITERAL),
        ("jsx-literal", JSX_HREF),
        ("router-literal", ROUTER_LITERAL),
    ):
        for match in pattern.finditer(line):
            add(match.group("value"), source)
    for match in MARKDOWN_LINK.finditer(line):
        add(match.group("value"), "markdown", match.group("label"))
    for source, pattern in (("jsx-expression", JSX_ROUTE_EXPRESSION), ("router-expression", ROUTER_EXPRESSION)):
        for match in pattern.finditer(line):
            expression = match.group("expr")
            for value in _resolve_route_expression(expression, symbols, object_routes):
                add(value, source)
    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate["value"]
        if key not in seen:
            deduped.append(candidate)
            seen.add(key)
    return deduped


def _extract_vinext(
    repository: Path, revision: str, entries: list[TrackedFile], manifest: SiteGraphManifest
) -> tuple[list[PageFact], list[LinkOccurrence], dict[str, Any]]:
    total = [0]
    retired_routes: set[str] = set()
    for entry in entries:
        if entry.path.endswith("app/content/publicPages.ts"):
            policy_text = _read_source(repository, revision, entry, total).decode("utf-8", "strict")
            retired_routes.update(
                _normalize_route(match, manifest)
                for match in re.findall(r"[\"'](/[^\"']+/)[\"']", policy_text)
            )
    candidates = [
        entry for entry in entries
        if Path(entry.path).suffix.lower() in SOURCE_EXTENSIONS
        and (entry.path.startswith("app/") or entry.path.startswith("src/"))
        and not entry.path.endswith("content/publicPages.ts")
    ]
    sources: dict[str, str] = {}
    route_sources: dict[str, tuple[str, float, str]] = {"/": ("app/page.tsx", 0.95, "")}
    raw_occurrences: list[dict[str, Any]] = []
    global_occurrences: list[dict[str, Any]] = []
    for entry in candidates:
        raw = _read_source(repository, revision, entry, total)
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise IngestError(f"source is not UTF-8: {entry.path}") from exc
        sources[entry.path] = text
        symbols, object_routes = _typescript_route_symbols(text)
        file_route = _source_route(entry.path)
        if file_route:
            route_sources.setdefault(_normalize_route(file_route, manifest), (entry.path, 1.0, text))
        current_route = file_route
        current_route_excluded = current_route in retired_routes
        recent: list[str] = []
        last_label = ""
        for line_number, line in enumerate(text.splitlines(), 1):
            recent.append(line)
            recent = recent[-8:]
            label_match = LABEL_LITERAL.search(line)
            if label_match:
                last_label = label_match.group("value")[:500]
            route_matches = list(ROUTE_LITERAL.finditer(line))
            for match in route_matches:
                value = match.group("value")
                if match.group(0).lstrip().startswith("slug") and value.startswith("/"):
                    current_route = _normalize_route(urlsplit(value).path, manifest)
                    current_route_excluded = current_route in retired_routes
                    if not current_route_excluded:
                        route_sources.setdefault(current_route, (entry.path, 0.96, text))
            link_candidates = _typescript_link_candidates(line, symbols, object_routes)
            for candidate in link_candidates:
                raw_destination = candidate["value"]
                if not raw_destination.startswith(("/", "#", "http://", "https://", "mailto:", "tel:")):
                    continue
                if current_route_excluded:
                    continue
                if raw_destination.startswith("/") and not raw_destination.startswith("//"):
                    destination_route = _normalize_route(urlsplit(raw_destination).path or "/", manifest)
                    if _route_allowed(destination_route, manifest):
                        route_sources.setdefault(destination_route, (entry.path, 0.72, text))
                context = "\n".join(recent)
                layer, confidence, landmark, repeated = _typescript_layer(entry.path, context)
                occurrence = {
                    "source": current_route,
                    "raw": raw_destination,
                    "label": candidate["label"] or last_label,
                    "line": line_number,
                    "path": entry.path,
                    "context": re.sub(r"\s+", " ", context).strip()[-500:],
                    "layer": layer,
                    "confidence": confidence,
                    "landmark": landmark,
                    "repeated": repeated,
                    "source_kind": candidate["source"],
                }
                (global_occurrences if current_route is None and repeated else raw_occurrences).append(occurrence)
    routes = sorted(
        route for route in route_sources
        if route not in retired_routes and _route_allowed(route, manifest)
    )[:manifest.analysis.maximum_pages]
    if not routes:
        raise IngestError("vinext adapter found no included source routes")
    pages: list[PageFact] = []
    for route in routes:
        source_path, confidence, source_text = route_sources[route]
        pages.append(PageFact(
            f"page:{route}", route, f"https://{manifest.site.canonical_hosts[0]}{route}", source_path,
            _page_evidence(route, source_path, manifest, "vinext", confidence),
            hashlib.sha256((route + "\0" + source_text).encode("utf-8")).hexdigest(),
        ))
    route_set = set(routes)
    expanded_count = len(raw_occurrences) + len(global_occurrences) * len(routes)
    if expanded_count > MAX_LINKS:
        raise IngestError(f"source extraction exceeds {MAX_LINKS} link occurrences")
    expanded = raw_occurrences + [
        {**occurrence, "source": route}
        for occurrence in global_occurrences
        for route in routes
    ]
    links: list[LinkOccurrence] = []
    for ordinal, occurrence in enumerate(expanded, 1):
        source = occurrence["source"] or "/"
        if source not in route_set:
            source = "/"
        canonical, crawlable, fragment, external, action_kind = _destination(occurrence["raw"], source, manifest)
        digest = hashlib.sha256(
            f"{occurrence['path']}\0{occurrence['line']}\0{source}\0{ordinal}\0{occurrence['raw']}".encode("utf-8")
        ).hexdigest()[:24]
        links.append(LinkOccurrence(
            occurrence_key=f"vinext:{digest}",
            source_fact_key=f"page:{source}",
            raw_destination=occurrence["raw"],
            canonical_destination=canonical,
            anchor_text=occurrence["label"],
            context_excerpt=occurrence["context"],
            source_location=f"{occurrence['path']}:{occurrence['line']}",
            landmark=occurrence["landmark"],
            layer=occurrence["layer"],
            confidence=occurrence["confidence"],
            repeated_template=occurrence["repeated"],
            crawlable=crawlable,
            nofollow=False,
            external=external,
            fragment=fragment,
            action_kind=action_kind or ("cta" if occurrence["layer"] == "action" else None),
            evidence={
                "source": "typescript",
                "classification": "bounded-source-heuristic",
                "extractor": occurrence.get("source_kind", "unknown"),
            },
        ))
        if len(links) > MAX_LINKS:
            raise IngestError(f"source extraction exceeds {MAX_LINKS} link occurrences")
    return pages, links, {
        "source_files": len(candidates),
        "source_bytes": total[0],
        "route_evidence": "app-router-and-content-literals",
        "classification": "bounded-source-heuristic",
        "ambiguous_links": sum(1 for link in links if link.confidence < 0.8),
        "build_output": "not-executed-source-only",
        "retired_source_routes": len(retired_routes),
    }


def _stored_fact_hash(store: SiteGraphStore, repository_snapshot_id: str) -> str:
    with store.connect(readonly=True) as db:
        page_hashes = [
            row["record_hash"] for row in db.execute(
                "SELECT record_hash FROM site_graph_page_facts WHERE repository_snapshot_id=? ORDER BY fact_key,id",
                (repository_snapshot_id,),
            )
        ]
        link_hashes = [
            row["record_hash"] for row in db.execute(
                "SELECT record_hash FROM site_graph_link_occurrences WHERE repository_snapshot_id=? ORDER BY occurrence_key,id",
                (repository_snapshot_id,),
            )
        ]
    return hashlib.sha256(
        json.dumps({"pages": page_hashes, "links": link_hashes}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _existing_result(
    store: SiteGraphStore, manifest: SiteGraphManifest, inspection: RepositoryInspection
) -> IngestResult | None:
    with store.connect(readonly=True) as db:
        row = db.execute(
            """SELECT r.id,r.revision,r.content_hash
               FROM site_graph_repository_snapshots r
               JOIN site_graph_ingest_runs i ON i.id=r.ingest_run_id
               JOIN site_graph_manifest_versions m ON m.id=i.manifest_version_id
               WHERE m.manifest_hash=? AND i.site_key=? AND i.analysis_mode=?
                 AND i.status='succeeded' AND r.revision=? AND r.content_hash=?
               ORDER BY r.captured_at DESC LIMIT 1""",
            (manifest.manifest_hash, manifest.site.key, manifest.analysis.mode, inspection.revision, inspection.content_hash),
        ).fetchone()
        if row is None:
            return None
        pages = db.execute(
            "SELECT fact_key FROM site_graph_page_facts WHERE repository_snapshot_id=? ORDER BY fact_key",
            (row["id"],),
        ).fetchall()
        links = db.execute(
            "SELECT occurrence_key,layer FROM site_graph_link_occurrences WHERE repository_snapshot_id=? ORDER BY occurrence_key",
            (row["id"],),
        ).fetchall()
        layers: dict[str, int] = {}
        for link in links:
            layers[link["layer"]] = layers.get(link["layer"], 0) + 1
        digest = _stored_fact_hash(store, row["id"])
    return IngestResult(
        manifest.site.key, row["id"], inspection.revision, inspection.adapter, manifest.analysis.mode,
        len(pages), len(links), layers, digest, True, {"source": "existing-immutable-snapshot"},
    )


def ingest_repository(
    store: SiteGraphStore,
    manifest: SiteGraphManifest,
    *,
    allow_dirty_snapshot: bool = False,
) -> IngestResult:
    inspection = inspect_repository(manifest, allow_dirty_snapshot=allow_dirty_snapshot)
    existing = _existing_result(store, manifest, inspection)
    if existing is not None:
        return existing
    repository = Path(manifest.repository.local_path).expanduser().resolve()
    entries, _ = _tracked_files(repository, inspection.revision)
    if inspection.adapter == "static-html":
        pages, links, coverage = _extract_static(repository, inspection.revision, entries, manifest)
    elif inspection.adapter == "vinext":
        pages, links, coverage = _extract_vinext(repository, inspection.revision, entries, manifest)
    else:
        raise IngestError(f"adapter {inspection.adapter} is detected but extraction is not implemented")
    pages = sorted(pages, key=lambda item: item.fact_key)
    links = sorted(links, key=lambda item: item.occurrence_key)
    manifest_id = store.save_manifest(manifest)
    run_id = store.start_ingest(
        manifest_version_id=manifest_id,
        site_key=manifest.site.key,
        analysis_mode=manifest.analysis.mode,
    )
    try:
        snapshot_id = store.save_repository_snapshot(
            ingest_run_id=run_id,
            site_key=manifest.site.key,
            repository_identity=inspection.repository_identity,
            remote_url=inspection.remote_url,
            revision=inspection.revision,
            ref=inspection.ref,
            clean=inspection.clean,
            content_hash=inspection.content_hash,
        )
        store.save_fact_batch(snapshot_id, pages=pages, links=links)
        store.finish_ingest(run_id, status="succeeded")
    except Exception:
        try:
            store.finish_ingest(run_id, status="failed")
        except Exception:
            pass
        raise
    fact_hash = _stored_fact_hash(store, snapshot_id)
    layers: dict[str, int] = {}
    for link in links:
        layers[link.layer] = layers.get(link.layer, 0) + 1
    coverage = {
        **coverage,
        "adapter_version": inspection.adapter_version,
        "manifest_hash": manifest.manifest_hash,
        "tracked_files": inspection.tracked_files,
        "excluded_files": inspection.tracked_files - coverage.get("source_files", 0),
        "dirty_override": inspection.dirty_override,
    }
    return IngestResult(
        manifest.site.key, snapshot_id, inspection.revision, inspection.adapter, manifest.analysis.mode,
        len(pages), len(links), layers, fact_hash, False, coverage,
    )
