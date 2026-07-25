"""Shared, storage-independent Graph Evidence Core 2.1 primitives."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterable


RESOLUTION_STATES = frozenset({
    "confirmed-page",
    "redirect",
    "missing",
    "source-only",
    "artifact-only",
    "rendered-only",
    "dynamic-unknown",
    "contradicted",
    "unresolved",
    "excluded",
    "unchecked",
    "action",
    "fragment",
    "external",
})
REVISION_RELATIONS = frozenset({"exact", "mismatch", "unchecked"})
ADAPTER_STATUSES = frozenset({"succeeded", "partial", "failed", "unchecked"})
DIAGNOSTIC_SEVERITIES = frozenset({"info", "warning", "error"})
LINK_LAYERS = frozenset({"menu", "breadcrumb", "contextual", "related", "action", "utility"})
NON_TOPOLOGY_STATES = frozenset({"action", "fragment", "external"})

MAX_DIAGNOSTICS = 100
MAX_DIAGNOSTIC_TEXT = 500
MAX_JSON_BYTES = 64 * 1024

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EMAIL = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.I)
_PRIVATE_PATH = re.compile(r"(?:^|[\s\"'])(?:/Users/|/home/|[A-Z]:\\Users\\)", re.I)
_IPV4 = re.compile(r"(?<![\w.-])(?:\d{1,3}\.){3}\d{1,3}(?![\w.-])")
_PHONE = re.compile(
    r"(?<!\w)(?=[+\d\s().-]{8,}\d(?!\w))(?=[+\d\s().-]*[\s().-])"
    r"(?:\+?\d[\s().-]*){7,}\d(?!\w)"
)
_RAW_QUERY = re.compile(r"(?:https?://|/)[^\s\"']*\?[^\s\"']*=", re.I)
_SECRET = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
    r"\bgh[pousr]_[A-Za-z0-9]{30,}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b|"
    r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b)"
)
_SENSITIVE_KEYS = ("password", "secret", "token", "credential", "private_key")
_PRIVATE_KEYS = ("email", "session", "visitor", "distinct_id", "ip_address", "user_agent", "raw_query")


def stable_json(value: Any) -> str:
    """Serialize finite JSON data deterministically."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def stable_id(namespace: str, *values: Any) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,15}", namespace):
        raise ValueError("identity namespace must be a bounded lowercase identifier")
    return f"{namespace}_{stable_hash([namespace, *values])[:32]}"


def require_text(
    value: Any,
    where: str,
    *,
    maximum: int = 2000,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise ValueError(f"{where} must be {qualifier} of at most {maximum} characters")
    if _CONTROL.search(value):
        raise ValueError(f"{where} contains control characters")
    if (
        _EMAIL.search(value)
        or _PRIVATE_PATH.search(value)
        or _IPV4.search(value)
        or _PHONE.search(value)
        or _RAW_QUERY.search(value)
        or _SECRET.search(value)
    ):
        raise ValueError(f"{where} contains private or secret-shaped text")
    return value if allow_empty else value.strip()


def require_choice(value: Any, choices: frozenset[str], where: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{where} must be one of: {', '.join(sorted(choices))}")
    return value


def require_revision(value: Any, where: str, *, allow_empty: bool = False) -> str:
    result = require_text(value, where, maximum=128, allow_empty=allow_empty)
    if result and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", result):
        raise ValueError(f"{where} must be a lowercase Git revision")
    return result


def require_relative_path(value: Any, where: str) -> str:
    result = require_text(value, where, maximum=1000)
    posix = PurePosixPath(result)
    windows = PureWindowsPath(result)
    if posix.is_absolute() or windows.is_absolute() or ".." in posix.parts or ".." in windows.parts:
        raise ValueError(f"{where} must be a repository-relative path without traversal")
    return result


def reject_private_json(value: Any, where: str = "value") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = require_text(str(key), f"{where}.key", maximum=100)
            normalized = key_text.casefold().replace("-", "_")
            if any(part in normalized for part in (*_SENSITIVE_KEYS, *_PRIVATE_KEYS)):
                raise ValueError(f"private field is not allowed in {where}: {key_text}")
            reject_private_json(child, f"{where}.{key_text}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            reject_private_json(child, f"{where}[{index}]")
    elif isinstance(value, str):
        require_text(value, where, maximum=5000, allow_empty=True)


def bounded_json(value: Any, where: str, *, maximum: int = MAX_JSON_BYTES) -> str:
    reject_private_json(value, where)
    try:
        result = stable_json(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where} must be finite JSON data") from exc
    if len(result.encode("utf-8")) > maximum:
        raise ValueError(f"{where} exceeds {maximum} serialized bytes")
    return result


def normalize_diagnostics(value: Iterable[dict[str, str]]) -> tuple[dict[str, str], ...]:
    items = tuple(value)
    if len(items) > MAX_DIAGNOSTICS:
        raise ValueError(f"diagnostics must contain at most {MAX_DIAGNOSTICS} entries")
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {"severity", "code", "message"}:
            raise ValueError(f"diagnostics[{index}] must contain severity, code, and message")
        normalized.append({
            "severity": require_choice(item["severity"], DIAGNOSTIC_SEVERITIES, f"diagnostics[{index}].severity"),
            "code": require_text(item["code"], f"diagnostics[{index}].code", maximum=80),
            "message": require_text(item["message"], f"diagnostics[{index}].message", maximum=MAX_DIAGNOSTIC_TEXT),
        })
    return tuple(sorted(normalized, key=lambda item: (item["severity"], item["code"], item["message"])))
