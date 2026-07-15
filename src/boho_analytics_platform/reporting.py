"""Provider-neutral report, comparison, and export service."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .catalog import METRICS
from .models import QueryWindow


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral() else float(value)


class ReportService:
    def __init__(self, config, store) -> None: self.config = config; self.store = store

    def definition(self, report_id: str, subreport_id: str | None = None):
        report = next((item for item in self.config.reports if item.id == report_id), None)
        if report is None: raise ValueError(f"unknown report: {report_id}")
        metrics = report.metric_ids; title = report.title; filters = ()
        if subreport_id:
            sub = next((item for item in report.subreports if item.id == subreport_id), None)
            if sub is None: raise ValueError(f"unknown subreport: {subreport_id}")
            metrics = sub.metric_ids; title = f"{report.title} - {sub.title}"; filters = sub.filters
        return report, metrics, title, filters

    @staticmethod
    def _aggregate(points, window, requested_metrics):
        output: dict[tuple[str, str, str, str], Decimal] = defaultdict(Decimal)
        freshness: dict[str, datetime] = {}
        latest: dict[tuple[str, str, str, str], object] = {}
        for point in points:
            current = freshness.get(point.source)
            if current is None or point.observed_at > current: freshness[point.source] = point.observed_at
            definition = METRICS[point.metric]
            key = (point.metric, point.site_id, point.source, point.unit)
            if definition.aggregation == "sum": output[key] += point.value
            elif definition.aggregation == "latest":
                previous = latest.get(key)
                if previous is None or (point.end, point.observed_at) > (previous.end, previous.observed_at): latest[key] = point
            elif definition.aggregation == "window" and point.start == window.start and point.end == window.end:
                output[key] = point.value
        for key, point in latest.items(): output[key] = point.value
        grouped = defaultdict(dict)
        for point in points: grouped[(point.site_id, point.source, point.start, point.end, point.dimensions)][point.metric] = point.value
        weighted_position: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
        clicks: dict[tuple[str, str], Decimal] = defaultdict(Decimal); impressions: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        for (site_id, source, _start, _end, _dimensions), values in grouped.items():
            key = (site_id, source); clicks[key] += values.get("search.clicks", Decimal()); impressions[key] += values.get("search.impressions", Decimal())
            if "search.position" in values and "search.impressions" in values:
                numerator, denominator = weighted_position.get(key, (Decimal(), Decimal()))
                weighted_position[key] = (numerator + values["search.position"] * values["search.impressions"], denominator + values["search.impressions"])
        if "search.ctr" in requested_metrics:
            for (site_id, source), count in impressions.items():
                if count: output[("search.ctr", site_id, source, "ratio")] = clicks[(site_id, source)] / count
        if "search.position" in requested_metrics:
            for (site_id, source), (numerator, denominator) in weighted_position.items():
                if denominator: output[("search.position", site_id, source, "position")] = numerator / denominator
        rows = [{"metric": key[0], "site_id": key[1], "source": key[2], "unit": key[3], "value": _number(value)}
            for key, value in sorted(output.items()) if key[0] in requested_metrics]
        return rows, {key: value.astimezone(UTC).isoformat() for key, value in sorted(freshness.items())}

    def render(self, report_id: str, window: QueryWindow, subreport_id: str | None = None) -> dict[str, Any]:
        report, metrics, title, filters = self.definition(report_id, subreport_id)
        duration = window.end - window.start
        previous = QueryWindow(window.start - duration, window.start, window.timezone, window.completeness)
        query_metrics = tuple(dict.fromkeys((*metrics, *(("search.clicks", "search.impressions") if any(item in metrics for item in ("search.ctr", "search.position")) else ()))))
        current_points = self.store.query(client_id=report.client_id, site_ids=report.site_ids, metric_ids=query_metrics, window=window)
        previous_points = self.store.query(client_id=report.client_id, site_ids=report.site_ids, metric_ids=query_metrics, window=previous)
        if filters:
            required = dict(filters)
            current_points = [point for point in current_points if all(dict(point.dimensions).get(key) == value for key, value in required.items())]
            previous_points = [point for point in previous_points if all(dict(point.dimensions).get(key) == value for key, value in required.items())]
        current, freshness = self._aggregate(current_points, window, metrics); prior, _ = self._aggregate(previous_points, previous, metrics)
        prior_map = {(r["metric"], r["site_id"], r["source"], r["unit"]): r["value"] for r in prior}
        for row in current:
            prior_value = prior_map.get((row["metric"], row["site_id"], row["source"], row["unit"]))
            row["previous_value"] = prior_value
            row["change_percent"] = None if prior_value in {None, 0} else round((row["value"] - prior_value) / prior_value * 100, 2)
        warnings = []
        missing = sorted(set(metrics) - {row["metric"] for row in current})
        if missing: warnings.append("No stored data for: " + ", ".join(missing))
        metric_totals: dict[str, float] = defaultdict(float)
        for row in current: metric_totals[row["metric"]] += float(row["value"])
        forms = None
        if "forms.submissions" in metric_totals or "forms.inbox-deliveries" in metric_totals:
            stored = metric_totals.get("forms.submissions", 0); delivered = metric_totals.get("forms.inbox-deliveries", 0)
            forms = {"submissions": _number(Decimal(str(stored))), "inbox_deliveries": _number(Decimal(str(delivered))),
                "delivery_gap": _number(Decimal(str(stored - delivered))), "pending": _number(Decimal(str(metric_totals.get("forms.pending", 0)))),
                "failed": _number(Decimal(str(metric_totals.get("forms.failed", 0))))}
            if stored != delivered: warnings.append("Form storage and inbox-delivery counts differ; inspect the notification pipeline.")
        return {"schema_version": 1, "report_id": report.id, "subreport_id": subreport_id, "title": title,
            "window": {"start": window.start.isoformat(), "end": window.end.isoformat(), "timezone": window.timezone},
            "comparison_window": {"start": previous.start.isoformat(), "end": previous.end.isoformat()},
            "filters": dict(filters),
            "generated_at": datetime.now(UTC).isoformat(), "rows": current, "freshness": freshness,
            "forms_pipeline": forms, "warnings": warnings, "complete": not missing}


def to_csv(report: dict[str, Any]) -> str:
    output = io.StringIO(newline=""); fields = ["metric", "site_id", "source", "unit", "value", "previous_value", "change_percent"]
    writer = csv.DictWriter(output, fieldnames=fields); writer.writeheader()
    for row in report["rows"]: writer.writerow({key: row.get(key) for key in fields})
    return output.getvalue()
