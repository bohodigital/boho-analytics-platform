"""Strict, non-secret configuration loading for schema version 1."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = 1
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_REFERENCE_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_FORBIDDEN_INLINE_KEYS = {
    "accesstoken",
    "apikey",
    "clientsecret",
    "credential",
    "password",
    "refreshtoken",
    "secret",
    "token",
}


class ConfigError(ValueError):
    """Raised when a configuration file violates the public schema."""


@dataclass(frozen=True, slots=True)
class PlatformConfig:
    default_timezone: str
    state_path: str


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


@dataclass(frozen=True, slots=True)
class AppConfig:
    schema_version: int
    platform: PlatformConfig
    clients: tuple[ClientConfig, ...]
    sites: tuple[SiteConfig, ...]
    connections: tuple[ConnectionConfig, ...]
    bindings: tuple[BindingConfig, ...]


def _as_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{label} must be a TOML table")
    return dict(value)


def _as_table_list(value: object, label: str) -> list[dict[str, Any]]:
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


def _validate_id(value: str, label: str) -> str:
    if not _ID_PATTERN.fullmatch(value):
        raise ConfigError(f"{label} must be a lowercase hyphenated identifier")
    return value


def _validate_timezone(value: str, label: str) -> str:
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
                raise ConfigError(
                    f"{label}.{raw_key} is an inline secret field; use credential_ref instead"
                )
            _reject_inline_secrets(child, f"{label}.{raw_key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_inline_secrets(child, f"{label}[{index}]")


def _ensure_unique(items: list[Any], label: str) -> None:
    seen: set[str] = set()
    for item in items:
        identifier = item.id
        if identifier in seen:
            raise ConfigError(f"duplicate {label} id: {identifier}")
        seen.add(identifier)


def load_config(path: str | Path) -> AppConfig:
    """Load and validate a schema-versioned TOML configuration file."""

    config_path = Path(path)
    try:
        raw_bytes = config_path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"could not read configuration: {config_path}") from exc
    try:
        root = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"configuration is not valid UTF-8 TOML: {config_path}") from exc

    _reject_inline_secrets(root)
    _reject_unknown(
        root,
        {"schema_version", "platform", "clients", "sites", "connections", "bindings"},
        "configuration",
    )
    if root.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION}")

    platform_table = _as_mapping(root.get("platform"), "platform")
    _reject_unknown(platform_table, {"default_timezone", "state_path"}, "platform")
    platform = PlatformConfig(
        default_timezone=_validate_timezone(
            _required_text(platform_table, "default_timezone", "platform"),
            "platform.default_timezone",
        ),
        state_path=_required_text(platform_table, "state_path", "platform"),
    )

    clients: list[ClientConfig] = []
    for index, table in enumerate(_as_table_list(root.get("clients"), "clients")):
        label = f"clients[{index}]"
        _reject_unknown(table, {"id", "name"}, label)
        clients.append(
            ClientConfig(
                id=_validate_id(_required_text(table, "id", label), f"{label}.id"),
                name=_required_text(table, "name", label),
            )
        )

    sites: list[SiteConfig] = []
    for index, table in enumerate(_as_table_list(root.get("sites"), "sites")):
        label = f"sites[{index}]"
        _reject_unknown(
            table, {"id", "client_id", "name", "canonical_url", "timezone"}, label
        )
        sites.append(
            SiteConfig(
                id=_validate_id(_required_text(table, "id", label), f"{label}.id"),
                client_id=_validate_id(
                    _required_text(table, "client_id", label), f"{label}.client_id"
                ),
                name=_required_text(table, "name", label),
                canonical_url=_validate_canonical_url(
                    _required_text(table, "canonical_url", label), f"{label}.canonical_url"
                ),
                timezone=_validate_timezone(
                    _required_text(table, "timezone", label), f"{label}.timezone"
                ),
            )
        )

    connections: list[ConnectionConfig] = []
    for index, table in enumerate(_as_table_list(root.get("connections"), "connections")):
        label = f"connections[{index}]"
        _reject_unknown(table, {"id", "provider", "credential_ref", "options"}, label)
        options = table.get("options", {})
        connections.append(
            ConnectionConfig(
                id=_validate_id(_required_text(table, "id", label), f"{label}.id"),
                provider=_validate_id(
                    _required_text(table, "provider", label), f"{label}.provider"
                ),
                credential_ref=_validate_credential_ref(
                    _required_text(table, "credential_ref", label),
                    f"{label}.credential_ref",
                ),
                options=_as_mapping(options, f"{label}.options"),
            )
        )

    bindings: list[BindingConfig] = []
    for index, table in enumerate(_as_table_list(root.get("bindings"), "bindings")):
        label = f"bindings[{index}]"
        _reject_unknown(
            table, {"site_id", "connection_id", "resource_type", "resource_id"}, label
        )
        bindings.append(
            BindingConfig(
                site_id=_validate_id(
                    _required_text(table, "site_id", label), f"{label}.site_id"
                ),
                connection_id=_validate_id(
                    _required_text(table, "connection_id", label), f"{label}.connection_id"
                ),
                resource_type=_validate_id(
                    _required_text(table, "resource_type", label), f"{label}.resource_type"
                ),
                resource_id=_required_text(table, "resource_id", label),
            )
        )

    if not clients or not sites or not connections or not bindings:
        raise ConfigError("clients, sites, connections, and bindings must each contain an entry")
    _ensure_unique(clients, "client")
    _ensure_unique(sites, "site")
    _ensure_unique(connections, "connection")

    client_ids = {item.id for item in clients}
    site_ids = {item.id for item in sites}
    connection_ids = {item.id for item in connections}
    for site in sites:
        if site.client_id not in client_ids:
            raise ConfigError(f"site {site.id} references unknown client {site.client_id}")

    binding_keys: set[tuple[str, str, str]] = set()
    for binding in bindings:
        if binding.site_id not in site_ids:
            raise ConfigError(f"binding references unknown site {binding.site_id}")
        if binding.connection_id not in connection_ids:
            raise ConfigError(f"binding references unknown connection {binding.connection_id}")
        key = (binding.site_id, binding.connection_id, binding.resource_type)
        if key in binding_keys:
            raise ConfigError(
                "duplicate binding for "
                f"site={binding.site_id}, connection={binding.connection_id}, "
                f"resource_type={binding.resource_type}"
            )
        binding_keys.add(key)

    return AppConfig(
        schema_version=SCHEMA_VERSION,
        platform=platform,
        clients=tuple(clients),
        sites=tuple(sites),
        connections=tuple(connections),
        bindings=tuple(bindings),
    )
