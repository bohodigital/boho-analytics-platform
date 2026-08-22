"""Failure-isolated synchronization orchestration."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time

from .catalog import validate_points
from .connectors import build_connector
from .contracts import SyncRequest
from .credentials import ReferenceCredentialProvider
from .http import JsonHttpClient, ProviderError
from .models import QueryWindow
from .storage import LockBusy, SQLiteMetricStore
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class SyncResult:
    connection_id: str
    site_id: str | None
    status: str
    points: int
    error_category: str | None = None


def _safe_category(exc: Exception) -> str:
    if isinstance(exc, ProviderError): return exc.category
    name = re.sub(r"(?<!^)(?=[A-Z])", "-", type(exc).__name__).casefold()
    return name[:60]


class SyncEngine:
    def __init__(self, config, store: SQLiteMetricStore, credential_provider=None, http=None) -> None:
        self.config = config; self.store = store
        self.credentials = credential_provider or ReferenceCredentialProvider()
        self.http = http or JsonHttpClient(timeout=config.platform.http_timeout_seconds,
            max_bytes=config.platform.max_response_bytes)

    def _selected_connection_ids(self, connection_ids: set[str] | None) -> set[str]:
        configured = {item.id for item in self.config.connections}
        if not connection_ids:
            return configured
        unknown = sorted(connection_ids - configured)
        if unknown:
            raise ValueError(f"unknown connection id(s): {', '.join(unknown)}")
        return set(connection_ids)

    def _selected_site_ids(self, site_ids: set[str] | None) -> set[str]:
        configured = {item.id for item in self.config.sites}
        if not site_ids:
            return configured
        unknown = sorted(site_ids - configured)
        if unknown:
            raise ValueError(f"unknown site id(s): {', '.join(unknown)}")
        return set(site_ids)

    def probe(self, connection_ids: set[str] | None = None) -> list[SyncResult]:
        selected = self._selected_connection_ids(connection_ids)
        results = []
        for connection in self.config.connections:
            if connection.id not in selected: continue
            run_id = self.store.start_run(connection.id, None)
            try:
                with self.credentials.acquire(connection.credential_ref) as credential:
                    snapshot = build_connector(connection.provider, self.config, self.http).probe(connection, credential)
                self.store.save_capability(snapshot); self.store.finish_run(run_id, "success")
                results.append(SyncResult(connection.id, None, "success", 0))
            except Exception as exc:
                category = _safe_category(exc); self.store.finish_run(run_id, "failed", category=category, message=type(exc).__name__)
                results.append(SyncResult(connection.id, None, "failed", 0, category))
        return results

    @staticmethod
    def _binding_window(window: QueryWindow, site_timezone: str) -> QueryWindow:
        """Project requested calendar dates onto one binding's local timezone."""

        request_zone = (
            UTC if window.timezone == "UTC" else ZoneInfo(window.timezone)
        )
        site_zone = UTC if site_timezone == "UTC" else ZoneInfo(site_timezone)
        start_day = window.start.astimezone(request_zone).date()
        end_day = window.end.astimezone(request_zone).date()
        return QueryWindow(
            datetime.combine(start_day, time.min, site_zone),
            datetime.combine(end_day, time.min, site_zone),
            site_timezone,
            window.completeness,
        )

    def sync(
        self,
        window: QueryWindow,
        connection_ids: set[str] | None = None,
        site_ids: set[str] | None = None,
    ) -> list[SyncResult]:
        selected = self._selected_connection_ids(connection_ids)
        selected_sites = self._selected_site_ids(site_ids)
        selected_bindings = [
            binding for binding in self.config.bindings
            if (
                binding.connection_id in selected
                and binding.site_id in selected_sites
            )
        ]
        if not selected_bindings:
            if site_ids:
                raise ValueError(
                    "selected connection(s) and site(s) have no configured bindings"
                )
            raise ValueError("selected connection(s) have no configured bindings")
        owner = uuid.uuid4().hex; self.store.acquire_lock("global-sync", owner)
        results: list[SyncResult] = []
        try:
            connection_map = {item.id: item for item in self.config.connections}
            site_timezones = {
                site.id: site.timezone for site in self.config.sites
            }
            for binding in selected_bindings:
                connection = connection_map[binding.connection_id]
                binding_window = self._binding_window(
                    window, site_timezones[binding.site_id]
                )
                binding_key = f"{binding.site_id}:{binding.connection_id}:{binding.resource_type}:{binding.resource_id}"
                run_id = self.store.start_run(
                    connection.id,
                    binding.site_id,
                    binding_key=binding_key,
                    source=connection.provider,
                    window=binding_window,
                )
                try:
                    request = SyncRequest(
                        binding, binding_window, binding.metric_groups
                    )
                    with self.credentials.acquire(connection.credential_ref) as credential:
                        connector = build_connector(
                            connection.provider, self.config, self.http
                        )
                        collect_batches = getattr(connector, "collect_batches", None)
                        if callable(collect_batches):
                            completed_batches = []
                            try:
                                for batch in collect_batches(
                                    connection, credential, request
                                ):
                                    validate_points(
                                        list(batch.points),
                                        fixture=connection.provider == "fixture",
                                    )
                                    completed_batches.append(batch)
                            except Exception:
                                if completed_batches:
                                    try:
                                        self.store.record_acquisition_batches(
                                            run_id,
                                            binding_key,
                                            tuple(completed_batches),
                                            publish_current=False,
                                        )
                                    except Exception:
                                        # Preserve the provider failure category;
                                        # attempt-evidence persistence is best effort.
                                        pass
                                raise
                            batches = tuple(completed_batches)
                            points = [
                                point
                                for batch in batches
                                for point in batch.points
                            ]
                        else:
                            batches = ()
                            points = list(
                                connector.collect(connection, credential, request)
                            )
                    validate_points(points, fixture=connection.provider == "fixture")
                    count = (
                        self.store.record_acquisition_batches(
                            run_id, binding_key, batches
                        )
                        if batches
                        else self.store.upsert(points)
                    )
                    if count:
                        data_through = min(max(point.end for point in points), binding_window.end)
                        self.store.set_watermark(binding_key, data_through)
                        self.store.finish_run(
                            run_id,
                            "success",
                            count,
                            result_kind="data",
                            data_through=data_through,
                        )
                        results.append(SyncResult(connection.id, binding.site_id, "success", count))
                    else:
                        # A completed read-only provider query is authoritative
                        # acquisition coverage even when the window contains no
                        # events. Preserve event freshness as null while advancing
                        # the query watermark through the requested end.
                        self.store.set_watermark(binding_key, binding_window.end)
                        self.store.finish_run(
                            run_id,
                            "success",
                            result_kind="empty",
                        )
                        results.append(SyncResult(
                            connection.id,
                            binding.site_id,
                            "success",
                            0,
                        ))
                except Exception as exc:
                    category = _safe_category(exc); self.store.finish_run(
                        run_id,
                        "failed",
                        category=category,
                        message=type(exc).__name__,
                        result_kind="failed",
                    )
                    results.append(SyncResult(connection.id, binding.site_id, "failed", 0, category))
        finally:
            self.store.release_lock("global-sync", owner)
        self.store.enforce_retention(hourly_days=self.config.retention.hourly_days, daily_days=self.config.retention.daily_days)
        return results
