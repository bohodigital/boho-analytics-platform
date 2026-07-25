"""Google Analytics Data API and Search Console read-only adapters."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..config import route_analytics_options
from ..credentials import CredentialError, require_text
from ..models import CapabilitySnapshot, Completeness
from .common import bearer, binding_site, connection_bindings, daily_point, normalize_route, safe_public_label, sanitize_referrer


SEARCH_CONSOLE_TIMEZONE = "America/Los_Angeles"
_GA4_ROUTE_METRICS = (
    "google.landing-page-sessions", "google.page-path-views",
    "google.route-engaged-sessions", "google.route-engagement-seconds",
    "google.route-key-events",
)
_GA4_OPTIONAL_METRICS = {
    "title": "google.page-title-views",
    "channel": "google.channel-sessions",
    "referrer": "google.referrer-sessions",
}
_SEARCH_ROUTE_METRICS = (
    "search.route-clicks", "search.route-impressions",
    "search.route-ctr", "search.route-position",
)


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


def _ga_geography_body(start_date, end_date, *, limit: str = "100000") -> dict:
    return {"dateRanges": [{"startDate": start_date.isoformat(), "endDate": end_date.isoformat()}],
        "dimensions": [{"name": name} for name in ("date", "countryId", "region")],
        "metrics": [{"name": "sessions"}], "limit": limit}


def _ga_observation_body(start_date, end_date, *, dimension: str, metric: str, limit: int, offset: int = 0, filter_name: str | None = None, filter_value: str | None = None) -> dict:
    body = {"dateRanges": [{"startDate": start_date.isoformat(), "endDate": end_date.isoformat()}],
        "dimensions": [{"name": "date"}, {"name": dimension}], "metrics": [{"name": metric}],
        "limit": str(limit), "offset": str(offset), "returnPropertyQuota": True}
    if filter_name and filter_value:
        body["dimensionFilter"] = {"filter": {"fieldName": filter_name,
            "stringFilter": {"matchType": "EXACT", "value": filter_value}}}
    return body


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


def _search_console_geography_body(start_date, end_date, *, row_limit: int = 25000) -> dict:
    return {"startDate": start_date.isoformat(), "endDate": end_date.isoformat(),
        "dimensions": ["date", "country"], "rowLimit": row_limit, "dataState": "final"}


def _search_console_route_body(start_date, end_date, *, dimensions: list[str], options, start_row: int = 0, cluster: str | None = None) -> dict:
    body = {"startDate": start_date.isoformat(), "endDate": end_date.isoformat(),
        "dimensions": dimensions, "rowLimit": options.page_size, "startRow": start_row,
        "dataState": "final", "type": options.search_type, "aggregationType": "auto"}
    if cluster is not None:
        body["dimensionFilterGroups"] = [{"groupType": "and", "filters": [{
            "dimension": "query", "operator": "includingRegex", "expression": cluster,
        }]}]
    return body


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
        route_enabled = False
        for binding in bindings:
            property_id = binding.resource_id.removeprefix("properties/")
            result = _require_response(self.http.request("POST",
                f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport",
                headers=bearer(token), body=_ga_body(probe_day, probe_day, limit="1")), "GA4")
            site = binding_site(self.config, binding.site_id)
            if _validate_ga_timezone(result, site.timezone) is None:
                warnings.append(
                    f"GA4 property {binding.resource_id} did not disclose its timezone; timezone alignment is unverified.")
            options = route_analytics_options(binding)
            if options.enabled:
                route_enabled = True
                metadata = _require_response(self.http.request("GET",
                    f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}/metadata",
                    headers=bearer(token)), "GA4 metadata")
                _validate_ga_metadata(metadata, options)
        supported_metrics = (
            *self.metrics.values(), "google.country-sessions", "google.region-sessions",
            *(_GA4_ROUTE_METRICS if route_enabled else ()),
        )
        if route_enabled:
            supported_metrics += tuple(
                _GA4_OPTIONAL_METRICS[item]
                for binding in bindings
                for item in route_analytics_options(binding).ga4_dimensions
                if route_analytics_options(binding).enabled
            )
            if any(
                route_analytics_options(binding).enabled
                and route_analytics_options(binding).ga4_event_names
                for binding in bindings
            ):
                supported_metrics += ("google.configured-event-count",)
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, resources,
            tuple(sorted(set(supported_metrics))),
            warnings=tuple(sorted(set(warnings))))

    def collect(self, connection, credential, request):
        token = _access_token(credential); property_id = request.binding.resource_id.removeprefix("properties/")
        options = route_analytics_options(request.binding)
        route_dates = _route_dates(request.window, site_timezone=binding_site(self.config, request.binding.site_id).timezone, options=options) if options.enabled else None
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
        geography = _require_response(self.http.request(
            "POST", f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport",
            headers=bearer(token), body=_ga_geography_body(
                request.window.start.date(), request.window.end.date() - timedelta(days=1))), "GA4")
        _validate_ga_timezone(geography, site.timezone)
        geo_headers = [item["name"] for item in geography.get("metricHeaders", [])]
        sessions_index = geo_headers.index("sessions") if "sessions" in geo_headers else None
        for row in geography.get("rows", []):
            dimensions = [item.get("value", "") for item in row.get("dimensionValues", [])]
            values = row.get("metricValues", [])
            if len(dimensions) < 3 or sessions_index is None or sessions_index >= len(values):
                continue
            raw_day, country, region = dimensions[:3]
            country = country.strip().upper()
            if len(country) != 2 or not country.isalpha():
                continue
            day = f"{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:]}"
            value = values[sessions_index].get("value")
            if value is None:
                continue
            base_dimensions = {"country_code": country, "country_code_system": "iso-alpha2"}
            yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                metric="google.country-sessions", unit="count", day=day, value=value,
                timezone=site.timezone, dimensions=base_dimensions)
            region = region.strip()
            if region and region.casefold() not in {"(not set)", "not set"}:
                yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                    metric="google.region-sessions", unit="count", day=day, value=value,
                    timezone=site.timezone, dimensions={**base_dimensions, "region_name": region})
        if options.enabled:
            yield from self._collect_route_observations(token, property_id, route_dates, site, options)

    def _collect_route_observations(self, token, property_id, route_dates, site, options):
        start, end = route_dates
        definitions = (
            ("landingPagePlusQueryString", "sessions", "google.landing-page-sessions", "route"),
            ("pagePathPlusQueryString", "screenPageViews", "google.page-path-views", "route"),
            ("landingPagePlusQueryString", "engagedSessions", "google.route-engaged-sessions", "route"),
            ("landingPagePlusQueryString", "userEngagementDuration", "google.route-engagement-seconds", "route"),
            ("landingPagePlusQueryString", "keyEvents", "google.route-key-events", "route"),
        )
        optional_definitions = {
            "title": ("pageTitle", "screenPageViews", "google.page-title-views", "page_title"),
            "channel": ("sessionDefaultChannelGroup", "sessions", "google.channel-sessions", "channel"),
            "referrer": ("fullReferrer", "sessions", "google.referrer-sessions", "referrer"),
        }
        definitions += tuple(optional_definitions[item] for item in options.ga4_dimensions)
        for dimension, metric, metric_id, dimension_key in definitions:
            for row in self._ga_rows(token, property_id, start, end, dimension, metric, options):
                values = row.get("dimensionValues", [])
                measures = row.get("metricValues", [])
                if len(values) < 2 or not measures:
                    continue
                day = _ga_day(values[0].get("value")); raw = values[1].get("value")
                dimensions = _ga_observation_dimensions(
                    dimension_key, raw, site.canonical_url, options
                )
                if day and dimensions:
                    yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                        metric=metric_id, unit="seconds" if metric_id.endswith("seconds") else "count",
                        day=day, value=measures[0].get("value"), timezone=site.timezone, dimensions=dimensions)
        for event_name in options.ga4_event_names:
            for row in self._ga_rows(token, property_id, start, end, "eventName", "eventCount", options,
                    filter_name="eventName", filter_value=event_name):
                values = row.get("dimensionValues", []); measures = row.get("metricValues", [])
                day = _ga_day(values[0].get("value")) if values else None
                if day and measures:
                    yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                        metric="google.configured-event-count", unit="count", day=day,
                        value=measures[0].get("value"), timezone=site.timezone,
                        dimensions={"event_name": event_name})

    def _ga_rows(self, token, property_id, start, end, dimension, metric, options, *, filter_name=None, filter_value=None):
        for page in range(options.max_pages):
            offset = page * options.page_size
            result = _require_response(self.http.request("POST",
                f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport",
                headers=bearer(token), body=_ga_observation_body(start, end, dimension=dimension,
                    metric=metric, limit=options.page_size, offset=offset,
                    filter_name=filter_name, filter_value=filter_value)), "GA4")
            rows = result.get("rows", [])
            if not isinstance(rows, list) or len(rows) > options.page_size:
                raise ValueError("GA4 route observation rows were invalid")
            yield from rows
            total = result.get("rowCount")
            if isinstance(total, bool) or (
                total is not None
                and not isinstance(total, int)
                and not (isinstance(total, str) and total.isdecimal())
            ):
                raise ValueError("GA4 route observation rowCount was invalid")
            total_rows = int(total) if total is not None else None
            if total_rows is not None and total_rows < 0:
                raise ValueError("GA4 route observation rowCount was invalid")
            if len(rows) < options.page_size or (
                total_rows is not None and offset + len(rows) >= total_rows
            ):
                return
        raise ValueError("GA4 route observation pagination exceeded configured max_pages")


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
        route_enabled = any(route_analytics_options(binding).enabled for binding in bindings)
        supported_metrics = (
            *[value[0] for value in self.metrics.values()], "search.country-clicks",
            *(_SEARCH_ROUTE_METRICS if route_enabled else ()),
        )
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, resources,
            tuple(sorted(supported_metrics)), max_lookback_days=480,
            warnings=(f"Search Console daily facts use the provider date basis {SEARCH_CONSOLE_TIMEZONE}.",))

    def collect(self, connection, credential, request):
        token = _access_token(credential); encoded = urllib.parse.quote(request.binding.resource_id, safe="")
        options = route_analytics_options(request.binding)
        if options.enabled:
            _route_dates(request.window, site_timezone=binding_site(self.config, request.binding.site_id).timezone, options=options)
        start_date, end_date = _search_console_window(request.window.start, request.window.end)
        body = _search_console_body(start_date, end_date)
        result = _require_response(self.http.request("POST", f"https://www.googleapis.com/webmasters/v3/sites/{encoded}/searchAnalytics/query", headers=bearer(token), body=body), "Search Console")
        site = binding_site(self.config, request.binding.site_id)
        for row in result.get("rows", []):
            for key, (metric, unit) in self.metrics.items():
                if key in row: yield daily_point(client_id=site.client_id, site_id=site.id,
                    source=self.provider, metric=metric, unit=unit, day=row["keys"][0], value=row[key],
                    timezone=site.timezone)
        geography = _require_response(self.http.request(
            "POST", f"https://www.googleapis.com/webmasters/v3/sites/{encoded}/searchAnalytics/query",
            headers=bearer(token), body=_search_console_geography_body(start_date, end_date)),
            "Search Console")
        for row in geography.get("rows", []):
            keys = row.get("keys", [])
            country = str(keys[1]).strip().upper() if len(keys) > 1 else ""
            if len(country) == 3 and country.isalpha() and "clicks" in row:
                yield daily_point(client_id=site.client_id, site_id=site.id,
                    source=self.provider, metric="search.country-clicks", unit="count",
                    day=keys[0], value=row["clicks"], timezone=site.timezone,
                    dimensions={"country_code": country, "country_code_system": "iso-alpha3"})
        if options.enabled:
            yield from self._collect_route_observations(token, encoded, start_date, end_date, site, options)

    def _collect_route_observations(self, token, encoded, start_date, end_date, site, options):
        queries = [(["date", "page"], None, None, "page")]
        queries.extend(
            (["date", "page", item], None, item, f"page-{item}")
            for item in options.search_console_dimensions
        )
        queries.extend(
            (["date", "page"], expression, cluster_id, "query-cluster")
            for cluster_id, expression in options.query_clusters
        )
        for dimensions, cluster_expression, cluster_id, observation_scope in queries:
            for row in _search_console_rows(self.http, token, encoded, start_date, end_date, dimensions, options, cluster_expression):
                keys = row.get("keys", [])
                if len(keys) < 2:
                    continue
                route = normalize_route(keys[1], site.canonical_url,
                    allow_query_parameters=options.allowed_query_parameters, exclusions=options.exclusions)
                if route is None:
                    continue
                extra = {
                    "route": route,
                    "data_state": "final",
                    "observation_scope": observation_scope,
                }
                if cluster_id and cluster_expression is not None:
                    extra["query_cluster"] = cluster_id
                elif len(keys) > 2:
                    key = dimensions[2]
                    value = str(keys[2]).strip()
                    if key == "country" and len(value) == 3 and value.isalpha():
                        extra["country_code"] = value.upper(); extra["country_code_system"] = "iso-alpha3"
                    elif key == "device" and value.casefold() in {"desktop", "mobile", "tablet"}:
                        extra["device"] = value.casefold()
                    elif key == "searchAppearance" and 0 < len(value) <= 80:
                        extra["search_appearance"] = value
                    else:
                        continue
                for key, metric, unit in (("clicks", "search.route-clicks", "count"), ("impressions", "search.route-impressions", "count"),
                                          ("ctr", "search.route-ctr", "ratio"), ("position", "search.route-position", "position")):
                    if key in row:
                        yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                            metric=metric, unit=unit, day=keys[0], value=row[key], timezone=site.timezone,
                            dimensions=extra, completeness=Completeness.UNKNOWN)


def _ga_day(value):
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        return None
    return f"{value[:4]}-{value[4:6]}-{value[6:]}"


def _ga_observation_dimensions(kind, value, canonical_url, options):
    if kind == "route":
        route = normalize_route(value, canonical_url, allow_query_parameters=options.allowed_query_parameters, exclusions=options.exclusions)
        return {"route": route} if route else None
    if kind == "referrer":
        return sanitize_referrer(value, canonical_url, approved_domains=options.approved_referrer_domains,
            allow_query_parameters=options.allowed_query_parameters, exclusions=options.exclusions)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if kind == "channel":
        label = safe_public_label(value, maximum=80)
        return {"channel": label} if label else None
    if kind == "page_title":
        label = safe_public_label(value, maximum=160)
        return {"page_title": label} if label else None
    return None


def _validate_ga_metadata(metadata, options):
    dimensions = metadata.get("dimensions", [])
    metrics = metadata.get("metrics", [])
    if not isinstance(dimensions, list) or not isinstance(metrics, list):
        raise ValueError("GA4 metadata response was invalid")
    dimension_names = {item.get("apiName") for item in dimensions if isinstance(item, dict)}
    metric_names = {item.get("apiName") for item in metrics if isinstance(item, dict)}
    optional_dimensions = {
        "title": "pageTitle",
        "channel": "sessionDefaultChannelGroup",
        "referrer": "fullReferrer",
    }
    required_dimensions = {
        "date", "landingPagePlusQueryString", "pagePathPlusQueryString",
        *(optional_dimensions[item] for item in options.ga4_dimensions),
        *(("eventName",) if options.ga4_event_names else ()),
    }
    required_metrics = {
        "sessions", "screenPageViews", "engagedSessions",
        "userEngagementDuration", "keyEvents",
        *(("eventCount",) if options.ga4_event_names else ()),
    }
    if not required_dimensions <= dimension_names:
        raise ValueError("GA4 metadata lacks required route observation dimensions")
    if not required_metrics <= metric_names:
        raise ValueError("GA4 metadata lacks required route observation metrics")


def _search_console_rows(http, token, encoded, start_date, end_date, dimensions, options, cluster_expression):
    for page in range(options.max_pages):
        result = _require_response(http.request("POST",
            f"https://www.googleapis.com/webmasters/v3/sites/{encoded}/searchAnalytics/query",
            headers=bearer(token), body=_search_console_route_body(start_date, end_date,
                dimensions=dimensions, options=options, start_row=page * options.page_size,
                cluster=cluster_expression)), "Search Console")
        rows = result.get("rows", [])
        if not isinstance(rows, list) or len(rows) > options.page_size:
            raise ValueError("Search Console route observation rows were invalid")
        yield from rows
        if len(rows) < options.page_size:
            return
    raise ValueError("Search Console route observation pagination exceeded configured max_pages")


def _route_dates(window, *, site_timezone, options):
    zone = ZoneInfo(site_timezone)
    start = window.start.astimezone(zone); end = window.end.astimezone(zone)
    if start.timetz().replace(tzinfo=None) != time.min or end.timetz().replace(tzinfo=None) != time.min:
        raise ValueError("route observations require whole site-local days")
    days = (end.date() - start.date()).days
    if days < 1 or days > options.max_days:
        raise ValueError("route analytics request exceeds configured max_days")
    return start.date(), end.date() - timedelta(days=1)
