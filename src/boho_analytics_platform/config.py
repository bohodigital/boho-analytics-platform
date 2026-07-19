"""Strict, non-secret configuration loading for schema version 2."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .catalog import METRICS


SCHEMA_VERSION = 2
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_REFERENCE_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_FORBIDDEN_INLINE_KEYS = {
    "accesstoken", "apikey", "authorization", "bearer", "clientsecret", "cookie",
    "password", "privatekey", "refreshtoken", "secret", "token"
}


class ConfigError(ValueError):
    """Raised when a configuration file violates the public schema."""


@dataclass(frozen=True, slots=True)
class PlatformConfig:
    default_timezone: str
    state_path: str
    default_sync_days: int = 30
    http_timeout_seconds: int = 30
    max_response_bytes: int = 5_000_000


@dataclass(frozen=True, slots=True)
class WebConfig:
    bind_host: str = "127.0.0.1"
    port: int = 8787
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    auth_mode: str = "none"
    username: str | None = None
    auth_credential_ref: str | None = None


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    hourly_days: int = 90
    daily_days: int = 1095


@dataclass(frozen=True, slots=True)
class ClientConfig:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class SiteConfig:
    id: str
    client_id: str
    name: str
    canonical_url: str
    timezone: str


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    id: str
    provider: str
    credential_ref: str
    options: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class BindingConfig:
    site_id: str
    connection_id: str
    resource_type: str
    resource_id: str
    metric_groups: tuple[str, ...] = ()
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SubreportConfig:
    id: str
    title: str
    metric_ids: tuple[str, ...]
    default_window_days: int
    filters: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ReportConfig:
    id: str
    title: str
    client_id: str
    site_ids: tuple[str, ...]
    metric_ids: tuple[str, ...]
    default_window_days: int
    subreports: tuple[SubreportConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class AppConfig:
    schema_version: int
    platform: PlatformConfig
    web: WebConfig
    retention: RetentionConfig
    clients: tuple[ClientConfig, ...]
    sites: tuple[SiteConfig, ...]
    connections: tuple[ConnectionConfig, ...]
    bindings: tuple[BindingConfig, ...]
    reports: tuple[ReportConfig, ...]
    source_path: Path

    def resolve_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else (self.source_path.parent / path).resolve()


def binding_observation_start(binding: BindingConfig) -> date | None:
    """Return a strict site-local observation boundary for an opted-in binding."""

    raw = binding.options.get("observation_start")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ConfigError("binding observation_start must use YYYY-MM-DD")
    try:
        value = date.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigError("binding observation_start must use YYYY-MM-DD") from exc
    if value.isoformat() != raw:
        raise ConfigError("binding observation_start must use YYYY-MM-DD")
    return value


def binding_observation_boundary(
    config: AppConfig, binding: BindingConfig
) -> datetime | None:
    """Resolve an observation start to the bound site's local midnight."""

    value = binding_observation_start(binding)
    if value is None:
        return None
    site = next((item for item in config.sites if item.id == binding.site_id), None)
    if site is None:
        raise ConfigError(
            f"binding observation_start references unknown site {binding.site_id}"
        )
    return datetime.combine(value, time.min, ZoneInfo(site.timezone))


def _as_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{label} must be a TOML table")
    return dict(value)


def _as_table_list(value: object, label: str, *, required: bool = True) -> list[dict[str, Any]]:
    if value is None and not required:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{label} must be an array of tables")
    return [_as_mapping(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _reject_unknown(table: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{label} contains unknown field(s): {', '.join(unknown)}")


def _required_text(table: Mapping[str, Any], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label}.{key} must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigError(f"{label}.{key} contains a control character")
    return value.strip()


def _int(table: Mapping[str, Any], key: str, default: int, label: str, low: int, high: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{label}.{key} must be an integer from {low} to {high}")
    return value


def _text_list(value: object, label: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(f"{label} must be a non-empty array of strings")
    output = tuple(item.strip() for item in value)
    if len(set(output)) != len(output):
        raise ConfigError(f"{label} must not contain duplicates")
    return output


def _metric_ids(value: object, label: str) -> tuple[str, ...]:
    output = _text_list(value, label, required=True)
    unknown = tuple(item for item in output if item not in METRICS)
    if unknown:
        raise ConfigError(f"{label} contains unknown metric ids: {', '.join(unknown)}")
    return output


def _validate_id(value: str, label: str) -> str:
    if not _ID_PATTERN.fullmatch(value):
        raise ConfigError(f"{label} must be a lowercase hyphenated identifier")
    return value


def _validate_timezone(value: str, label: str) -> str:
    if value == "UTC":
        return value
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"{label} is not an available IANA timezone: {value}") from exc
    return value


def _validate_canonical_url(value: str, label: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigError(f"{label} must be an absolute HTTP or HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError(f"{label} must not include credentials, a query, or a fragment")
    return value


def _validate_credential_ref(value: str, label: str) -> str:
    scheme, separator, target = value.partition(":")
    if not separator or not _REFERENCE_SCHEME_PATTERN.fullmatch(scheme) or not target.strip():
        raise ConfigError(f"{label} must use the form provider-scheme:reference")
    if any(character.isspace() for character in target):
        raise ConfigError(f"{label} must not contain whitespace")
    return value


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def _reject_inline_secrets(value: object, label: str = "configuration") -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                raise ConfigError(f"{label} contains a non-string key")
            if _normalized_key(raw_key) in _FORBIDDEN_INLINE_KEYS:
                raise ConfigError(f"{label}.{raw_key} is an inline secret field; use credential_ref instead")
            _reject_inline_secrets(child, f"{label}.{raw_key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_inline_secrets(child, f"{label}[{index}]")


def _ensure_unique(items: list[Any], label: str) -> None:
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            raise ConfigError(f"duplicate {label} id: {item.id}")
        seen.add(item.id)


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_config(path: str | Path) -> AppConfig:
    """Load and validate a schema-versioned TOML configuration file."""

    config_path = Path(path).resolve()
    try:
        root = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"configuration is not valid readable UTF-8 TOML: {config_path}") from exc
    _reject_inline_secrets(root)
    _reject_unknown(root, {"schema_version", "platform", "web", "retention", "clients", "sites", "connections", "bindings", "reports"}, "configuration")
    if root.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION}")

    platform_table = _as_mapping(root.get("platform"), "platform")
    _reject_unknown(platform_table, {"default_timezone", "state_path", "default_sync_days", "http_timeout_seconds", "max_response_bytes"}, "platform")
    platform = PlatformConfig(
        default_timezone=_validate_timezone(_required_text(platform_table, "default_timezone", "platform"), "platform.default_timezone"),
        state_path=_required_text(platform_table, "state_path", "platform"),
        default_sync_days=_int(platform_table, "default_sync_days", 30, "platform", 1, 3650),
        http_timeout_seconds=_int(platform_table, "http_timeout_seconds", 30, "platform", 1, 300),
        max_response_bytes=_int(platform_table, "max_response_bytes", 5_000_000, "platform", 1024, 50_000_000),
    )

    web_table = _as_mapping(root.get("web", {}), "web")
    _reject_unknown(web_table, {"bind_host", "port", "allowed_hosts", "auth_mode", "username", "auth_credential_ref"}, "web")
    bind_host = str(web_table.get("bind_host", "127.0.0.1")).strip()
    auth_mode = str(web_table.get("auth_mode", "none")).strip()
    if auth_mode not in {"none", "basic"}:
        raise ConfigError("web.auth_mode must be none or basic")
    if auth_mode == "none" and not _is_loopback(bind_host):
        raise ConfigError("web.auth_mode=none is allowed only for a loopback bind_host")
    username = web_table.get("username")
    auth_ref = web_table.get("auth_credential_ref")
    if auth_mode == "basic":
        if not isinstance(username, str) or not username.strip() or not isinstance(auth_ref, str):
            raise ConfigError("web basic authentication requires username and auth_credential_ref")
        auth_ref = _validate_credential_ref(auth_ref, "web.auth_credential_ref")
    web = WebConfig(
        bind_host=bind_host,
        port=_int(web_table, "port", 8787, "web", 1, 65535),
        allowed_hosts=_text_list(web_table.get("allowed_hosts", ["127.0.0.1", "localhost"]), "web.allowed_hosts", required=True),
        auth_mode=auth_mode,
        username=username.strip() if isinstance(username, str) else None,
        auth_credential_ref=auth_ref if isinstance(auth_ref, str) else None,
    )

    retention_table = _as_mapping(root.get("retention", {}), "retention")
    _reject_unknown(retention_table, {"hourly_days", "daily_days"}, "retention")
    retention = RetentionConfig(
        hourly_days=_int(retention_table, "hourly_days", 90, "retention", 1, 3650),
        daily_days=_int(retention_table, "daily_days", 1095, "retention", 1, 36500),
    )

    clients: list[ClientConfig] = []
    for index, table in enumerate(_as_table_list(root.get("clients"), "clients")):
        label = f"clients[{index}]"; _reject_unknown(table, {"id", "name"}, label)
        clients.append(ClientConfig(_validate_id(_required_text(table, "id", label), f"{label}.id"), _required_text(table, "name", label)))

    sites: list[SiteConfig] = []
    for index, table in enumerate(_as_table_list(root.get("sites"), "sites")):
        label = f"sites[{index}]"; _reject_unknown(table, {"id", "client_id", "name", "canonical_url", "timezone"}, label)
        sites.append(SiteConfig(
            _validate_id(_required_text(table, "id", label), f"{label}.id"),
            _validate_id(_required_text(table, "client_id", label), f"{label}.client_id"),
            _required_text(table, "name", label),
            _validate_canonical_url(_required_text(table, "canonical_url", label), f"{label}.canonical_url"),
            _validate_timezone(_required_text(table, "timezone", label), f"{label}.timezone"),
        ))

    connections: list[ConnectionConfig] = []
    for index, table in enumerate(_as_table_list(root.get("connections"), "connections")):
        label = f"connections[{index}]"; _reject_unknown(table, {"id", "provider", "credential_ref", "options"}, label)
        connections.append(ConnectionConfig(
            _validate_id(_required_text(table, "id", label), f"{label}.id"),
            _validate_id(_required_text(table, "provider", label), f"{label}.provider"),
            _validate_credential_ref(_required_text(table, "credential_ref", label), f"{label}.credential_ref"),
            _as_mapping(table.get("options", {}), f"{label}.options"),
        ))

    bindings: list[BindingConfig] = []
    for index, table in enumerate(_as_table_list(root.get("bindings"), "bindings")):
        label = f"bindings[{index}]"; _reject_unknown(table, {"site_id", "connection_id", "resource_type", "resource_id", "metric_groups", "options"}, label)
        binding = BindingConfig(
            _validate_id(_required_text(table, "site_id", label), f"{label}.site_id"),
            _validate_id(_required_text(table, "connection_id", label), f"{label}.connection_id"),
            _validate_id(_required_text(table, "resource_type", label), f"{label}.resource_type"),
            _required_text(table, "resource_id", label),
            _text_list(table.get("metric_groups"), f"{label}.metric_groups"),
            _as_mapping(table.get("options", {}), f"{label}.options"),
        )
        try:
            binding_observation_start(binding)
        except ConfigError as exc:
            raise ConfigError(
                f"{label}.options.observation_start must use YYYY-MM-DD"
            ) from exc
        bindings.append(binding)

    reports: list[ReportConfig] = []
    for index, table in enumerate(_as_table_list(root.get("reports"), "reports")):
        label = f"reports[{index}]"; _reject_unknown(table, {"id", "title", "client_id", "site_ids", "metric_ids", "default_window_days", "subreports"}, label)
        subs: list[SubreportConfig] = []
        for sub_index, sub in enumerate(_as_table_list(table.get("subreports"), f"{label}.subreports", required=False)):
            sub_label = f"{label}.subreports[{sub_index}]"; _reject_unknown(sub, {"id", "title", "metric_ids", "default_window_days", "filters"}, sub_label)
            raw_filters = _as_mapping(sub.get("filters", {}), f"{sub_label}.filters")
            if not all(isinstance(key, str) and key.strip() and isinstance(value, str) and value.strip() for key, value in raw_filters.items()):
                raise ConfigError(f"{sub_label}.filters must contain non-empty string keys and values")
            subs.append(SubreportConfig(
                _validate_id(_required_text(sub, "id", sub_label), f"{sub_label}.id"),
                _required_text(sub, "title", sub_label),
                _metric_ids(sub.get("metric_ids"), f"{sub_label}.metric_ids"),
                _int(sub, "default_window_days", _int(table, "default_window_days", 30, label, 1, 3650), sub_label, 1, 3650),
                tuple(sorted((key.strip(), value.strip()) for key, value in raw_filters.items())),
            ))
        _ensure_unique(subs, f"{label} subreport")
        reports.append(ReportConfig(
            _validate_id(_required_text(table, "id", label), f"{label}.id"),
            _required_text(table, "title", label),
            _validate_id(_required_text(table, "client_id", label), f"{label}.client_id"),
            _text_list(table.get("site_ids"), f"{label}.site_ids", required=True),
            _metric_ids(table.get("metric_ids"), f"{label}.metric_ids"),
            _int(table, "default_window_days", 30, label, 1, 3650),
            tuple(subs),
        ))

    if not clients or not sites or not connections or not bindings or not reports:
        raise ConfigError("clients, sites, connections, bindings, and reports must each contain an entry")
    _ensure_unique(clients, "client"); _ensure_unique(sites, "site"); _ensure_unique(connections, "connection"); _ensure_unique(reports, "report")
    client_ids = {item.id for item in clients}; site_ids = {item.id for item in sites}; connection_ids = {item.id for item in connections}
    for site in sites:
        if site.client_id not in client_ids: raise ConfigError(f"site {site.id} references unknown client {site.client_id}")
    binding_keys: set[tuple[str, str, str, str]] = set()
    for binding in bindings:
        if binding.site_id not in site_ids: raise ConfigError(f"binding references unknown site {binding.site_id}")
        if binding.connection_id not in connection_ids: raise ConfigError(f"binding references unknown connection {binding.connection_id}")
        key = (binding.site_id, binding.connection_id, binding.resource_type, binding.resource_id)
        if key in binding_keys: raise ConfigError(f"duplicate binding for site={binding.site_id}, connection={binding.connection_id}, resource={binding.resource_id}")
        binding_keys.add(key)
    for report in reports:
        if report.client_id not in client_ids: raise ConfigError(f"report {report.id} references unknown client {report.client_id}")
        for site_id in report.site_ids:
            site = next((item for item in sites if item.id == site_id), None)
            if site is None or site.client_id != report.client_id: raise ConfigError(f"report {report.id} references an unavailable site {site_id}")

    return AppConfig(SCHEMA_VERSION, platform, web, retention, tuple(clients), tuple(sites), tuple(connections), tuple(bindings), tuple(reports), config_path)
