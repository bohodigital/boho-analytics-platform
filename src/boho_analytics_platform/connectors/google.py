"""Google Analytics Data API and Search Console read-only adapters."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ..credentials import CredentialError, require_text
from ..models import CapabilitySnapshot
from .common import bearer, binding_site, connection_bindings, daily_point


SEARCH_CONSOLE_TIMEZONE = "America/Los_Angeles"


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


def _require_response(value, provider: str) -> dict:
    if not isinstance(value, dict) or value.get("error"):
        raise ValueError(f"{provider} returned an invalid response")
    return value


def _ga_body(start_date, end_date, *, limit: str = "100000") -> dict:
    return {"dateRanges": [{"startDate": start_date.isoformat(), "endDate": end_date.isoformat()}],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": name} for name in GoogleAnalyticsConnector.metrics], "limit": limit}


def _reported_ga_timezone(result: dict) -> str | None:
    metadata = result.get("metadata")
    value = metadata.get("timeZone") if isinstance(metadata, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _validate_ga_timezone(result: dict, expected: str) -> str | None:
    reported = _reported_ga_timezone(result)
    if reported is not None and reported != expected:
        raise ValueError(
            f"GA4 property timezone {reported!r} does not match configured site timezone {expected!r}")
    return reported


def _search_console_body(start_date, end_date, *, row_limit: int = 25000) -> dict:
    return {"startDate": start_date.isoformat(), "endDate": end_date.isoformat(),
        "dimensions": ["date"], "rowLimit": row_limit, "dataState": "final"}


def _search_console_window(start: datetime, end: datetime):
    provider_zone = ZoneInfo(SEARCH_CONSOLE_TIMEZONE)
    return (start.astimezone(provider_zone).date(),
        (end - timedelta(microseconds=1)).astimezone(provider_zone).date())


class GoogleAnalyticsConnector:
    provider = "google-analytics"
    metrics = {"activeUsers": "google.active-users", "sessions": "google.sessions",
               "screenPageViews": "google.pageviews", "eventCount": "google.events", "keyEvents": "google.key-events"}

    def __init__(self, config, http) -> None: self.config = config; self.http = http

    def probe(self, connection, credential):
        token = _access_token(credential); probe_day = datetime.now(UTC).date() - timedelta(days=1)
        bindings = connection_bindings(self.config, connection.id)
        resources = tuple(sorted({binding.resource_id for binding in bindings}))
        warnings: list[str] = []
        for binding in bindings:
            property_id = binding.resource_id.removeprefix("properties/")
            result = _require_response(self.http.request("POST",
                f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport",
                headers=bearer(token), body=_ga_body(probe_day, probe_day, limit="1")), "GA4")
            site = binding_site(self.config, binding.site_id)
            if _validate_ga_timezone(result, site.timezone) is None:
                warnings.append(
                    f"GA4 property {binding.resource_id} did not disclose its timezone; timezone alignment is unverified.")
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, resources,
            tuple(sorted(self.metrics.values())), warnings=tuple(sorted(set(warnings))))

    def collect(self, connection, credential, request):
        token = _access_token(credential); property_id = request.binding.resource_id.removeprefix("properties/")
        body = _ga_body(request.window.start.date(), request.window.end.date() - timedelta(days=1))
        result = _require_response(self.http.request("POST", f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport", headers=bearer(token), body=body), "GA4")
        site = binding_site(self.config, request.binding.site_id)
        _validate_ga_timezone(result, site.timezone)
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
        token = _access_token(credential); probe_day = datetime.now(UTC).date() - timedelta(days=10)
        bindings = connection_bindings(self.config, connection.id)
        resources = tuple(sorted({binding.resource_id for binding in bindings}))
        for binding in bindings:
            encoded = urllib.parse.quote(binding.resource_id, safe="")
            _require_response(self.http.request("POST",
                f"https://www.googleapis.com/webmasters/v3/sites/{encoded}/searchAnalytics/query",
                headers=bearer(token), body=_search_console_body(probe_day, probe_day, row_limit=1)),
                "Search Console")
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, resources,
            tuple(sorted(v[0] for v in self.metrics.values())), max_lookback_days=480,
            warnings=(f"Search Console daily facts use the provider date basis {SEARCH_CONSOLE_TIMEZONE}.",))

    def collect(self, connection, credential, request):
        token = _access_token(credential); encoded = urllib.parse.quote(request.binding.resource_id, safe="")
        start_date, end_date = _search_console_window(request.window.start, request.window.end)
        body = _search_console_body(start_date, end_date)
        result = _require_response(self.http.request("POST", f"https://www.googleapis.com/webmasters/v3/sites/{encoded}/searchAnalytics/query", headers=bearer(token), body=body), "Search Console")
        site = binding_site(self.config, request.binding.site_id)
        for row in result.get("rows", []):
            for key, (metric, unit) in self.metrics.items():
                if key in row: yield daily_point(client_id=site.client_id, site_id=site.id,
                    source=self.provider, metric=metric, unit=unit, day=row["keys"][0], value=row[key],
                    timezone=site.timezone)
