"""Strict normalization of owner-supplied, read-only deployment metadata."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PARSER_VERSION = "site-graph-deployment-metadata-core-2.1.0"
MAX_METADATA_BYTES = 256 * 1024
MAX_METADATA_NODES = 10_000
MAX_LIST_ITEMS = 100
_REVISION = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
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
_SCALAR_FIELDS = frozenset(
    {
        "provider",
        "project_name",
        "deployment_id",
        "deployment_url",
        "url",
        "branch",
        "commit_hash",
        "commit_sha",
        "revision",
        "created_on",
        "created_at",
        "status",
        "environment",
    }
)
_LIST_FIELDS = frozenset({"hostnames", "aliases"})


class DeploymentMetadataError(ValueError):
    """Deployment metadata was unsafe, malformed, or outside its bounds."""


@dataclass(frozen=True)
class DeploymentMetadataEvidence:
    parser_version: str
    source: str
    provider_mutation: bool
    expected_revision: str
    observed_revision: str
    revision_state: str
    fields: dict[str, str | tuple[str, ...]]
    source_hash: str
    evidence_hash: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise DeploymentMetadataError(f"deployment metadata has duplicate key: {key[:100]}")
        output[key] = value
    return output


def _reject_sensitive_and_bound(payload: Any) -> None:
    stack: list[tuple[str, Any]] = [("deployment metadata", payload)]
    nodes = 0
    while stack:
        where, value = stack.pop()
        nodes += 1
        if nodes > MAX_METADATA_NODES:
            raise DeploymentMetadataError("deployment metadata exceeds the node-count limit")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 200:
                    raise DeploymentMetadataError("deployment metadata contains an invalid key")
                normalized = key.lower().replace("-", "_")
                if any(part in normalized for part in _SECRET_PARTS):
                    raise DeploymentMetadataError(f"sensitive field is not allowed: {where}.{key}")
                stack.append((f"{where}.{key}", child))
        elif isinstance(value, list):
            if len(value) > MAX_LIST_ITEMS:
                raise DeploymentMetadataError("deployment metadata list exceeds the item limit")
            stack.extend((f"{where}[]", child) for child in value)
        elif value is not None and not isinstance(value, (str, bool, int, float)):
            raise DeploymentMetadataError("deployment metadata contains an unsupported value")
        elif isinstance(value, str) and len(value) > 4096:
            raise DeploymentMetadataError("deployment metadata string exceeds the size limit")


def _text(value: Any, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, bool, int, float)):
        raise DeploymentMetadataError("deployment metadata public field must be scalar")
    text = str(value)
    if len(text) > maximum or "\x00" in text or any(ord(char) < 32 for char in text):
        raise DeploymentMetadataError("deployment metadata public field is invalid")
    return text


def _validate_url(value: str) -> str:
    if not value:
        return value
    split = urlsplit(value)
    if (
        split.scheme != "https"
        or not split.hostname
        or split.username is not None
        or split.password is not None
        or split.query
        or split.fragment
    ):
        raise DeploymentMetadataError(
            "deployment URL must be a public HTTPS URL without credentials, query, or fragment"
        )
    return value


def load_deployment_metadata(
    path: Path,
    *,
    expected_revision: str,
) -> DeploymentMetadataEvidence:
    """Load a local JSON evidence file; this function performs no provider calls."""
    if not _REVISION.fullmatch(expected_revision):
        raise DeploymentMetadataError(
            "expected revision must be a full 40- or 64-character hex digest"
        )
    if path.is_symlink():
        raise DeploymentMetadataError("deployment metadata path must not be a symlink")
    try:
        if not path.is_file():
            raise DeploymentMetadataError("deployment metadata path must be a regular JSON file")
        if path.stat().st_size > MAX_METADATA_BYTES:
            raise DeploymentMetadataError("deployment metadata exceeds the byte limit")
        raw = path.read_bytes()
    except OSError as exc:
        raise DeploymentMetadataError("deployment metadata cannot be read") from exc
    if len(raw) > MAX_METADATA_BYTES:
        raise DeploymentMetadataError("deployment metadata exceeds the byte limit")
    try:
        payload = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                DeploymentMetadataError(f"non-finite value is not allowed: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise DeploymentMetadataError("deployment metadata must be bounded UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise DeploymentMetadataError("deployment metadata must be a JSON object")
    _reject_sensitive_and_bound(payload)

    fields: dict[str, str | tuple[str, ...]] = {}
    for key in sorted(_SCALAR_FIELDS):
        if key in payload:
            maximum = 4096 if key in {"deployment_url", "url"} else 500
            value = _text(payload[key], maximum)
            if key in {"deployment_url", "url"}:
                value = _validate_url(value)
            fields[key] = value
    for key in sorted(_LIST_FIELDS):
        if key not in payload:
            continue
        values = payload[key]
        if not isinstance(values, list):
            raise DeploymentMetadataError(f"deployment metadata {key} must be a list")
        normalized = tuple(sorted({_text(item, 253).lower() for item in values}))
        if any(not item or "/" in item or "@" in item for item in normalized):
            raise DeploymentMetadataError(f"deployment metadata {key} contains an invalid host")
        fields[key] = normalized

    revisions = {
        value.lower()
        for key in ("commit_hash", "commit_sha", "revision")
        if (value := fields.get(key)) and isinstance(value, str)
    }
    if any(not _REVISION.fullmatch(value) for value in revisions):
        raise DeploymentMetadataError("deployment metadata contains an invalid revision")
    if len(revisions) > 1:
        revision_state = "conflicting"
        observed_revision = ",".join(sorted(revisions))
    elif not revisions:
        revision_state = "unchecked"
        observed_revision = ""
    else:
        observed_revision = next(iter(revisions))
        revision_state = "matched" if observed_revision == expected_revision.lower() else "mismatched"

    source_hash = hashlib.sha256(raw).hexdigest()
    normalized = {
        "parser_version": PARSER_VERSION,
        "expected_revision": expected_revision.lower(),
        "observed_revision": observed_revision,
        "revision_state": revision_state,
        "fields": {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in sorted(fields.items())
        },
        "source_hash": source_hash,
        "source": "owner-supplied-json",
        "provider_mutation": False,
    }
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return DeploymentMetadataEvidence(
        parser_version=PARSER_VERSION,
        source="owner-supplied-json",
        provider_mutation=False,
        expected_revision=expected_revision.lower(),
        observed_revision=observed_revision,
        revision_state=revision_state,
        fields=fields,
        source_hash=source_hash,
        evidence_hash=hashlib.sha256(encoded.encode("ascii")).hexdigest(),
    )
