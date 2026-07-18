from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from ..models import Completeness, MetricPoint, TimeGrain


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
