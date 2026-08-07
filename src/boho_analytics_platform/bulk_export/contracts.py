"""Contracts shared by the BigQuery source and the private Parquet lake."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from .config import SearchConsolePropertyConfig


SEARCHDATA_TABLES = (
    "searchdata_site_impression",
    "searchdata_url_impression",
)


@dataclass(frozen=True, slots=True)
class ExportRevision:
    namespace: str
    data_date: date
    epoch_version: int
    publish_time: datetime


@dataclass(frozen=True, slots=True)
class PartitionTotals:
    row_count: int
    clicks: int
    impressions: int
    position_sum: Decimal

    def json_value(self) -> dict[str, object]:
        return {
            "row_count": self.row_count,
            "clicks": self.clicks,
            "impressions": self.impressions,
            "position_sum": str(self.position_sum),
        }


@dataclass(frozen=True, slots=True)
class PartitionRead:
    batches: Iterable[Any]
    source_schema: tuple[Mapping[str, object], ...]
    expected_totals: PartitionTotals
    query_audit: list[Mapping[str, object]]
    export_log_history: list[Mapping[str, object]]


class SearchConsoleBulkSource(Protocol):
    def probe(self, property_config: SearchConsolePropertyConfig) -> Mapping[str, object]:
        """Return sanitized dataset/table capability metadata."""

    def revisions(
        self,
        property_config: SearchConsolePropertyConfig,
        start: date,
        end: date,
    ) -> tuple[ExportRevision, ...]:
        """Return successful ExportLog revisions for an exclusive date window."""

    def read_partition(
        self,
        property_config: SearchConsolePropertyConfig,
        revision: ExportRevision,
    ) -> PartitionRead:
        """Return a bounded, streaming read for exactly one exported partition."""
