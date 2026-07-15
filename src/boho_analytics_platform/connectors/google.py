"""Google Analytics Data API and Search Console read-only adapters."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from ..credentials import CredentialError, require_text
from ..models import CapabilitySnapshot
from .common import bearer, binding_site, daily_point


def _access_token(credential) -> str:
    direct = credential.read("access_token") or credential.read("value")
    if direct: return direct.decode("utf-8")
    refresh = credential.read("refresh_token")
    client_id = credential.read("client_id"); client_secret = credential.read("client_secret")
    if refresh and client_id and client_secret:
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh.decode(),
            "client_id": client_id.decode(), "client_secret": client_secret.decode()}).encode()
        request = urllib.request.Request("https://oauth2.googleapis.com/token", data=body, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read(1_000_001).decode("utf-8"))
            return str(result["access_token"])
        except Exception as exc:
            raise CredentialError("Google refresh-token exchange failed") from exc
    service_info = credential.read("service_account_json")
    private_key = credential.read("private_key"); client_email = credential.read("client_email"); token_uri = credential.read("token_uri")
    if service_info or (private_key and client_email):
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
        except ImportError as exc:
            raise CredentialError("service-account credentials require the google optional dependency") from exc
        info = json.loads(service_info.decode("utf-8")) if service_info else {
            "type": "service_account", "private_key": private_key.decode("utf-8"),
            "client_email": client_email.decode("utf-8"),
            "token_uri": token_uri.decode("utf-8") if token_uri else "https://oauth2.googleapis.com/token",
        }
        credentials = service_account.Credentials.from_service_account_info(info,
            scopes=["https://www.googleapis.com/auth/analytics.readonly", "https://www.googleapis.com/auth/webmasters.readonly"])
        credentials.refresh(Request())
        return str(credentials.token)
    raise CredentialError("Google credential needs access_token, refresh-token fields, or service_account_json")


class GoogleAnalyticsConnector:
    provider = "google-analytics"
    metrics = {"activeUsers": "google.active-users", "sessions": "google.sessions",
               "screenPageViews": "google.pageviews", "eventCount": "google.events", "keyEvents": "google.key-events"}

    def __init__(self, config, http) -> None: self.config = config; self.http = http

    def probe(self, connection, credential):
        _access_token(credential)
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, (), tuple(sorted(self.metrics.values())))

    def collect(self, connection, credential, request):
        token = _access_token(credential); property_id = request.binding.resource_id.removeprefix("properties/")
        body = {"dateRanges": [{"startDate": request.window.start.date().isoformat(),
            "endDate": (request.window.end.date() - timedelta(days=1)).isoformat()}], "dimensions": [{"name": "date"}],
            "metrics": [{"name": name} for name in self.metrics], "limit": "100000"}
        result = self.http.request("POST", f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport", headers=bearer(token), body=body)
        site = binding_site(self.config, request.binding.site_id)
        headers = [item["name"] for item in result.get("metricHeaders", [])]
        for row in result.get("rows", []):
            raw_day = row["dimensionValues"][0]["value"]; day = f"{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:]}"
            for name, value in zip(headers, row.get("metricValues", []), strict=False):
                if name in self.metrics: yield daily_point(client_id=site.client_id, site_id=site.id,
                    source=self.provider, metric=self.metrics[name], unit="count", day=day,
                    value=value["value"], timezone=site.timezone)


class SearchConsoleConnector:
    provider = "search-console"
    metrics = {"clicks": ("search.clicks", "count"), "impressions": ("search.impressions", "count"),
               "ctr": ("search.ctr", "ratio"), "position": ("search.position", "position")}

    def __init__(self, config, http) -> None: self.config = config; self.http = http

    def probe(self, connection, credential):
        _access_token(credential)
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, (), tuple(sorted(v[0] for v in self.metrics.values())), max_lookback_days=480)

    def collect(self, connection, credential, request):
        token = _access_token(credential); encoded = urllib.parse.quote(request.binding.resource_id, safe="")
        body = {"startDate": request.window.start.date().isoformat(), "endDate": (request.window.end.date() - timedelta(days=1)).isoformat(),
            "dimensions": ["date"], "rowLimit": 25000, "dataState": "final"}
        result = self.http.request("POST", f"https://www.googleapis.com/webmasters/v3/sites/{encoded}/searchAnalytics/query", headers=bearer(token), body=body)
        site = binding_site(self.config, request.binding.site_id)
        for row in result.get("rows", []):
            for key, (metric, unit) in self.metrics.items():
                if key in row: yield daily_point(client_id=site.client_id, site_id=site.id,
                    source=self.provider, metric=metric, unit=unit, day=row["keys"][0], value=row[key], timezone=site.timezone)
