"""Provider-neutral domain models for ingestion and reporting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
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
