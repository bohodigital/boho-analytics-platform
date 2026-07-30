from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import re
from typing import Any
import unicodedata
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from ..models import Completeness, MetricPoint, TimeGrain


_EMAIL_PATTERN = re.compile(r"[^\s/@]+@[^\s/@]+\.[^\s/@]+")
_PHONE_PATTERN = re.compile(r"(?:\+?\d[\s().-]*){7,}\d")
_PERCENT_ESCAPE_PATTERN = re.compile(r"%(?![0-9a-fA-F]{2})")
_VALID_PERCENT_ESCAPE_PATTERN = re.compile(r"%[0-9a-fA-F]{2}")
_ENCODED_PATH_SEPARATOR_PATTERN = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
_SCHEME_LIKE_PATH_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?:/|%2f)", re.IGNORECASE)
_DOMAIN_PATTERN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_DECIMAL_TEXT_PATTERN = re.compile(
    r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"
)
_LEXICAL_CONTENT_SLUG_PATTERN = re.compile(r"^[a-z]+(?:-[a-z]+)+$")
_MAX_COUNT_RAW_LENGTH = 128
_MAX_COUNT_SIGNIFICANT_DIGITS = 38
_MAX_COUNT_ADJUSTED_EXPONENT = 37
_MAX_COUNT_INTEGER_BITS = 127

_SAFE_CONTENT_SLUG_WORDS = frozenset({
    "alpha", "appointment", "article", "blog", "booking", "careers",
    "case", "contact", "documentation", "docs", "event", "faq",
    "guide", "help", "news", "page", "portfolio", "post", "pricing",
    "privacy", "product", "project", "resource", "service", "study",
    "support", "team", "terms",
})

_OPAQUE_IDENTITY_ROUTE_LABELS = frozenset({
    "auth",
    "invite",
    "oauth",
    "reset",
    "resource",
    "session",
    "sessions",
    "token",
    "tokens",
})


def nonnegative_bounded_number(
    value: object, *, integral: bool = False
) -> Decimal | None:
    """Return a provider number only inside the bounded reporting domain."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        if value < 0 or value.bit_length() > _MAX_COUNT_INTEGER_BITS:
            return None
        parsed = Decimal(value)
    elif isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, float):
        raw = str(value)
        if len(raw) > _MAX_COUNT_RAW_LENGTH:
            return None
        try:
            parsed = Decimal(raw)
        except (InvalidOperation, ValueError):
            return None
    elif isinstance(value, str):
        raw = value.strip()
        if (
            not raw
            or len(raw) > _MAX_COUNT_RAW_LENGTH
            or _DECIMAL_TEXT_PATTERN.fullmatch(raw) is None
        ):
            return None
        try:
            parsed = Decimal(raw)
        except (InvalidOperation, ValueError):
            return None
    else:
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    digits = parsed.as_tuple().digits
    if (
        len(digits) > _MAX_COUNT_SIGNIFICANT_DIGITS
        or abs(parsed.adjusted()) > _MAX_COUNT_ADJUSTED_EXPONENT
        or (integral and parsed != parsed.to_integral_value())
    ):
        return None
    return parsed


def nonnegative_integral_count(value: object) -> Decimal | None:
    """Return a provider count only when its exact numeric domain is safe."""

    return nonnegative_bounded_number(value, integral=True)


def site_local_daily_bounds(window, timezone: str) -> tuple[datetime, datetime]:
    """Return an exact nonempty site-local half-open daily window."""

    zone = UTC if timezone == "UTC" else ZoneInfo(timezone)
    start = window.start.astimezone(zone)
    end = window.end.astimezone(zone)
    expected_start = datetime.combine(start.date(), time.min, zone)
    expected_end = datetime.combine(end.date(), time.min, zone)
    if (
        start.astimezone(UTC) != expected_start.astimezone(UTC)
        or end.astimezone(UTC) != expected_end.astimezone(UTC)
        or expected_start.astimezone(UTC) >= expected_end.astimezone(UTC)
    ):
        raise ValueError("provider acquisition requires whole site-local days")
    return expected_start, expected_end


def aggregate_dimension_values(rows, *, integral: bool):
    """Combine normalized rows exactly and reject any out-of-domain sum."""

    aggregated = {}
    rejected = False
    for day, dimensions, value in rows:
        parsed = nonnegative_bounded_number(value, integral=integral)
        if parsed is None:
            rejected = True
            continue
        key = (day, tuple(sorted(dimensions.items())))
        if integral:
            aggregated[key] = aggregated.get(key, 0) + int(parsed)
        else:
            with localcontext() as context:
                context.prec = 96
                aggregated[key] = aggregated.get(key, Decimal()) + parsed
    output = []
    for (day, dimensions), value in aggregated.items():
        bounded = nonnegative_bounded_number(value, integral=integral)
        if bounded is None:
            rejected = True
            continue
        output.append((day, dict(dimensions), bounded))
    return output, rejected

def daily_point(*, client_id: str, site_id: str, source: str, metric: str, unit: str,
                day: date | str, value: Any, timezone: str, dimensions: dict[str, str] | None = None,
                observed_at: datetime | None = None,
                completeness: Completeness = Completeness.FINAL) -> MetricPoint:
    parsed = date.fromisoformat(day) if isinstance(day, str) else day
    zone = UTC if timezone == "UTC" else ZoneInfo(timezone)
    start = datetime.combine(parsed, time.min, zone)
    end = datetime.combine(parsed + timedelta(days=1), time.min, zone)
    return MetricPoint(client_id, site_id, source, metric, unit, start, end, TimeGrain.DAY,
        Decimal(str(value)), tuple(sorted((dimensions or {}).items())), completeness,
        observed_at or datetime.now(UTC))


def total_point(*, client_id: str, site_id: str, source: str, metric: str, unit: str,
                start: datetime, end: datetime, value: Any, dimensions: dict[str, str] | None = None,
                observed_at: datetime | None = None) -> MetricPoint:
    return MetricPoint(client_id, site_id, source, metric, unit, start, end, TimeGrain.TOTAL,
        Decimal(str(value)), tuple(sorted((dimensions or {}).items())), Completeness.FINAL,
        observed_at or datetime.now(UTC))


def binding_site(config, site_id: str):
    return next(item for item in config.sites if item.id == site_id)


def connection_bindings(config, connection_id: str):
    bindings = tuple(item for item in config.bindings if item.connection_id == connection_id)
    if not bindings:
        raise ValueError(f"connection {connection_id} has no configured resource bindings")
    return bindings


def timestamp_day(value: object, timezone: str) -> date:
    """Convert an explicitly zoned provider timestamp to a configured local day."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("provider timestamp must be a non-empty ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("provider timestamp is not valid ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("provider timestamp must include a UTC offset")
    zone = UTC if timezone == "UTC" else ZoneInfo(timezone)
    return parsed.astimezone(zone).date()


def option_text(options: dict | Any, key: str, *, required: bool = False, default: str | None = None) -> str | None:
    value = options.get(key, default)
    if value is None and not required: return None
    if not isinstance(value, str) or not value.strip(): raise ValueError(f"connector option {key} must be a non-empty string")
    return value.strip()


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def normalize_route(
    value: object,
    canonical_url: str,
    *,
    allow_query_parameters: tuple[str, ...] = (),
    exclusions: tuple[str, ...] = (),
    path_only: bool = False,
) -> str | None:
    """Return a bounded internal route, never a full URL or arbitrary query."""

    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        return None
    raw = value.strip()
    if not raw:
        return None
    if "\\" in raw or _PERCENT_ESCAPE_PATTERN.search(raw):
        return None
    if path_only:
        if not raw.startswith("/") or raw.startswith("//"):
            return None
        try:
            path_dimension = urlsplit(raw)
        except ValueError:
            return None
        if (
            path_dimension.scheme or path_dimension.netloc
            or path_dimension.query or path_dimension.fragment
        ):
            return None
    try:
        canonical = urlsplit(canonical_url)
        supplied = urlsplit(raw)
        # Accessing port forces malformed port values to fail closed.
        _ = canonical.port
        _ = supplied.port
        if supplied.scheme and not supplied.netloc:
            return None
        origin = f"{canonical.scheme}://{canonical.netloc}/"
        parsed = urlsplit(urljoin(origin, raw))
        _ = parsed.port
    except ValueError:
        return None
    if (
        _ENCODED_PATH_SEPARATOR_PATTERN.search(parsed.path or "")
        or canonical.scheme not in {"http", "https"}
        or not canonical.hostname
        or canonical.username
        or canonical.password
        or parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.hostname is None
        or parsed.hostname.casefold() != canonical.hostname.casefold()
        or (parsed.port or _default_port(parsed.scheme)) != (canonical.port or _default_port(canonical.scheme))
    ):
        return None
    try:
        decoded_path = unquote(parsed.path or "/", errors="strict")
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (UnicodeError, ValueError):
        return None
    if (
        any(unicodedata.category(character) in {"Cc", "Cf"} for character in decoded_path)
        or "\\" in decoded_path
        or decoded_path.startswith("//")
        or _VALID_PERCENT_ESCAPE_PATTERN.search(decoded_path)
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
        or _SCHEME_LIKE_PATH_PATTERN.match(decoded_path.lstrip("/"))
    ):
        return None
    encoded_path = quote(decoded_path, safe="/:@-._~!$&'()*+,;=")
    path = encoded_path if encoded_path.startswith("/") else f"/{encoded_path}"
    path = path.rstrip("/") or "/"
    if len(path) > 4096:
        return None
    if (
        _has_direct_identifier(decoded_path)
        or _has_opaque_identity_route(decoded_path)
    ):
        return None
    if any(path == item or path.startswith(f"{item}/") for item in exclusions):
        return None
    allowed = set(allow_query_parameters)
    retained = [
        (key, item)
        for key, item in pairs
        if key in allowed and len(key) <= 64 and len(item) <= 256
        and _safe_query_value(item)
    ]
    query = "&".join(
        f"{quote(key, safe='-._~')}={quote(item, safe='-._~')}" for key, item in sorted(retained)
    )
    return urlunsplit(("", "", path, query, ""))


def sanitize_referrer(
    value: object,
    canonical_url: str,
    *,
    approved_domains: tuple[str, ...] = (),
    allow_query_parameters: tuple[str, ...] = (),
    exclusions: tuple[str, ...] = (),
) -> dict[str, str] | None:
    """Keep an internal route or an explicitly approved external domain only."""

    route = normalize_route(
        value, canonical_url, allow_query_parameters=allow_query_parameters, exclusions=exclusions
    )
    if route is not None:
        return {"referrer_route": route}
    if not isinstance(value, str) or len(value) > 4096 or _PERCENT_ESCAPE_PATTERN.search(value):
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or hostname is None:
        return None
    domain = hostname.casefold().rstrip(".")
    if domain in {item.casefold().rstrip(".") for item in approved_domains}:
        return {"referrer_domain": domain}
    return None


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _safe_query_value(value: str) -> bool:
    return (
        not any(ord(character) < 32 or ord(character) == 127 for character in value)
        and not _has_direct_identifier(value) and "://" not in value
    )


def safe_public_label(value: object, *, maximum: int) -> str | None:
    """Return a bounded public label only when it cannot resemble direct contact data."""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "://" in normalized:
        return None
    if any(ord(item) < 32 or ord(item) == 127 for item in normalized) or _has_direct_identifier(normalized):
        return None
    return normalized


def safe_domain(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold().rstrip(".")
    return normalized if _DOMAIN_PATTERN.fullmatch(normalized) else None


def _has_direct_identifier(value: str) -> bool:
    return bool(_EMAIL_PATTERN.search(value) or _PHONE_PATTERN.search(value))


def _has_opaque_identity_route(value: str) -> bool:
    """Reject labelled opaque identities without returning or logging the value."""

    segments = tuple(segment for segment in value.split("/") if segment)
    for label, identifier in zip(segments, segments[1:]):
        if (
            label.casefold() not in _OPAQUE_IDENTITY_ROUTE_LABELS
            or len(identifier) < 12
        ):
            continue
        lexical = identifier.casefold()
        compact_hex = lexical.replace("-", "")
        if (
            identifier == lexical
            and _LEXICAL_CONTENT_SLUG_PATTERN.fullmatch(lexical) is not None
            and all(
                word in _SAFE_CONTENT_SLUG_WORDS
                for word in lexical.split("-")
            )
            and not (
                len(compact_hex) >= 16
                and all(character in "0123456789abcdef" for character in compact_hex)
            )
        ):
            continue
        return True
    return False
