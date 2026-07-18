"""Cloudflare GraphQL traffic and D1 form-state adapters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..credentials import require_text
from ..models import CapabilitySnapshot, Completeness
from .common import binding_site, connection_bindings, daily_point, timestamp_day


GRAPHQL_QUERY = """query Traffic($zone: String!, $start: Date!, $end: Date!) {
  viewer { zones(filter: {zoneTag: $zone}) { httpRequestsAdaptiveGroups(
    limit: 10000, filter: {date_geq: $start, date_lt: $end, requestSource: \"eyeball\"}, orderBy: [date_ASC]) {
      dimensions { date } count sum { visits edgeResponseBytes }
  } } } }"""


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

    def probe(self, connection, credential):
        end = datetime.now(UTC).date(); start = end - timedelta(days=1)
        resources = tuple(sorted({binding.resource_id for binding in connection_bindings(self.config, connection.id)}))
        for resource_id in resources:
            result = self._call(credential, {"query": GRAPHQL_QUERY, "variables": {
                "zone": resource_id, "start": start.isoformat(), "end": end.isoformat()}})
            self._groups(result, resource_id)
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, resources,
            ("cloudflare.bytes", "cloudflare.requests", "cloudflare.visits"), warnings=(
                "Cloudflare httpRequestsAdaptiveGroups facts use adaptive sampling and are provisional; values are not rescaled.",))

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

    def __init__(self, config, http) -> None: self.config = config; self.http = http

    def _query(self, connection, credential, sql, params):
        token = require_text(credential, "api_token", "value")
        account = str(connection.options["account_id"]); database = str(connection.options["database_id"])
        result = self.http.request("POST", f"https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{database}/query",
            headers={"Authorization": f"Bearer {token}"}, body={"sql": sql, "params": params})
        if not isinstance(result, dict) or result.get("success") is False or result.get("errors"):
            raise ValueError("Cloudflare D1 query failed")
        return result

    def probe(self, connection, credential):
        resources = tuple(sorted({binding.resource_id for binding in connection_bindings(self.config, connection.id)}))
        for resource_id in resources:
            self._query(connection, credential,
                "SELECT COUNT(*) AS aggregate_count FROM form_submissions WHERE site_id = ? LIMIT 1", [resource_id])
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, resources,
            ("forms.failed", "forms.pending", "forms.sent", "forms.submissions"))

    def collect(self, connection, credential, request):
        # Deliberately aggregate in D1. payload_json and all submission content are never selected.
        sql = """SELECT received_at, form_id, notification_status, COUNT(*) AS aggregate_count
          FROM form_submissions WHERE site_id = ?
            AND julianday(received_at) >= julianday(?) AND julianday(received_at) < julianday(?)
          GROUP BY received_at, form_id, notification_status ORDER BY received_at"""
        start = request.window.start.astimezone(UTC).isoformat()
        end = request.window.end.astimezone(UTC).isoformat()
        result = self._query(connection, credential, sql, [request.binding.resource_id, start, end])
        blocks = result.get("result", [])
        rows = blocks[0].get("results", []) if blocks else []
        site = binding_site(self.config, request.binding.site_id)
        status_totals: dict[tuple[object, str, str], int] = {}
        totals: dict[tuple[object, str], int] = {}
        for row in rows:
            day = timestamp_day(row.get("received_at"), site.timezone)
            status = str(row["notification_status"]); count = int(row["aggregate_count"])
            form_id = str(row["form_id"])
            status_key = (day, form_id, status)
            status_totals[status_key] = status_totals.get(status_key, 0) + count
            totals[(day, form_id)] = totals.get((day, form_id), 0) + count
        for (day, form_id, status), count in sorted(status_totals.items()):
            yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                metric=f"forms.{status}", unit="count", day=day, value=count,
                timezone=site.timezone, dimensions={"form_id": form_id})
        for (day, form_id), count in totals.items():
            yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                metric="forms.submissions", unit="count", day=day, value=count, timezone=site.timezone,
                dimensions={"form_id": form_id})
