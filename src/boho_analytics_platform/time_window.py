"""Strict, comparison-safe report window parsing shared by CLI and web."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import QueryWindow


MAX_WINDOW_DAYS = 3650
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _date(value: str, label: str) -> date:
    if not _ISO_DATE.fullmatch(value):
        raise ValueError(f"{label} must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid ISO date (YYYY-MM-DD)") from exc


def report_window(
    *,
    timezone: str,
    default_days: int,
    start: str | None = None,
    end: str | None = None,
    now: datetime | None = None,
) -> QueryWindow:
    """Build a bounded window whose immediately prior comparison cannot underflow."""

    if (start is None) != (end is None):
        raise ValueError("start and end must be provided together")
    zone = UTC if timezone == "UTC" else ZoneInfo(timezone)
    if start is None:
        if default_days < 1 or default_days > MAX_WINDOW_DAYS:
            raise ValueError(f"days must be from 1 to {MAX_WINDOW_DAYS}")
        end_date = (now or datetime.now(zone)).astimezone(zone).date()
        start_date = end_date - timedelta(days=default_days)
    else:
        start_date = _date(start, "start")
        end_date = _date(end or "", "end")

    duration_days = (end_date - start_date).days
    if duration_days < 1 or duration_days > MAX_WINDOW_DAYS:
        raise ValueError(f"report window must be from 1 to {MAX_WINDOW_DAYS} days")
    if duration_days >= start_date.toordinal():
        raise ValueError("report window is too early to calculate its comparison period")
    return QueryWindow(
        datetime.combine(start_date, time.min, zone),
        datetime.combine(end_date, time.min, zone),
        timezone,
    )
