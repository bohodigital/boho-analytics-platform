"""Google Analytics Data API and Search Console read-only adapters."""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..catalog import SEARCH_CONSOLE_POSITION_SEARCH_TYPES
from ..config import route_analytics_options
from ..credentials import CredentialError, require_text
from ..models import (
    AcquisitionBatch,
    AcquisitionSlice,
    CapabilitySnapshot,
    Completeness,
    MetricPoint,
    TimeGrain,
)
from .common import aggregate_dimension_values, bearer, binding_site, connection_bindings, daily_point, nonnegative_bounded_number, nonnegative_integral_count, normalize_route, site_local_daily_bounds, safe_public_label, sanitize_referrer


SEARCH_CONSOLE_TIMEZONE = "America/Los_Angeles"
SEARCH_CONSOLE_REDACTED_QUERY = "[redacted]"
_SEARCH_CONSOLE_DAILY_ROW_CAP = 50_000
_SEARCH_CONSOLE_HOURLY_DAYS = 10
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
_SEARCH_METRIC_FIELDS = (
    ("clicks", "clicks", "count"),
    ("impressions", "impressions", "count"),
    ("ctr", "ctr", "ratio"),
    ("position", "position", "position"),
)


def _search_console_metric_fields(search_type: str):
    """Return only metrics Google defines for the selected search surface."""

    if search_type in SEARCH_CONSOLE_POSITION_SEARCH_TYPES:
        return _SEARCH_METRIC_FIELDS
    return tuple(item for item in _SEARCH_METRIC_FIELDS if item[0] != "position")


def _search_console_supports_query_dimension(search_type: str) -> bool:
    """Discover and Google News do not expose search-query wording."""

    return search_type not in {"discover", "googleNews"}


def _search_console_supports_route_dimension(
    search_type: str, dimension: str
) -> bool:
    """Apply the narrower grouping contract of non-Search reports."""

    return not (search_type == "discover" and dimension == "device")


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


def _search_console_aggregation(search_type: str, *, by_page: bool = False) -> str:
    """Return an explicit aggregation compatible with the selected surface."""

    if by_page or search_type in {"discover", "googleNews"}:
        return "byPage"
    return "byProperty"


def _search_console_body(
    start_date,
    end_date,
    *,
    dimensions: list[str],
    search_type: str,
    data_state: str,
    aggregation: str,
    row_limit: int = 25_000,
    start_row: int = 0,
    filters: tuple[tuple[str, str, str], ...] = (),
) -> dict:
    """Build one explicit Search Analytics request without implicit semantics."""

    body = {
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "dimensions": dimensions,
        "rowLimit": row_limit,
        "startRow": start_row,
        "dataState": data_state,
        "type": search_type,
        "aggregationType": aggregation,
    }
    if filters:
        body["dimensionFilterGroups"] = [{
            "groupType": "and",
            "filters": [
                {"dimension": dimension, "operator": operator, "expression": expression}
                for dimension, operator, expression in filters
            ],
        }]
    return body


def _search_console_base_dimensions(
    search_type: str, data_state: str, aggregation: str
) -> dict[str, str]:
    return {
        "aggregation": aggregation,
        "data_state": data_state,
        "search_type": search_type,
    }


def _search_console_daily_dimensions(
    base: dict[str, str], provider_day: date
) -> dict[str, str]:
    """Keep the provider's date identity when mapping into a site-day fact."""

    return {
        **base,
        "provider_date": provider_day.isoformat(),
        "provider_timezone": SEARCH_CONSOLE_TIMEZONE,
    }


def _search_console_collection_options(binding):
    """Use defaults capable of proving the documented 50k daily API cap."""

    options = route_analytics_options(binding)
    raw = binding.options.get("route_analytics", {})
    if (
        isinstance(raw, dict)
        and "page_size" not in raw
        and "max_pages" not in raw
    ):
        return replace(options, page_size=25_000, max_pages=3)
    return options


def _search_console_response_aggregation(result: dict, expected: str) -> None:
    reported = result.get("responseAggregationType")
    if reported is not None and reported != expected:
        raise ValueError("Search Console returned an unexpected aggregation type")


def _search_console_metrics(
    row: object, *, require_all: bool = False, recompute_ctr: bool = False,
    metric_fields=_SEARCH_METRIC_FIELDS,
) -> dict[str, Decimal]:
    if not isinstance(row, dict):
        raise ValueError("Search Console returned an invalid row")
    output: dict[str, Decimal] = {}
    for key, _suffix, _unit in metric_fields:
        if key not in row:
            continue
        value = nonnegative_bounded_number(
            row[key], integral=key in {"clicks", "impressions"}
        )
        if value is None or (key == "ctr" and value > 1):
            raise ValueError("Search Console returned an invalid metric value")
        output[key] = value
    if require_all and set(output) != {item[0] for item in metric_fields}:
        raise ValueError("Search Console row omitted required metrics")
    if recompute_ctr:
        if not {"clicks", "impressions"} <= output.keys():
            raise ValueError("Search Console row cannot derive CTR")
        if output["impressions"]:
            output["ctr"] = output["clicks"] / output["impressions"]
            if output["ctr"] > 1:
                raise ValueError("Search Console returned inconsistent control metrics")
        elif output["clicks"]:
            raise ValueError("Search Console returned clicks without impressions")
        else:
            output["ctr"] = Decimal()
    return output


def _combined_search_observations(entries):
    """Aggregate normalized identities without summing ratios or positions."""

    combined = {}
    for day, dimensions, metrics in entries:
        identity = (day, tuple(sorted(dimensions.items())))
        totals = combined.setdefault(identity, {
            "clicks": Decimal(),
            "impressions": Decimal(),
            "position_weight": Decimal(),
            "position_impressions": Decimal(),
            "seen": set(),
        })
        if "clicks" in metrics:
            totals["clicks"] += metrics["clicks"]
            totals["seen"].add("clicks")
        if "impressions" in metrics:
            totals["impressions"] += metrics["impressions"]
            totals["seen"].add("impressions")
        if "position" in metrics and "impressions" in metrics:
            totals["position_weight"] += metrics["position"] * metrics["impressions"]
            totals["position_impressions"] += metrics["impressions"]
            totals["seen"].add("position")
    for (day, dimensions), totals in combined.items():
        metrics = {}
        if "clicks" in totals["seen"]:
            metrics["clicks"] = totals["clicks"]
        if "impressions" in totals["seen"]:
            metrics["impressions"] = totals["impressions"]
        if {"clicks", "impressions"} <= totals["seen"] and totals["impressions"]:
            metrics["ctr"] = totals["clicks"] / totals["impressions"]
        if "position" in totals["seen"] and totals["position_impressions"]:
            metrics["position"] = (
                totals["position_weight"] / totals["position_impressions"]
            )
        yield day, dict(dimensions), metrics


def _search_console_query_dimensions(value: object) -> dict[str, str]:
    label = safe_public_label(value, maximum=256)
    return {
        "query_text": label or SEARCH_CONSOLE_REDACTED_QUERY,
        "query_visibility": "safe" if label is not None else "redacted",
    }


def _provider_days(start_date: date, end_date: date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


@dataclass(frozen=True, slots=True)
class _SearchConsoleSlice:
    """One bounded request slice, ready to map to shared batch evidence later."""

    rows: tuple[dict, ...]
    pages: int
    raw_rows: int
    accepted_rows: int
    rejected_rows: int
    exhaustion: str
    data_state: str
    dimensions: tuple[str, ...]
    aggregation: str


def _search_console_batch(
    site,
    *,
    slice_key: str,
    metric_family: str,
    start_date: date,
    end_date: date,
    completeness: Completeness,
    data_state: str,
    provider_scope: str,
    request_dimensions: tuple[str, ...],
    aggregation: str,
    pages: int,
    raw_rows: int,
    accepted_rows: int,
    rejected_rows: int,
    exhaustion: str,
    points,
) -> AcquisitionBatch:
    """Bind normalized facts to one bounded Search Analytics request slice."""

    materialized = tuple(points)
    provider_zone = ZoneInfo(SEARCH_CONSOLE_TIMEZONE)
    acquisition = AcquisitionSlice(
        slice_key=slice_key,
        metric_family=metric_family,
        start=datetime.combine(start_date, datetime.min.time(), provider_zone),
        end=datetime.combine(
            end_date + timedelta(days=1), datetime.min.time(), provider_zone
        ),
        completeness=completeness,
        data_state=data_state,
        provider_scope=provider_scope,
        request_dimensions=request_dimensions,
        provider_aggregation=aggregation,
        pages_fetched=pages,
        raw_rows=raw_rows,
        accepted_rows=accepted_rows,
        rejected_rows=rejected_rows,
        exhaustion_reason=exhaustion,
    )
    return AcquisitionBatch(acquisition, materialized)


def _search_console_slice_key(search_type: str, *parts: str) -> str:
    """Build a private-value-free, bounded key unique within one sync."""

    return ".".join(("gsc", search_type, *parts))


def _search_console_scope(search_type: str, *parts: str) -> str:
    return ":".join((search_type, *parts))


def _search_console_label_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _search_console_window(start: datetime, end: datetime):
    # The platform requests calendar labels, while Search Console interprets
    # those labels in America/Los_Angeles. Preserve the requested labels rather
    # than expanding one non-Pacific site day into two provider dates.
    return start.date(), (end - timedelta(microseconds=1)).date()


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
        site = binding_site(self.config, request.binding.site_id)
        local_start, local_end = site_local_daily_bounds(
            request.window, site.timezone
        )
        route_dates = _route_dates(
            request.window, site_timezone=site.timezone, options=options
        ) if options.enabled else None
        body = _ga_body(
            local_start.date(), local_end.date() - timedelta(days=1)
        )
        result = _require_response(self.http.request("POST", f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport", headers=bearer(token), body=body), "GA4")
        _validate_ga_timezone(result, site.timezone)
        raw_headers = result.get("metricHeaders")
        rows = result.get("rows", [])
        if not isinstance(raw_headers, list) or not isinstance(rows, list):
            raise ValueError("GA4 pageview series was absent or invalid")
        try:
            headers = [item["name"] for item in raw_headers]
        except (KeyError, TypeError):
            raise ValueError("GA4 pageview series was absent or invalid") from None
        if headers.count("screenPageViews") != 1:
            raise ValueError("GA4 pageview series was absent or invalid")
        pageview_index = headers.index("screenPageViews")
        for row in rows:
            dimensions = row.get("dimensionValues", []) if isinstance(row, dict) else []
            values = row.get("metricValues", []) if isinstance(row, dict) else []
            if (
                len(dimensions) != 1
                or len(values) != len(headers)
                or not isinstance(values[pageview_index], dict)
            ):
                raise ValueError("GA4 headline row was invalid")
            day = _ga_day(
                dimensions[0].get("value"), start=local_start.date(),
                end=local_end.date() - timedelta(days=1),
            )
            if day is None:
                raise ValueError("GA4 headline date was outside the request")
            for name, value in zip(headers, values, strict=True):
                if name not in self.metrics:
                    continue
                raw_value = value.get("value")
                if name == "screenPageViews":
                    raw_value = nonnegative_integral_count(raw_value)
                    if raw_value is None:
                        raise ValueError("GA4 pageview count was invalid")
                yield daily_point(client_id=site.client_id, site_id=site.id,
                    source=self.provider, metric=self.metrics[name], unit="count", day=day,
                    value=raw_value, timezone=site.timezone)
        geography = _require_response(self.http.request(
            "POST", f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport",
            headers=bearer(token), body=_ga_geography_body(
                local_start.date(), local_end.date() - timedelta(days=1))), "GA4")
        _validate_ga_timezone(geography, site.timezone)
        geo_headers = [item["name"] for item in geography.get("metricHeaders", [])]
        sessions_index = geo_headers.index("sessions") if "sessions" in geo_headers else None
        country_rows = []
        region_rows = []
        geography_rejected = False
        for row in geography.get("rows", []):
            dimensions = [item.get("value", "") for item in row.get("dimensionValues", [])]
            values = row.get("metricValues", [])
            if len(dimensions) < 3 or sessions_index is None or sessions_index >= len(values):
                geography_rejected = True
                continue
            raw_day, country, region = dimensions[:3]
            country = country.strip().upper()
            if len(country) != 2 or not country.isalpha():
                geography_rejected = True
                continue
            day = _ga_day(
                raw_day, start=local_start.date(),
                end=local_end.date() - timedelta(days=1),
            )
            if day is None:
                raise ValueError("GA4 geography date was outside the request")
            value = nonnegative_integral_count(values[sessions_index].get("value"))
            if value is None:
                geography_rejected = True
                continue
            base_dimensions = {"country_code": country, "country_code_system": "iso-alpha2"}
            country_rows.append((day, base_dimensions, value))
            region = region.strip()
            if region and region.casefold() not in {"(not set)", "not set"}:
                region_rows.append((day, {**base_dimensions, "region_name": region}, value))
        normalized_countries, country_rejected = aggregate_dimension_values(
            country_rows, integral=True
        )
        normalized_regions, region_rejected = aggregate_dimension_values(
            region_rows, integral=True
        )
        geography_completeness = (
            Completeness.FINAL
            if not geography_rejected and not country_rejected and not region_rejected
            else Completeness.UNKNOWN
        )
        for day, dimensions, value in normalized_countries:
            yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                metric="google.country-sessions", unit="count", day=day, value=value,
                timezone=site.timezone, dimensions=dimensions,
                completeness=geography_completeness)
        for day, dimensions, value in normalized_regions:
            yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                metric="google.region-sessions", unit="count", day=day, value=value,
                timezone=site.timezone, dimensions=dimensions,
                completeness=geography_completeness)
        if options.enabled:
            yield from self._collect_route_observations(token, property_id, route_dates, site, options)

    def _collect_route_observations(self, token, property_id, route_dates, site, options):
        start, end = route_dates
        definitions = (
            ("landingPagePlusQueryString", "sessions", "google.landing-page-sessions", "route"),
            ("pagePath", "screenPageViews", "google.page-path-views", "route_path_only"),
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
            rows, exhaustive = self._ga_rows(
                token, property_id, start, end, dimension, metric, options
            )
            accepted = []
            rejected = False
            for row in rows:
                values = row.get("dimensionValues", [])
                measures = row.get("metricValues", [])
                if len(values) < 2 or not measures:
                    rejected = True
                    continue
                day = _ga_day(
                    values[0].get("value"), start=start, end=end
                )
                raw = values[1].get("value")
                dimensions = _ga_observation_dimensions(
                    dimension_key, raw, site.canonical_url, options
                )
                measure = measures[0].get("value")
                if not metric_id.endswith("seconds"):
                    measure = nonnegative_integral_count(measure)
                if day and dimensions and measure is not None:
                    accepted.append((day, dimensions, measure))
                else:
                    rejected = True
            normalized, aggregate_rejected = aggregate_dimension_values(
                accepted, integral=not metric_id.endswith("seconds")
            )
            completeness = (
                Completeness.FINAL
                if exhaustive and not rejected and not aggregate_rejected
                else Completeness.UNKNOWN
            )
            for day, dimensions, value in normalized:
                yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                    metric=metric_id, unit="seconds" if metric_id.endswith("seconds") else "count",
                    day=day, value=value, timezone=site.timezone, dimensions=dimensions,
                    completeness=completeness)
        for event_name in options.ga4_event_names:
            rows, exhaustive = self._ga_rows(
                token, property_id, start, end, "eventName", "eventCount", options,
                filter_name="eventName", filter_value=event_name
            )
            accepted = []
            rejected = False
            for row in rows:
                values = row.get("dimensionValues", []); measures = row.get("metricValues", [])
                day = _ga_day(
                    values[0].get("value"), start=start, end=end
                ) if values else None
                measure = nonnegative_integral_count(
                    measures[0].get("value")
                ) if measures else None
                if day and measure is not None:
                    accepted.append((day, measure))
                else:
                    rejected = True
            completeness = (
                Completeness.FINAL if exhaustive and not rejected
                else Completeness.UNKNOWN
            )
            for day, value in accepted:
                yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                    metric="google.configured-event-count", unit="count", day=day,
                    value=value, timezone=site.timezone,
                    dimensions={"event_name": event_name}, completeness=completeness)

    def _ga_rows(self, token, property_id, start, end, dimension, metric, options, *, filter_name=None, filter_value=None):
        collected = []
        expected_total = None
        seen_dimension_keys = set()
        for page in range(options.max_pages):
            offset = page * options.page_size
            result = _require_response(self.http.request("POST",
                f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport",
                headers=bearer(token), body=_ga_observation_body(start, end, dimension=dimension,
                    metric=metric, limit=options.page_size, offset=offset,
                    filter_name=filter_name, filter_value=filter_value)), "GA4")
            dimension_headers = result.get("dimensionHeaders")
            metric_headers = result.get("metricHeaders")
            if (
                not isinstance(dimension_headers, list)
                or not isinstance(metric_headers, list)
                or any(not isinstance(item, dict) for item in dimension_headers)
                or any(not isinstance(item, dict) for item in metric_headers)
                or [item.get("name") for item in dimension_headers]
                != ["date", dimension]
                or [item.get("name") for item in metric_headers] != [metric]
            ):
                return collected, False
            rows = result.get("rows", [])
            if not isinstance(rows, list) or len(rows) > options.page_size:
                raise ValueError("GA4 route observation rows were invalid")
            page_dimension_keys = set()
            valid_rows = []
            invalid_count = False
            for row in rows:
                dimensions = row.get("dimensionValues") if isinstance(row, dict) else None
                measures = row.get("metricValues") if isinstance(row, dict) else None
                if (
                    not isinstance(dimensions, list)
                    or len(dimensions) != 2
                    or not all(
                        isinstance(item, dict)
                        and isinstance(item.get("value"), str)
                        for item in dimensions
                    )
                    or not isinstance(measures, list)
                    or len(measures) != 1
                    or not isinstance(measures[0], dict)
                    or "value" not in measures[0]
                ):
                    return collected, False
                key = tuple(item["value"] for item in dimensions)
                if key in seen_dimension_keys or key in page_dimension_keys:
                    return collected, False
                page_dimension_keys.add(key)
                if nonnegative_bounded_number(
                    measures[0]["value"],
                    integral=metric != "userEngagementDuration",
                ) is None:
                    invalid_count = True
                    continue
                valid_rows.append(row)
            seen_dimension_keys.update(page_dimension_keys)
            if invalid_count:
                return [*collected, *valid_rows], False
            collected.extend(valid_rows)
            total = result.get("rowCount")
            bounded_total = (
                nonnegative_integral_count(total) if total is not None else None
            )
            if total is not None and bounded_total is None:
                raise ValueError("GA4 route observation rowCount was invalid")
            total_rows = int(bounded_total) if bounded_total is not None else None
            if total_rows is None:
                return collected, False
            if (
                total_rows < 0
                or offset + len(rows) > total_rows
                or (expected_total is not None and total_rows != expected_total)
            ):
                return collected, False
            expected_total = total_rows
            if offset + len(rows) == total_rows:
                return collected, True
            if len(rows) < options.page_size:
                return collected, False
        return collected, False


class SearchConsoleConnector:
    provider = "search-console"
    metrics = {"clicks": ("search.clicks", "count"), "impressions": ("search.impressions", "count"),
               "ctr": ("search.ctr", "ratio"), "position": ("search.position", "position")}

    def __init__(self, config, http) -> None: self.config = config; self.http = http

    def _call(self, token: str, encoded: str, body: dict) -> dict:
        return _require_response(self.http.request(
            "POST",
            f"https://www.googleapis.com/webmasters/v3/sites/{encoded}/searchAnalytics/query",
            headers=bearer(token),
            body=body,
        ), "Search Console")

    def probe(self, connection, credential):
        token = _access_token(credential); probe_day = datetime.now(UTC).date() - timedelta(days=10)
        bindings = connection_bindings(self.config, connection.id)
        resources = tuple(sorted({binding.resource_id for binding in bindings}))
        supported_metrics = set(self.metrics[key][0] for key in self.metrics)
        supported_metrics.update(
            f"search.country-{suffix}" for _key, suffix, _unit in _SEARCH_METRIC_FIELDS
        )
        route_enabled = False
        for binding in bindings:
            encoded = urllib.parse.quote(binding.resource_id, safe="")
            options = route_analytics_options(binding)
            for search_type in options.search_types:
                aggregation = _search_console_aggregation(search_type)
                result = self._call(token, encoded, _search_console_body(
                    probe_day, probe_day,
                    dimensions=["date"], search_type=search_type,
                    data_state="final", aggregation=aggregation, row_limit=1,
                ))
                _search_console_response_aggregation(result, aggregation)
            route_enabled = route_enabled or options.enabled
            if options.search_console_query_text:
                supported_metrics.update(
                    f"search.query-{suffix}"
                    for _key, suffix, _unit in _SEARCH_METRIC_FIELDS
                )
            if options.search_console_page_query:
                supported_metrics.update(
                    f"search.page-query-{suffix}"
                    for _key, suffix, _unit in _SEARCH_METRIC_FIELDS
                )
            if options.search_console_hourly:
                supported_metrics.update(
                    f"search.hourly-{suffix}"
                    for _key, suffix, _unit in _SEARCH_METRIC_FIELDS
                )
        if route_enabled:
            supported_metrics.update(_SEARCH_ROUTE_METRICS)
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, resources,
            tuple(sorted(supported_metrics)), max_lookback_days=480,
            warnings=(f"Search Console daily facts use the provider date basis {SEARCH_CONSOLE_TIMEZONE}.",))

    def collect(self, connection, credential, request):
        for batch in self.collect_batches(connection, credential, request):
            yield from batch.points

    def collect_batches(self, connection, credential, request):
        token = _access_token(credential)
        encoded = urllib.parse.quote(request.binding.resource_id, safe="")
        configured_options = _search_console_collection_options(request.binding)
        site = binding_site(self.config, request.binding.site_id)
        if configured_options.enabled or configured_options.search_console_query_text or configured_options.search_console_page_query:
            _route_dates(
                request.window, site_timezone=site.timezone,
                options=configured_options,
            )
        start_date, end_date = _search_console_window(request.window.start, request.window.end)
        for search_type in configured_options.search_types:
            options = replace(configured_options, search_type=search_type)
            yield from self._collect_search_type_batches(
                token, encoded, start_date, end_date, site, options
            )

    def _collect_search_type_batches(
        self, token, encoded, start_date, end_date, site, options
    ):
        control, first_incomplete = self._collect_control_batch(
            token, encoded, start_date, end_date, site, options
        )
        yield control
        settled_end = (
            end_date
            if first_incomplete is None
            else min(end_date, first_incomplete - timedelta(days=1))
        )
        if settled_end >= start_date:
            yield from self._collect_geography_batches(
                token, encoded, start_date, settled_end, site, options
            )
            if options.enabled:
                yield from self._collect_route_batches(
                    token, encoded, start_date, settled_end, site, options
                )
            if (
                options.search_console_query_text
                and _search_console_supports_query_dimension(options.search_type)
            ):
                yield from self._collect_query_batches(
                    token, encoded, start_date, settled_end, site, options
                )
            if (
                options.search_console_page_query
                and _search_console_supports_query_dimension(options.search_type)
            ):
                yield from self._collect_page_query_batches(
                    token, encoded, start_date, settled_end, site, options
                )
        if options.search_console_hourly:
            yield from self._collect_hourly_batches(
                token, encoded, start_date, end_date, site, options
            )

    def _collect_control_batch(
        self, token, encoded, start_date, end_date, site, options
    ):
        aggregation = _search_console_aggregation(options.search_type)
        result = self._call(token, encoded, _search_console_body(
            start_date, end_date,
            dimensions=["date"], search_type=options.search_type,
            data_state="all", aggregation=aggregation,
        ))
        _search_console_response_aggregation(result, aggregation)
        rows = result.get("rows", [])
        if not isinstance(rows, list):
            raise ValueError("Search Console headline rows were invalid")
        if len(rows) > (end_date - start_date).days + 1:
            raise ValueError("Search Console returned too many headline rows")
        metadata = result.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Search Console headline metadata was invalid")
        incomplete_value = metadata.get("first_incomplete_date")
        first_incomplete = (
            _search_console_day(incomplete_value, start_date, end_date)
            if incomplete_value is not None
            else None
        )
        if incomplete_value is not None and first_incomplete is None:
            raise ValueError("Search Console first incomplete date was invalid")
        base_dimensions = _search_console_base_dimensions(
            options.search_type, "all", aggregation
        )
        points = []
        seen_days = set()
        metric_fields = _search_console_metric_fields(options.search_type)
        for row in rows:
            keys = row.get("keys", []) if isinstance(row, dict) else []
            day = _search_console_day(
                keys[0] if len(keys) == 1 else None, start_date, end_date
            )
            if day is None or day in seen_days:
                raise ValueError("Search Console headline dates were invalid")
            seen_days.add(day)
            values = _search_console_metrics(
                row, require_all=True, recompute_ctr=True,
                metric_fields=metric_fields,
            )
            completeness = (
                Completeness.PROVISIONAL
                if first_incomplete is not None and day >= first_incomplete
                else Completeness.FINAL
            )
            dimensions = _search_console_daily_dimensions(
                base_dimensions, day
            )
            for key, suffix, unit in metric_fields:
                points.append(daily_point(
                    client_id=site.client_id, site_id=site.id,
                    source=self.provider, metric=f"search.{suffix}", unit=unit, day=day,
                    value=values[key], timezone=site.timezone,
                    dimensions=dimensions,
                    completeness=completeness,
                ))
        batch = _search_console_batch(
            site,
            slice_key=_search_console_slice_key(
                options.search_type, "control", start_date.isoformat(),
                end_date.isoformat(),
            ),
            metric_family="search.control",
            start_date=start_date,
            end_date=end_date,
            completeness=(
                Completeness.PROVISIONAL
                if first_incomplete is not None
                else Completeness.FINAL
            ),
            data_state="all",
            provider_scope=_search_console_scope(options.search_type, "control"),
            request_dimensions=("date",),
            aggregation=aggregation,
            pages=1,
            raw_rows=len(rows),
            accepted_rows=len(rows),
            rejected_rows=0,
            exhaustion="bounded-control",
            points=points,
        )
        return batch, first_incomplete

    def _collect_geography(self, token, encoded, start_date, end_date, site, options):
        for batch in self._collect_geography_batches(
            token, encoded, start_date, end_date, site, options
        ):
            yield from batch.points

    def _collect_geography_batches(
        self, token, encoded, start_date, end_date, site, options
    ):
        aggregation = _search_console_aggregation(options.search_type)
        base = _search_console_base_dimensions(options.search_type, "final", aggregation)
        for provider_day in _provider_days(start_date, end_date):
            evidence = _collect_search_console_slice(
                self.http, token, encoded, provider_day, provider_day,
                ["date", "country"], options, data_state="final",
                aggregation=aggregation,
            )
            entries = []
            rejected = 0
            for row in evidence.rows:
                keys = row["keys"]
                day = _search_console_day(keys[0], provider_day, provider_day)
                country = keys[1].strip().upper()
                if day is None or len(country) != 3 or not country.isalpha():
                    rejected += 1
                    continue
                entries.append((day, {
                    **base,
                    **_search_console_daily_dimensions({}, day),
                    "country_code": country,
                    "country_code_system": "iso-alpha3",
                }, _search_console_metrics(
                    row, require_all=True, recompute_ctr=True,
                    metric_fields=_search_console_metric_fields(options.search_type),
                )))
            points = tuple(_daily_search_points(
                site, self.provider, "search.country", entries,
                Completeness.UNKNOWN,
            ))
            yield _search_console_batch(
                site,
                slice_key=_search_console_slice_key(
                    options.search_type, "country", provider_day.isoformat()
                ),
                metric_family="search.country",
                start_date=provider_day,
                end_date=provider_day,
                completeness=Completeness.UNKNOWN,
                data_state=evidence.data_state,
                provider_scope=_search_console_scope(
                    options.search_type, "country"
                ),
                request_dimensions=evidence.dimensions,
                aggregation=evidence.aggregation,
                pages=evidence.pages,
                raw_rows=evidence.raw_rows,
                accepted_rows=evidence.raw_rows - rejected,
                rejected_rows=rejected,
                exhaustion=evidence.exhaustion,
                points=points,
            )

    def _collect_query_observations(
        self, token, encoded, start_date, end_date, site, options
    ):
        for batch in self._collect_query_batches(
            token, encoded, start_date, end_date, site, options
        ):
            yield from batch.points

    def _collect_query_batches(
        self, token, encoded, start_date, end_date, site, options
    ):
        aggregation = _search_console_aggregation(options.search_type)
        base = _search_console_base_dimensions(options.search_type, "final", aggregation)
        for provider_day in _provider_days(start_date, end_date):
            evidence = _collect_search_console_slice(
                self.http, token, encoded, provider_day, provider_day,
                ["date", "query"], options, data_state="final",
                aggregation=aggregation,
            )
            entries = []
            rejected = 0
            for row in evidence.rows:
                keys = row["keys"]
                day = _search_console_day(keys[0], provider_day, provider_day)
                if day is None:
                    rejected += 1
                    continue
                entries.append((day, {
                    **base,
                    **_search_console_daily_dimensions({}, day),
                    "observation_scope": "query",
                    **_search_console_query_dimensions(keys[1]),
                }, _search_console_metrics(
                    row, require_all=True, recompute_ctr=True,
                    metric_fields=_search_console_metric_fields(options.search_type),
                )))
            points = tuple(_daily_search_points(
                site, self.provider, "search.query", entries,
                Completeness.UNKNOWN,
            ))
            yield _search_console_batch(
                site,
                slice_key=_search_console_slice_key(
                    options.search_type, "query", provider_day.isoformat()
                ),
                metric_family="search.query",
                start_date=provider_day,
                end_date=provider_day,
                completeness=Completeness.UNKNOWN,
                data_state=evidence.data_state,
                provider_scope=_search_console_scope(options.search_type, "query"),
                request_dimensions=evidence.dimensions,
                aggregation=evidence.aggregation,
                pages=evidence.pages,
                raw_rows=evidence.raw_rows,
                accepted_rows=evidence.raw_rows - rejected,
                rejected_rows=rejected,
                exhaustion=evidence.exhaustion,
                points=points,
            )

    def _collect_page_query_observations(
        self, token, encoded, start_date, end_date, site, options
    ):
        for batch in self._collect_page_query_batches(
            token, encoded, start_date, end_date, site, options
        ):
            yield from batch.points

    def _collect_page_query_batches(
        self, token, encoded, start_date, end_date, site, options
    ):
        aggregation = _search_console_aggregation(options.search_type, by_page=True)
        base = _search_console_base_dimensions(options.search_type, "final", aggregation)
        for provider_day in _provider_days(start_date, end_date):
            evidence = _collect_search_console_slice(
                self.http, token, encoded, provider_day, provider_day,
                ["date", "page", "query"], options, data_state="final",
                aggregation=aggregation,
            )
            entries = []
            rejected = 0
            for row in evidence.rows:
                keys = row["keys"]
                day = _search_console_day(keys[0], provider_day, provider_day)
                route = normalize_route(
                    keys[1], site.canonical_url,
                    allow_query_parameters=options.allowed_query_parameters,
                    exclusions=options.exclusions,
                )
                if day is None or route is None:
                    rejected += 1
                    continue
                entries.append((day, {
                    **base,
                    **_search_console_daily_dimensions({}, day),
                    "observation_scope": "page-query",
                    **_search_console_query_dimensions(keys[2]),
                    "route": route,
                }, _search_console_metrics(
                    row, require_all=True, recompute_ctr=True,
                    metric_fields=_search_console_metric_fields(options.search_type),
                )))
            points = tuple(_daily_search_points(
                site, self.provider, "search.page-query", entries,
                Completeness.UNKNOWN,
            ))
            yield _search_console_batch(
                site,
                slice_key=_search_console_slice_key(
                    options.search_type, "page-query", provider_day.isoformat()
                ),
                metric_family="search.page-query",
                start_date=provider_day,
                end_date=provider_day,
                completeness=Completeness.UNKNOWN,
                data_state=evidence.data_state,
                provider_scope=_search_console_scope(
                    options.search_type, "page-query"
                ),
                request_dimensions=evidence.dimensions,
                aggregation=evidence.aggregation,
                pages=evidence.pages,
                raw_rows=evidence.raw_rows,
                accepted_rows=evidence.raw_rows - rejected,
                rejected_rows=rejected,
                exhaustion=evidence.exhaustion,
                points=points,
            )

    def _collect_route_observations(self, token, encoded, start_date, end_date, site, options):
        for batch in self._collect_route_batches(
            token, encoded, start_date, end_date, site, options
        ):
            yield from batch.points

    def _collect_route_batches(
        self, token, encoded, start_date, end_date, site, options
    ):
        aggregation = _search_console_aggregation(options.search_type, by_page=True)
        base = _search_console_base_dimensions(options.search_type, "final", aggregation)
        ordinary_dimensions = tuple(
            item for item in options.search_console_dimensions
            if item != "searchAppearance"
            and _search_console_supports_route_dimension(
                options.search_type, item
            )
        )
        for provider_day in _provider_days(start_date, end_date):
            queries = [(["date", "page"], (), None, "page", "page")]
            queries.extend(
                (["date", "page", item], (), item, f"page-{item}", item)
                for item in ordinary_dimensions
            )
            if _search_console_supports_query_dimension(options.search_type):
                queries.extend(
                    (["date", "page"], (("query", "includingRegex", expression),),
                     cluster_id, "query-cluster", f"query-cluster-{cluster_id}")
                    for cluster_id, expression in options.query_clusters
                )
            for (
                dimensions, filters, scope_value, observation_scope, identifier
            ) in queries:
                evidence = _collect_search_console_slice(
                    self.http, token, encoded, provider_day, provider_day,
                    dimensions, options, data_state="final",
                    aggregation=aggregation, filters=filters,
                )
                entries, rejected = _route_search_entries(
                    evidence.rows, dimensions, provider_day, site, options, base,
                    observation_scope, scope_value,
                    _search_console_metric_fields(options.search_type),
                )
                points = tuple(_daily_search_points(
                    site, self.provider, "search.route", entries,
                    Completeness.UNKNOWN,
                ))
                yield _search_console_batch(
                    site,
                    slice_key=_search_console_slice_key(
                        options.search_type, "route", identifier,
                        provider_day.isoformat(),
                    ),
                    metric_family="search.route",
                    start_date=provider_day,
                    end_date=provider_day,
                    completeness=Completeness.UNKNOWN,
                    data_state=evidence.data_state,
                    provider_scope=_search_console_scope(
                        options.search_type, "route", identifier
                    ),
                    request_dimensions=evidence.dimensions,
                    aggregation=evidence.aggregation,
                    pages=evidence.pages,
                    raw_rows=evidence.raw_rows,
                    accepted_rows=evidence.raw_rows - rejected,
                    rejected_rows=rejected,
                    exhaustion=evidence.exhaustion,
                    points=points,
                )

            if "searchAppearance" in options.search_console_dimensions:
                discovery = _collect_search_console_slice(
                    self.http, token, encoded, provider_day, provider_day,
                    ["searchAppearance"], options, data_state="final",
                    aggregation=aggregation,
                )
                appearances = []
                discovery_rejected = 0
                for row in discovery.rows:
                    _search_console_metrics(
                        row, require_all=True, recompute_ctr=True,
                        metric_fields=_search_console_metric_fields(options.search_type),
                    )
                    label = safe_public_label(row["keys"][0], maximum=80)
                    if label is None or label in appearances:
                        discovery_rejected += 1
                        continue
                    appearances.append(label)
                yield _search_console_batch(
                    site,
                    slice_key=_search_console_slice_key(
                        options.search_type, "appearance-discovery",
                        provider_day.isoformat(),
                    ),
                    metric_family="search.discovery",
                    start_date=provider_day,
                    end_date=provider_day,
                    completeness=Completeness.UNKNOWN,
                    data_state=discovery.data_state,
                    provider_scope=_search_console_scope(
                        options.search_type, "appearance-discovery"
                    ),
                    request_dimensions=discovery.dimensions,
                    aggregation=discovery.aggregation,
                    pages=discovery.pages,
                    raw_rows=discovery.raw_rows,
                    accepted_rows=discovery.raw_rows - discovery_rejected,
                    rejected_rows=discovery_rejected,
                    exhaustion=discovery.exhaustion,
                    points=(),
                )
                for appearance in appearances:
                    appearance_fingerprint = _search_console_label_fingerprint(
                        appearance
                    )
                    evidence = _collect_search_console_slice(
                        self.http, token, encoded, provider_day, provider_day,
                        ["date", "page"], options, data_state="final",
                        aggregation=aggregation,
                        filters=(("searchAppearance", "equals", appearance),),
                    )
                    entries, rejected = _route_search_entries(
                        evidence.rows, ["date", "page"], provider_day,
                        site, options,
                        base, "page-searchAppearance", appearance,
                        _search_console_metric_fields(options.search_type),
                    )
                    points = tuple(_daily_search_points(
                        site, self.provider, "search.route", entries,
                        Completeness.UNKNOWN,
                    ))
                    yield _search_console_batch(
                        site,
                        slice_key=_search_console_slice_key(
                            options.search_type, "route", "appearance",
                            appearance_fingerprint, provider_day.isoformat(),
                        ),
                        metric_family="search.route",
                        start_date=provider_day,
                        end_date=provider_day,
                        completeness=Completeness.UNKNOWN,
                        data_state=evidence.data_state,
                        provider_scope=_search_console_scope(
                            options.search_type, "appearance",
                            appearance_fingerprint,
                        ),
                        request_dimensions=evidence.dimensions,
                        aggregation=evidence.aggregation,
                        pages=evidence.pages,
                        raw_rows=evidence.raw_rows,
                        accepted_rows=evidence.raw_rows - rejected,
                        rejected_rows=rejected,
                        exhaustion=evidence.exhaustion,
                        points=points,
                    )

    def _collect_hourly(self, token, encoded, start_date, end_date, site, options):
        for batch in self._collect_hourly_batches(
            token, encoded, start_date, end_date, site, options
        ):
            yield from batch.points

    def _collect_hourly_batches(
        self, token, encoded, start_date, end_date, site, options
    ):
        hourly_start = max(
            start_date, end_date - timedelta(days=_SEARCH_CONSOLE_HOURLY_DAYS - 1)
        )
        aggregation = _search_console_aggregation(options.search_type)
        result = self._call(token, encoded, _search_console_body(
            hourly_start, end_date,
            dimensions=["hour"], search_type=options.search_type,
            data_state="hourly_all", aggregation=aggregation,
        ))
        _search_console_response_aggregation(result, aggregation)
        rows = result.get("rows", [])
        if not isinstance(rows, list):
            raise ValueError("Search Console hourly rows were invalid")
        metadata = result.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Search Console hourly metadata was invalid")
        incomplete = metadata.get("first_incomplete_hour")
        first_incomplete = (
            _search_console_hour(incomplete, hourly_start, end_date)
            if incomplete is not None else None
        )
        if incomplete is not None and first_incomplete is None:
            raise ValueError("Search Console first incomplete hour was invalid")
        base = _search_console_base_dimensions(
            options.search_type, "hourly_all", aggregation
        )
        seen = set()
        points = []
        any_provisional = False
        metric_fields = _search_console_metric_fields(options.search_type)
        for row in rows:
            keys = row.get("keys", []) if isinstance(row, dict) else []
            hour = _search_console_hour(keys[0] if len(keys) == 1 else None,
                                        hourly_start, end_date)
            if hour is None or hour in seen:
                raise ValueError("Search Console hourly rows were invalid")
            seen.add(hour)
            values = _search_console_metrics(
                row, require_all=True, recompute_ctr=True,
                metric_fields=metric_fields,
            )
            completeness = (
                Completeness.PROVISIONAL
                if first_incomplete is not None and hour >= first_incomplete
                else Completeness.FINAL
            )
            any_provisional = any_provisional or (
                completeness is Completeness.PROVISIONAL
            )
            for key, suffix, unit in metric_fields:
                points.append(MetricPoint(
                    site.client_id, site.id, self.provider,
                    f"search.hourly-{suffix}", unit,
                    hour, hour + timedelta(hours=1), TimeGrain.HOUR,
                    values[key], tuple(sorted(base.items())), completeness,
                    datetime.now(UTC),
                ))
        yield _search_console_batch(
            site,
            slice_key=_search_console_slice_key(
                options.search_type, "hourly", hourly_start.isoformat(),
                end_date.isoformat(),
            ),
            metric_family="search.hourly",
            start_date=hourly_start,
            end_date=end_date,
            completeness=(
                Completeness.PROVISIONAL
                if first_incomplete is not None or any_provisional
                else Completeness.FINAL
            ),
            data_state="hourly_all",
            provider_scope=_search_console_scope(
                options.search_type, "hourly"
            ),
            request_dimensions=("hour",),
            aggregation=aggregation,
            pages=1,
            raw_rows=len(rows),
            accepted_rows=len(rows),
            rejected_rows=0,
            exhaustion="bounded-control",
            points=points,
        )


def _search_console_day(value, start, end):
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    if parsed < start or parsed > end:
        return None
    return parsed


def _ga_day(value, *, start=None, end=None):
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        return None
    if start is None or end is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None
    if parsed < start or parsed > end:
        return None
    return parsed.isoformat()


def _ga_observation_dimensions(kind, value, canonical_url, options):
    if kind in {"route", "route_path_only"}:
        route = normalize_route(value, canonical_url, allow_query_parameters=options.allowed_query_parameters,
            exclusions=options.exclusions, path_only=kind == "route_path_only")
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
        "date", "landingPagePlusQueryString", "pagePath",
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


def _collect_search_console_slice(
    http,
    token,
    encoded,
    start_date,
    end_date,
    dimensions,
    options,
    *,
    data_state,
    aggregation,
    filters=(),
):
    """Collect one daily high-dimensional slice with explicit exhaustion proof."""

    collected = []
    seen_keys = set()
    for page in range(options.max_pages):
        start_row = len(collected)
        if start_row > _SEARCH_CONSOLE_DAILY_ROW_CAP:
            raise ValueError("Search Console pagination exceeded the provider row cap")
        result = _require_response(http.request(
            "POST",
            f"https://www.googleapis.com/webmasters/v3/sites/{encoded}/searchAnalytics/query",
            headers=bearer(token),
            body=_search_console_body(
                start_date, end_date, dimensions=list(dimensions),
                search_type=options.search_type, data_state=data_state,
                aggregation=aggregation, row_limit=options.page_size,
                start_row=start_row, filters=tuple(filters),
            ),
        ), "Search Console")
        _search_console_response_aggregation(result, aggregation)
        rows = result.get("rows", [])
        if not isinstance(rows, list) or len(rows) > options.page_size:
            raise ValueError("Search Console observation rows were invalid")
        if not rows:
            return _SearchConsoleSlice(
                rows=tuple(collected), pages=page + 1,
                raw_rows=len(collected), accepted_rows=len(collected),
                rejected_rows=0,
                exhaustion=(
                    "provider-cap-empty"
                    if len(collected) == _SEARCH_CONSOLE_DAILY_ROW_CAP
                    else "empty-page"
                ),
                data_state=data_state, dimensions=tuple(dimensions),
                aggregation=aggregation,
            )
        if start_row >= _SEARCH_CONSOLE_DAILY_ROW_CAP:
            raise ValueError("Search Console returned rows beyond the provider row cap")
        if len(collected) + len(rows) > _SEARCH_CONSOLE_DAILY_ROW_CAP:
            raise ValueError("Search Console returned too many daily rows")
        page_keys = set()
        for row in rows:
            keys = row.get("keys") if isinstance(row, dict) else None
            if (
                not isinstance(keys, list)
                or len(keys) != len(dimensions)
                or not all(isinstance(value, str) for value in keys)
            ):
                raise ValueError("Search Console observation row keys were invalid")
            identity = tuple(keys)
            if identity in seen_keys or identity in page_keys:
                raise ValueError("Search Console pagination returned duplicate row keys")
            page_keys.add(identity)
        seen_keys.update(page_keys)
        collected.extend(rows)
    raise ValueError("Search Console observation pagination exceeded configured max_pages")


def _search_console_rows(
    http,
    token,
    encoded,
    start_date,
    end_date,
    dimensions,
    options,
    *,
    data_state,
    aggregation,
    filters=(),
):
    return _collect_search_console_slice(
        http, token, encoded, start_date, end_date, dimensions, options,
        data_state=data_state, aggregation=aggregation, filters=filters,
    ).rows


def _daily_search_points(site, provider, metric_prefix, entries, completeness):
    for day, dimensions, metrics in _combined_search_observations(entries):
        for key, suffix, unit in _SEARCH_METRIC_FIELDS:
            if key not in metrics:
                continue
            yield daily_point(
                client_id=site.client_id, site_id=site.id, source=provider,
                metric=f"{metric_prefix}-{suffix}", unit=unit, day=day,
                value=metrics[key], timezone=site.timezone,
                dimensions=dimensions, completeness=completeness,
            )


def _route_search_entries(
    rows, dimensions, provider_day, site, options, base,
    observation_scope, scope_value, metric_fields=_SEARCH_METRIC_FIELDS,
):
    entries = []
    rejected = 0
    for row in rows:
        keys = row["keys"]
        day = _search_console_day(keys[0], provider_day, provider_day)
        route = normalize_route(
            keys[1], site.canonical_url,
            allow_query_parameters=options.allowed_query_parameters,
            exclusions=options.exclusions,
        )
        if day is None or route is None:
            rejected += 1
            continue
        extra = {
            **base,
            **_search_console_daily_dimensions({}, day),
            "observation_scope": observation_scope,
            "route": route,
        }
        if observation_scope == "query-cluster":
            extra["query_cluster"] = scope_value
        elif observation_scope == "page-searchAppearance":
            extra["search_appearance"] = scope_value
        elif len(dimensions) > 2:
            key = dimensions[2]
            value = keys[2].strip()
            if key == "country" and len(value) == 3 and value.isalpha():
                extra["country_code"] = value.upper()
                extra["country_code_system"] = "iso-alpha3"
            elif key == "device" and value.casefold() in {"desktop", "mobile", "tablet"}:
                extra["device"] = value.casefold()
            else:
                rejected += 1
                continue
        entries.append((day, extra, _search_console_metrics(
            row, require_all=True, recompute_ctr=True,
            metric_fields=metric_fields,
        )))
    return entries, rejected


def _search_console_hour(value, start_date, end_date):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.minute != 0
        or parsed.second != 0
        or parsed.microsecond != 0
    ):
        return None
    provider_day = parsed.astimezone(ZoneInfo(SEARCH_CONSOLE_TIMEZONE)).date()
    if provider_day < start_date or provider_day > end_date:
        return None
    return parsed


def _route_dates(window, *, site_timezone, options):
    start, end = site_local_daily_bounds(window, site_timezone)
    days = (end.date() - start.date()).days
    if days < 1 or days > options.max_days:
        raise ValueError("route analytics request exceeds configured max_days")
    return start.date(), end.date() - timedelta(days=1)
