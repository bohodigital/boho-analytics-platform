"""Revision-aware orchestration for the private Search Console bulk lake."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .config import BulkExportManifest, SearchConsolePropertyConfig
from .contracts import SEARCHDATA_TABLES, ExportRevision, SearchConsoleBulkSource
from .lake import BulkLake, BulkLakeError


@dataclass(frozen=True, slots=True)
class BulkSyncResult:
    site_id: str
    data_date: str | None
    namespace: str | None
    epoch_version: int | None
    status: str
    rows: int = 0
    error_category: str | None = None

    def json_value(self) -> dict[str, object]:
        return {
            "site_id": self.site_id,
            "data_date": self.data_date,
            "namespace": self.namespace,
            "epoch_version": self.epoch_version,
            "status": self.status,
            "rows": self.rows,
            "error_category": self.error_category,
        }


class BulkExportEngine:
    """Mirror complete BigQuery export revisions without touching SQLite."""

    def __init__(
        self,
        manifest: BulkExportManifest,
        source: SearchConsoleBulkSource,
        lake: BulkLake | None = None,
    ) -> None:
        self.manifest = manifest
        self.source = source
        self.lake = lake or BulkLake(manifest)

    def _properties(
        self, selected_site_ids: set[str] | None
    ) -> tuple[SearchConsolePropertyConfig, ...]:
        configured = {item.site_id for item in self.manifest.properties}
        if not selected_site_ids:
            return self.manifest.properties
        unknown = sorted(selected_site_ids - configured)
        if unknown:
            raise ValueError(f"unknown bulk property site id(s): {', '.join(unknown)}")
        return tuple(
            item for item in self.manifest.properties if item.site_id in selected_site_ids
        )

    @staticmethod
    def _validate_window(start: date, end: date) -> None:
        if start >= end:
            raise ValueError("bulk sync start must precede end")
        if (end - start).days > 366:
            raise ValueError("bulk sync windows may not exceed 366 days")

    def probe(self, selected_site_ids: set[str] | None = None) -> list[dict[str, object]]:
        storage = self.lake.preflight(create=True)
        return [
            {"storage": storage, **self.source.probe(property_config)}
            for property_config in self._properties(selected_site_ids)
        ]

    def sync(
        self,
        start: date,
        end: date,
        selected_site_ids: set[str] | None = None,
    ) -> list[BulkSyncResult]:
        self._validate_window(start, end)
        results: list[BulkSyncResult] = []
        with self.lake.lock():
            for property_config in self._properties(selected_site_ids):
                effective_start = max(start, property_config.first_export_date)
                if effective_start >= end:
                    results.append(
                        BulkSyncResult(
                            property_config.site_id,
                            None,
                            None,
                            None,
                            "pre-activation",
                        )
                    )
                    continue
                try:
                    self.source.probe(property_config)
                    revisions = self.source.revisions(
                        property_config, effective_start, end
                    )
                except Exception as exc:
                    results.append(
                        BulkSyncResult(
                            property_config.site_id,
                            None,
                            None,
                            None,
                            "failed",
                            error_category=type(exc).__name__,
                        )
                    )
                    continue
                by_date: dict[date, dict[str, ExportRevision]] = {}
                for revision in revisions:
                    existing = by_date.setdefault(revision.data_date, {}).get(
                        revision.namespace
                    )
                    if existing is not None:
                        raise ValueError("bulk source returned duplicate date/table revisions")
                    by_date[revision.data_date][revision.namespace] = revision

                cursor = effective_start
                while cursor < end:
                    available = by_date.get(cursor, {})
                    if set(available) != set(SEARCHDATA_TABLES):
                        missing = tuple(
                            table for table in SEARCHDATA_TABLES if table not in available
                        )
                        results.append(
                            BulkSyncResult(
                                property_config.site_id,
                                cursor.isoformat(),
                                None,
                                None,
                                "export-log-incomplete",
                                error_category="missing:" + ",".join(missing),
                            )
                        )
                        cursor += timedelta(days=1)
                        continue
                    for namespace in SEARCHDATA_TABLES:
                        revision = available[namespace]
                        try:
                            current_epoch = self.lake.current_epoch(
                                property_config, revision
                            )
                            if current_epoch is not None and current_epoch > revision.epoch_version:
                                raise BulkLakeError(
                                    "local bulk epoch is newer than BigQuery ExportLog"
                                )
                            if current_epoch == revision.epoch_version:
                                value = self.lake.verify_partition(
                                    property_config, revision
                                )
                                results.append(
                                    BulkSyncResult(
                                        property_config.site_id,
                                        cursor.isoformat(),
                                        namespace,
                                        revision.epoch_version,
                                        "current",
                                        rows=int(value["totals"]["row_count"]),
                                    )
                                )
                                continue
                            partition_read = self.source.read_partition(
                                property_config, revision
                            )
                            value = self.lake.write_partition(
                                property_config, revision, partition_read
                            )
                            results.append(
                                BulkSyncResult(
                                    property_config.site_id,
                                    cursor.isoformat(),
                                    namespace,
                                    revision.epoch_version,
                                    str(value["status"]),
                                    rows=int(value["totals"]["row_count"]),
                                )
                            )
                        except Exception as exc:
                            results.append(
                                BulkSyncResult(
                                    property_config.site_id,
                                    cursor.isoformat(),
                                    namespace,
                                    revision.epoch_version,
                                    "failed",
                                    error_category=type(exc).__name__,
                                )
                            )
                    cursor += timedelta(days=1)
        return results
