"""Umami v3 read-only API adapter."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from ..config import route_analytics_options
from ..credentials import require_text
from ..models import CapabilitySnapshot, Completeness
from .common import aggregate_dimension_values, binding_site, connection_bindings, daily_point, nonnegative_integral_count, normalize_route, safe_domain, site_local_daily_bounds, safe_public_label, total_point


_UMAMI_ROUTE_METRICS = {
    "path": "umami.route-visits",
    "entry": "umami.entry-visits",
    "exit": "umami.exit-visits",
    "title": "umami.page-title-visits",
    "channel": "umami.channel-visits",
    "domain": "umami.domain-visits",
    "device": "umami.device-visits",
    "country": "umami.daily-country-visits",
}


class UmamiConnector:
    provider = "umami"

    def __init__(self, config, http) -> None:
        self.config = config; self.http = http

    def _headers(self, connection, credential):
        api_key = credential.read("api_key")
        if api_key: return {"x-umami-api-key": api_key.decode("utf-8")}
        token = credential.read("token") or credential.read("value")
        if token: return {"Authorization": f"Bearer {token.decode('utf-8')}"}
        base = str(connection.options["base_url"]).rstrip("/")
        result = self.http.request("POST", f"{base}/api/auth/login", body={
            "username": require_text(credential, "username"), "password": require_text(credential, "password")})
        if not isinstance(result, dict) or not isinstance(result.get("token"), str): raise ValueError("Umami authentication response did not contain a token")
        return {"Authorization": f"Bearer {result['token']}"}

    def probe(self, connection, credential):
        base = str(connection.options["base_url"]).rstrip("/")
        headers = self._headers(connection, credential)
        result = self.http.request("GET", f"{base}/api/websites", headers=headers)
        data = result.get("data", result) if isinstance(result, dict) else result
        resources = tuple(sorted(str(item["id"]) for item in data if isinstance(item, dict) and "id" in item))
        warnings: list[str] = []
        supported_metrics = {
            "umami.pageviews", "umami.sessions", "umami.summary",
            "umami.country-visits", "umami.region-visits",
        }
        for binding in connection_bindings(self.config, connection.id):
            options = route_analytics_options(binding)
            if options.enabled:
                available = self.http.request("GET", f"{base}/api/websites/{binding.resource_id}/daterange", headers=headers)
                _available_range(available)
                warnings.append("Umami route observations use the provider-reported available date range and bounded daily requests.")
                supported_metrics.add("umami.route-pageviews")
                supported_metrics.update(_UMAMI_ROUTE_METRICS[item] for item in ("path", "entry", "exit"))
                supported_metrics.update(_UMAMI_ROUTE_METRICS[item] for item in options.umami_dimensions)
                if options.umami_event_names:
                    supported_metrics.add("umami.configured-event-count")
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, resources,
            tuple(sorted(supported_metrics)), warnings=tuple(sorted(set(warnings))))

    def collect(self, connection, credential, request):
        base = str(connection.options["base_url"]).rstrip("/"); headers = self._headers(connection, credential)
        site = binding_site(self.config, request.binding.site_id)
        local_start, local_end = site_local_daily_bounds(
            request.window, site.timezone
        )
        query = urlencode({"startAt": int(local_start.timestamp() * 1000), "endAt": int(local_end.timestamp() * 1000), "unit": "day", "timezone": site.timezone})
        root = f"{base}/api/websites/{request.binding.resource_id}"
        pageviews = self.http.request("GET", f"{root}/pageviews?{query}", headers=headers)
        if (
            not isinstance(pageviews, dict)
            or "pageviews" not in pageviews
            or not isinstance(pageviews["pageviews"], list)
        ):
            raise ValueError("Umami pageview series was absent or invalid")
        sessions = pageviews.get("sessions", [])
        if not isinstance(sessions, list):
            raise ValueError("Umami session series was invalid")
        for metric, series in (("umami.pageviews", pageviews["pageviews"]), ("umami.sessions", sessions)):
            for item in series:
                day = _series_day(item["x"], site.timezone)
                if not (local_start.date() <= day < local_end.date()):
                    raise ValueError("Umami headline date was outside the request")
                value = item.get("y")
                if metric == "umami.pageviews":
                    value = nonnegative_integral_count(value)
                    if value is None:
                        raise ValueError("Umami pageview count was invalid")
                yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider, metric=metric,
                    unit="count", day=day, value=value, timezone=site.timezone)
        stats = self.http.request("GET", f"{root}/stats?{query}", headers=headers)
        for key, metric, unit in (("visitors", "umami.visitors", "count"), ("visits", "umami.visits", "count"),
                                  ("bounces", "umami.bounces", "count"), ("totaltime", "umami.total-time", "seconds")):
            value = stats.get(key)
            if isinstance(value, dict): value = value.get("value")
            if value is not None:
                yield total_point(client_id=site.client_id, site_id=site.id, source=self.provider, metric=metric,
                    unit=unit, start=request.window.start, end=request.window.end, value=value)
        country_rows = _metric_rows(self.http.request(
            "GET", f"{root}/metrics/expanded?{query}&type=country", headers=headers))
        for row in country_rows:
            country = _country_code(row.get("name"))
            visits = row.get("visits")
            if country and visits is not None:
                yield total_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                    metric="umami.country-visits", unit="count", start=request.window.start,
                    end=request.window.end, value=visits, dimensions={
                        "country_code": country, "country_code_system": "iso-alpha2",
                    })
        region_rows = _metric_rows(self.http.request(
            "GET", f"{root}/metrics/expanded?{query}&type=region", headers=headers))
        for row in region_rows:
            country = _country_code(row.get("country"))
            region = str(row.get("name", "")).strip().upper()
            visits = row.get("visits")
            if country and region and visits is not None:
                yield total_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                    metric="umami.region-visits", unit="count", start=request.window.start,
                    end=request.window.end, value=visits, dimensions={
                        "country_code": country, "country_code_system": "iso-alpha2",
                        "region_code": region,
                    })
        options = route_analytics_options(request.binding)
        if options.enabled:
            yield from self._collect_route_observations(root, headers, request, site, options)

    def _collect_route_observations(self, root, headers, request, site, options):
        zone = UTC if site.timezone == "UTC" else ZoneInfo(site.timezone)
        start_local = request.window.start.astimezone(zone); end_local = request.window.end.astimezone(zone)
        if start_local.time() != datetime.min.time() or end_local.time() != datetime.min.time():
            raise ValueError("Umami route observations require whole site-local days")
        days = (end_local.date() - start_local.date()).days
        if days < 1 or days > options.max_days:
            raise ValueError("Umami route analytics request exceeds configured max_days")
        available_start, available_end = _available_range(
            self.http.request("GET", f"{root}/daterange", headers=headers)
        )
        available_start_local = available_start.astimezone(zone)
        available_end_local = available_end.astimezone(zone)
        if (
            start_local < available_start_local
            or end_local > available_end_local
        ):
            raise ValueError("Umami route analytics request is outside the provider available date range")
        definitions = (
            ("path", "pageviews", "umami.route-pageviews", "route"),
            ("path", "visits", "umami.route-visits", "route"),
            ("entry", "visits", "umami.entry-visits", "route"),
            ("exit", "visits", "umami.exit-visits", "route"),
        )
        definitions += tuple((
            item,
            "visits",
            _UMAMI_ROUTE_METRICS[item],
            {
                "title": "page_title",
                "channel": "channel",
                "domain": "domain",
                "device": "device",
                "country": "country",
            }[item],
        ) for item in options.umami_dimensions)
        for offset in range(days):
            day = start_local.date() + timedelta(days=offset)
            start = datetime.combine(day, datetime.min.time(), zone)
            end = start + timedelta(days=1)
            query = urlencode({"startAt": int(start.timestamp() * 1000), "endAt": int(end.timestamp() * 1000), "timezone": site.timezone})
            for kind, field, metric, dimension_key in definitions:
                rows, exhaustive = self._expanded_rows(
                    root, headers, query, kind, field, options
                )
                accepted = []
                rejected = False
                for row in rows:
                    dimensions = _umami_dimensions(
                        kind, dimension_key, row.get("name"), site.canonical_url, options,
                        path_only=metric == "umami.route-pageviews")
                    count = nonnegative_integral_count(row.get(field))
                    if dimensions and count is not None:
                        accepted.append((dimensions, count))
                    else:
                        rejected = True
                normalized, aggregate_rejected = aggregate_dimension_values(
                    ((day, dimensions, value) for dimensions, value in accepted),
                    integral=True,
                )
                completeness = (
                    Completeness.FINAL
                    if (
                        exhaustive and not rejected and not aggregate_rejected
                        and end <= available_end.astimezone(zone)
                    )
                    else Completeness.UNKNOWN
                )
                for _, dimensions, value in normalized:
                    yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                        metric=metric, unit="count", day=day, value=value, timezone=site.timezone,
                        dimensions=dimensions, completeness=completeness)
            for event_name in options.umami_event_names:
                event_query = f"{query}&{urlencode({'event': event_name})}"
                result = self.http.request("GET", f"{root}/events/series?{event_query}", headers=headers)
                rows = result.get("data", result) if isinstance(result, dict) else result
                if not isinstance(rows, list) or len(rows) > options.page_size:
                    raise ValueError("Umami configured event response was invalid")
                for row in rows:
                    if not isinstance(row, dict) or row.get("x") != event_name or row.get("y") is None:
                        continue
                    yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                        metric="umami.configured-event-count", unit="count", day=day,
                        value=row["y"], timezone=site.timezone, dimensions={"event_name": event_name})

    def _expanded_rows(self, root, headers, query, kind, field, options):
        collected = []
        seen_identities = set()
        for page in range(options.max_pages):
            page_query = f"{query}&{urlencode({'type': kind, 'field': field, 'limit': options.page_size, 'offset': page * options.page_size})}"
            rows = _metric_rows(self.http.request("GET", f"{root}/metrics/expanded?{page_query}", headers=headers))
            if len(rows) > options.page_size:
                raise ValueError("Umami route observation rows exceeded configured page_size")
            page_identities = set()
            for row in rows:
                identity = row.get("name")
                if (
                    not isinstance(identity, str)
                    or identity in seen_identities
                    or identity in page_identities
                ):
                    return collected, False
                page_identities.add(identity)
            seen_identities.update(page_identities)
            collected.extend(rows)
            if len(rows) < options.page_size:
                return collected, True
        return collected, False


def _metric_rows(value) -> list[dict]:
    rows = value.get("data", value) if isinstance(value, dict) else value
    if (
        not isinstance(rows, list)
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise ValueError("Umami returned invalid expanded metrics")
    return rows


def _available_range(value) -> tuple[datetime, datetime]:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("startDate"), str)
        or not isinstance(value.get("endDate"), str)
    ):
        raise ValueError("Umami route observations require an available-date-range response")
    try:
        start = datetime.fromisoformat(value["startDate"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(value["endDate"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Umami returned an invalid available-date-range response") from exc
    if (
        start.tzinfo is None
        or start.utcoffset() is None
        or end.tzinfo is None
        or end.utcoffset() is None
        or end < start
    ):
        raise ValueError("Umami returned an invalid available-date-range response")
    return start, end


def _country_code(value) -> str | None:
    code = str(value or "").strip().upper()
    return code if len(code) == 2 and code.isalpha() else None


def _series_day(value, timezone: str) -> date:
    zone = UTC if timezone == "UTC" else ZoneInfo(timezone)
    if isinstance(value, str):
        stripped = value.strip()
        try:
            return date.fromisoformat(stripped)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            try:
                value = float(stripped)
            except ValueError as exc:
                raise ValueError("Umami returned an unsupported series timestamp") from exc
        else:
            return parsed.astimezone(zone).date() if parsed.tzinfo else parsed.date()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000, UTC).astimezone(zone).date()
    raise ValueError("Umami returned an unsupported series timestamp")


def _umami_dimensions(kind, dimension_key, value, canonical_url, options, *, path_only=False):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if kind in {"path", "entry", "exit"}:
        route = normalize_route(value, canonical_url, allow_query_parameters=options.allowed_query_parameters,
            exclusions=options.exclusions, path_only=path_only)
        return {dimension_key: route} if route else None
    if kind == "country" and len(value) == 2 and value.isalpha():
        return {"country_code": value.upper(), "country_code_system": "iso-alpha2"}
    if kind == "device" and value.casefold() in {"desktop", "mobile", "tablet"}:
        return {"device": value.casefold()}
    if kind == "domain":
        domain = safe_domain(value)
        return {"domain": domain} if domain else None
    if kind == "channel":
        label = safe_public_label(value, maximum=80)
        return {"channel": label} if label else None
    if kind == "title":
        label = safe_public_label(value, maximum=160)
        return {"page_title": label} if label else None
    return None
