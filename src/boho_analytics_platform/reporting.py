"""Provider-neutral report, comparison, coverage, and export service."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .catalog import METRICS, SOURCE_SEMANTICS, SourceSemantics
from .models import Completeness, QueryWindow


UNKNOWN_SOURCE_SEMANTICS = SourceSemantics("unknown", "unknown", "unknown")

DECISION_INPUT_METRICS = (
    "umami.pageviews",
    "umami.visits",
    "umami.bounces",
    "umami.total-time",
    "google.sessions",
    "google.pageviews",
    "google.events",
    "search.clicks",
    "search.impressions",
    "forms.submissions",
    "forms.sent",
    "forms.pending",
    "forms.failed",
    "forms.inbox-deliveries",
)

SUPPORTING_SUMMARY_METRICS = (
    "search.clicks",
    "umami.pageviews",
    "umami.visits",
    "google.sessions",
)

SUPPORTING_SUMMARY_LABELS = {
    "search.clicks": "Search clicks",
    "umami.pageviews": "Umami page views",
    "umami.visits": "Umami visits",
    "google.sessions": "GA sessions",
}

MEASUREMENT_GAPS = (
    {
        "id": "commercial_outcomes",
        "label": "Qualified leads and revenue",
        "question": "Which leads became qualified opportunities, wins, and revenue?",
        "requires": "A controlled CRM or business-outcome feed with response, qualification, win, and revenue stages.",
    },
    {
        "id": "acquisition_content",
        "label": "Acquisition and content drivers",
        "question": "Which channels, campaigns, landing pages, queries, and pages create useful demand?",
        "requires": "Bounded GA, Umami, and Search Console dimension ingestion; totals must remain provider-labeled.",
    },
    {
        "id": "funnel_attribution",
        "label": "True funnel and attribution",
        "question": "Where do people drop between intent, form start, durable acceptance, and follow-up?",
        "requires": "Meaningful-action events, consistent UTMs, and a defined goal; current visit-to-submission is only directional.",
    },
    {
        "id": "retention",
        "label": "Retention cohorts",
        "question": "Do useful visitors return after 1, 7, or 30 days?",
        "requires": "GA or Umami cohort reports; daily unique counts cannot be summed into retention.",
    },
    {
        "id": "experience_reliability",
        "label": "Core Web Vitals and errors",
        "question": "Are real users seeing slow pages, 404s, or server failures?",
        "requires": "Privacy-safe RUM plus bounded edge/origin status, cache, and error dimensions.",
    },
)


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral() else float(value)


def _utc_iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _usable_for_window(point, window: QueryWindow) -> bool:
    if window.completeness == Completeness.FINAL:
        # Cloudflare's adaptive estimates retain provisional measurement
        # provenance even after a completed daily bucket is temporally usable.
        # Do not conflate sampling uncertainty with an unfinished date.
        semantics = SOURCE_SEMANTICS.get(point.source, UNKNOWN_SOURCE_SEMANTICS)
        return point.completeness == Completeness.FINAL or (
            point.completeness == Completeness.PROVISIONAL
            and semantics.data_state == "provisional"
        )
    if window.completeness == Completeness.PROVISIONAL:
        return point.completeness in {Completeness.FINAL, Completeness.PROVISIONAL}
    return True


def _compress_missing_cells(cells: list[dict[str, object]]) -> list[dict[str, object]]:
    """Compact consecutive missing dates with identical analytical causes."""

    ranges: list[dict[str, object]] = []
    for cell in cells:
        metric = str(cell["metric"])
        date_label = cell.get("date")
        missing_inputs = list(cell["missing_inputs"])
        if date_label is None:
            ranges.append(
                {
                    "metric": metric,
                    "start": None,
                    "end": None,
                    "cells": 1,
                    "missing_inputs": missing_inputs,
                }
            )
            continue
        current_day = datetime.fromisoformat(str(date_label)).date()
        previous = ranges[-1] if ranges else None
        can_extend = bool(
            previous
            and previous["metric"] == metric
            and previous["missing_inputs"] == missing_inputs
            and previous["end"] is not None
            and datetime.fromisoformat(str(previous["end"])).date()
            + timedelta(days=1)
            == current_day
        )
        if can_extend:
            previous["end"] = str(date_label)
            previous["cells"] = int(previous["cells"]) + 1
        else:
            ranges.append(
                {
                    "metric": metric,
                    "start": str(date_label),
                    "end": str(date_label),
                    "cells": 1,
                    "missing_inputs": missing_inputs,
                }
            )
    return ranges


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

    def _currently_supported_points(self, points):
        """Exclude facts whose site/provider binding is no longer configured.

        Fixture bindings are deliberately wildcard bindings for local test/demo
        metrics, but fixture-sourced facts are never accepted unless the site
        still has an explicit fixture binding.
        """

        providers_by_site: dict[str, set[str]] = defaultdict(set)
        connection_sources = {item.id: item.provider for item in self.config.connections}
        for binding in self.config.bindings:
            providers_by_site[binding.site_id].add(
                connection_sources[binding.connection_id]
            )

        supported = []
        for point in points:
            definition = METRICS.get(point.metric)
            if definition is None:
                continue
            configured = providers_by_site.get(point.site_id, set())
            logical_source = definition.source
            if point.source == "fixture" and "fixture" in configured:
                supported.append(point)
            elif point.source == logical_source and logical_source in configured:
                supported.append(point)
        return supported

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
        providers_by_site_source: dict[tuple[str, str], set[str]] = defaultdict(set)
        binding_keys: list[str] = []
        for binding in self.config.bindings:
            provider = connection_sources[binding.connection_id]
            configured_by_site[binding.site_id].add(provider)
            binding_keys.append(
                f"{binding.site_id}:{binding.connection_id}:{binding.resource_type}:{binding.resource_id}"
            )
            if provider == "fixture":
                for source in {METRICS[metric].source for metric in requested_metrics}:
                    providers_by_site_source[(binding.site_id, source)].add(provider)
            else:
                providers_by_site_source[(binding.site_id, provider)].add(provider)

        provider_sources = sorted(set(connection_sources.values()))
        sync_coverage = self.store.query_sync_coverage(
            site_ids=site_ids,
            sources=provider_sources,
            binding_keys=binding_keys,
            window=window,
        )
        coverage_runs: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
        for item in sync_coverage:
            coverage_runs[(str(item["site_id"]), str(item["source"]))].append(item)

        usable = [point for point in points if _usable_for_window(point, window)]
        daily_presence: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        exact_window_presence: dict[tuple[str, str], set[str]] = defaultdict(set)
        for point in usable:
            daily_presence[(
                point.site_id,
                point.metric,
                point.start.astimezone(zone).date().isoformat(),
            )].add(point.source)
            if point.start == window.start and point.end == window.end:
                exact_window_presence[(point.site_id, point.metric)].add(
                    point.source
                )

        metrics_by_source: dict[str, list[str]] = defaultdict(list)
        for metric in requested_metrics:
            metrics_by_source[METRICS[metric].source].append(metric)

        by_site_source = []
        by_metric_counts: dict[str, dict[str, int]] = {
            metric: {"expected": 0, "covered": 0, "configured_sites": 0}
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
                configured_providers = providers_by_site_source.get((site_id, source), set())
                expected_cells = 0
                covered_cells = 0
                missing_cells = []
                metric_status: dict[str, str] = {}
                metric_coverage: dict[str, dict[str, int | str]] = {}
                metric_evidence_providers: dict[str, list[str]] = {}

                for metric in source_metrics:
                    definition = METRICS[metric]
                    metric_expected = 0
                    metric_covered = 0
                    evidence_providers: set[str] = set()
                    if not configured:
                        metric_status[metric] = "not_configured"
                        metric_coverage[metric] = {
                            "status": "not_configured", "expected": 0, "covered": 0,
                        }
                        continue
                    by_metric_counts[metric]["configured_sites"] += 1
                    cell_dates: list[str | None] = (
                        [None] if definition.aggregation == "window" else dates
                    )
                    for date_label in cell_dates:
                        metric_expected += 1
                        expected_cells += 1
                        by_metric_counts[metric]["expected"] += 1
                        missing_inputs = []
                        for input_metric in definition.coverage_inputs:
                            fact_providers = (
                                exact_window_presence.get(
                                    (site_id, input_metric), set()
                                )
                                if date_label is None
                                else daily_presence.get(
                                    (site_id, input_metric, date_label), set()
                                )
                            )
                            present = bool(fact_providers)
                            cell_providers = set(fact_providers)
                            if date_label is not None and source not in {
                                "cloudflare-forms", "forms-inbox"
                            }:
                                local_day = datetime.fromisoformat(date_label).date()
                                cell_start = datetime.combine(local_day, time.min, zone)
                                cell_end = datetime.combine(
                                    local_day + timedelta(days=1), time.min, zone
                                )
                                for provider in configured_providers:
                                    for run in coverage_runs.get(
                                        (site_id, provider), ()
                                    ):
                                        effective_end = (
                                            run["data_through"]
                                            if provider == "search-console"
                                            else run["window_end"]
                                        )
                                        if (
                                            run["window_start"] <= cell_start
                                            and effective_end is not None
                                            and effective_end >= cell_end
                                        ):
                                            cell_providers.add(provider)
                                present = bool(cell_providers)
                            if not present:
                                missing_inputs.append(input_metric)
                            else:
                                evidence_providers.update(cell_providers)
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
                    if metric_covered == metric_expected and metric_expected:
                        metric_status[metric] = "complete"
                    else:
                        metric_status[metric] = "partial"
                    metric_coverage[metric] = {
                        "status": metric_status[metric],
                        "expected": metric_expected,
                        "covered": metric_covered,
                    }
                    metric_evidence_providers[metric] = sorted(
                        evidence_providers
                    )

                if not configured:
                    status = "not_configured"
                elif covered_cells == expected_cells and expected_cells:
                    status = "complete"
                else:
                    status = "partial"

                bucket = {
                    "site_id": site_id,
                    "source": source,
                    "configured_providers": sorted(configured_providers),
                    "status": status,
                    "configured": configured,
                    "expected_cells": expected_cells,
                    "covered_cells": covered_cells,
                    "missing_cells_count": len(missing_cells),
                    "missing_ranges": _compress_missing_cells(missing_cells),
                    "metric_status": metric_status,
                    "metric_coverage": metric_coverage,
                    "metric_evidence_providers": metric_evidence_providers,
                }
                by_site_source.append(bucket)
                if configured:
                    total_expected += expected_cells
                    total_covered += covered_cells

                # Coverage eligibility remains governed by the requested completeness,
                # but observational health must describe every displayed source fact.
                if not configured:
                    continue
                source_points = [
                    point
                    for point in points
                    if point.site_id == site_id
                    and point.metric in METRICS
                    and METRICS[point.metric].source == source
                ]
                actual_sources = sorted({point.source for point in source_points}) or sorted(
                    configured_providers or {source}
                )
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
            if not counts["configured_sites"]:
                by_metric[metric] = "not_configured"
            elif counts["expected"] and counts["expected"] == counts["covered"]:
                by_metric[metric] = "complete"
            else:
                by_metric[metric] = "partial"
        status = (
            "complete"
            if total_expected and total_expected == total_covered
            and all(
                item["status"] == "complete"
                for item in by_site_source
                if item["configured"]
            )
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
                "by_metric_cells": {
                    metric: {
                        "expected": counts["expected"],
                        "covered": counts["covered"],
                    }
                    for metric, counts in by_metric_counts.items()
                },
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

        def aggregate_source(rows, metric, metric_coverage):
            actual_sources = {
                row["source"] for row in rows if row["metric"] == metric
            }
            logical_source = METRICS[metric].source
            evidence_providers = {
                provider
                for bucket in metric_coverage.get("by_site_source", [])
                if bucket.get("source") == logical_source
                and bucket.get("metric_status", {}).get(metric)
                not in {None, "not_configured", "unavailable"}
                for provider in bucket.get(
                    "metric_evidence_providers", {}
                ).get(metric, [])
            }
            sources = actual_sources | evidence_providers
            if len(sources) == 1:
                return next(iter(sources))
            if len(sources) > 1:
                return "mixed"
            configured_providers = {
                provider
                for bucket in metric_coverage.get("by_site_source", [])
                if bucket.get("source") == logical_source
                and bucket.get("metric_status", {}).get(metric)
                not in {None, "not_configured", "unavailable"}
                for provider in bucket.get("configured_providers", [])
            }
            if len(configured_providers) == 1:
                return next(iter(configured_providers))
            if len(configured_providers) > 1:
                return "mixed"
            return logical_source

        output = {}
        for metric in requested_metrics:
            definition = METRICS[metric]
            value = current_values.get(metric)
            coverage_status = coverage["by_metric"].get(metric, "unavailable")
            prior_status = prior_coverage["by_metric"].get(metric, "unavailable")
            coverage_cells = coverage.get("by_metric_cells", {}).get(
                metric, {"expected": 0, "covered": 0}
            )
            prior_coverage_cells = prior_coverage.get("by_metric_cells", {}).get(
                metric, {"expected": 0, "covered": 0}
            )
            current_source = aggregate_source(current_rows, metric, coverage)
            prior_source = aggregate_source(
                prior_rows, metric, prior_coverage
            )
            if (
                value is None
                and definition.aggregation == "sum"
                and coverage_status == "complete"
                and current_source != "mixed"
            ):
                value = Decimal()
            if definition.aggregation == "weighted" and coverage_status != "complete":
                value = None
            if current_source == "mixed":
                value = None
            comparison_available = (
                coverage_status == "complete" and prior_status == "complete"
                and current_source == prior_source and current_source != "mixed"
            )
            previous = prior_values.get(metric) if comparison_available else None
            if (
                previous is None
                and comparison_available
                and definition.aggregation == "sum"
            ):
                previous = Decimal()
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
                "covered_cells": coverage_cells["covered"],
                "expected_cells": coverage_cells["expected"],
                "prior_covered_cells": prior_coverage_cells["covered"],
                "prior_expected_cells": prior_coverage_cells["expected"],
                "observed": any(row["metric"] == metric for row in current_rows),
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

    @classmethod
    def _metric_scope(cls, coverage, site_ids, metric: str) -> tuple[str, ...]:
        """Return configured sites for a metric without treating missing as zero."""

        return tuple(
            site_id for site_id in site_ids
            if cls._coverage_status(coverage, site_id, metric)
            not in {"not_configured", "unavailable"}
        )

    @classmethod
    def _site_metric_value(cls, rows, coverage, site_id: str, metric: str):
        """Return one site's complete value and its actual provider source."""

        status = cls._coverage_status(coverage, site_id, metric)
        if status in {"not_configured", "unavailable"}:
            return status, None, None
        if status != "complete":
            return "withheld", None, None
        matches = [
            row
            for row in rows
            if row["site_id"] == site_id and row["metric"] == metric
        ]
        source = METRICS[metric].source
        bucket = next(
            (
                item for item in coverage["by_site_source"]
                if item["site_id"] == site_id and item["source"] == source
            ),
            None,
        )
        evidence_sources = set(
            bucket.get("metric_evidence_providers", {}).get(metric, ())
            if bucket else ()
        )
        actual_sources = {row["source"] for row in matches}
        represented_sources = actual_sources | evidence_sources
        if len(represented_sources) > 1:
            return "withheld", None, None
        actual_source = next(iter(represented_sources), None)
        if actual_source is None:
            configured_providers = tuple(
                bucket.get("configured_providers", ()) if bucket else ()
            )
            if len(configured_providers) == 1:
                actual_source = configured_providers[0]
        if matches:
            return "observed", _number(sum(
                (Decimal(str(row["value"])) for row in matches), Decimal()
            )), actual_source
        if METRICS[metric].aggregation == "sum" and actual_source is not None:
            return "observed", 0, actual_source
        return "withheld", None, None

    @classmethod
    def _ratio_values(
        cls,
        rows,
        coverage,
        site_ids,
        numerator_metric: str,
        denominator_metric: str,
        *,
        fixed_scope: tuple[str, ...] | None = None,
    ):
        candidates = fixed_scope if fixed_scope is not None else tuple(site_ids)
        scope = cls._metric_scope(coverage, candidates, numerator_metric)
        scope = tuple(
            site_id for site_id in scope
            if cls._coverage_status(coverage, site_id, denominator_metric)
            not in {"not_configured", "unavailable"}
        )
        numerator = Decimal()
        denominator = Decimal()
        numerator_sources = set()
        denominator_sources = set()
        for site_id in scope:
            numerator_state, numerator_value, numerator_source = cls._site_metric_value(
                rows, coverage, site_id, numerator_metric
            )
            denominator_state, denominator_value, denominator_source = cls._site_metric_value(
                rows, coverage, site_id, denominator_metric
            )
            if "withheld" in {numerator_state, denominator_state}:
                return "withheld", None, scope, None
            numerator += Decimal(str(numerator_value))
            denominator += Decimal(str(denominator_value))
            numerator_sources.add(numerator_source)
            denominator_sources.add(denominator_source)
        if not scope:
            return "not_measured", None, (), None
        if (
            None in numerator_sources or None in denominator_sources
            or len(numerator_sources) != 1 or len(denominator_sources) != 1
        ):
            return "withheld", None, scope, None
        source_pair = (
            next(iter(numerator_sources)), next(iter(denominator_sources))
        )
        logical_pair = (
            METRICS[numerator_metric].source,
            METRICS[denominator_metric].source,
        )
        compatible_sources = (
            source_pair[0] == source_pair[1]
            if logical_pair[0] == logical_pair[1]
            else source_pair == logical_pair or source_pair == ("fixture", "fixture")
        )
        if not compatible_sources:
            return "withheld", None, scope, None
        if denominator == 0:
            return "unavailable", None, scope, source_pair
        return "observed", _number(numerator / denominator), scope, source_pair

    @classmethod
    def _ratio_card(
        cls,
        *,
        identifier: str,
        label: str,
        numerator_metric: str,
        denominator_metric: str,
        unit: str,
        note: str,
        rows,
        prior_rows,
        coverage,
        prior_coverage,
        site_ids,
    ):
        state, value, scope, source_pair = cls._ratio_values(
            rows, coverage, site_ids, numerator_metric, denominator_metric
        )
        previous_value = None
        change_percent = None
        change_state = None
        comparison_available = False
        if state == "observed":
            prior_state, previous_value, _prior_scope, prior_source_pair = cls._ratio_values(
                prior_rows,
                prior_coverage,
                site_ids,
                numerator_metric,
                denominator_metric,
                fixed_scope=scope,
            )
            comparison_available = (
                prior_state == "observed" and prior_source_pair == source_pair
            )
            if comparison_available and previous_value == 0 and value != 0:
                change_state = "new"
            elif comparison_available and previous_value not in {None, 0}:
                change_percent = round(
                    (float(value) - float(previous_value))
                    / float(previous_value)
                    * 100,
                    2,
                )
        state_note = note
        withheld_reason = None
        if state == "withheld":
            complete_cells = all(
                cls._coverage_status(coverage, site_id, metric) == "complete"
                for site_id in scope
                for metric in (numerator_metric, denominator_metric)
            )
            withheld_reason = (
                "source_conflict" if complete_cells else "incomplete_coverage"
            )
            state_note += (
                " Withheld: complete inputs and one consistent actual provider "
                "per metric are required across the card scope."
            )
        elif state == "not_measured":
            state_note += " No site in this scope has both required sources configured."
        elif state == "unavailable":
            state_note += " The complete denominator is zero for this window."
        return {
            "id": identifier,
            "label": label,
            "value": value,
            "previous_value": previous_value if comparison_available else None,
            "change_percent": change_percent,
            "change_state": change_state,
            "comparison_available": comparison_available,
            "unit": unit,
            "state": state,
            "withheld_reason": withheld_reason,
            "source": (
                f"{source_pair[0]} / {source_pair[1]}"
                if source_pair else "provider scope unavailable"
            ),
            "scope_site_ids": list(scope),
            "note": state_note,
        }

    @staticmethod
    def _metric_card(
        identifier: str,
        label: str,
        metric: str,
        total,
        note: str,
        *,
        scope_site_ids=(),
    ):
        coverage_status = total["coverage_status"]
        withheld_reason = None
        if total.get("source") == "mixed":
            state = "withheld"
            withheld_reason = "source_conflict"
        elif coverage_status == "complete" and total["value"] is not None:
            state = "observed"
        elif coverage_status == "partial":
            state = "withheld"
            withheld_reason = "incomplete_coverage"
        else:
            state = "not_measured"
        change_state = (
            "new"
            if total.get("comparison_available")
            and total.get("previous_value") == 0
            and total.get("value") not in {None, 0}
            else None
        )
        if withheld_reason == "source_conflict":
            note += " Aggregate withheld because more than one actual provider source is present."
        elif state == "withheld":
            note += " Aggregate withheld because configured coverage is partial."
        elif state == "not_measured":
            note += " No complete configured observation is available."
        return {
            "id": identifier,
            "label": label,
            "metric": metric,
            "value": total["value"] if state == "observed" else None,
            "previous_value": total.get("previous_value"),
            "change_percent": total.get("change_percent"),
            "change_state": change_state,
            "comparison_available": bool(total.get("comparison_available")),
            "unit": total["unit"],
            "state": state,
            "withheld_reason": withheld_reason,
            "source": total["source"],
            "scope_site_ids": list(scope_site_ids),
            "note": note,
        }

    @classmethod
    def _site_pulse(cls, site_ids, rows, coverage):
        metrics = (
            "umami.visits",
            "google.sessions",
            "search.clicks",
            "forms.submissions",
        )
        output = []
        for site_id in site_ids:
            cells = {}
            for metric in metrics:
                state, value, source = cls._site_metric_value(
                    rows, coverage, site_id, metric
                )
                cells[metric] = {
                    "state": state, "value": value, "source": source,
                }
            buckets = [
                item for item in coverage["by_site_source"]
                if item["site_id"] == site_id and item["configured"]
            ]
            expected = sum(int(item["expected_cells"]) for item in buckets)
            covered = sum(int(item["covered_cells"]) for item in buckets)
            status = (
                "complete" if expected and expected == covered
                else "unavailable" if not covered
                else "partial"
            )
            output.append({
                "site_id": site_id,
                "metrics": cells,
                "coverage": {
                    "status": status,
                    "covered_cells": covered,
                    "expected_cells": expected,
                },
            })
        return output

    @staticmethod
    def _binding_operations(bindings, connection_sources, latest_rows):
        """Represent every current binding, including bindings with no run yet."""

        latest_by_index = {
            int(item["binding_index"]): item for item in latest_rows
        }
        output = []
        for index, binding in enumerate(bindings):
            source = connection_sources[binding.connection_id]
            latest = latest_by_index.get(index)
            if latest is None:
                output.append({
                    "site_id": binding.site_id,
                    "source": source,
                    "started_at": None,
                    "finished_at": None,
                    "status": "never_run",
                    "points_written": 0,
                    "error_category": None,
                    "result_kind": None,
                    "data_through": None,
                })
                continue
            output.append({
                key: value for key, value in latest.items()
                if key not in {"binding_index", "connection_id"}
            } | {
                "site_id": binding.site_id,
                "source": source,
            })
        return output

    @staticmethod
    def _connection_capabilities(
        connection_ids, connection_sources, snapshots
    ):
        """Represent every selected connection, including one never probed.

        Capability records are dated observations only.  A missing record is
        an explicit measurement gap, not evidence that authentication or the
        provider feature set is healthy.
        """

        by_connection = {
            str(item["connection_id"]): item for item in snapshots
        }
        output = []
        for connection_id in connection_ids:
            snapshot = by_connection.get(connection_id)
            if snapshot is None:
                output.append({
                    "provider": connection_sources[connection_id],
                    "state": "not_recorded",
                    "probed_at": None,
                    "max_lookback_days": None,
                    "warning_count": 0,
                })
                continue
            output.append({
                "provider": connection_sources[connection_id],
                "state": "recorded",
                "probed_at": snapshot.get("probed_at"),
                "max_lookback_days": snapshot.get("max_lookback_days"),
                "warning_count": len(snapshot.get("warnings", [])),
            })
        return output

    @classmethod
    def _build_decision_support(
        cls,
        *,
        site_ids,
        current_rows,
        prior_rows,
        report_coverage,
        insight_coverage,
        prior_insight_coverage,
        insight_totals,
        operations_health,
        capabilities,
    ):
        outcomes = [
            cls._metric_card(
                "durable_leads", "Durable leads", "forms.submissions",
                insight_totals["forms.submissions"],
                "Accepted records in the forms database; this is the lead-count source of truth.",
                scope_site_ids=cls._metric_scope(
                    insight_coverage, site_ids, "forms.submissions"
                ),
            ),
            cls._ratio_card(
                identifier="visit_to_submission",
                label="Visit-to-submission proxy",
                numerator_metric="forms.submissions",
                denominator_metric="umami.visits",
                unit="ratio",
                note="Directional cross-system ratio on same-site complete inputs; not attribution or a user conversion rate.",
                rows=current_rows,
                prior_rows=prior_rows,
                coverage=insight_coverage,
                prior_coverage=prior_insight_coverage,
                site_ids=site_ids,
            ),
            cls._ratio_card(
                identifier="notification_sent_rate",
                label="Notification sent state",
                numerator_metric="forms.sent",
                denominator_metric="forms.submissions",
                unit="ratio",
                note="Forms database notification state; mailbox arrival remains independent reconciliation evidence.",
                rows=current_rows,
                prior_rows=prior_rows,
                coverage=insight_coverage,
                prior_coverage=prior_insight_coverage,
                site_ids=site_ids,
            ),
        ]
        expected = int(report_coverage["expected_cells"])
        covered = int(report_coverage["covered_cells"])
        outcomes.append({
            "id": "report_coverage",
            "label": "Report coverage",
            "value": covered / expected if expected else None,
            "previous_value": None,
            "change_percent": None,
            "change_state": None,
            "comparison_available": False,
            "unit": "ratio",
            "state": report_coverage["status"],
            "source": "sync ledger",
            "scope_site_ids": list(site_ids),
            "note": f"{covered} of {expected} expected site/source/metric/date cells are covered.",
        })

        engagement = [
            cls._ratio_card(
                identifier="umami_bounce_rate",
                label="Umami bounce rate",
                numerator_metric="umami.bounces",
                denominator_metric="umami.visits",
                unit="ratio",
                note="Umami defines a bounce as a visit with one event; do not compare this directly with GA bounce rate.",
                rows=current_rows, prior_rows=prior_rows,
                coverage=insight_coverage, prior_coverage=prior_insight_coverage,
                site_ids=site_ids,
            ),
            cls._ratio_card(
                identifier="umami_views_per_visit",
                label="Umami views per visit",
                numerator_metric="umami.pageviews",
                denominator_metric="umami.visits",
                unit="number",
                note="Page views divided by exact-window Umami visits using complete same-site inputs.",
                rows=current_rows, prior_rows=prior_rows,
                coverage=insight_coverage, prior_coverage=prior_insight_coverage,
                site_ids=site_ids,
            ),
            cls._ratio_card(
                identifier="umami_average_visit_duration",
                label="Umami average visit duration",
                numerator_metric="umami.total-time",
                denominator_metric="umami.visits",
                unit="seconds",
                note="Total Umami visit time divided by visits; single-event visits contribute no elapsed duration.",
                rows=current_rows, prior_rows=prior_rows,
                coverage=insight_coverage, prior_coverage=prior_insight_coverage,
                site_ids=site_ids,
            ),
            cls._ratio_card(
                identifier="ga_views_per_session",
                label="GA views per session",
                numerator_metric="google.pageviews",
                denominator_metric="google.sessions",
                unit="number",
                note="GA screen/page views per GA session; kept separate from Umami definitions.",
                rows=current_rows, prior_rows=prior_rows,
                coverage=insight_coverage, prior_coverage=prior_insight_coverage,
                site_ids=site_ids,
            ),
            cls._ratio_card(
                identifier="ga_events_per_session",
                label="GA events per session",
                numerator_metric="google.events",
                denominator_metric="google.sessions",
                unit="number",
                note="Event frequency, not conversion rate; a session can contain multiple events.",
                rows=current_rows, prior_rows=prior_rows,
                coverage=insight_coverage, prior_coverage=prior_insight_coverage,
                site_ids=site_ids,
            ),
        ]

        supporting_metrics = {
            metric: insight_totals[metric]
            for metric in SUPPORTING_SUMMARY_METRICS
        }

        attention = []
        source_conflicts = [
            item for item in (*outcomes, *engagement)
            if item.get("withheld_reason") == "source_conflict"
        ]
        source_conflicts.extend(
            {
                "label": SUPPORTING_SUMMARY_LABELS[metric],
                "withheld_reason": "source_conflict",
            }
            for metric, total in supporting_metrics.items()
            if total.get("source") == "mixed"
        )
        if source_conflicts:
            labels = ", ".join(
                item["label"] for item in source_conflicts[:5]
            )
            remainder = len(source_conflicts) - 5
            attention.append({
                "id": "decision_source_conflict",
                "severity": "review",
                "title": "Decision inputs mix incompatible provider sources",
                "evidence": (
                    f"Withheld cards: {labels}"
                    + (f" and {remainder} more" if remainder > 0 else "")
                    + "."
                ),
                "action": (
                    "Remove stale fixture or duplicate-provider facts, or narrow "
                    "the scope until each card uses compatible actual sources."
                ),
            })
        if report_coverage["status"] != "complete":
            attention.append({
                "id": "data_coverage",
                "severity": "review",
                "title": "Coverage is incomplete",
                "evidence": f"{covered} of {expected} expected report cells are covered.",
                "action": "Review source health and missing ranges; do not use withheld aggregates or period changes for decisions.",
            })
            source_gaps: dict[str, dict[str, object]] = {}
            for bucket in report_coverage["by_site_source"]:
                if not bucket["configured"] or bucket["status"] == "complete":
                    continue
                gap = source_gaps.setdefault(bucket["source"], {
                    "expected": 0, "covered": 0, "sites": [], "ranges": [],
                })
                gap["expected"] = int(gap["expected"]) + int(bucket["expected_cells"])
                gap["covered"] = int(gap["covered"]) + int(bucket["covered_cells"])
                gap["sites"].append(bucket["site_id"])
                gap["ranges"].extend(bucket.get("missing_ranges", []))
            ranked_gaps = sorted(
                source_gaps.items(),
                key=lambda item: int(item[1]["expected"]) - int(item[1]["covered"]),
                reverse=True,
            )[:3]
            for source, gap in ranked_gaps:
                examples = []
                for missing in gap["ranges"][:2]:
                    span = (
                        f"{missing['start']} to {missing['end']}"
                        if missing.get("start") else "the exact requested window"
                    )
                    examples.append(f"{missing['metric']} ({span})")
                evidence = (
                    f"{source} covers {gap['covered']} of {gap['expected']} expected cells "
                    f"across {len(set(gap['sites']))} configured site(s)"
                    + (f"; examples: {', '.join(examples)}" if examples else "")
                    + "."
                )
                action = (
                    "Use the provider data-through date or a fully covered earlier end date; do not interpret the newest missing Search day as zero."
                    if source == "search-console"
                    else "Use a window inside verified edge history and keep older Cloudflare totals provisional and partial."
                    if source == "cloudflare"
                    else "Review the latest current-binding sync and exact-window availability for this provider."
                )
                attention.append({
                    "id": "coverage_gap",
                    "severity": "review",
                    "title": f"{source} limits this decision window",
                    "evidence": evidence,
                    "action": action,
                })
        if insight_coverage["status"] != "complete":
            insight_expected = int(insight_coverage["expected_cells"])
            insight_covered = int(insight_coverage["covered_cells"])
            attention.append({
                "id": "decision_input_coverage",
                "severity": "review",
                "title": "Decision inputs are incomplete",
                "evidence": (
                    f"{insight_covered} of {insight_expected} expected decision-input "
                    "cells are covered; one or more derived cards may be withheld."
                ),
                "action": (
                    "Treat withheld KPIs as unknown and repair or narrow the exact "
                    "provider window before acting on them."
                ),
            })
        for item in operations_health:
            if item["status"] == "failed":
                attention.append({
                    "id": "sync_failure",
                    "severity": "immediate",
                    "title": "A current data binding failed its latest sync",
                    "evidence": (
                        f"{item['site_id']} / {item['source']} failed"
                        + (f" ({item['error_category']})" if item["error_category"] else "")
                        + "."
                    ),
                    "action": "Inspect the bounded provider job and retry only after the underlying access or response issue is understood.",
                })
            elif item["status"] == "never_run":
                attention.append({
                    "id": "sync_never_run",
                    "severity": "review",
                    "title": "A current data binding has no sync history",
                    "evidence": f"{item['site_id']} / {item['source']} has no recorded attempt.",
                    "action": "Verify the binding is intentional, then run its bounded sync before relying on this source.",
                })
            elif item["status"] == "running":
                attention.append({
                    "id": "sync_unfinished",
                    "severity": "review",
                    "title": "A current data binding has an unfinished sync",
                    "evidence": (
                        f"{item['site_id']} / {item['source']} started at "
                        f"{item['started_at'] or 'an unknown time'} and has not finished."
                    ),
                    "action": "Confirm whether the job is active; investigate and safely clear it only if it is genuinely stale.",
                })

        missing_capabilities = [
            item for item in capabilities
            if item.get("state") == "not_recorded"
        ]
        if missing_capabilities:
            providers = sorted({
                str(item["provider"]) for item in missing_capabilities
            })
            attention.append({
                "id": "capability_never_probed",
                "severity": "review",
                "title": "Provider capability limits have not been recorded",
                "evidence": (
                    f"{len(missing_capabilities)} current connection(s) have no "
                    f"dated capability snapshot: {', '.join(providers)}."
                ),
                "action": (
                    "Run the bounded provider probe before relying on lookback "
                    "limits or supported metric groups; absence is not health."
                ),
            })

        def complete(metric):
            total = insight_totals[metric]
            value = total["value"] if total["coverage_status"] == "complete" else None
            return value, cls._metric_scope(insight_coverage, site_ids, metric)

        submissions, submission_scope = complete("forms.submissions")
        sent, sent_scope = complete("forms.sent")
        pending, pending_scope = complete("forms.pending")
        failed, failed_scope = complete("forms.failed")
        delivered, delivered_scope = complete("forms.inbox-deliveries")
        if failed not in {None, 0}:
            attention.append({
                "id": "notification_failures",
                "severity": "immediate",
                "title": "Form notifications are marked failed",
                "evidence": f"{failed} accepted submission notification(s) are failed in the selected window.",
                "action": "Inspect and retry the notification pipeline while preserving the durable submissions.",
            })
        if pending not in {None, 0}:
            attention.append({
                "id": "pending_notifications",
                "severity": "immediate",
                "title": "Form notifications remain pending",
                "evidence": f"{pending} accepted submission notification(s) are pending in the selected window.",
                "action": "Check age and worker state; escalate stale pending records rather than treating them as delivered.",
            })
        pipeline_values = {submissions, sent, pending, failed}
        pipeline_scopes = {submission_scope, sent_scope, pending_scope, failed_scope}
        if None not in pipeline_values:
            if len(pipeline_scopes) != 1:
                attention.append({
                    "id": "notification_scope_mismatch",
                    "severity": "review",
                    "title": "Notification-state scopes do not match",
                    "evidence": "Accepted, sent, pending, and failed facts cover different configured sites.",
                    "action": "Align the forms-database bindings before reconciling portfolio notification states.",
                })
            elif submissions != sent + pending + failed:
                attention.append({
                    "id": "notification_pipeline_mismatch",
                    "severity": "immediate",
                    "title": "Notification states do not conserve accepted submissions",
                    "evidence": f"Accepted {submissions}; sent + pending + failed equals {sent + pending + failed}.",
                    "action": "Inspect form-state classification before using notification rates.",
                })
        if submissions is not None and delivered is not None:
            if submission_scope != delivered_scope:
                attention.append({
                    "id": "mailbox_scope_mismatch",
                    "severity": "review",
                    "title": "Mailbox and durable-lead scopes differ",
                    "evidence": "Forms database and inbox evidence cover different configured sites, so portfolio totals are not comparable.",
                    "action": "Align mailbox bindings or compare only the shared site scope before reconciling counts.",
                })
            elif submissions != delivered:
                attention.append({
                    "id": "mailbox_reconciliation",
                    "severity": "review",
                    "title": "Mailbox evidence differs from durable leads",
                    "evidence": f"Forms database accepted {submissions}; inbox evidence observed {delivered}.",
                    "action": "Correlate canary-excluded message identities; inbox count is reconciliation evidence, not the lead total.",
                })
        if not attention:
            attention.append({
                "id": "no_immediate_action",
                "severity": "clear",
                "title": "No evidence-backed issue needs immediate action",
                "evidence": "Configured decision inputs and current operations show no triggered rule.",
                "action": "Continue watching equal-period trends and source freshness.",
            })
        attention.sort(key=lambda item: {
            "immediate": 0, "review": 1, "clear": 2,
        }.get(item["severity"], 1))

        return {
            "schema_version": 1,
            "outcomes": outcomes,
            "engagement": engagement,
            "site_pulse": cls._site_pulse(site_ids, current_rows, insight_coverage),
            "attention_items": attention,
            "operations_health": operations_health,
            "capabilities": capabilities,
            "supporting_metrics": supporting_metrics,
            "measurement_gaps": list(MEASUREMENT_GAPS),
            "coverage": insight_coverage,
        }

    def render(
        self,
        report_id: str,
        window: QueryWindow,
        subreport_id: str | None = None,
        site_id: str | None = None,
        *,
        include_decision_support: bool = True,
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
        decision_metrics = (
            DECISION_INPUT_METRICS
            if subreport_id is None and include_decision_support
            else ()
        )
        query_metrics = tuple(dict.fromkeys(
            (*metrics, *weighted_inputs, *decision_metrics)
        ))
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
        current_points = self._currently_supported_points(current_points)
        previous_points = self._currently_supported_points(previous_points)
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
        decision_support = None
        if decision_metrics:
            insight_coverage, _insight_source_health = self._coverage(
                site_ids, decision_metrics, current_points, window
            )
            prior_insight_coverage, _prior_insight_source_health = self._coverage(
                site_ids, decision_metrics, previous_points, previous
            )
            insight_totals = self._summary_totals(
                current_basis,
                prior_basis,
                decision_metrics,
                insight_coverage,
                prior_insight_coverage,
            )
            selected_bindings = [
                binding for binding in self.config.bindings
                if binding.site_id in site_ids
            ]
            binding_keys = [
                f"{binding.site_id}:{binding.connection_id}:"
                f"{binding.resource_type}:{binding.resource_id}"
                for binding in selected_bindings
            ]
            connection_ids = sorted({
                binding.connection_id for binding in selected_bindings
            })
            connection_sources = {
                connection.id: connection.provider
                for connection in self.config.connections
            }
            latest_operations = self.store.query_latest_sync_status(
                binding_keys=binding_keys
            )
            decision_support = self._build_decision_support(
                site_ids=site_ids,
                current_rows=current_basis,
                prior_rows=prior_basis,
                report_coverage=coverage,
                insight_coverage=insight_coverage,
                prior_insight_coverage=prior_insight_coverage,
                insight_totals=insight_totals,
                operations_health=self._binding_operations(
                    selected_bindings,
                    connection_sources,
                    latest_operations,
                ),
                capabilities=self._connection_capabilities(
                    connection_ids,
                    connection_sources,
                    self.store.query_capability_summaries(
                        connection_ids=connection_ids
                    ),
                ),
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
        observed_metrics = {row["metric"] for row in current}
        missing = sorted(
            metric for metric in metrics
            if metric not in observed_metrics
            and summary_totals[metric]["value"] is None
            and summary_totals[metric]["coverage_status"] != "not_configured"
        )
        withheld = sorted(
            metric for metric in metrics
            if metric in observed_metrics and summary_totals[metric]["value"] is None
        )
        if missing:
            warnings.append("No stored observations for: " + ", ".join(missing))
        if withheld:
            warnings.append(
                "Partial aggregate withheld for: " + ", ".join(withheld)
                + ". Observed site or daily values remain available below."
            )
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
        current_series = self._series(current_points, window, metrics)
        prior_series = self._series(previous_points, previous, metrics)
        comparable_prior_series = [
            item for item in prior_series
            if self._coverage_status(
                coverage, item["site_id"], item["metric"]
            ) == "complete"
            and self._coverage_status(
                prior_coverage, item["site_id"], item["metric"]
            ) == "complete"
        ]
        return {
            "schema_version": 2,
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
            "series": current_series,
            "comparison_series": comparable_prior_series,
            "forms_pipeline": forms,
            "decision_support": decision_support,
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
        exported["comparison_available"] = row.get(
            "comparison_available", exported.get("comparison_available")
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
    if include_comparison and report.get("comparison_available", False):
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
