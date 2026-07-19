"""Cloudflare GraphQL traffic and D1 form-state adapters."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..credentials import require_text
from ..models import CapabilitySnapshot, Completeness
from .common import binding_site, connection_bindings, daily_point, timestamp_day


GRAPHQL_QUERY = """query Traffic($zone: String!, $start: Date!, $end: Date!) {
  viewer { zones(filter: {zoneTag: $zone}) { httpRequestsAdaptiveGroups(
    limit: 10000, filter: {date_geq: $start, date_lt: $end, requestSource: \"eyeball\"}, orderBy: [date_ASC]) {
      dimensions { date } count sum { visits edgeResponseBytes }
  } } } }"""

PROBE_QUERY = """query Probe($zone: String!, $start: Date!, $end: Date!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    settings {
      httpRequestsAdaptiveGroups { enabled maxDuration maxNumberOfFields maxPageSize notOlderThan }
    }
    httpRequestsAdaptiveGroups(
      limit: 10000, filter: {date_geq: $start, date_lt: $end, requestSource: \"eyeball\"}, orderBy: [date_ASC]) {
        dimensions { date } count sum { visits edgeResponseBytes }
    }
  } }
}"""

_SECONDS_PER_DAY = 24 * 60 * 60


def _duration_label(seconds: int) -> str:
    for divisor, unit in ((_SECONDS_PER_DAY, "day"), (60 * 60, "hour"), (60, "minute")):
        if seconds % divisor == 0:
            value = seconds // divisor
            suffix = "" if value == 1 else "s"
            return f"{value} {unit}{suffix}"
    return f"{seconds} seconds"


class CloudflareAnalyticsConnector:
    provider = "cloudflare"

    def __init__(self, config, http) -> None: self.config = config; self.http = http

    def _call(self, credential, body):
        token = require_text(credential, "api_token", "value")
        return self.http.request("POST", "https://api.cloudflare.com/client/v4/graphql",
            headers={"Authorization": f"Bearer {token}"}, body=body)

    @staticmethod
    def _groups(result, resource_id: str):
        if not isinstance(result, dict) or result.get("errors"):
            raise ValueError("Cloudflare GraphQL returned errors")
        zones = result.get("data", {}).get("viewer", {}).get("zones")
        if not isinstance(zones, list) or not zones:
            raise ValueError(f"Cloudflare configured zone is not accessible: {resource_id}")
        groups = zones[0].get("httpRequestsAdaptiveGroups") if isinstance(zones[0], dict) else None
        if not isinstance(groups, list):
            raise ValueError(f"Cloudflare adaptive analytics are unavailable for configured zone: {resource_id}")
        return groups

    @staticmethod
    def _settings(result, resource_id: str) -> tuple[int, int]:
        if not isinstance(result, dict) or result.get("errors"):
            raise ValueError("Cloudflare GraphQL settings query returned errors")
        zones = result.get("data", {}).get("viewer", {}).get("zones")
        if not isinstance(zones, list) or not zones:
            raise ValueError(f"Cloudflare configured zone is not accessible: {resource_id}")
        zone = zones[0]
        settings = zone.get("settings") if isinstance(zone, dict) else None
        limits = settings.get("httpRequestsAdaptiveGroups") if isinstance(settings, dict) else None
        if not isinstance(limits, dict):
            raise ValueError("Cloudflare did not report httpRequestsAdaptiveGroups settings")
        enabled = limits.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("Cloudflare reported an invalid httpRequestsAdaptiveGroups enabled flag")
        if not enabled:
            raise ValueError("Cloudflare httpRequestsAdaptiveGroups are unavailable for a configured zone")
        values = []
        for key in ("maxDuration", "notOlderThan"):
            value = limits.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(
                    f"Cloudflare reported an invalid httpRequestsAdaptiveGroups {key} limit"
                )
            values.append(value)
        return values[0], values[1]

    def probe(self, connection, credential):
        probed_at = datetime.now(UTC)
        end = probed_at.date()
        start = end - timedelta(days=1)
        resources = tuple(sorted({binding.resource_id for binding in connection_bindings(self.config, connection.id)}))
        max_durations = []
        lookbacks = []
        for resource_id in resources:
            result = self._call(credential, {
                "query": PROBE_QUERY,
                "variables": {
                    "zone": resource_id,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
            })
            self._groups(result, resource_id)
            max_duration, not_older_than = self._settings(result, resource_id)
            max_durations.append(max_duration)
            lookbacks.append(not_older_than)
        max_duration = min(max_durations)
        max_lookback_days = min(lookbacks) // _SECONDS_PER_DAY
        return CapabilitySnapshot(connection.id, self.provider, probed_at, True, resources,
            ("cloudflare.bytes", "cloudflare.requests", "cloudflare.visits"),
            max_lookback_days=max_lookback_days,
            warnings=(
                "Cloudflare httpRequestsAdaptiveGroups facts use adaptive sampling and are provisional; values are not rescaled.",
                f"Cloudflare plan limits httpRequestsAdaptiveGroups history to {max_lookback_days} days across configured zones.",
                f"Cloudflare plan limits each httpRequestsAdaptiveGroups query to a maximum of {_duration_label(max_duration)} across configured zones.",
            ))

    def collect(self, connection, credential, request):
        result = self._call(credential, {"query": GRAPHQL_QUERY, "variables": {
            "zone": request.binding.resource_id, "start": request.window.start.date().isoformat(), "end": request.window.end.date().isoformat()}})
        groups = self._groups(result, request.binding.resource_id)
        site = binding_site(self.config, request.binding.site_id)
        for row in groups:
            for metric, value, unit in (("cloudflare.requests", row.get("count"), "count"),
                ("cloudflare.visits", row.get("sum", {}).get("visits"), "count"),
                ("cloudflare.bytes", row.get("sum", {}).get("edgeResponseBytes"), "bytes")):
                if value is not None: yield daily_point(client_id=site.client_id, site_id=site.id,
                    source=self.provider, metric=metric, unit=unit, day=row["dimensions"]["date"],
                    value=value, timezone=site.timezone, completeness=Completeness.PROVISIONAL)


class CloudflareFormsConnector:
    provider = "cloudflare-forms"
    _statuses = ("failed", "pending", "sent")
    _MAX_RETENTION_DAYS = 90

    def __init__(self, config, http, *, now=None) -> None:
        self.config = config; self.http = http
        self._now = now or (lambda: datetime.now(UTC))

    @classmethod
    def _retention_days(cls, connection) -> int:
        value = connection.options.get("source_retention_days")
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= cls._MAX_RETENTION_DAYS:
            raise ValueError(
                "cloudflare-forms source_retention_days must be an integer from 1 to "
                f"{cls._MAX_RETENTION_DAYS}"
            )
        return value

    def _query(self, connection, credential, sql, params):
        token = require_text(credential, "api_token", "value")
        account = str(connection.options["account_id"]); database = str(connection.options["database_id"])
        result = self.http.request("POST", f"https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{database}/query",
            headers={"Authorization": f"Bearer {token}"}, body={"sql": sql, "params": params})
        if not isinstance(result, dict) or result.get("success") is False or result.get("errors"):
            raise ValueError("Cloudflare D1 query failed")
        return result

    def probe(self, connection, credential):
        retention_days = self._retention_days(connection)
        resources = tuple(sorted({binding.resource_id for binding in connection_bindings(self.config, connection.id)}))
        for resource_id in resources:
            self._query(connection, credential,
                "SELECT COUNT(*) AS aggregate_count FROM form_submissions WHERE site_id = ? LIMIT 1", [resource_id])
        return CapabilitySnapshot(connection.id, self.provider, self._now().astimezone(UTC), True, resources,
            ("forms.failed", "forms.pending", "forms.sent", "forms.submissions"),
            max_lookback_days=retention_days,
            warnings=("Forms source retention bounds historical zero evidence; earlier cells remain unknown unless captured while retained.",))

    @staticmethod
    def _requested_days(window, timezone):
        zone = UTC if timezone == "UTC" else ZoneInfo(timezone)
        day = window.start.astimezone(zone).date()
        while True:
            day_start = datetime.combine(day, time.min, zone)
            if day_start >= window.end:
                break
            day_end = datetime.combine(day + timedelta(days=1), time.min, zone)
            if day_start >= window.start and day_end <= window.end:
                yield day
            day += timedelta(days=1)

    def _validate_window(self, connection, binding, window) -> datetime:
        """Fail closed unless the entire request is trustworthy source coverage."""
        site = binding_site(self.config, binding.site_id)
        zone = UTC if site.timezone == "UTC" else ZoneInfo(site.timezone)
        current = self._now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("cloudflare-forms clock must be timezone-aware")
        local_start = window.start.astimezone(zone)
        local_end = window.end.astimezone(zone)
        if local_start.time() != time.min or local_end.time() != time.min:
            raise ValueError("cloudflare-forms sync window must contain only complete site-local days")
        current_day = current.astimezone(zone).date()
        if local_end.date() > current_day:
            raise ValueError("cloudflare-forms sync window must not include an incomplete or future site-local day")
        cutoff_day = (current.astimezone(UTC) - timedelta(
            days=self._retention_days(connection)
        )).astimezone(zone).date()
        if local_start.date() <= cutoff_day:
            raise ValueError("cloudflare-forms sync window exceeds configured source retention")
        return current.astimezone(UTC)

    def _configured_form_ids(self, site):
        return {
            value
            for report in self.config.reports
            if report.client_id == site.client_id and site.id in report.site_ids
            for subreport in report.subreports
            for key, value in subreport.filters
            if key == "form_id"
        }

    def collect(self, connection, credential, request):
        # Deliberately aggregate in D1. payload_json and all submission content are never selected.
        observed_at = self._validate_window(connection, request.binding, request.window)
        window = request.window
        sql = """SELECT received_at, form_id, notification_status, COUNT(*) AS aggregate_count
          FROM form_submissions WHERE site_id = ?
            AND julianday(received_at) >= julianday(?) AND julianday(received_at) < julianday(?)
          GROUP BY received_at, form_id, notification_status ORDER BY received_at"""
        start = window.start.astimezone(UTC).isoformat()
        end = window.end.astimezone(UTC).isoformat()
        result = self._query(connection, credential, sql, [request.binding.resource_id, start, end])
        blocks = result.get("result", [])
        rows = blocks[0].get("results", []) if blocks else []
        site = binding_site(self.config, request.binding.site_id)
        status_totals: dict[tuple[object, str, str], int] = {}
        totals: dict[tuple[object, str], int] = {}
        form_ids = self._configured_form_ids(site)
        for row in rows:
            status = str(row["notification_status"])
            if status not in self._statuses:
                raise ValueError(f"Cloudflare D1 returned unsupported notification status: {status}")
            day = timestamp_day(row.get("received_at"), site.timezone)
            count = int(row["aggregate_count"])
            form_id = str(row["form_id"])
            form_ids.add(form_id)
            status_key = (day, form_id, status)
            status_totals[status_key] = status_totals.get(status_key, 0) + count
            totals[(day, form_id)] = totals.get((day, form_id), 0) + count
        for day in self._requested_days(window, site.timezone):
            for form_id in sorted(form_ids):
                dimensions = {"form_id": form_id}
                yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                    metric="forms.submissions", unit="count", day=day,
                    value=totals.get((day, form_id), 0), timezone=site.timezone,
                    dimensions=dimensions, observed_at=observed_at)
                for status in self._statuses:
                    yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                        metric=f"forms.{status}", unit="count", day=day,
                        value=status_totals.get((day, form_id, status), 0),
                        timezone=site.timezone, dimensions=dimensions, observed_at=observed_at)
