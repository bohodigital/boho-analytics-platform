"""Strict, deterministic site-graph manifest parsing."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

import yaml
from yaml.tokens import AliasToken, AnchorToken, TagToken


MAX_MANIFEST_BYTES = 256 * 1024
MAX_PAGES = 100_000
IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
COMMIT = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
LINK_LAYERS = ("menu", "breadcrumb", "contextual", "related", "action", "utility")
SECRET_KEY_PARTS = ("password", "secret", "token", "credential", "private_key")


class ManifestError(ValueError):
    """A manifest failed structural or security validation."""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ManifestError("manifest keys must be strings")
        if key in mapping:
            raise ManifestError(f"duplicate key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


@dataclass(frozen=True)
class Site:
    key: str
    display_name: str
    canonical_hosts: tuple[str, ...]


@dataclass(frozen=True)
class Repository:
    local_path: str
    expected_remote: str
    ref: str
    expected_commit: str | None
    require_clean: bool


@dataclass(frozen=True)
class Analysis:
    mode: str
    adapter: str
    include_drafts: bool
    maximum_pages: int


@dataclass(frozen=True)
class Build:
    enabled: bool
    adapter_command: None
    output_directory: str | None


@dataclass(frozen=True)
class CloudflarePages:
    enabled: bool
    account_id_ref: str | None
    project_name: str | None
    expected_production_branch: str | None


@dataclass(frozen=True)
class Routes:
    include: tuple[str, ...]
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class Canonicalization:
    normalize_trailing_slash: bool
    strip_fragments: bool
    remove_query_parameters: bool


@dataclass(frozen=True)
class PageRule:
    id: str
    path_regex: str
    roles: tuple[str, ...]
    journey_stage: int


@dataclass(frozen=True)
class LinkLayers:
    explicit_attribute: str
    selectors: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Goal:
    id: str
    kind: str
    paths: tuple[str, ...]
    roles: tuple[str, ...]


@dataclass(frozen=True)
class SiteGraphManifest:
    schema_version: int
    site: Site
    repository: Repository
    analysis: Analysis
    build: Build
    cloudflare_pages: CloudflarePages
    routes: Routes
    canonicalization: Canonicalization
    page_rules: tuple[PageRule, ...]
    link_layers: LinkLayers
    goals: tuple[Goal, ...]
    manifest_hash: str
    canonical_json: str

    def sanitized_summary(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "manifest_hash": self.manifest_hash,
            "site_key": self.site.key,
            "canonical_hosts": list(self.site.canonical_hosts),
            "analysis_mode": self.analysis.mode,
            "adapter": self.analysis.adapter,
            "maximum_pages": self.analysis.maximum_pages,
            "page_rule_ids": [rule.id for rule in self.page_rules],
            "goal_ids": [goal.id for goal in self.goals],
            "cloudflare_pages_enabled": self.cloudflare_pages.enabled,
        }


def _mapping(value: Any, where: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{where} must be a mapping")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ManifestError(f"unknown field in {where}: {unknown[0]}")
    return value


def _required(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ManifestError(f"missing required field {where}.{key}")
    return mapping[key]


def _string(value: Any, where: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ManifestError(f"{where} must be a non-empty string of at most {maximum} characters")
    if "\x00" in value or any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise ManifestError(f"{where} contains control characters")
    return value.strip()


def _optional_string(value: Any, where: str, *, maximum: int = 500) -> str | None:
    return None if value is None else _string(value, where, maximum=maximum)


def _boolean(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError(f"{where} must be true or false")
    return value


def _integer(value: Any, where: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ManifestError(f"{where} must be an integer from {minimum} to {maximum}")
    return value


def _identifier(value: Any, where: str) -> str:
    result = _string(value, where, maximum=100)
    if not IDENTIFIER.fullmatch(result):
        raise ManifestError(f"{where} must be a lowercase identifier")
    return result


def _string_list(value: Any, where: str, *, minimum: int = 1, maximum: int = 100) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ManifestError(f"{where} must contain from {minimum} to {maximum} strings")
    values = tuple(_string(item, f"{where}[]") for item in value)
    if len(set(values)) != len(values):
        raise ManifestError(f"{where} contains duplicate values")
    return values


def _reject_secret_fields(value: Any, where: str = "manifest") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in SECRET_KEY_PARTS):
                raise ManifestError(f"secret field is not allowed in {where}: {key}")
            _reject_secret_fields(nested, f"{where}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_secret_fields(nested, f"{where}[{index}]")


def _absolute_path(value: Any, where: str) -> str:
    result = _string(value, where, maximum=1000)
    posix = PurePosixPath(result)
    windows = PureWindowsPath(result)
    if not (posix.is_absolute() or windows.is_absolute()) or ".." in posix.parts or ".." in windows.parts:
        raise ManifestError(f"{where} must be an absolute path without parent traversal")
    return result


def _relative_path(value: Any, where: str) -> str:
    result = _string(value, where, maximum=500)
    posix = PurePosixPath(result)
    windows = PureWindowsPath(result)
    if posix.is_absolute() or windows.is_absolute() or ".." in posix.parts or ".." in windows.parts:
        raise ManifestError(f"{where} must be a safe relative path")
    return result


def validate_repository_remote(value: Any, where: str = "repository.expected_remote") -> str:
    """Return a normalized public Git remote without embedded secrets."""
    result = _string(value, where, maximum=1000)
    if result.startswith("https://"):
        parsed = urlsplit(result)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ManifestError(f"{where} must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ManifestError(f"{where} must not contain query parameters or fragments")
        return result
    if result.startswith("ssh://"):
        parsed = urlsplit(result)
        if not parsed.hostname or parsed.password is not None or parsed.username not in {None, "git"}:
            raise ManifestError(f"{where} must not contain credentials")
        if parsed.query or parsed.fragment or not parsed.path or parsed.path == "/":
            raise ManifestError(f"{where} must identify an SSH repository path")
        return result
    if re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9._/-]+", result):
        return result
    raise ManifestError(f"{where} must be an HTTPS or SSH repository URL without credentials")


def _route_patterns(value: Any, where: str) -> tuple[str, ...]:
    patterns = _string_list(value, where, minimum=1 if where.endswith("include") else 0)
    if any(not pattern.startswith("/") or "\x00" in pattern for pattern in patterns):
        raise ManifestError(f"{where} entries must be absolute route patterns")
    return patterns


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def _reject_unsafe_regex(pattern: str, where: str) -> None:
    dangerous_group = re.search(r"\([^()]*(?:[+*{]|\|)[^()]*\)[+*?{]", pattern)
    repeated_wildcard = re.search(r"\.\*[^\n]{0,40}\.\*", pattern)
    if "(?" in pattern or re.search(r"\\[1-9]", pattern) or dangerous_group or repeated_wildcard:
        raise ManifestError(f"unsafe regular expression in {where}; use a linear path expression")


def _parse_site(data: Any) -> Site:
    item = _mapping(data, "site", {"key", "display_name", "canonical_hosts"})
    hosts = _string_list(_required(item, "canonical_hosts", "site"), "site.canonical_hosts", maximum=20)
    normalized: list[str] = []
    for host in hosts:
        if host != host.lower() or "://" in host or "/" in host or not re.fullmatch(r"[a-z0-9.-]+", host):
            raise ManifestError("site.canonical_hosts entries must be lowercase host names")
        normalized.append(host)
    return Site(
        _identifier(_required(item, "key", "site"), "site.key"),
        _string(_required(item, "display_name", "site"), "site.display_name", maximum=200),
        tuple(normalized),
    )


def _parse_repository(data: Any) -> Repository:
    item = _mapping(data, "repository", {"local_path", "expected_remote", "ref", "expected_commit", "require_clean"})
    ref = _string(_required(item, "ref", "repository"), "repository.ref", maximum=255)
    if not SAFE_REF.fullmatch(ref) or ".." in ref or ref.endswith(("/", ".")):
        raise ManifestError("repository.ref is not a safe Git ref")
    commit = item.get("expected_commit")
    if commit is not None and (not isinstance(commit, str) or not COMMIT.fullmatch(commit)):
        raise ManifestError("repository.expected_commit must be a full 40- or 64-character hexadecimal commit")
    return Repository(
        _absolute_path(_required(item, "local_path", "repository"), "repository.local_path"),
        validate_repository_remote(_required(item, "expected_remote", "repository")),
        ref,
        commit.lower() if commit else None,
        _boolean(_required(item, "require_clean", "repository"), "repository.require_clean"),
    )


def _parse_analysis(data: Any) -> Analysis:
    item = _mapping(data, "analysis", {"mode", "adapter", "include_drafts", "maximum_pages"})
    mode = _string(_required(item, "mode", "analysis"), "analysis.mode")
    adapter = _string(_required(item, "adapter", "analysis"), "analysis.adapter")
    if mode not in {"source-only", "build"}:
        raise ManifestError("analysis.mode must be source-only or build")
    if adapter not in {"auto", "static-html", "astro", "vinext"}:
        raise ManifestError("analysis.adapter is not supported by schema v1")
    return Analysis(
        mode,
        adapter,
        _boolean(_required(item, "include_drafts", "analysis"), "analysis.include_drafts"),
        _integer(_required(item, "maximum_pages", "analysis"), "analysis.maximum_pages", 1, MAX_PAGES),
    )


def _parse_build(data: Any, analysis: Analysis) -> Build:
    item = _mapping(data, "build", {"enabled", "adapter_command", "output_directory"})
    enabled = _boolean(_required(item, "enabled", "build"), "build.enabled")
    if item.get("adapter_command") is not None:
        raise ManifestError("build.adapter_command must be null; build execution is adapter-owned")
    output = _optional_string(item.get("output_directory"), "build.output_directory")
    if output is not None:
        output = _relative_path(output, "build.output_directory")
    if analysis.mode == "source-only" and enabled:
        raise ManifestError("build.enabled cannot be true when analysis.mode is source-only")
    if analysis.mode == "build" and (not enabled or output is None):
        raise ManifestError("analysis.mode build requires build.enabled and build.output_directory")
    if not enabled and output is not None:
        raise ManifestError("build.output_directory must be null when build.enabled is false")
    return Build(enabled, None, output)


def _parse_cloudflare(data: Any) -> CloudflarePages:
    item = _mapping(data, "cloudflare_pages", {"enabled", "account_id_ref", "project_name", "expected_production_branch"})
    enabled = _boolean(_required(item, "enabled", "cloudflare_pages"), "cloudflare_pages.enabled")
    result = CloudflarePages(
        enabled,
        _optional_string(item.get("account_id_ref"), "cloudflare_pages.account_id_ref", maximum=200),
        _optional_string(item.get("project_name"), "cloudflare_pages.project_name", maximum=200),
        _optional_string(item.get("expected_production_branch"), "cloudflare_pages.expected_production_branch", maximum=255),
    )
    if enabled and not all((result.account_id_ref, result.project_name, result.expected_production_branch)):
        raise ManifestError("enabled cloudflare_pages requires all read-only reference fields")
    return result


def _parse_routes(data: Any) -> Routes:
    item = _mapping(data, "routes", {"include", "exclude"})
    return Routes(
        _route_patterns(_required(item, "include", "routes"), "routes.include"),
        _route_patterns(_required(item, "exclude", "routes"), "routes.exclude"),
    )


def _parse_canonicalization(data: Any) -> Canonicalization:
    item = _mapping(data, "canonicalization", {"normalize_trailing_slash", "strip_fragments", "remove_query_parameters"})
    return Canonicalization(*(
        _boolean(_required(item, key, "canonicalization"), f"canonicalization.{key}")
        for key in ("normalize_trailing_slash", "strip_fragments", "remove_query_parameters")
    ))


def _parse_page_rules(data: Any) -> tuple[PageRule, ...]:
    if not isinstance(data, list) or not 1 <= len(data) <= 500:
        raise ManifestError("page_rules must contain from 1 to 500 rules")
    rules: list[PageRule] = []
    ids: set[str] = set()
    for index, raw in enumerate(data):
        where = f"page_rules[{index}]"
        item = _mapping(raw, where, {"id", "path_regex", "roles", "journey_stage"})
        rule_id = _identifier(_required(item, "id", where), f"{where}.id")
        if rule_id in ids:
            raise ManifestError(f"duplicate page rule id: {rule_id}")
        ids.add(rule_id)
        pattern = _string(_required(item, "path_regex", where), f"{where}.path_regex", maximum=500)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ManifestError(f"invalid regular expression in {where}.path_regex") from exc
        _reject_unsafe_regex(pattern, f"{where}.path_regex")
        roles = tuple(_identifier(role, f"{where}.roles[]") for role in _string_list(_required(item, "roles", where), f"{where}.roles"))
        rules.append(PageRule(rule_id, pattern, roles, _integer(_required(item, "journey_stage", where), f"{where}.journey_stage", 1, 5)))
    return tuple(rules)


def _parse_link_layers(data: Any) -> LinkLayers:
    item = _mapping(data, "link_layers", {"explicit_attribute", "selectors"})
    explicit = _string(_required(item, "explicit_attribute", "link_layers"), "link_layers.explicit_attribute", maximum=100)
    if not re.fullmatch(r"data-[a-z0-9-]+", explicit):
        raise ManifestError("link_layers.explicit_attribute must be a data-* attribute")
    selectors = _mapping(_required(item, "selectors", "link_layers"), "link_layers.selectors", set(LINK_LAYERS))
    if set(selectors) != set(LINK_LAYERS):
        missing = sorted(set(LINK_LAYERS) - set(selectors))[0]
        raise ManifestError(f"missing required field link_layers.selectors.{missing}")
    parsed = {layer: _string_list(selectors[layer], f"link_layers.selectors.{layer}", maximum=50) for layer in LINK_LAYERS}
    return LinkLayers(explicit, parsed)


def _parse_goals(data: Any, page_rules: tuple[PageRule, ...], routes: Routes) -> tuple[Goal, ...]:
    if not isinstance(data, list) or not 1 <= len(data) <= 500:
        raise ManifestError("goals must contain from 1 to 500 goals")
    known_roles = {role for rule in page_rules for role in rule.roles}
    ids: set[str] = set()
    goals: list[Goal] = []
    for index, raw in enumerate(data):
        where = f"goals[{index}]"
        item = _mapping(raw, where, {"id", "kind", "paths", "roles"})
        goal_id = _identifier(_required(item, "id", where), f"{where}.id")
        if goal_id in ids:
            raise ManifestError(f"duplicate goal id: {goal_id}")
        ids.add(goal_id)
        kind = _string(_required(item, "kind", where), f"{where}.kind")
        if kind not in {"page", "role"}:
            raise ManifestError(f"{where}.kind must be page or role")
        paths = _route_patterns(item.get("paths", []), f"{where}.paths") if kind == "page" else ()
        roles = tuple(_identifier(role, f"{where}.roles[]") for role in _string_list(item.get("roles", []), f"{where}.roles")) if kind == "role" else ()
        if kind == "page" and not paths:
            raise ManifestError(f"goal {goal_id} requires at least one path")
        if kind == "page" and "roles" in item:
            raise ManifestError(f"unknown field in {where}: roles")
        if kind == "role" and "paths" in item:
            raise ManifestError(f"unknown field in {where}: paths")
        for path in paths:
            if _matches(path, routes.exclude):
                raise ManifestError(f"goal {goal_id} resolves to an excluded route")
            if not _matches(path, routes.include):
                raise ManifestError(f"goal {goal_id} is outside included routes")
        unknown_roles = sorted(set(roles) - known_roles)
        if unknown_roles:
            raise ManifestError(f"goal {goal_id} references unknown role: {unknown_roles[0]}")
        goals.append(Goal(goal_id, kind, paths, roles))
    return tuple(goals)


def load_manifest_text(text: str) -> SiteGraphManifest:
    if not isinstance(text, str):
        raise ManifestError("manifest text must be a string")
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ManifestError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    try:
        if any(isinstance(token, (AliasToken, AnchorToken, TagToken)) for token in yaml.scan(text)):
            raise ManifestError("YAML aliases, anchors, and explicit tags are not allowed")
        raw = yaml.load(text, Loader=_UniqueKeyLoader)
    except ManifestError:
        raise
    except yaml.YAMLError as exc:
        raise ManifestError("manifest is not valid YAML or contains an invalid regular expression") from exc
    root = _mapping(raw, "manifest", {
        "schema_version", "site", "repository", "analysis", "build", "cloudflare_pages",
        "routes", "canonicalization", "page_rules", "link_layers", "goals",
    })
    _reject_secret_fields(root)
    version = _integer(_required(root, "schema_version", "manifest"), "schema_version", 1, 1)
    site = _parse_site(_required(root, "site", "manifest"))
    repository = _parse_repository(_required(root, "repository", "manifest"))
    analysis = _parse_analysis(_required(root, "analysis", "manifest"))
    build = _parse_build(_required(root, "build", "manifest"), analysis)
    cloudflare = _parse_cloudflare(_required(root, "cloudflare_pages", "manifest"))
    routes = _parse_routes(_required(root, "routes", "manifest"))
    canonicalization = _parse_canonicalization(_required(root, "canonicalization", "manifest"))
    page_rules = _parse_page_rules(_required(root, "page_rules", "manifest"))
    link_layers = _parse_link_layers(_required(root, "link_layers", "manifest"))
    goals = _parse_goals(_required(root, "goals", "manifest"), page_rules, routes)
    canonical = {
        "schema_version": version,
        "site": asdict(site),
        "repository": asdict(repository),
        "analysis": asdict(analysis),
        "build": asdict(build),
        "cloudflare_pages": asdict(cloudflare),
        "routes": asdict(routes),
        "canonicalization": asdict(canonicalization),
        "page_rules": [asdict(rule) for rule in page_rules],
        "link_layers": asdict(link_layers),
        "goals": [asdict(goal) for goal in goals],
    }
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    manifest_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return SiteGraphManifest(
        version, site, repository, analysis, build, cloudflare, routes, canonicalization,
        page_rules, link_layers, goals, manifest_hash, canonical_json,
    )


def load_manifest(path: str | Path) -> SiteGraphManifest:
    manifest_path = Path(path)
    try:
        size = manifest_path.stat().st_size
        if size > MAX_MANIFEST_BYTES:
            raise ManifestError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
        return load_manifest_text(manifest_path.read_text(encoding="utf-8"))
    except ManifestError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ManifestError(f"cannot read manifest: {manifest_path.name}") from exc
