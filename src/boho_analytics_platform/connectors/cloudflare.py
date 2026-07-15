"""Cloudflare GraphQL traffic and D1 form-state adapters."""

from __future__ import annotations

from datetime import UTC, datetime

from ..credentials import require_text
from ..models import CapabilitySnapshot
from .common import binding_site, daily_point


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

    def probe(self, connection, credential):
        token = require_text(credential, "api_token", "value")
        self.http.request("GET", "https://api.cloudflare.com/client/v4/user/tokens/verify",
            headers={"Authorization": f"Bearer {token}"})
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, (),
            ("cloudflare.bytes", "cloudflare.requests", "cloudflare.visits"))

    def collect(self, connection, credential, request):
        result = self._call(credential, {"query": GRAPHQL_QUERY, "variables": {
            "zone": request.binding.resource_id, "start": request.window.start.date().isoformat(), "end": request.window.end.date().isoformat()}})
        if result.get("errors"): raise ValueError("Cloudflare GraphQL returned errors")
        groups = result.get("data", {}).get("viewer", {}).get("zones", [{}])[0].get("httpRequestsAdaptiveGroups", [])
        site = binding_site(self.config, request.binding.site_id)
        for row in groups:
            for metric, value, unit in (("cloudflare.requests", row.get("count"), "count"),
                ("cloudflare.visits", row.get("sum", {}).get("visits"), "count"),
                ("cloudflare.bytes", row.get("sum", {}).get("edgeResponseBytes"), "bytes")):
                if value is not None: yield daily_point(client_id=site.client_id, site_id=site.id,
                    source=self.provider, metric=metric, unit=unit, day=row["dimensions"]["date"],
                    value=value, timezone=site.timezone)


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
        self._query(connection, credential, "SELECT 1 AS schema_ok FROM form_submissions LIMIT 1", [])
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, (),
            ("forms.failed", "forms.pending", "forms.sent", "forms.submissions"))

    def collect(self, connection, credential, request):
        # Deliberately aggregate in D1. payload_json and all submission content are never selected.
        sql = """SELECT substr(received_at,1,10) AS metric_day, form_id, notification_status, COUNT(*) AS aggregate_count
          FROM form_submissions WHERE site_id = ? AND received_at >= ? AND received_at < ?
          GROUP BY metric_day, form_id, notification_status ORDER BY metric_day"""
        result = self._query(connection, credential, sql, [request.binding.resource_id, request.window.start.isoformat(), request.window.end.isoformat()])
        blocks = result.get("result", [])
        rows = blocks[0].get("results", []) if blocks else []
        site = binding_site(self.config, request.binding.site_id)
        totals: dict[tuple[str, str], int] = {}
        for row in rows:
            day = str(row["metric_day"]); status = str(row["notification_status"]); count = int(row["aggregate_count"])
            dimensions = {"form_id": str(row["form_id"])}
            yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                metric=f"forms.{status}", unit="count", day=day, value=count, timezone=site.timezone, dimensions=dimensions)
            totals[(day, dimensions["form_id"])] = totals.get((day, dimensions["form_id"]), 0) + count
        for (day, form_id), count in totals.items():
            yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                metric="forms.submissions", unit="count", day=day, value=count, timezone=site.timezone,
                dimensions={"form_id": form_id})
