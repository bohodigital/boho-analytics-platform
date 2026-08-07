"""Strict private-manifest contract for Search Console BigQuery exports."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml


SCHEMA_VERSION = 1
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_DATASET_PATTERN = re.compile(r"^searchconsole[A-Za-z0-9_]{0,1011}$")
_LOCATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_REFERENCE_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_FORBIDDEN_INLINE_KEYS = {
    "accesstoken", "apikey", "authorization", "bearer", "clientsecret",
    "cookie", "password", "privatekey", "refreshtoken", "secret", "token",
}


class BulkExportConfigError(ValueError):
    """Raised when a private bulk-export manifest is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class WarehouseConfig:
    project_id: str
    location: str
    credential_ref: str
    maximum_bytes_billed: int = 1_073_741_824
    use_storage_api: bool = False


@dataclass(frozen=True, slots=True)
class LakeStorageConfig:
    root: Path
    required_mountpoint: Path
    required_filesystem_uuid: str
    minimum_free_bytes: int = 10_737_418_240
    parquet_compression: str = "zstd"
    batch_rows: int = 50_000

    @property
    def identity_marker_name(self) -> str:
        return f".boho-storage-{self.required_filesystem_uuid}"


@dataclass(frozen=True, slots=True)
class SearchConsolePropertyConfig:
    site_id: str
    site_url: str
    dataset_id: str
    first_export_date: date
    identity_proof_date: date


@dataclass(frozen=True, slots=True)
class BulkExportManifest:
    schema_version: int
    warehouse: WarehouseConfig
    storage: LakeStorageConfig
    properties: tuple[SearchConsolePropertyConfig, ...]
    source_path: Path


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BulkExportConfigError(f"{label} must be a mapping")
    return dict(value)


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise BulkExportConfigError(
            f"{label} contains unknown field(s): {', '.join(unknown)}"
        )


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _reject_inline_secrets(value: object, label: str = "manifest") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise BulkExportConfigError(f"{label} contains a non-string key")
            if _normalized_key(key) in _FORBIDDEN_INLINE_KEYS:
                raise BulkExportConfigError(
                    f"{label}.{key} is an inline secret field; use credential_ref"
                )
            _reject_inline_secrets(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_inline_secrets(child, f"{label}[{index}]")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BulkExportConfigError(f"{label} must be non-empty text")
    output = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in output):
        raise BulkExportConfigError(f"{label} contains a control character")
    return output


def _bounded_int(
    value: object, label: str, default: int, minimum: int, maximum: int
) -> int:
    candidate = default if value is None else value
    if (
        isinstance(candidate, bool)
        or not isinstance(candidate, int)
        or not minimum <= candidate <= maximum
    ):
        raise BulkExportConfigError(
            f"{label} must be an integer from {minimum} to {maximum}"
        )
    return candidate


def _credential_ref(value: object) -> str:
    candidate = _text(value, "warehouse.credential_ref")
    scheme, separator, target = candidate.partition(":")
    if (
        not separator
        or not _REFERENCE_SCHEME_PATTERN.fullmatch(scheme)
        or not target
        or any(character.isspace() for character in target)
    ):
        raise BulkExportConfigError(
            "warehouse.credential_ref must use provider-scheme:reference"
        )
    return candidate


def _absolute_path(value: object, label: str) -> Path:
    candidate = _text(value, label)
    pure = PurePosixPath(candidate)
    if not pure.is_absolute() or ".." in pure.parts:
        raise BulkExportConfigError(f"{label} must be an absolute normalized path")
    return Path(candidate)


def _site_url(value: object, label: str) -> str:
    candidate = _text(value, label)
    if candidate.startswith("sc-domain:"):
        domain = candidate.removeprefix("sc-domain:")
        if not domain or "/" in domain or any(character.isspace() for character in domain):
            raise BulkExportConfigError(f"{label} is not a valid domain property")
        return candidate
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise BulkExportConfigError(
            f"{label} must be a Search Console domain or URL-prefix property"
        )
    return candidate


def _iso_date(value: object, label: str) -> date:
    if type(value) is date:
        return value
    if isinstance(value, date):
        raise BulkExportConfigError(f"{label} must use YYYY-MM-DD without a time")
    candidate = _text(value, label)
    try:
        parsed = date.fromisoformat(candidate)
    except ValueError as exc:
        raise BulkExportConfigError(f"{label} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != candidate:
        raise BulkExportConfigError(f"{label} must use YYYY-MM-DD")
    return parsed


def load_bulk_export_manifest(path: str | Path) -> BulkExportManifest:
    """Load a strict, non-secret Search Console bulk-export manifest."""

    source_path = Path(path).resolve()
    try:
        root = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BulkExportConfigError(
            f"bulk-export manifest is not readable UTF-8 YAML: {source_path}"
        ) from exc
    root = _mapping(root, "manifest")
    _reject_inline_secrets(root)
    _reject_unknown(root, {"schema_version", "warehouse", "storage", "properties"}, "manifest")
    if root.get("schema_version") != SCHEMA_VERSION:
        raise BulkExportConfigError(f"schema_version must be {SCHEMA_VERSION}")

    raw_warehouse = _mapping(root.get("warehouse"), "warehouse")
    _reject_unknown(
        raw_warehouse,
        {
            "project_id", "location", "credential_ref", "maximum_bytes_billed",
            "use_storage_api",
        },
        "warehouse",
    )
    project_id = _text(raw_warehouse.get("project_id"), "warehouse.project_id")
    if not _PROJECT_PATTERN.fullmatch(project_id):
        raise BulkExportConfigError("warehouse.project_id is not a valid Google Cloud project id")
    location = _text(raw_warehouse.get("location"), "warehouse.location")
    if not _LOCATION_PATTERN.fullmatch(location):
        raise BulkExportConfigError("warehouse.location is not a valid BigQuery location")
    use_storage_api = raw_warehouse.get("use_storage_api", False)
    if not isinstance(use_storage_api, bool):
        raise BulkExportConfigError("warehouse.use_storage_api must be a boolean")
    warehouse = WarehouseConfig(
        project_id=project_id,
        location=location,
        credential_ref=_credential_ref(raw_warehouse.get("credential_ref")),
        maximum_bytes_billed=_bounded_int(
            raw_warehouse.get("maximum_bytes_billed"),
            "warehouse.maximum_bytes_billed",
            1_073_741_824,
            10_485_760,
            54_975_581_388_800,
        ),
        use_storage_api=use_storage_api,
    )

    raw_storage = _mapping(root.get("storage"), "storage")
    _reject_unknown(
        raw_storage,
        {
            "root", "required_mountpoint", "required_filesystem_uuid",
            "minimum_free_bytes", "parquet_compression", "batch_rows",
        },
        "storage",
    )
    mountpoint = _absolute_path(
        raw_storage.get("required_mountpoint"), "storage.required_mountpoint"
    )
    storage_root = _absolute_path(raw_storage.get("root"), "storage.root")
    try:
        storage_root.relative_to(mountpoint)
    except ValueError as exc:
        raise BulkExportConfigError(
            "storage.root must be strictly beneath storage.required_mountpoint"
        ) from exc
    if storage_root == mountpoint:
        raise BulkExportConfigError(
            "storage.root must be strictly beneath storage.required_mountpoint"
        )
    filesystem_uuid = _text(
        raw_storage.get("required_filesystem_uuid"),
        "storage.required_filesystem_uuid",
    ).casefold()
    if not _UUID_PATTERN.fullmatch(filesystem_uuid):
        raise BulkExportConfigError(
            "storage.required_filesystem_uuid must be a canonical lowercase UUID"
        )
    compression = _text(
        raw_storage.get("parquet_compression", "zstd"),
        "storage.parquet_compression",
    ).casefold()
    if compression not in {"zstd", "snappy", "gzip"}:
        raise BulkExportConfigError(
            "storage.parquet_compression must be zstd, snappy, or gzip"
        )
    storage = LakeStorageConfig(
        root=storage_root,
        required_mountpoint=mountpoint,
        required_filesystem_uuid=filesystem_uuid,
        minimum_free_bytes=_bounded_int(
            raw_storage.get("minimum_free_bytes"),
            "storage.minimum_free_bytes",
            10_737_418_240,
            1_073_741_824,
            109_951_162_777_600,
        ),
        parquet_compression=compression,
        batch_rows=_bounded_int(
            raw_storage.get("batch_rows"),
            "storage.batch_rows",
            50_000,
            1_000,
            1_000_000,
        ),
    )

    raw_properties = root.get("properties")
    if not isinstance(raw_properties, list) or not raw_properties:
        raise BulkExportConfigError("properties must be a non-empty list")
    properties: list[SearchConsolePropertyConfig] = []
    for index, raw_property in enumerate(raw_properties):
        label = f"properties[{index}]"
        value = _mapping(raw_property, label)
        _reject_unknown(
            value,
            {
                "site_id", "site_url", "dataset_id", "first_export_date",
                "identity_proof_date",
            },
            label,
        )
        site_id = _text(value.get("site_id"), f"{label}.site_id")
        if not _ID_PATTERN.fullmatch(site_id):
            raise BulkExportConfigError(f"{label}.site_id must be a lowercase hyphenated id")
        dataset_id = _text(value.get("dataset_id"), f"{label}.dataset_id")
        if not _DATASET_PATTERN.fullmatch(dataset_id):
            raise BulkExportConfigError(
                f"{label}.dataset_id must start with searchconsole and use BigQuery-safe characters"
            )
        first_export_date = _iso_date(
            value.get("first_export_date"), f"{label}.first_export_date"
        )
        identity_proof_date = _iso_date(
            value.get("identity_proof_date"), f"{label}.identity_proof_date"
        )
        if identity_proof_date < first_export_date:
            raise BulkExportConfigError(
                f"{label}.identity_proof_date cannot precede first_export_date"
            )
        properties.append(SearchConsolePropertyConfig(
            site_id=site_id,
            site_url=_site_url(value.get("site_url"), f"{label}.site_url"),
            dataset_id=dataset_id,
            first_export_date=first_export_date,
            identity_proof_date=identity_proof_date,
        ))
    site_ids = [item.site_id for item in properties]
    dataset_ids = [item.dataset_id for item in properties]
    if len(site_ids) != len(set(site_ids)):
        raise BulkExportConfigError("properties.site_id values must be unique")
    if len(dataset_ids) != len(set(dataset_ids)):
        raise BulkExportConfigError(
            "each Search Console property must use a distinct dataset_id"
        )
    return BulkExportManifest(
        schema_version=SCHEMA_VERSION,
        warehouse=warehouse,
        storage=storage,
        properties=tuple(properties),
        source_path=source_path,
    )
