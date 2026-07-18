"""Provider-neutral report, comparison, coverage, and export service."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .catalog import METRICS, SOURCE_SEMANTICS, SourceSemantics
from .models import Completeness, QueryWindow


UNKNOWN_SOURCE_SEMANTICS = SourceSemantics("unknown", "unknown", "unknown")


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral() else float(value)


def _utc_iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _usable_for_window(point, window: QueryWindow) -> bool:
    if window.completeness == Completeness.FINAL:
        return point.completeness == Completeness.FINAL
    if window.completeness == Completeness.PROVISIONAL:
        return point.completeness in {Completeness.FINAL, Completeness.PROVISIONAL}
    return True


class ReportService:
    def __init__(self, config, store) -> None:
        self.config = config
        self.store = store

    def definition(self, report_id: str, subreport_id: str | None = None):
        report = next((item for item in self.config.reports if item.id == report_id), None)
        if report is None:
            raise ValueError(f"unknown report: {report_id}")
        metrics = report.metric_ids
        title = report.title
        filters = ()
        if subreport_id:
            sub = next((item for item in report.subreports if item.id == subreport_id), None)
            if sub is None:
                raise ValueError(f"unknown subreport: {subreport_id}")
            metrics = sub.metric_ids
            title = f"{report.title} - {sub.title}"
            filters = sub.filters
        return report, metrics, title, filters

    @staticmethod
    def _aggregate(points, window, requested_metrics):
        output: dict[tuple[str, str, str, str], Decimal] = defaultdict(Decimal)
        freshness: dict[str, datetime] = {}
        latest: dict[tuple[str, str, str, str], object] = {}
        for point in points:
            current = freshness.get(point.source)
            if current is None or point.observed_at > current:
                freshness[point.source] = point.observed_at
            definition = METRICS[point.metric]
            key = (point.metric, point.site_id, point.source, point.unit)
            if definition.aggregation == "sum":
                output[key] += point.value
            elif definition.aggregation == "latest":
                previous = latest.get(key)
                if previous is None or (point.end, point.observed_at) > (
                    previous.end,
                    previous.observed_at,
                ):
                    latest[key] = point
            elif (
                definition.aggregation == "window"
                and point.start == window.start
                and point.end == window.end
            ):
                output[key] = point.value
        for key, point in latest.items():
            output[key] = point.value

        grouped = defaultdict(dict)
        for point in points:
            grouped[
                (point.site_id, point.source, point.start, point.end, point.dimensions)
            ][point.metric] = point.value
        weighted_position: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
        clicks: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        impressions: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        incomplete_ctr: set[tuple[str, str]] = set()
        for (site_id, source, _start, _end, _dimensions), values in grouped.items():
            key = (site_id, source)
            if "search.impressions" in values:
                impressions[key] += values["search.impressions"]
                if "search.clicks" in values:
                    clicks[key] += values["search.clicks"]
                else:
                    incomplete_ctr.add(key)
            if "search.position" in values and "search.impressions" in values:
                numerator, denominator = weighted_position.get(
                    key, (Decimal(), Decimal())
                )
                weighted_position[key] = (
                    numerator
                    + values["search.position"] * values["search.impressions"],
                    denominator + values["search.impressions"],
                )
        if "search.ctr" in requested_metrics:
            for (site_id, source), count in impressions.items():
                if count and (site_id, source) not in incomplete_ctr:
                    output[("search.ctr", site_id, source, "ratio")] = (
                        clicks[(site_id, source)] / count
                    )
        if "search.position" in requested_metrics:
            for (site_id, source), (numerator, denominator) in weighted_position.items():
                if denominator:
                    output[("search.position", site_id, source, "position")] = (
                        numerator / denominator
                    )
        rows = [
            {
                "metric": key[0],
                "site_id": key[1],
                "source": key[2],
                "unit": key[3],
                "value": _number(value),
            }
            for key, value in sorted(output.items())
            if key[0] in requested_metrics
        ]
        return rows, {
            key: value.astimezone(UTC).isoformat()
            for key, value in sorted(freshness.items())
        }

    @staticmethod
    def _series(points, window, requested_metrics):
        """Build compact daily series without changing provider aggregation semantics."""

        zone = UTC if window.timezone == "UTC" else ZoneInfo(window.timezone)
        daily: dict[tuple[str, str, str, str, str], Decimal] = defaultdict(Decimal)
        latest: dict[tuple[str, str, str, str, str], object] = {}
        grouped = defaultdict(dict)
        requested = set(requested_metrics)
        for point in points:
            day = point.start.astimezone(zone).date().isoformat()
            definition = METRICS[point.metric]
            key = (point.metric, point.site_id, point.source, point.unit, day)
            if point.metric in requested:
                if definition.aggregation == "sum":
                    daily[key] += point.value
                elif definition.aggregation == "latest":
                    previous = latest.get(key)
                    if previous is None or (point.end, point.observed_at) > (
                        previous.end,
                        previous.observed_at,
                    ):
                        latest[key] = point
            grouped[(point.site_id, point.source, day, point.dimensions)][
                point.metric
            ] = point.value
        for key, point in latest.items():
            daily[key] = point.value

        clicks: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
        impressions: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
        incomplete_ctr: set[tuple[str, str, str]] = set()
        positions: dict[tuple[str, str, str], tuple[Decimal, Decimal]] = {}
        for (site_id, source, day, _dimensions), values in grouped.items():
            key = (site_id, source, day)
            if "search.impressions" in values:
                impressions[key] += values["search.impressions"]
                if "search.clicks" in values:
                    clicks[key] += values["search.clicks"]
                else:
                    incomplete_ctr.add(key)
            if "search.position" in values and "search.impressions" in values:
                numerator, denominator = positions.get(key, (Decimal(), Decimal()))
                positions[key] = (
                    numerator
                    + values["search.position"] * values["search.impressions"],
                    denominator + values["search.impressions"],
                )
        if "search.ctr" in requested:
            for (site_id, source, day), count in impressions.items():
                if count and (site_id, source, day) not in incomplete_ctr:
                    daily[("search.ctr", site_id, source, "ratio", day)] = (
                        clicks[(site_id, source, day)] / count
                    )
        if "search.position" in requested:
            for (site_id, source, day), (numerator, denominator) in positions.items():
                if denominator:
                    daily[("search.position", site_id, source, "position", day)] = (
                        numerator / denominator
                    )

        output: dict[tuple[str, str, str, str], list[dict[str, object]]] = (
            defaultdict(list)
        )
        for (metric, site_id, source, unit, day), value in sorted(daily.items()):
            output[(metric, site_id, source, unit)].append(
                {"date": day, "value": _number(value)}
            )
        return [
            {
                "metric": key[0],
                "site_id": key[1],
                "source": key[2],
                "unit": key[3],
                "points": values,
            }
            for key, values in sorted(output.items())
        ]

    def _coverage(self, site_ids, requested_metrics, points, window):
        zone = UTC if window.timezone == "UTC" else ZoneInfo(window.timezone)
        start_day = window.start.astimezone(zone).date()
        end_day = window.end.astimezone(zone).date()
        dates: list[str] = []
        day = start_day
        while day < end_day:
            dates.append(day.isoformat())
            day += timedelta(days=1)

        connection_sources = {item.id: item.provider for item in self.config.connections}
        configured_by_site: dict[str, set[str]] = defaultdict(set)
        for binding in self.config.bindings:
            configured_by_site[binding.site_id].add(
                connection_sources[binding.connection_id]
            )

        usable = [point for point in points if _usable_for_window(point, window)]
        daily_presence: set[tuple[str, str, str]] = set()
        exact_window_presence: set[tuple[str, str]] = set()
        for point in usable:
            daily_presence.add(
                (
                    point.site_id,
                    point.metric,
                    point.start.astimezone(zone).date().isoformat(),
                )
            )
            if point.start == window.start and point.end == window.end:
                exact_window_presence.add((point.site_id, point.metric))

        metrics_by_source: dict[str, list[str]] = defaultdict(list)
        for metric in requested_metrics:
            metrics_by_source[METRICS[metric].source].append(metric)

        by_site_source = []
        by_metric_counts: dict[str, dict[str, int | bool]] = {
            metric: {"expected": 0, "covered": 0, "not_configured": False}
            for metric in requested_metrics
        }
        source_health = []
        total_expected = 0
        total_covered = 0

        for site_id in site_ids:
            configured_sources = configured_by_site.get(site_id, set())
            wildcard_fixture = "fixture" in configured_sources
            for source, source_metrics in sorted(metrics_by_source.items()):
                configured = source in configured_sources or wildcard_fixture
                expected_cells = 0
                covered_cells = 0
                missing_cells = []
                metric_status: dict[str, str] = {}

                for metric in source_metrics:
                    definition = METRICS[metric]
                    metric_expected = 0
                    metric_covered = 0
                    cell_dates: list[str | None] = (
                        [None] if definition.aggregation == "window" else dates
                    )
                    for date_label in cell_dates:
                        metric_expected += 1
                        expected_cells += 1
                        by_metric_counts[metric]["expected"] += 1
                        missing_inputs = []
                        for input_metric in definition.coverage_inputs:
                            present = (
                                (site_id, input_metric) in exact_window_presence
                                if date_label is None
                                else (site_id, input_metric, date_label)
                                in daily_presence
                            )
                            if not configured or not present:
                                missing_inputs.append(input_metric)
                        if not missing_inputs:
                            metric_covered += 1
                            covered_cells += 1
                            by_metric_counts[metric]["covered"] += 1
                        else:
                            missing_cells.append(
                                {
                                    "metric": metric,
                                    "date": date_label,
                                    "missing_inputs": missing_inputs,
                                }
                            )
                    if not configured:
                        metric_status[metric] = "not_configured"
                        by_metric_counts[metric]["not_configured"] = True
                    elif metric_covered == metric_expected and metric_expected:
                        metric_status[metric] = "complete"
                    else:
                        metric_status[metric] = "partial"

                if not configured:
                    status = "not_configured"
                elif covered_cells == expected_cells and expected_cells:
                    status = "complete"
                else:
                    status = "partial"

                bucket = {
                    "site_id": site_id,
                    "source": source,
                    "status": status,
                    "configured": configured,
                    "expected_cells": expected_cells,
                    "covered_cells": covered_cells,
                    "missing_cells": missing_cells,
                    "metric_status": metric_status,
                }
                by_site_source.append(bucket)
                total_expected += expected_cells
                total_covered += covered_cells

                # Coverage eligibility remains governed by the requested completeness,
                # but observational health must describe every displayed source fact.
                source_points = [
                    point
                    for point in points
                    if point.site_id == site_id
                    and point.metric in METRICS
                    and METRICS[point.metric].source == source
                ]
                actual_sources = sorted({point.source for point in source_points}) or [source]
                for actual_source in actual_sources:
                    actual_points = [
                        point for point in source_points if point.source == actual_source
                    ]
                    semantics = SOURCE_SEMANTICS.get(
                        actual_source, UNKNOWN_SOURCE_SEMANTICS
                    )
                    source_health.append(
                        {
                            "site_id": site_id,
                            "source": actual_source,
                            "metric_source": source,
                            "status": status,
                            "data_through": _utc_iso(
                                max((point.end for point in actual_points), default=None)
                            ),
                            "ingested_at": _utc_iso(
                                max(
                                    (point.observed_at for point in actual_points),
                                    default=None,
                                )
                            ),
                            "time_basis": semantics.time_basis,
                            "sampling": semantics.sampling,
                            "data_state": semantics.data_state,
                        }
                    )

        by_metric: dict[str, str] = {}
        for metric, counts in by_metric_counts.items():
            if counts["not_configured"]:
                by_metric[metric] = "not_configured"
            elif counts["expected"] and counts["expected"] == counts["covered"]:
                by_metric[metric] = "complete"
            else:
                by_metric[metric] = "partial"
        status = (
            "complete"
            if total_expected and total_expected == total_covered
            and all(item["status"] == "complete" for item in by_site_source)
            else "unavailable"
            if total_covered == 0
            else "partial"
        )
        return (
            {
                "status": status,
                "expected_cells": total_expected,
                "covered_cells": total_covered,
                "by_metric": by_metric,
                "by_site_source": by_site_source,
            },
            source_health,
        )

    @staticmethod
    def _summary_values(rows, requested_metrics):
        by_metric: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_metric[row["metric"]].append(row)
        values: dict[str, Decimal] = {}
        for metric in requested_metrics:
            matches = by_metric.get(metric, [])
            if not matches:
                continue
            if metric == "search.ctr":
                clicks = sum(
                    (Decimal(str(item["value"])) for item in by_metric.get("search.clicks", [])),
                    Decimal(),
                )
                impressions = sum(
                    (Decimal(str(item["value"])) for item in by_metric.get("search.impressions", [])),
                    Decimal(),
                )
                if impressions:
                    values[metric] = clicks / impressions
            elif metric == "search.position":
                impression_rows = {
                    (item["site_id"], item["source"]): Decimal(str(item["value"]))
                    for item in by_metric.get("search.impressions", [])
                }
                numerator = Decimal()
                denominator = Decimal()
                for item in matches:
                    weight = impression_rows.get((item["site_id"], item["source"]))
                    if weight is not None:
                        numerator += Decimal(str(item["value"])) * weight
                        denominator += weight
                if denominator:
                    values[metric] = numerator / denominator
            else:
                values[metric] = sum(
                    (Decimal(str(item["value"])) for item in matches), Decimal()
                )
        return values

    @classmethod
    def _summary_totals(
        cls,
        current_rows,
        prior_rows,
        requested_metrics,
        coverage,
        prior_coverage,
    ):
        current_values = cls._summary_values(current_rows, requested_metrics)
        prior_values = cls._summary_values(prior_rows, requested_metrics)
        output = {}
        for metric in requested_metrics:
            definition = METRICS[metric]
            value = current_values.get(metric)
            coverage_status = coverage["by_metric"].get(metric, "unavailable")
            prior_status = prior_coverage["by_metric"].get(metric, "unavailable")
            current_sources = {
                row["source"] for row in current_rows if row["metric"] == metric
            }
            prior_sources = {
                row["source"] for row in prior_rows if row["metric"] == metric
            }
            current_source = (
                next(iter(current_sources)) if len(current_sources) == 1
                else definition.source if not current_sources
                else "mixed"
            )
            prior_source = (
                next(iter(prior_sources)) if len(prior_sources) == 1
                else definition.source if not prior_sources
                else "mixed"
            )
            if definition.aggregation == "weighted" and coverage_status != "complete":
                value = None
            if current_source == "mixed":
                value = None
            comparison_available = (
                coverage_status == "complete" and prior_status == "complete"
                and current_source == prior_source and current_source != "mixed"
            )
            previous = prior_values.get(metric) if comparison_available else None
            change = None
            if value is not None and previous not in {None, Decimal()}:
                change = round(float((value - previous) / previous * 100), 2)
            output[metric] = {
                "metric": metric,
                "source": current_source,
                "unit": definition.unit,
                "aggregation": definition.aggregation,
                "value": _number(value) if value is not None else None,
                "previous_value": _number(previous) if previous is not None else None,
                "change_percent": change,
                "coverage_status": coverage_status,
                "comparison_available": comparison_available,
            }
        return output

    @staticmethod
    def _coverage_status(coverage, site_id: str, metric: str) -> str:
        source = METRICS[metric].source
        bucket = next(
            (
                item
                for item in coverage["by_site_source"]
                if item["site_id"] == site_id and item["source"] == source
            ),
            None,
        )
        return (
            bucket["metric_status"].get(metric, "unavailable")
            if bucket
            else "unavailable"
        )

    def render(
        self,
        report_id: str,
        window: QueryWindow,
        subreport_id: str | None = None,
        site_id: str | None = None,
    ) -> dict[str, Any]:
        report, metrics, title, filters = self.definition(report_id, subreport_id)
        if site_id is not None and site_id not in report.site_ids:
            raise ValueError(f"site is unavailable in report: {site_id}")
        site_ids = (site_id,) if site_id else report.site_ids
        duration = window.end - window.start
        try:
            previous = QueryWindow(
                window.start - duration,
                window.start,
                window.timezone,
                window.completeness,
            )
        except OverflowError as exc:
            raise ValueError("comparison window is outside the supported date range") from exc
        weighted_inputs = (
            ("search.clicks", "search.impressions")
            if any(item in metrics for item in ("search.ctr", "search.position"))
            else ()
        )
        query_metrics = tuple(dict.fromkeys((*metrics, *weighted_inputs)))
        current_points = self.store.query(
            client_id=report.client_id,
            site_ids=site_ids,
            metric_ids=query_metrics,
            window=window,
        )
        previous_points = self.store.query(
            client_id=report.client_id,
            site_ids=site_ids,
            metric_ids=query_metrics,
            window=previous,
        )
        if filters:
            required = dict(filters)
            current_points = [
                point
                for point in current_points
                if all(
                    dict(point.dimensions).get(key) == value
                    for key, value in required.items()
                )
            ]
            previous_points = [
                point
                for point in previous_points
                if all(
                    dict(point.dimensions).get(key) == value
                    for key, value in required.items()
                )
            ]

        current_basis, freshness = self._aggregate(
            current_points, window, query_metrics
        )
        prior_basis, _prior_freshness = self._aggregate(
            previous_points, previous, query_metrics
        )
        requested = set(metrics)
        current = [row for row in current_basis if row["metric"] in requested]
        prior = [row for row in prior_basis if row["metric"] in requested]
        coverage, source_health = self._coverage(
            site_ids, metrics, current_points, window
        )
        prior_coverage, prior_source_health = self._coverage(
            site_ids, metrics, previous_points, previous
        )
        prior_map = {
            (row["metric"], row["site_id"], row["source"], row["unit"]): row[
                "value"
            ]
            for row in prior
        }
        for row in current:
            coverage_status = self._coverage_status(
                coverage, row["site_id"], row["metric"]
            )
            prior_status = self._coverage_status(
                prior_coverage, row["site_id"], row["metric"]
            )
            comparison_available = (
                coverage_status == "complete" and prior_status == "complete"
            )
            prior_value = (
                prior_map.get(
                    (row["metric"], row["site_id"], row["source"], row["unit"])
                )
                if comparison_available
                else None
            )
            row["coverage_status"] = coverage_status
            row["comparison_available"] = comparison_available
            row["previous_value"] = prior_value
            row["change_percent"] = (
                None
                if prior_value in {None, 0}
                else round((row["value"] - prior_value) / prior_value * 100, 2)
            )

        summary_totals = self._summary_totals(
            current_basis, prior_basis, metrics, coverage, prior_coverage
        )
        warnings = []
        missing = sorted(
            metric
            for metric in metrics
            if summary_totals[metric]["value"] is None
        )
        if missing:
            warnings.append("No stored data for: " + ", ".join(missing))
        if coverage["status"] != "complete":
            warnings.append(
                "Coverage is incomplete for one or more requested site, source, metric, or date cells."
            )

        forms = None
        if any(metric.startswith("forms.") for metric in metrics):
            def form_value(metric):
                item = summary_totals.get(metric)
                return item["value"] if item else None

            stored = form_value("forms.submissions")
            delivered = form_value("forms.inbox-deliveries")
            forms = {
                "submissions": stored,
                "inbox_deliveries": delivered,
                "delivery_gap": (
                    stored - delivered
                    if stored is not None and delivered is not None
                    else None
                ),
                "pending": form_value("forms.pending"),
                "failed": form_value("forms.failed"),
            }
            if (
                stored is not None
                and delivered is not None
                and stored != delivered
            ):
                warnings.append(
                    "Form storage and inbox-delivery counts differ; inspect the notification pipeline."
                )

        comparison_status = (
            "complete"
            if coverage["status"] == "complete"
            and prior_coverage["status"] == "complete"
            else "unavailable"
            if prior_coverage["covered_cells"] == 0
            else "partial"
        )
        return {
            "schema_version": 1,
            "report_id": report.id,
            "subreport_id": subreport_id,
            "site_id": site_id,
            "title": title,
            "window": {
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "timezone": window.timezone,
            },
            "comparison_window": {
                "start": previous.start.isoformat(),
                "end": previous.end.isoformat(),
            },
            "filters": dict(filters),
            "generated_at": datetime.now(UTC).isoformat(),
            "rows": current,
            "freshness": freshness,
            "source_health": source_health,
            "coverage": coverage,
            "summary_totals": summary_totals,
            "comparison": {
                "status": comparison_status,
                "available": comparison_status == "complete",
                "coverage": prior_coverage,
            },
            "comparison_source_health": prior_source_health,
            "series": self._series(current_points, window, metrics),
            "comparison_series": self._series(previous_points, previous, metrics),
            "forms_pipeline": forms,
            "warnings": warnings,
            "complete": coverage["status"] == "complete",
        }


REPORT_CONTEXT_FIELDS = [
    "report_id",
    "subreport_id",
    "scope_site_id",
    "window_start",
    "window_end",
    "timezone",
    "generated_at",
    "aggregation",
    "coverage_status",
    "comparison_available",
    "data_through",
    "ingested_at",
    "time_basis",
    "sampling",
    "data_state",
]


def _health_for(
    report, *, site_id: str, metric: str, source: str, comparison: bool = False
):
    metric_source = METRICS[metric].source
    items = (
        report.get("comparison_source_health", [])
        if comparison
        else report.get("source_health", [])
    )
    return next(
        (
            item
            for item in items
            if item["site_id"] == site_id
            and item["source"] == source
            and item.get("metric_source", metric_source) == metric_source
        ),
        None,
    )


def _coverage_for(report, *, site_id: str, metric: str, comparison: bool = False):
    coverage = (
        report.get("comparison", {}).get("coverage", {})
        if comparison
        else report.get("coverage", {})
    )
    source = METRICS[metric].source
    bucket = next(
        (
            item
            for item in coverage.get("by_site_source", [])
            if item["site_id"] == site_id and item["source"] == source
        ),
        None,
    )
    return (
        bucket.get("metric_status", {}).get(metric, "unavailable")
        if bucket
        else "unavailable"
    )


def _context(
    report, *, metric: str, site_id: str, source: str, comparison: bool = False
):
    definition = METRICS[metric]
    health = _health_for(
        report, site_id=site_id, metric=metric, source=source, comparison=comparison
    )
    semantics = SOURCE_SEMANTICS.get(source, UNKNOWN_SOURCE_SEMANTICS)
    window = report["comparison_window"] if comparison else report["window"]
    total = report.get("summary_totals", {}).get(metric, {})
    return {
        "report_id": report["report_id"],
        "subreport_id": report.get("subreport_id"),
        "scope_site_id": report.get("site_id"),
        "window_start": window["start"],
        "window_end": window["end"],
        "timezone": report["window"]["timezone"],
        "generated_at": report.get("generated_at"),
        "aggregation": definition.aggregation,
        "coverage_status": _coverage_for(
            report, site_id=site_id, metric=metric, comparison=comparison
        ),
        "comparison_available": total.get(
            "comparison_available", report.get("comparison_available", False)
        ),
        "data_through": health.get("data_through") if health else None,
        "ingested_at": health.get("ingested_at") if health else None,
        "time_basis": health.get("time_basis") if health else semantics.time_basis,
        "sampling": health.get("sampling") if health else semantics.sampling,
        "data_state": health.get("data_state") if health else semantics.data_state,
    }


def to_csv(report: dict[str, Any]) -> str:
    output = io.StringIO(newline="")
    fields = [
        "metric",
        "site_id",
        "source",
        "unit",
        "value",
        "previous_value",
        "change_percent",
        *REPORT_CONTEXT_FIELDS,
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in report["rows"]:
        exported = {key: row.get(key) for key in fields}
        exported.update(
            _context(
                report,
                metric=row["metric"],
                site_id=row["site_id"],
                source=row["source"],
            )
        )
        writer.writerow(exported)
    return output.getvalue()


def to_series_csv(report: dict[str, Any], *, include_comparison: bool = False) -> str:
    """Flatten selected daily series into a portable, context-rich CSV shape."""

    output = io.StringIO(newline="")
    fields = [
        "period",
        "date",
        "metric",
        "site_id",
        "source",
        "unit",
        "value",
        *REPORT_CONTEXT_FIELDS,
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    groups = [("current", report.get("series", []), False)]
    if include_comparison:
        groups.append(("comparison", report.get("comparison_series", []), True))
    for period, series_items, comparison in groups:
        for series in series_items:
            for point in series["points"]:
                row = {
                    "period": period,
                    "date": point["date"],
                    "metric": series["metric"],
                    "site_id": series["site_id"],
                    "source": series["source"],
                    "unit": series["unit"],
                    "value": point["value"],
                }
                row.update(
                    _context(
                        report,
                        metric=series["metric"],
                        site_id=series["site_id"],
                        source=series["source"],
                        comparison=comparison,
                    )
                )
                writer.writerow(row)
    return output.getvalue()
