"""Provider-neutral domain models for ingestion and reporting."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import re
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TimeGrain(StrEnum):
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    TOTAL = "total"


class Completeness(StrEnum):
    REALTIME = "realtime"
    PROVISIONAL = "provisional"
    FINAL = "final"
    UNKNOWN = "unknown"


class DefinitionType(StrEnum):
    GOAL = "goal"
    SEGMENT = "segment"
    ALERT_RULE = "alert_rule"
    REPORT_SUBSCRIPTION = "report_subscription"


class DefinitionValidationError(ValueError):
    """A definition failed the closed, privacy-safe storage contract."""


MAX_DEFINITION_KEY_BYTES = 128
MAX_DEFINITION_CONTENT_BYTES = 32_768
MAX_DEFINITION_METADATA_BYTES = 4_096
MAX_DEFINITION_DEPTH = 8
MAX_DEFINITION_ARRAY_ITEMS = 100
MAX_DEFINITION_STRING_BYTES = 512

_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_EMAIL = re.compile(
    r"(?iu)(?<![\w.!#$%&'*+/=?^`{|}~-])"
    r"[\w.!#$%&'*+/=?^`{|}~-]+@[^\s@/]+"
)
_RECIPIENT_LOCAL_ATOM = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+")
_RECIPIENT_DOMAIN_LABEL = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
)
_EXTERNAL_URL = re.compile(
    r"(?i)(?:\b[a-z][a-z0-9+.-]*://|\bwww\.|(?:^|[\s=(\"'])//[a-z0-9\[])"
)
_PRIVATE_PATH = re.compile(
    r"(?i)(?:"
    r"~?/(?:Users|home|root|srv|etc|var|private|opt|Library|Applications|Volumes)/|"
    r"/usr/local/|"
    r"(?:^|[/\\])\.\.(?:[/\\])|"
    r"[A-Z]:[\\/](?:Users|home|private)[\\/]"
    r")"
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:"
    r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\bASIA[0-9A-Z]{16}\b|"
    r"\b(?:gh[opusr]_|github_pat_)[A-Za-z0-9_]{20,}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b|"
    r"\bsk-[A-Za-z0-9_-]{16,}\b|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"(?![A-Za-z0-9_-])|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r")"
)
_RAW_QUERY = re.compile(
    r"(?is)(?:\bselect\b.{0,80}\bfrom\b|\binsert\s+into\b|\bupdate\b.{0,80}\bset\b|"
    r"\bdelete\s+from\b|\bquery\s*\{)"
)
_RAW_TOML = re.compile(
    r"(?im)(?:"
    r"^\s*\[\[?[A-Za-z0-9_.-]+\]?\]\s*(?:#.*)?$|"
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_.-]*|\"[^\"\r\n]+\"|'[^'\r\n]+')"
    r"\s*=\s*\S+"
    r")"
)
_COMMENT_TEXT = re.compile(r"(?:#|//|/\*|\*/)")
_FORBIDDEN_FIELD_PARTS = {
    "address",
    "api_key",
    "authorization",
    "body",
    "client_id",
    "client_secret",
    "comment",
    "credential",
    "email",
    "form_content",
    "form_payload",
    "ip",
    "ip_address",
    "message",
    "password",
    "private_path",
    "query",
    "raw_config",
    "raw_query",
    "recipient",
    "recipients",
    "secret",
    "session",
    "session_id",
    "sql",
    "token",
    "toml",
    "user_agent",
    "visitor",
    "visitor_id",
}
_FORBIDDEN_FIELD_COMPACT = {
    item.replace("_", "") for item in _FORBIDDEN_FIELD_PARTS
}
_OPAQUE_IDENTIFIER_FIELDS = {"recipient_set_id"}
_METADATA_FIELDS = {"description", "label", "source_reference"}
_GOAL_FIELDS = {
    "active_end",
    "active_start",
    "aggregation",
    "confidence",
    "coverage_requirement",
    "date_basis",
    "denominator",
    "filters",
    "goal_type",
    "goal_version_ids",
    "maturity_lag_days",
    "metric",
    "provider_bindings",
    "site_ids",
    "source",
    "unit",
}
_GOAL_REQUIRED = {
    "aggregation",
    "confidence",
    "coverage_requirement",
    "date_basis",
    "goal_type",
    "maturity_lag_days",
    "metric",
    "provider_bindings",
    "site_ids",
    "source",
    "unit",
}
_SEGMENT_FIELDS = {"expression", "site_ids"}
_ALERT_FIELDS = {
    "comparison",
    "cooldown_minutes",
    "evaluation_grain",
    "goal_version_id",
    "incomplete_data_policy",
    "maturity_lag_days",
    "metric",
    "minimum_baseline",
    "quiet_periods",
    "rule_type",
    "segment_version_id",
    "severity",
    "site_ids",
    "source",
    "threshold",
}
_ALERT_REQUIRED = {
    "cooldown_minutes",
    "evaluation_grain",
    "incomplete_data_policy",
    "maturity_lag_days",
    "minimum_baseline",
    "quiet_periods",
    "rule_type",
    "severity",
    "site_ids",
}
_ALERT_CONDITIONAL_FIELDS = {"comparison", "goal_version_id", "source", "threshold"}
_ALERT_RULE_FIELD_MATRIX = {
    "sync_failure": {
        "required": {"source", "threshold"},
        "forbidden": {"comparison"},
    },
    "stale_data": {
        "required": {"source", "threshold"},
        "forbidden": {"comparison"},
    },
    "missing_binding": {
        "required": {"source", "threshold"},
        "forbidden": {"comparison"},
    },
    "coverage_drop": {
        "required": {"threshold"},
        "forbidden": {"comparison"},
    },
    "absolute_threshold": {
        "required": {"threshold"},
        "forbidden": {"comparison"},
    },
    "relative_change": {
        "required": {"comparison"},
        "forbidden": {"threshold"},
    },
    "zero_after_nonzero": {
        "required": {"comparison"},
        "forbidden": {"threshold"},
    },
    "cross_provider_divergence": {
        "required": {"threshold"},
        "forbidden": {"comparison", "source"},
    },
    "goal_change": {
        "required": {"comparison", "goal_version_id"},
        "forbidden": {"source", "threshold"},
    },
}
_SUBSCRIPTION_FIELDS = {
    "formats",
    "frequency",
    "goal_version_ids",
    "incomplete_data_policy",
    "maturity_lag_days",
    "recipient_set_id",
    "report_type",
    "segment_version_id",
    "site_ids",
    "timezone",
}
_SUBSCRIPTION_REQUIRED = {
    "formats",
    "frequency",
    "incomplete_data_policy",
    "maturity_lag_days",
    "recipient_set_id",
    "report_type",
    "site_ids",
    "timezone",
}
_SEGMENT_DIMENSIONS = {
    "campaign",
    "channel",
    "completeness",
    "country",
    "date",
    "device",
    "event",
    "goal",
    "landing_route",
    "medium",
    "provider",
    "region",
    "route",
    "site",
    "source",
}
_SEGMENT_OPERATORS = {
    "contains",
    "ends_with",
    "equals",
    "in",
    "is_missing",
    "is_present",
    "matches_safe_pattern",
    "not_equals",
    "not_in",
    "starts_with",
}
_SAFE_PATTERN_FORBIDDEN = re.compile(
    r"(?:"
    r"\\[1-9]|"
    r"\(\?[=!<]|"
    r"\(\?>|"
    r"\(\?P|"
    r"[+*?}]\+|"
    r"\)[+*?{]"
    r")"
)


def _derive_recipient_set_identifier(
    recipients: Sequence[str], digest_key: bytes
) -> str:
    """Validate private recipient material and return its keyed identifier."""

    if type(digest_key) is not bytes or len(digest_key) < 32:
        raise DefinitionValidationError(
            "recipient-set digest key must contain at least 32 bytes"
        )
    if type(recipients) not in (list, tuple) or not recipients:
        raise DefinitionValidationError(
            "recipient set must be a bounded non-empty sequence"
        )
    canonical: list[str] = []
    for index, address in enumerate(recipients):
        if index >= MAX_DEFINITION_ARRAY_ITEMS:
            raise DefinitionValidationError(
                "recipient set must be a bounded non-empty sequence"
            )
        if type(address) is not str:
            raise DefinitionValidationError("recipient addresses must be strings")
        stripped = address.strip()
        if not stripped or not stripped.isascii():
            raise DefinitionValidationError("recipient address is invalid")
        normalized = unicodedata.normalize("NFC", stripped).casefold()
        if (
            not normalized
            or len(normalized.encode("ascii")) > 254
            or not _recipient_address_is_supported(normalized)
        ):
            raise DefinitionValidationError("recipient address is invalid")
        canonical.append(normalized)
    if len(canonical) != len(set(canonical)):
        raise DefinitionValidationError("recipient set must not contain duplicates")
    canonical_bytes = json.dumps(
        sorted(canonical),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        digest_key,
        b"boho-analytics-recipient-set-v1\0" + canonical_bytes,
        hashlib.sha256,
    ).hexdigest()


def _recipient_address_is_supported(address: str) -> bool:
    """Accept only the deliberately supported ASCII dot-atom mailbox grammar."""

    if address.count("@") != 1:
        return False
    local_part, domain = address.split("@")
    if not local_part or len(local_part.encode("ascii")) > 64:
        return False
    if not domain or len(domain.encode("ascii")) > 253:
        return False
    if any(
        not atom or _RECIPIENT_LOCAL_ATOM.fullmatch(atom) is None
        for atom in local_part.split(".")
    ):
        return False
    return all(
        label and _RECIPIENT_DOMAIN_LABEL.fullmatch(label) is not None
        for label in domain.split(".")
    )


def _field_name_is_prohibited(value: str) -> bool:
    if value in _OPAQUE_IDENTIFIER_FIELDS:
        return False
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    normalized = re.sub(r"[^a-z0-9]+", "_", separated.casefold()).strip("_")
    parts = tuple(part for part in normalized.split("_") if part)
    compact = normalized.replace("_", "")
    return (
        normalized in _FORBIDDEN_FIELD_PARTS
        or compact in _FORBIDDEN_FIELD_COMPACT
        or any(part in _FORBIDDEN_FIELD_PARTS for part in parts)
    )


def _safe_pattern_is_too_complex(value: str) -> bool:
    """Conservatively admit only a linear-time-oriented regex subset."""

    in_character_class = False
    variable_repetitions = 0
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\":
            index += 2
            continue
        if character == "[" and not in_character_class:
            in_character_class = True
            index += 1
            continue
        if character == "]" and in_character_class:
            in_character_class = False
            index += 1
            continue
        if in_character_class:
            index += 1
            continue
        if character in "()|":
            return True
        if character in "*+?":
            variable_repetitions += 1
        elif character == "{":
            closing = value.find("}", index + 1)
            if closing == -1:
                return True
            bounds = value[index + 1:closing]
            if re.fullmatch(r"\d+(?:,\d*)?", bounds) is None:
                return True
            lower_text, separator, upper_text = bounds.partition(",")
            lower = int(lower_text)
            upper = int(upper_text) if separator and upper_text else None
            if lower > 100 or (upper is not None and (upper > 100 or upper < lower)):
                return True
            if separator and upper != lower:
                variable_repetitions += 1
            index = closing
        if variable_repetitions > 1:
            return True
        index += 1
    return in_character_class


def _bounded_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_KEY.fullmatch(value):
        raise DefinitionValidationError(
            f"{label} must use only bounded ASCII letters, digits, '.', '_', ':', and '-'"
        )
    if len(value.encode("utf-8")) > MAX_DEFINITION_KEY_BYTES:
        raise DefinitionValidationError(f"{label} is too long")
    return value


def _validate_safe_string(value: str, label: str) -> None:
    if len(value.encode("utf-8")) > MAX_DEFINITION_STRING_BYTES:
        raise DefinitionValidationError(f"{label} is too long")
    normalized = unicodedata.normalize("NFC", value)
    if _EMAIL.search(normalized):
        raise DefinitionValidationError(f"{label} contains an email address")
    if _EXTERNAL_URL.search(normalized):
        raise DefinitionValidationError(f"{label} contains a full external URL")
    if _PRIVATE_PATH.search(normalized):
        raise DefinitionValidationError(f"{label} contains a private filesystem path")
    if _SECRET_VALUE.search(normalized):
        raise DefinitionValidationError(f"{label} contains secret-shaped material")
    if _RAW_QUERY.search(normalized):
        raise DefinitionValidationError(f"{label} contains raw query text")
    if _RAW_TOML.search(normalized):
        raise DefinitionValidationError(f"{label} contains raw TOML")
    if _COMMENT_TEXT.search(normalized):
        raise DefinitionValidationError(f"{label} contains comment text")


def _validate_json_value(value: Any, label: str, *, depth: int = 0) -> None:
    if depth > MAX_DEFINITION_DEPTH:
        raise DefinitionValidationError(f"{label} exceeds the nesting limit")
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DefinitionValidationError(f"{label} contains a non-finite number")
        return
    if isinstance(value, str):
        _validate_safe_string(value, label)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_DEFINITION_ARRAY_ITEMS:
            raise DefinitionValidationError(f"{label} exceeds the array-size limit")
        for index, item in enumerate(value):
            _validate_json_value(item, f"{label}[{index}]", depth=depth + 1)
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_DEFINITION_ARRAY_ITEMS:
            raise DefinitionValidationError(f"{label} exceeds the object-size limit")
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise DefinitionValidationError(f"{label} has an invalid object key")
            if _field_name_is_prohibited(key):
                raise DefinitionValidationError(f"{label}.{key} is private or prohibited")
            _validate_safe_string(key, f"{label} key")
            _validate_json_value(item, f"{label}.{key}", depth=depth + 1)
        return
    raise DefinitionValidationError(f"{label} contains unsupported JSON data")


def _require_fields(
    content: dict[str, Any],
    *,
    allowed: set[str],
    required: set[str],
    definition_type: DefinitionType,
) -> None:
    unknown = set(content) - allowed
    missing = required - set(content)
    if unknown:
        raise DefinitionValidationError(
            f"{definition_type.value} contains unknown fields: {', '.join(sorted(unknown))}"
        )
    if missing:
        raise DefinitionValidationError(
            f"{definition_type.value} is missing fields: {', '.join(sorted(missing))}"
        )


def _require_choice(value: object, choices: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise DefinitionValidationError(f"{label} is unsupported")
    return value


def _require_int(value: object, label: str, *, minimum: int = 0, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DefinitionValidationError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise DefinitionValidationError(f"{label} is outside its allowed range")
    return value


def _require_number(
    value: object, label: str, *, minimum: float, maximum: float
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DefinitionValidationError(f"{label} must be numeric")
    if not math.isfinite(float(value)) or not minimum <= float(value) <= maximum:
        raise DefinitionValidationError(f"{label} is outside its allowed range")
    return value


def _require_identifiers(value: object, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise DefinitionValidationError(f"{label} must be a non-empty list")
    output = [_bounded_identifier(item, label) for item in value]
    if len(output) != len(set(output)):
        raise DefinitionValidationError(f"{label} must not contain duplicates")
    return output


def _require_version_id(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise DefinitionValidationError(
            f"{label} must be a lowercase SHA-256 version identity"
        )
    return value


def _require_iso_date(value: object, label: str) -> date:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None
    ):
        raise DefinitionValidationError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DefinitionValidationError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise DefinitionValidationError(f"{label} must be a canonical ISO date")
    return parsed


def _require_hhmm(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", value
    ) is None:
        raise DefinitionValidationError(f"{label} is invalid")
    return value


def _require_internal_route(value: str, label: str) -> None:
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(
            unicodedata.category(character).startswith("C")
            for character in value
        )
        or "?" in value
        or "#" in value
    ):
        raise DefinitionValidationError(f"{label} must be an internal pathname")


def _require_internal_route_pattern(value: str, label: str) -> None:
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(
            unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        raise DefinitionValidationError(
            f"{label} must begin with a safe internal pathname"
        )


def _validate_goal(content: dict[str, Any]) -> None:
    _require_fields(
        content,
        allowed=_GOAL_FIELDS,
        required=_GOAL_REQUIRED,
        definition_type=DefinitionType.GOAL,
    )
    goal_type = _require_choice(
        content["goal_type"],
        {"page", "event", "form", "download", "outbound_action", "revenue", "composite"},
        "goal.goal_type",
    )
    referenced_goals = content.get("goal_version_ids")
    if goal_type == "composite":
        if not isinstance(referenced_goals, list) or not referenced_goals:
            raise DefinitionValidationError(
                "composite goals require goal_version_ids"
            )
        if len(referenced_goals) != len(set(referenced_goals)):
            raise DefinitionValidationError(
                "goal.goal_version_ids must not contain duplicates"
            )
        for version_id in referenced_goals:
            _require_version_id(version_id, "goal.goal_version_ids")
    elif referenced_goals is not None:
        raise DefinitionValidationError(
            "goal_version_ids are supported only for composite goals"
        )
    _require_identifiers(content["site_ids"], "goal.site_ids")
    for field in ("source", "metric", "unit"):
        _bounded_identifier(content[field], f"goal.{field}")
    aggregation = _require_choice(
        content["aggregation"],
        {"sum", "count", "maximum", "minimum", "latest", "ratio"},
        "goal.aggregation",
    )
    _require_choice(
        content["date_basis"], {"utc", "site_local", "provider"}, "goal.date_basis"
    )
    _require_int(content["maturity_lag_days"], "goal.maturity_lag_days", maximum=365)
    _require_number(
        content["coverage_requirement"],
        "goal.coverage_requirement",
        minimum=0,
        maximum=1,
    )
    _require_choice(content["confidence"], {"high", "medium", "low"}, "goal.confidence")
    active_start = (
        _require_iso_date(content["active_start"], "goal.active_start")
        if "active_start" in content
        else None
    )
    active_end = (
        _require_iso_date(content["active_end"], "goal.active_end")
        if "active_end" in content
        else None
    )
    if (
        active_start is not None
        and active_end is not None
        and active_end < active_start
    ):
        raise DefinitionValidationError(
            "goal.active_end must not precede goal.active_start"
        )
    bindings = content["provider_bindings"]
    if not isinstance(bindings, list) or not bindings:
        raise DefinitionValidationError("goal.provider_bindings must be a non-empty list")
    canonical_count = 0
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict) or set(binding) != {"metric", "role", "source"}:
            raise DefinitionValidationError(
                f"goal.provider_bindings[{index}] has an invalid shape"
            )
        _bounded_identifier(binding["source"], "goal provider source")
        _bounded_identifier(binding["metric"], "goal provider metric")
        role = _require_choice(
            binding["role"], {"canonical", "corroborating"}, "goal provider role"
        )
        canonical_count += role == "canonical"
    if canonical_count != 1:
        raise DefinitionValidationError(
            "goal.provider_bindings must contain exactly one canonical binding"
        )
    denominator = content.get("denominator")
    if aggregation == "ratio" and denominator is None:
        raise DefinitionValidationError(
            "goal aggregation ratio requires denominator"
        )
    if aggregation != "ratio" and denominator is not None:
        raise DefinitionValidationError(
            "goal denominator is supported only for ratio aggregation"
        )
    if denominator is not None:
        expected = {
            "completeness_policy",
            "date_basis",
            "grain",
            "metric",
            "scope",
            "unit",
            "window",
            "zero_behavior",
        }
        if not isinstance(denominator, dict) or set(denominator) != expected:
            raise DefinitionValidationError("goal.denominator has an invalid shape")
        for field in ("metric", "unit", "scope"):
            _bounded_identifier(denominator[field], f"goal.denominator.{field}")
        _require_choice(
            denominator["grain"],
            {item.value for item in TimeGrain},
            "goal.denominator.grain",
        )
        _require_int(denominator["window"], "goal.denominator.window", minimum=1, maximum=3660)
        _require_choice(
            denominator["date_basis"],
            {"utc", "site_local", "provider"},
            "goal.denominator.date_basis",
        )
        _require_choice(
            denominator["completeness_policy"],
            {"final_only", "allow_provisional"},
            "goal.denominator.completeness_policy",
        )
        _require_choice(
            denominator["zero_behavior"],
            {"unknown", "zero"},
            "goal.denominator.zero_behavior",
        )
    filters = content.get("filters", {})
    if not isinstance(filters, dict):
        raise DefinitionValidationError("goal.filters must be an object")
    for key, value in filters.items():
        _bounded_identifier(key, "goal filter")
        if _field_name_is_prohibited(key):
            raise DefinitionValidationError(
                f"goal filter {key} is private or prohibited"
            )
        if not isinstance(value, str):
            raise DefinitionValidationError("goal filter values must be strings")


def _validate_segment_node(node: object, *, depth: int = 0, counter: list[int]) -> None:
    counter[0] += 1
    if counter[0] > 100 or depth > MAX_DEFINITION_DEPTH:
        raise DefinitionValidationError("segment expression exceeds its complexity limit")
    if not isinstance(node, dict):
        raise DefinitionValidationError("segment expression nodes must be objects")
    logical = set(node) & {"all", "any", "not"}
    if logical:
        if len(node) != 1 or len(logical) != 1:
            raise DefinitionValidationError("segment logical nodes have an invalid shape")
        operator = next(iter(logical))
        children = node[operator]
        if operator == "not":
            _validate_segment_node(children, depth=depth + 1, counter=counter)
            return
        if not isinstance(children, list) or not children:
            raise DefinitionValidationError("segment all/any nodes require children")
        for child in children:
            _validate_segment_node(child, depth=depth + 1, counter=counter)
        return
    allowed = {"dimension", "operator", "value"}
    if set(node) - allowed or not {"dimension", "operator"}.issubset(node):
        raise DefinitionValidationError("segment predicate has an invalid shape")
    dimension = _require_choice(
        node["dimension"], _SEGMENT_DIMENSIONS, "segment dimension"
    )
    operator = _require_choice(
        node["operator"], _SEGMENT_OPERATORS, "segment operator"
    )
    requires_value = operator not in {"is_present", "is_missing"}
    if requires_value != ("value" in node):
        raise DefinitionValidationError("segment predicate value does not match its operator")
    if not requires_value:
        return
    value = node["value"]
    if operator in {"in", "not_in"}:
        if not isinstance(value, list) or not value or len(value) > 50:
            raise DefinitionValidationError("segment list predicate is invalid")
        if any(not isinstance(item, str) for item in value):
            raise DefinitionValidationError("segment list values must be strings")
    elif not isinstance(value, str):
        raise DefinitionValidationError("segment predicate value must be a string")
    if operator == "matches_safe_pattern":
        if (
            len(value) > 128
            or _SAFE_PATTERN_FORBIDDEN.search(value)
            or _safe_pattern_is_too_complex(value)
        ):
            raise DefinitionValidationError("segment safe pattern is too complex")
        try:
            re.compile(value)
        except re.error as exc:
            raise DefinitionValidationError("segment safe pattern is invalid") from exc
    if dimension in {"route", "landing_route"}:
        values = value if isinstance(value, list) else [value]
        for route_value in values:
            if operator == "matches_safe_pattern":
                _require_internal_route_pattern(
                    route_value,
                    "route segment patterns",
                )
            else:
                _require_internal_route(route_value, "route segment values")


def _validate_segment(content: dict[str, Any]) -> None:
    _require_fields(
        content,
        allowed=_SEGMENT_FIELDS,
        required={"expression"},
        definition_type=DefinitionType.SEGMENT,
    )
    if "site_ids" in content:
        _require_identifiers(content["site_ids"], "segment.site_ids")
    _validate_segment_node(content["expression"], counter=[0])


def _validate_alert(content: dict[str, Any]) -> None:
    _require_fields(
        content,
        allowed=_ALERT_FIELDS,
        required=_ALERT_REQUIRED,
        definition_type=DefinitionType.ALERT_RULE,
    )
    rule_type = _require_choice(
        content["rule_type"],
        set(_ALERT_RULE_FIELD_MATRIX),
        "alert_rule.rule_type",
    )
    field_matrix = _ALERT_RULE_FIELD_MATRIX[rule_type]
    present_conditional_fields = set(content) & _ALERT_CONDITIONAL_FIELDS
    missing_conditional_fields = (
        field_matrix["required"] - present_conditional_fields
    )
    forbidden_conditional_fields = (
        field_matrix["forbidden"] & present_conditional_fields
    )
    if missing_conditional_fields:
        raise DefinitionValidationError(
            f"alert_rule {rule_type} requires fields: "
            f"{', '.join(sorted(missing_conditional_fields))}"
        )
    if forbidden_conditional_fields:
        raise DefinitionValidationError(
            f"alert_rule {rule_type} forbids fields: "
            f"{', '.join(sorted(forbidden_conditional_fields))}"
        )
    _require_identifiers(content["site_ids"], "alert_rule.site_ids")
    _require_choice(
        content["evaluation_grain"],
        {item.value for item in TimeGrain},
        "alert_rule.evaluation_grain",
    )
    _require_int(
        content["maturity_lag_days"], "alert_rule.maturity_lag_days", maximum=365
    )
    _require_number(
        content["minimum_baseline"],
        "alert_rule.minimum_baseline",
        minimum=0,
        maximum=1_000_000_000,
    )
    quiet_periods = content["quiet_periods"]
    if not isinstance(quiet_periods, list) or len(quiet_periods) > 32:
        raise DefinitionValidationError("alert_rule.quiet_periods must be a bounded list")
    for period in quiet_periods:
        if not isinstance(period, dict) or set(period) != {"end", "start"}:
            raise DefinitionValidationError("alert_rule quiet period has an invalid shape")
        for field in ("start", "end"):
            _require_hhmm(
                period[field],
                "alert_rule quiet-period time",
            )
    _require_choice(
        content["incomplete_data_policy"],
        {"allow", "suppress"},
        "alert_rule.incomplete_data_policy",
    )
    _require_int(
        content["cooldown_minutes"],
        "alert_rule.cooldown_minutes",
        maximum=525_600,
    )
    _require_choice(
        content["severity"], {"info", "warning", "critical"}, "alert_rule.severity"
    )
    for field in ("source", "metric", "goal_version_id", "segment_version_id"):
        if field in content:
            if field.endswith("_version_id"):
                _require_version_id(content[field], f"alert_rule.{field}")
            else:
                _bounded_identifier(content[field], f"alert_rule.{field}")
    for field in ("threshold", "comparison"):
        if field in content:
            _require_number(
                content[field],
                f"alert_rule.{field}",
                minimum=-1_000_000_000,
                maximum=1_000_000_000,
            )


def _validate_subscription(content: dict[str, Any]) -> None:
    _require_fields(
        content,
        allowed=_SUBSCRIPTION_FIELDS,
        required=_SUBSCRIPTION_REQUIRED,
        definition_type=DefinitionType.REPORT_SUBSCRIPTION,
    )
    _bounded_identifier(content["report_type"], "report_subscription.report_type")
    _require_identifiers(content["site_ids"], "report_subscription.site_ids")
    timezone = content["timezone"]
    if not isinstance(timezone, str):
        raise DefinitionValidationError("report_subscription.timezone must be a string")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise DefinitionValidationError("report_subscription.timezone is invalid") from exc
    _require_choice(
        content["frequency"],
        {"daily", "weekly", "monthly", "quarterly"},
        "report_subscription.frequency",
    )
    _require_int(
        content["maturity_lag_days"],
        "report_subscription.maturity_lag_days",
        maximum=365,
    )
    _require_choice(
        content["incomplete_data_policy"],
        {"allow", "suppress"},
        "report_subscription.incomplete_data_policy",
    )
    formats = content["formats"]
    if (
        not isinstance(formats, list)
        or not formats
        or len(formats) != len(set(formats))
        or any(item not in {"csv", "html", "json", "pdf"} for item in formats)
    ):
        raise DefinitionValidationError("report_subscription.formats is invalid")
    recipient_set_id = content["recipient_set_id"]
    if (
        not isinstance(recipient_set_id, str)
        or len(recipient_set_id) != 64
        or re.fullmatch(r"[0-9a-f]{64}", recipient_set_id) is None
    ):
        raise DefinitionValidationError(
            "report_subscription.recipient_set_id must be a keyed lowercase SHA-256 digest"
        )
    if "goal_version_ids" in content:
        goal_version_ids = content["goal_version_ids"]
        if not isinstance(goal_version_ids, list):
            raise DefinitionValidationError(
                "report_subscription.goal_version_ids must be a list"
            )
        if len(goal_version_ids) != len(set(goal_version_ids)):
            raise DefinitionValidationError(
                "report_subscription.goal_version_ids must not contain duplicates"
            )
        for version_id in goal_version_ids:
            _require_version_id(
                version_id, "report_subscription.goal_version_ids"
            )
    if "segment_version_id" in content:
        _require_version_id(
            content["segment_version_id"], "report_subscription.segment_version_id"
        )


class _FrozenDefinitionMapping(Mapping[str, Any]):
    """Deeply immutable dictionary used by the public definition boundary."""

    __slots__ = ("_entries",)

    def __init__(self, value: object = ()) -> None:
        material = dict(value)  # type: ignore[arg-type]
        object.__setattr__(
            self,
            "_entries",
            tuple(
                (key, _freeze_public_definition_value(item))
                for key, item in material.items()
            ),
        )

    def __getitem__(self, key: str) -> Any:
        for entry_key, value in self._entries:
            if entry_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return repr(dict(self.items()))

    def __setitem__(self, key: str, value: object) -> None:
        raise TypeError("analytics definition values are immutable")

    def __copy__(self) -> _FrozenDefinitionMapping:
        return self

    def __deepcopy__(
        self, memo: dict[int, object]
    ) -> _FrozenDefinitionMapping:
        memo[id(self)] = self
        return self

    def __reduce__(
        self,
    ) -> tuple[type[_FrozenDefinitionMapping], tuple[dict[str, Any]]]:
        return (_FrozenDefinitionMapping, (dict(self.items()),))


def _freeze_public_definition_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenDefinitionMapping(value)
    if isinstance(value, list):
        return tuple(_freeze_public_definition_value(item) for item in value)
    return value


def _snapshot_public_definition_value(value: Any) -> Any:
    """Materialize one detached plain-data snapshot of caller-owned input."""

    if isinstance(value, Mapping):
        material = dict(value)
        return {
            key: _snapshot_public_definition_value(item)
            for key, item in material.items()
        }
    if isinstance(value, (list, tuple)):
        material = tuple(value)
        return [
            _snapshot_public_definition_value(item)
            for item in material
        ]
    if isinstance(value, str):
        return str(value)
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return copy.deepcopy(value)


def _thaw_public_definition_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _thaw_public_definition_value(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_thaw_public_definition_value(item) for item in value]
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class AnalyticsDefinition:
    definition_type: DefinitionType
    definition_key: str
    scope_key: str
    content: Mapping[str, Any]
    metadata: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, Mapping):
            raise DefinitionValidationError("definition content must be an object")
        content = _snapshot_public_definition_value(self.content)
        _validate_json_value(content, "content")
        metadata: dict[str, str] | None = None
        if self.metadata is not None:
            if not isinstance(self.metadata, Mapping):
                raise DefinitionValidationError(
                    "definition metadata must be an object"
                )
            metadata = _snapshot_public_definition_value(self.metadata)
            _validate_json_value(metadata, "metadata")
        object.__setattr__(self, "content", _FrozenDefinitionMapping(content))
        object.__setattr__(
            self,
            "metadata",
            _FrozenDefinitionMapping(metadata) if metadata is not None else None,
        )


@dataclass(frozen=True, slots=True)
class ValidatedDefinition:
    definition_type: DefinitionType
    definition_key: str
    scope_key: str
    content_json: str
    metadata_json: str
    content_hash: str


def _validate_definition_payload(
    *,
    definition_type: DefinitionType,
    definition_key: str,
    scope_key: str,
    content: dict[str, Any],
    metadata: dict[str, str] | None,
) -> ValidatedDefinition:
    """Validate and serialize material that is already safe to canonicalize."""

    _validate_json_value(content, "content")
    {
        DefinitionType.GOAL: _validate_goal,
        DefinitionType.SEGMENT: _validate_segment,
        DefinitionType.ALERT_RULE: _validate_alert,
        DefinitionType.REPORT_SUBSCRIPTION: _validate_subscription,
    }[definition_type](content)
    metadata_copy = {} if metadata is None else copy.deepcopy(metadata)
    if not isinstance(metadata_copy, dict):
        raise DefinitionValidationError("definition metadata must be an object")
    unknown_metadata = set(metadata_copy) - _METADATA_FIELDS
    if unknown_metadata:
        raise DefinitionValidationError(
            f"metadata contains unknown fields: {', '.join(sorted(unknown_metadata))}"
        )
    _validate_json_value(metadata_copy, "metadata")
    if any(not isinstance(value, str) for value in metadata_copy.values()):
        raise DefinitionValidationError("metadata values must be strings")
    try:
        content_json = json.dumps(
            content,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        metadata_json = json.dumps(
            metadata_copy,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise DefinitionValidationError("definition is not canonical JSON") from exc
    if len(content_json.encode("utf-8")) > MAX_DEFINITION_CONTENT_BYTES:
        raise DefinitionValidationError("canonical definition content is too large")
    if len(metadata_json.encode("utf-8")) > MAX_DEFINITION_METADATA_BYTES:
        raise DefinitionValidationError("canonical definition metadata is too large")
    return ValidatedDefinition(
        definition_type=definition_type,
        definition_key=definition_key,
        scope_key=scope_key,
        content_json=content_json,
        metadata_json=metadata_json,
        content_hash=hashlib.sha256(content_json.encode("utf-8")).hexdigest(),
    )


def validate_analytics_definition(
    definition: AnalyticsDefinition,
    *,
    recipient_set: Sequence[str] | None = None,
    recipient_digest_key: bytes | None = None,
) -> ValidatedDefinition:
    """Validate and canonically serialize a storage-safe definition."""

    try:
        definition_type = DefinitionType(definition.definition_type)
    except (TypeError, ValueError) as exc:
        raise DefinitionValidationError("definition_type is unsupported") from exc
    definition_key = _bounded_identifier(definition.definition_key, "definition_key")
    scope_key = _bounded_identifier(definition.scope_key, "scope_key")
    if not isinstance(definition.content, Mapping):
        raise DefinitionValidationError("definition content must be an object")
    content = _thaw_public_definition_value(definition.content)
    has_recipient_input = recipient_set is not None or recipient_digest_key is not None
    if definition_type is DefinitionType.REPORT_SUBSCRIPTION:
        if "recipient_set_id" in content:
            raise DefinitionValidationError(
                "report_subscription.recipient_set_id must be derived during validation"
            )
        if (
            recipient_set is None
            or recipient_digest_key is None
        ):
            raise DefinitionValidationError(
                "report_subscription requires private recipient inputs"
            )
        content["recipient_set_id"] = _derive_recipient_set_identifier(
            recipient_set,
            recipient_digest_key,
        )
    elif has_recipient_input:
        raise DefinitionValidationError(
            "private recipient inputs are valid only for report_subscription"
        )
    return _validate_definition_payload(
        definition_type=definition_type,
        definition_key=definition_key,
        scope_key=scope_key,
        content=content,
        metadata=(
            _thaw_public_definition_value(definition.metadata)
            if definition.metadata is not None
            else None
        ),
    )


def _validate_persisted_analytics_definition(
    *,
    definition_type: object,
    definition_key: object,
    scope_key: object,
    content_json: object,
    metadata_json: object,
) -> ValidatedDefinition:
    """Revalidate canonical stored bytes without accepting them as public input."""

    try:
        parsed_type = DefinitionType(definition_type)
    except (TypeError, ValueError) as exc:
        raise DefinitionValidationError("definition_type is unsupported") from exc
    parsed_key = _bounded_identifier(definition_key, "definition_key")
    parsed_scope = _bounded_identifier(scope_key, "scope_key")
    if not isinstance(content_json, str) or not isinstance(metadata_json, str):
        raise DefinitionValidationError("stored definition JSON must be text")
    try:
        content = json.loads(content_json)
        metadata = json.loads(metadata_json)
    except (TypeError, ValueError) as exc:
        raise DefinitionValidationError("stored definition is not JSON") from exc
    if not isinstance(content, dict) or not isinstance(metadata, dict):
        raise DefinitionValidationError("stored definition JSON must contain objects")
    validated = _validate_definition_payload(
        definition_type=parsed_type,
        definition_key=parsed_key,
        scope_key=parsed_scope,
        content=content,
        metadata=metadata,
    )
    if (
        validated.content_json != content_json
        or validated.metadata_json != metadata_json
    ):
        raise DefinitionValidationError("stored definition JSON is not canonical")
    return validated


@dataclass(frozen=True, slots=True)
class DefinitionVersion:
    id: str
    scope_key: str
    definition_type: DefinitionType
    definition_key: str
    version: int
    content_hash: str
    content_json: str
    metadata_json: str
    created_at: datetime
    record_hash: str


@dataclass(frozen=True, slots=True)
class DefinitionActivation:
    id: str
    definition_version_id: str
    scope_key: str
    definition_type: DefinitionType
    definition_key: str
    activated_at: datetime
    retired_at: datetime | None
    record_hash: str


@dataclass(frozen=True, slots=True)
class DefinitionIdentity:
    scope_key: str
    definition_type: DefinitionType
    definition_key: str


def validate_definition_identity(identity: DefinitionIdentity) -> DefinitionIdentity:
    try:
        definition_type = DefinitionType(identity.definition_type)
    except (TypeError, ValueError) as exc:
        raise DefinitionValidationError("definition_type is unsupported") from exc
    return DefinitionIdentity(
        scope_key=_bounded_identifier(identity.scope_key, "scope_key"),
        definition_type=definition_type,
        definition_key=_bounded_identifier(identity.definition_key, "definition_key"),
    )


@dataclass(frozen=True, slots=True)
class DefinitionChange:
    version: DefinitionVersion
    activation: DefinitionActivation
    outcome: str


@dataclass(frozen=True, slots=True)
class DefinitionPackageResult:
    changes: tuple[DefinitionChange, ...]
    retired: tuple[DefinitionActivation, ...]


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def canonical_dimensions(values: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Return a stable, duplicate-free dimension representation."""

    output: list[tuple[str, str]] = []
    for key, value in values.items():
        if not key or not value:
            raise ValueError("dimension keys and values must be non-empty")
        output.append((key, value))
    return tuple(sorted(output))


@dataclass(frozen=True, slots=True)
class QueryWindow:
    start: datetime
    end: datetime
    timezone: str
    completeness: Completeness = Completeness.FINAL

    def __post_init__(self) -> None:
        _require_aware(self.start, "start")
        _require_aware(self.end, "end")
        if self.start >= self.end:
            raise ValueError("start must be earlier than end")
        if self.timezone != "UTC":
            try:
                ZoneInfo(self.timezone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"unknown IANA timezone: {self.timezone}") from exc


@dataclass(frozen=True, slots=True)
class MetricPoint:
    client_id: str
    site_id: str
    source: str
    metric: str
    unit: str
    start: datetime
    end: datetime
    grain: TimeGrain
    value: Decimal
    dimensions: tuple[tuple[str, str], ...]
    completeness: Completeness
    observed_at: datetime

    def __post_init__(self) -> None:
        for label, value in (
            ("client_id", self.client_id),
            ("site_id", self.site_id),
            ("source", self.source),
            ("metric", self.metric),
        ):
            if not value:
                raise ValueError(f"{label} must be non-empty")
        _require_aware(self.start, "start")
        _require_aware(self.end, "end")
        _require_aware(self.observed_at, "observed_at")
        if self.start >= self.end:
            raise ValueError("metric interval start must be earlier than end")
        if not self.value.is_finite():
            raise ValueError("metric value must be finite")
        if self.dimensions != tuple(sorted(self.dimensions)):
            raise ValueError("dimensions must use canonical sorted order")
        if any(not key or not value for key, value in self.dimensions):
            raise ValueError("dimension keys and values must be non-empty")
        if len({key for key, _value in self.dimensions}) != len(self.dimensions):
            raise ValueError("dimension keys must be unique")


MAX_ACQUISITION_IDENTIFIER_BYTES = 128
MAX_ACQUISITION_DIMENSIONS = 32


def _validate_acquisition_identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or not _SAFE_KEY.fullmatch(value):
        raise ValueError(
            f"{label} must use only bounded ASCII letters, digits, '.', '_', ':', and '-'"
        )
    if len(value.encode("ascii")) > MAX_ACQUISITION_IDENTIFIER_BYTES:
        raise ValueError(f"{label} is too long")


@dataclass(frozen=True, slots=True)
class AcquisitionSlice:
    """One bounded provider request whose completeness can be audited."""

    slice_key: str
    metric_family: str
    start: datetime
    end: datetime
    completeness: Completeness
    data_state: str
    provider_scope: str
    request_dimensions: tuple[str, ...]
    provider_aggregation: str
    pages_fetched: int
    raw_rows: int
    accepted_rows: int
    rejected_rows: int
    exhaustion_reason: str

    def __post_init__(self) -> None:
        for label, value in (
            ("slice_key", self.slice_key),
            ("metric_family", self.metric_family),
            ("data_state", self.data_state),
            ("provider_scope", self.provider_scope),
            ("provider_aggregation", self.provider_aggregation),
            ("exhaustion_reason", self.exhaustion_reason),
        ):
            _validate_acquisition_identifier(value, label)
        _require_aware(self.start, "start")
        _require_aware(self.end, "end")
        if self.start >= self.end:
            raise ValueError("acquisition slice start must be earlier than end")
        if not isinstance(self.completeness, Completeness):
            raise ValueError("completeness must be a Completeness value")
        if not isinstance(self.request_dimensions, tuple):
            raise ValueError("request_dimensions must be an immutable tuple")
        if len(self.request_dimensions) > MAX_ACQUISITION_DIMENSIONS:
            raise ValueError("request_dimensions contains too many dimensions")
        for dimension in self.request_dimensions:
            _validate_acquisition_identifier(dimension, "request dimension")
        if len(set(self.request_dimensions)) != len(self.request_dimensions):
            raise ValueError("request_dimensions must be unique")
        request_dimensions_json = json.dumps(
            list(self.request_dimensions), separators=(",", ":"), ensure_ascii=True
        )
        if len(request_dimensions_json.encode("utf-8")) > 4096:
            raise ValueError("request_dimensions exceed the storage limit")
        for label, value in (
            ("pages_fetched", self.pages_fetched),
            ("raw_rows", self.raw_rows),
            ("accepted_rows", self.accepted_rows),
            ("rejected_rows", self.rejected_rows),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        if self.pages_fetched < 1:
            raise ValueError("pages_fetched must include at least one provider response")
        if self.accepted_rows + self.rejected_rows != self.raw_rows:
            raise ValueError("accepted_rows plus rejected_rows must equal raw_rows")
        if self.completeness is Completeness.FINAL and self.rejected_rows:
            raise ValueError("a final acquisition slice cannot contain rejected rows")


@dataclass(frozen=True, slots=True)
class AcquisitionBatch:
    """An acquisition slice and the normalized metric facts it owns."""

    slice: AcquisitionSlice
    points: tuple[MetricPoint, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.slice, AcquisitionSlice):
            raise ValueError("slice must be an AcquisitionSlice")
        if not isinstance(self.points, tuple):
            raise ValueError("points must be an immutable tuple")
        if any(not isinstance(point, MetricPoint) for point in self.points):
            raise ValueError("points must contain only MetricPoint values")
        # Discovery/control requests may accept rows without producing facts,
        # but a fact-bearing batch must be backed by provider evidence.
        if self.points and self.slice.accepted_rows == 0:
            raise ValueError("metric points require an accepted provider row")
        for point in self.points:
            # Provider request dates and normalized fact dates can use different
            # declared bases (notably Search Console Pacific dates mapped to a
            # site's reporting day). The run, request slice, and fact interval
            # are therefore retained separately instead of fabricating a union.
            if point.start >= self.slice.end or point.end <= self.slice.start:
                raise ValueError("metric point must overlap its acquisition slice")


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    connection_id: str
    provider: str
    probed_at: datetime
    authentication_ok: bool
    resources: tuple[str, ...]
    metric_groups: tuple[str, ...]
    max_lookback_days: int | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_aware(self.probed_at, "probed_at")
        if not self.connection_id or not self.provider:
            raise ValueError("connection_id and provider must be non-empty")
        if self.max_lookback_days is not None and self.max_lookback_days < 0:
            raise ValueError("max_lookback_days cannot be negative")
        if self.resources != tuple(sorted(set(self.resources))):
            raise ValueError("resources must be unique and sorted")
        if self.metric_groups != tuple(sorted(set(self.metric_groups))):
            raise ValueError("metric_groups must be unique and sorted")


@dataclass(frozen=True, slots=True)
class SubreportDefinition:
    id: str
    title: str
    section_ids: tuple[str, ...]
    default_window_days: int
    filters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not self.title or not self.section_ids:
            raise ValueError("subreport id, title, and section_ids are required")
        if self.default_window_days < 1:
            raise ValueError("default_window_days must be positive")
        if self.filters != tuple(sorted(self.filters)):
            raise ValueError("filters must use canonical sorted order")
        if len({key for key, _value in self.filters}) != len(self.filters):
            raise ValueError("filter keys must be unique")


@dataclass(frozen=True, slots=True)
class ReportDefinition:
    id: str
    title: str
    client_id: str
    site_ids: tuple[str, ...]
    section_ids: tuple[str, ...]
    default_window_days: int
    subreports: tuple[SubreportDefinition, ...] = ()

    def __post_init__(self) -> None:
        if (
            not self.id
            or not self.title
            or not self.client_id
            or not self.site_ids
            or not self.section_ids
        ):
            raise ValueError(
                "report id, title, client_id, site_ids, and section_ids are required"
            )
        if self.default_window_days < 1:
            raise ValueError("default_window_days must be positive")
        subreport_ids = [item.id for item in self.subreports]
        if len(set(subreport_ids)) != len(subreport_ids):
            raise ValueError("subreport ids must be unique within a report")
        if len(set(self.site_ids)) != len(self.site_ids):
            raise ValueError("site_ids must be unique within a report")
        if len(set(self.section_ids)) != len(self.section_ids):
            raise ValueError("section_ids must be unique within a report")
