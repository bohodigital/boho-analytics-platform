"""Umami v3 read-only API adapter."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
import re
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from ..config import route_analytics_options
from ..credentials import require_text
from ..models import (
    AcquisitionBatch,
    AcquisitionSlice,
    CapabilitySnapshot,
    Completeness,
)
from .common import (
    aggregate_dimension_values,
    binding_site,
    connection_bindings,
    daily_point,
    nonnegative_integral_count,
    normalize_route,
    safe_domain,
    safe_public_label,
    sanitize_referrer,
    site_local_daily_bounds,
    total_point,
)


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

_UMAMI_GENERIC_MEASURES = {
    "pageviews": ("umami.dimension-pageviews", "count"),
    "visitors": ("umami.dimension-visitors", "count"),
    "visits": ("umami.dimension-visits", "count"),
    "bounces": ("umami.dimension-bounces", "count"),
    "totaltime": ("umami.dimension-total-time", "seconds"),
}

_UMAMI_CORE_DIMENSIONS = ("path", "entry", "exit")
_UMAMI_PROBE_PAGE_SIZE = 100
_UMAMI_PROBE_MAX_PAGES = 100
_UMAMI_HEADLINE_MAX_DAYS = 31
_UMAMI_EVENT_ROWS_PER_DAY_LIMIT = 10
_LANGUAGE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_SCREEN = re.compile(r"^[1-9][0-9]{1,5}x[1-9][0-9]{1,5}$", re.IGNORECASE)
_REGION = re.compile(r"^[A-Z0-9]{1,12}(?:-[A-Z0-9]{1,12})?$")


@dataclass(frozen=True, slots=True)
class _ExpandedPageResult:
    """Bounded page evidence retained for a future acquisition-batch adapter."""

    dimension_type: str
    request_dimensions: tuple[str, ...]
    rows: tuple[dict, ...]
    pages: int
    raw_rows: int
    exhaustive: bool
    exhaustion: str


@dataclass(frozen=True, slots=True)
class _NormalizedCollection:
    points: tuple
    accepted_rows: int
    rejected_rows: int
    completeness: Completeness


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
        resources = self._website_resources(base, headers)
        warnings: list[str] = []
        supported_metrics = {
            "umami.pageviews", "umami.daily-visitors", "umami.visitors",
            "umami.visits", "umami.bounces", "umami.total-time",
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
                supported_metrics.update(
                    _UMAMI_ROUTE_METRICS[item]
                    for item in options.umami_dimensions
                    if item in _UMAMI_ROUTE_METRICS
                )
                supported_metrics.update(
                    metric for metric, _unit in _UMAMI_GENERIC_MEASURES.values()
                )
                if options.umami_event_names:
                    supported_metrics.add("umami.configured-event-count")
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, resources,
            tuple(sorted(supported_metrics)), warnings=tuple(sorted(set(warnings))))

    def _website_resources(self, base, headers):
        resources: set[str] = set()
        for page in range(1, _UMAMI_PROBE_MAX_PAGES + 1):
            query = urlencode({"page": page, "pageSize": _UMAMI_PROBE_PAGE_SIZE})
            result = self.http.request(
                "GET", f"{base}/api/websites?{query}", headers=headers
            )
            if isinstance(result, list):
                rows = result
                count = None
            elif isinstance(result, dict) and isinstance(result.get("data"), list):
                rows = result["data"]
                count = result.get("count")
                if count is not None and (
                    isinstance(count, bool) or not isinstance(count, int) or count < 0
                ):
                    raise ValueError("Umami website pagination count was invalid")
            else:
                raise ValueError("Umami website listing response was invalid")
            if len(rows) > _UMAMI_PROBE_PAGE_SIZE:
                raise ValueError("Umami website listing exceeded the requested page size")
            page_ids: set[str] = set()
            for row in rows:
                resource_id = row.get("id") if isinstance(row, dict) else None
                if not isinstance(resource_id, str) or not resource_id.strip():
                    raise ValueError("Umami website listing contained an invalid resource")
                resource_id = resource_id.strip()
                if resource_id in resources or resource_id in page_ids:
                    raise ValueError("Umami website pagination repeated a resource")
                page_ids.add(resource_id)
            resources.update(page_ids)
            if count is not None and len(resources) >= count:
                if len(resources) != count:
                    raise ValueError("Umami website pagination exceeded its declared count")
                return tuple(sorted(resources))
            if count is None and len(rows) < _UMAMI_PROBE_PAGE_SIZE:
                return tuple(sorted(resources))
            if count is not None and not rows:
                raise ValueError("Umami website pagination ended before its declared count")
        raise ValueError("Umami website listing exceeded the bounded page limit")

    def collect(self, connection, credential, request):
        for batch in self.collect_batches(connection, credential, request):
            yield from batch.points

    def collect_batches(self, connection, credential, request):
        base = str(connection.options["base_url"]).rstrip("/")
        headers = self._headers(connection, credential)
        site = binding_site(self.config, request.binding.site_id)
        options = route_analytics_options(request.binding)
        local_start, local_end = site_local_daily_bounds(
            request.window, site.timezone
        )
        root = f"{base}/api/websites/{request.binding.resource_id}"
        series_pageviews = Decimal()
        for chunk_start, chunk_end in _headline_windows(
            local_start, local_end, site.timezone
        ):
            query = _umami_window_query(
                chunk_start, chunk_end, site.timezone, unit="day"
            )
            pageviews = self.http.request(
                "GET", f"{root}/pageviews?{query}", headers=headers
            )
            if (
                not isinstance(pageviews, dict)
                or "pageviews" not in pageviews
                or not isinstance(pageviews["pageviews"], list)
            ):
                raise ValueError("Umami pageview series was absent or invalid")
            sessions = pageviews.get("sessions")
            if not isinstance(sessions, list):
                raise ValueError("Umami daily visitor series was invalid")
            points = []
            for metric, series in (
                ("umami.pageviews", pageviews["pageviews"]),
                ("umami.daily-visitors", sessions),
            ):
                parsed = _headline_series(
                    series,
                    metric,
                    chunk_start.date(),
                    chunk_end.date(),
                    site.timezone,
                )
                if metric == "umami.pageviews":
                    series_pageviews += sum(
                        (value for _day, value in parsed), Decimal()
                    )
                for day, value in parsed:
                    points.append(daily_point(
                        client_id=site.client_id,
                        site_id=site.id,
                        source=self.provider,
                        metric=metric,
                        unit="count",
                        day=day,
                        value=value,
                        timezone=site.timezone,
                    ))
            raw_rows = len(pageviews["pageviews"]) + len(sessions)
            completeness = _snapshot_completeness(
                chunk_start, chunk_end, site.timezone
            )
            yield _acquisition_batch(
                slice_key=(
                    f"umami.headline.{chunk_start:%Y%m%d}.{chunk_end:%Y%m%d}"
                ),
                metric_family="umami-headline",
                start=chunk_start,
                end=chunk_end,
                completeness=completeness,
                data_state="snapshot",
                provider_scope="pageviews",
                request_dimensions=("date",),
                provider_aggregation="daily-series",
                pages_fetched=1,
                raw_rows=raw_rows,
                accepted_rows=raw_rows,
                exhaustion_reason="fixed-response",
                points=points,
            )

        query = _umami_window_query(
            local_start, local_end, site.timezone, unit="day"
        )
        stats = self.http.request("GET", f"{root}/stats?{query}", headers=headers)
        if not isinstance(stats, dict):
            raise ValueError("Umami stats response was invalid")
        stats_pageviews = _required_count(stats, "pageviews", "stats pageviews")
        if stats_pageviews != series_pageviews:
            raise ValueError("Umami stats pageviews did not match the daily series")
        stats_values = {
            "pageviews": stats_pageviews,
            **{
                key: _required_count(stats, key, f"stats {key}")
                for key in ("visitors", "visits", "bounces", "totaltime")
            },
        }
        stats_points = []
        for key, metric, unit in (("visitors", "umami.visitors", "count"), ("visits", "umami.visits", "count"),
                                  ("bounces", "umami.bounces", "count"), ("totaltime", "umami.total-time", "seconds")):
            stats_points.append(total_point(
                client_id=site.client_id,
                site_id=site.id,
                source=self.provider,
                metric=metric,
                unit=unit,
                start=request.window.start,
                end=request.window.end,
                value=stats_values[key],
            ))
        stats_rows = 1
        stats_completeness = _snapshot_completeness(
            request.window.start, request.window.end, site.timezone
        )
        yield _acquisition_batch(
            slice_key="umami.stats",
            metric_family="umami-stats",
            start=request.window.start,
            end=request.window.end,
            completeness=stats_completeness,
            data_state="snapshot",
            provider_scope="stats",
            request_dimensions=(),
            provider_aggregation="window-total",
            pages_fetched=1,
            raw_rows=stats_rows,
            accepted_rows=stats_rows,
            exhaustion_reason="fixed-response",
            points=stats_points,
        )
        for kind, metric in (
            ("country", "umami.country-visits"),
            ("region", "umami.region-visits"),
        ):
            yield self._collect_exact_geography_batch(
                root, headers, query, request, site, options, kind, metric
            )
        if options.enabled:
            yield from self._collect_route_batches(
                root, headers, request, site, options
            )

    def _collect_exact_geography(
        self, root, headers, query, request, site, options, kind, metric
    ):
        yield from self._collect_exact_geography_batch(
            root, headers, query, request, site, options, kind, metric
        ).points

    def _collect_exact_geography_batch(
        self, root, headers, query, request, site, options, kind, metric
    ):
        result = self._expanded_result(root, headers, query, kind, options)
        accepted = []
        for row in result.rows:
            normalized = _normalized_dimension(kind, row, site, options)
            count = nonnegative_integral_count(row.get("visits"))
            dimensions = normalized[2] if normalized is not None else None
            if dimensions is None or count is None:
                continue
            accepted.append((dimensions, count))
        normalized_rows, aggregate_rejected = aggregate_dimension_values(
            (
                (request.window.start.date(), dimensions, value)
                for dimensions, value in accepted
            ),
            integral=True,
        )
        rejected_rows = result.raw_rows - len(accepted)
        completeness = (
            Completeness.FINAL
            if result.exhaustive and not rejected_rows and not aggregate_rejected
            else Completeness.UNKNOWN
        )
        if completeness is Completeness.FINAL:
            completeness = _snapshot_completeness(
                request.window.start, request.window.end, site.timezone
            )
        points = []
        for _day, dimensions, value in normalized_rows:
            points.append(total_point(
                client_id=site.client_id,
                site_id=site.id,
                source=self.provider,
                metric=metric,
                unit="count",
                start=request.window.start,
                end=request.window.end,
                value=value,
                dimensions=dimensions,
                completeness=completeness,
            ))
        return _acquisition_batch(
            slice_key=f"umami.geography.{kind}",
            metric_family="umami-geography",
            start=request.window.start,
            end=request.window.end,
            completeness=completeness,
            data_state="snapshot",
            provider_scope=kind,
            request_dimensions=result.request_dimensions,
            provider_aggregation="expanded-window",
            pages_fetched=result.pages,
            raw_rows=result.raw_rows,
            accepted_rows=len(accepted),
            exhaustion_reason=result.exhaustion,
            points=points,
        )

    def _collect_route_observations(self, root, headers, request, site, options):
        for batch in self._collect_route_batches(
            root, headers, request, site, options
        ):
            yield from batch.points

    def _collect_route_batches(self, root, headers, request, site, options):
        zone = UTC if site.timezone == "UTC" else ZoneInfo(site.timezone)
        start_local = request.window.start.astimezone(zone); end_local = request.window.end.astimezone(zone)
        if start_local.time() != datetime.min.time() or end_local.time() != datetime.min.time():
            raise ValueError("Umami route observations require whole site-local days")
        days = (end_local.date() - start_local.date()).days
        if days < 1 or days > options.max_days:
            raise ValueError("Umami route analytics request exceeds configured max_days")
        daterange = self.http.request("GET", f"{root}/daterange", headers=headers)
        available_start, available_end = _available_range(daterange)
        yield _acquisition_batch(
            slice_key="umami.control.daterange",
            metric_family="umami-control",
            start=request.window.start,
            end=request.window.end,
            completeness=Completeness.FINAL,
            data_state="control",
            provider_scope="daterange",
            request_dimensions=(),
            provider_aggregation="observed-extent",
            pages_fetched=1,
            raw_rows=1,
            accepted_rows=1,
            exhaustion_reason="fixed-response",
            points=(),
        )
        available_start_local = available_start.astimezone(zone)
        available_end_local = available_end.astimezone(zone)
        # Umami's daterange is the first/last observed event extent, not a
        # coverage interval. Retain the first observed calendar day but mark it
        # unknown when history begins after midnight. A quiet trailing evening
        # does not invalidate an otherwise complete daily sync.
        observed_start = datetime.combine(
            available_start_local.date(), time.min, zone
        )
        trustworthy_start = datetime.combine(
            available_start_local.date()
            + (
                timedelta(days=1)
                if available_start_local.time() != time.min
                else timedelta()
            ),
            time.min,
            zone,
        )
        trustworthy_end = datetime.combine(
            available_end_local.date() + timedelta(days=1),
            time.min,
            zone,
        )
        route_start = max(start_local, observed_start)
        route_end = min(end_local, trustworthy_end)
        if route_start >= route_end:
            return
        days = (route_end.date() - route_start.date()).days
        dimension_types = tuple(dict.fromkeys(
            (*_UMAMI_CORE_DIMENSIONS, *options.umami_dimensions)
        ))
        for offset in range(days):
            day = route_start.date() + timedelta(days=offset)
            start = datetime.combine(day, datetime.min.time(), zone)
            end = start + timedelta(days=1)
            query = _umami_window_query(start, end, site.timezone)
            for kind in dimension_types:
                result = self._expanded_result(
                    root, headers, query, kind, options
                )
                collection = self._daily_dimension_collection(
                    rows=result.rows,
                    raw_rows=result.raw_rows,
                    exhaustive=result.exhaustive,
                    kind=kind,
                    day=day,
                    start=start,
                    end=end,
                    trustworthy_start=trustworthy_start,
                    trustworthy_end=trustworthy_end,
                    site=site,
                    options=options,
                )
                yield _acquisition_batch(
                    slice_key=f"umami.dimension.{kind}.{day:%Y%m%d}",
                    metric_family="umami-dimension",
                    start=start,
                    end=end,
                    completeness=collection.completeness,
                    data_state="snapshot",
                    provider_scope=kind,
                    request_dimensions=result.request_dimensions,
                    provider_aggregation="expanded-daily",
                    pages_fetched=result.pages,
                    raw_rows=result.raw_rows,
                    accepted_rows=collection.accepted_rows,
                    exhaustion_reason=result.exhaustion,
                    points=collection.points,
                )
        for event_name in options.umami_event_names:
            yield self._configured_event_batch(
                root,
                headers,
                route_start,
                route_end,
                trustworthy_start,
                trustworthy_end,
                site,
                options,
                event_name,
            )

    def _daily_dimension_points(
        self,
        *,
        rows,
        exhaustive,
        kind,
        day,
        start,
        end,
        trustworthy_start,
        trustworthy_end,
        site,
        options,
    ):
        collection = self._daily_dimension_collection(
            rows=rows,
            raw_rows=len(rows),
            exhaustive=exhaustive,
            kind=kind,
            day=day,
            start=start,
            end=end,
            trustworthy_start=trustworthy_start,
            trustworthy_end=trustworthy_end,
            site=site,
            options=options,
        )
        yield from collection.points

    def _daily_dimension_collection(
        self,
        *,
        rows,
        raw_rows,
        exhaustive,
        kind,
        day,
        start,
        end,
        trustworthy_start,
        trustworthy_end,
        site,
        options,
    ):
        accepted: dict[str, list[tuple[dict[str, str], Decimal]]] = {
            field: [] for field in _UMAMI_GENERIC_MEASURES
        }
        compatibility: dict[
            str, list[tuple[dict[str, str], Decimal]]
        ] = defaultdict(list)
        specs = _compatibility_specs(kind)
        fully_valid = raw_rows == len(rows)
        accepted_rows = 0

        for row in rows:
            normalized = _normalized_dimension(kind, row, site, options)
            if normalized is None:
                fully_valid = False
                continue
            value_kind, value, compatibility_dimensions = normalized
            generic_dimensions = {
                "dimension_type": kind,
                "dimension_value_kind": value_kind,
                "dimension_value": value,
            }
            parsed_fields = {}
            contributed = False
            for field in _UMAMI_GENERIC_MEASURES:
                count = nonnegative_integral_count(row.get(field))
                parsed_fields[field] = count
                if count is None:
                    fully_valid = False
                else:
                    accepted[field].append((generic_dimensions, count))
                    contributed = True
            for field, metric in specs:
                dimensions = compatibility_dimensions
                if metric == "umami.route-pageviews":
                    dimensions = _path_pageview_dimensions(row, site, options)
                count = parsed_fields[field]
                if dimensions is None or count is None:
                    fully_valid = False
                else:
                    compatibility[metric].append((dimensions, count))
                    contributed = True
            if contributed:
                accepted_rows += 1

        generic_rows = {}
        aggregate_rejected = False
        for field, (metric, unit) in _UMAMI_GENERIC_MEASURES.items():
            normalized_rows, rejected = aggregate_dimension_values(
                (
                    (day, dimensions, value)
                    for dimensions, value in accepted[field]
                ),
                integral=True,
            )
            generic_rows[field] = (metric, unit, normalized_rows)
            aggregate_rejected = aggregate_rejected or rejected

        compatibility_rows = {}
        for _field, metric in specs:
            normalized_rows, rejected = aggregate_dimension_values(
                (
                    (day, dimensions, value)
                    for dimensions, value in compatibility[metric]
                ),
                integral=True,
            )
            compatibility_rows[metric] = normalized_rows
            aggregate_rejected = aggregate_rejected or rejected

        completeness = _dimension_completeness(
            exhaustive,
            not fully_valid or aggregate_rejected,
            start,
            end,
            trustworthy_start,
            trustworthy_end,
            site.timezone,
        )
        points = []
        for metric, unit, normalized_rows in generic_rows.values():
            for _day, dimensions, value in normalized_rows:
                points.append(daily_point(
                    client_id=site.client_id,
                    site_id=site.id,
                    source=self.provider,
                    metric=metric,
                    unit=unit,
                    day=day,
                    value=value,
                    timezone=site.timezone,
                    dimensions=dimensions,
                    completeness=completeness,
                ))

        for _field, metric in specs:
            for _day, dimensions, value in compatibility_rows[metric]:
                points.append(daily_point(
                    client_id=site.client_id,
                    site_id=site.id,
                    source=self.provider,
                    metric=metric,
                    unit="count",
                    day=day,
                    value=value,
                    timezone=site.timezone,
                    dimensions=dimensions,
                    completeness=completeness,
                ))
        return _NormalizedCollection(
            tuple(points),
            accepted_rows,
            raw_rows - accepted_rows,
            completeness,
        )

    def _configured_event_points(
        self, root, headers, start, end, site, options, event_name
    ):
        yield from self._configured_event_batch(
            root,
            headers,
            start,
            end,
            start,
            end,
            site,
            options,
            event_name,
        ).points

    def _configured_event_batch(
        self,
        root,
        headers,
        start,
        end,
        trustworthy_start,
        trustworthy_end,
        site,
        options,
        event_name,
    ):
        query = urlencode({
            "startAt": int(start.timestamp() * 1000),
            "endAt": int(end.timestamp() * 1000) - 1,
            "unit": "day",
            "timezone": site.timezone,
            "event": event_name,
        })
        result = self.http.request(
            "GET", f"{root}/events/series?{query}", headers=headers
        )
        rows = result.get("data", result) if isinstance(result, dict) else result
        if (
            not isinstance(rows, list)
            or len(rows) > options.max_days * _UMAMI_EVENT_ROWS_PER_DAY_LIMIT
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise ValueError("Umami configured event response was invalid")
        accepted = []
        for row in rows:
            if row.get("x") != event_name or "t" not in row:
                raise ValueError("Umami configured event identity was invalid")
            day = _series_day(row["t"], site.timezone)
            if not (start.date() <= day < end.date()):
                raise ValueError("Umami configured event date was outside the request")
            count = nonnegative_integral_count(row.get("y"))
            if count is None:
                raise ValueError("Umami configured event count was invalid")
            accepted.append((day, {"event_name": event_name}, count))
        normalized, rejected = aggregate_dimension_values(accepted, integral=True)
        if rejected:
            raise ValueError("Umami configured event count was invalid")
        completeness = _dimension_completeness(
            True,
            False,
            start,
            end,
            trustworthy_start,
            trustworthy_end,
            site.timezone,
        )
        points = []
        for day, dimensions, value in normalized:
            points.append(daily_point(
                client_id=site.client_id,
                site_id=site.id,
                source=self.provider,
                metric="umami.configured-event-count",
                unit="count",
                day=day,
                value=value,
                timezone=site.timezone,
                dimensions=dimensions,
                completeness=completeness,
            ))
        return _acquisition_batch(
            slice_key=(
                f"umami.event.{event_name}.{start:%Y%m%d}.{end:%Y%m%d}"
            ),
            metric_family="umami-event",
            start=start,
            end=end,
            completeness=completeness,
            data_state="snapshot",
            provider_scope=f"event-series:{event_name}",
            request_dimensions=("date", "event"),
            provider_aggregation="daily-series",
            pages_fetched=1,
            raw_rows=len(rows),
            accepted_rows=len(rows),
            exhaustion_reason="fixed-response",
            points=points,
        )

    def _expanded_rows(self, root, headers, query, kind, *args):
        # Accept the pre-v3.1 private helper signature while deliberately
        # ignoring its unsupported field argument.
        if len(args) == 1:
            options = args[0]
        elif len(args) == 2:
            _legacy_field, options = args
        else:
            raise TypeError("Umami expanded rows require bounded options")
        result = self._expanded_result(root, headers, query, kind, options)
        return list(result.rows), result.exhaustive

    def _expanded_result(self, root, headers, query, kind, options):
        collected = []
        seen_identities = set()
        raw_rows = 0
        for page in range(options.max_pages):
            page_query = f"{query}&{urlencode({'type': kind, 'limit': options.page_size, 'offset': page * options.page_size})}"
            rows = _metric_rows(self.http.request("GET", f"{root}/metrics/expanded?{page_query}", headers=headers))
            raw_rows += len(rows)
            if len(rows) > options.page_size:
                raise ValueError("Umami route observation rows exceeded configured page_size")
            page_identities = set()
            for row in rows:
                identity = _expanded_identity(kind, row)
                if (
                    identity is None
                    or identity in seen_identities
                    or identity in page_identities
                ):
                    return _ExpandedPageResult(
                        kind,
                        (kind,),
                        tuple(collected),
                        page + 1,
                        raw_rows,
                        False,
                        "invalid-or-repeated-identity",
                    )
                page_identities.add(identity)
            seen_identities.update(page_identities)
            collected.extend(rows)
            if len(rows) < options.page_size:
                return _ExpandedPageResult(
                    kind,
                    (kind,),
                    tuple(collected),
                    page + 1,
                    raw_rows,
                    True,
                    "short-page",
                )
        return _ExpandedPageResult(
            kind,
            (kind,),
            tuple(collected),
            options.max_pages,
            raw_rows,
            False,
            "page-cap",
        )


def _acquisition_batch(
    *,
    slice_key,
    metric_family,
    start,
    end,
    completeness,
    data_state,
    provider_scope,
    request_dimensions,
    provider_aggregation,
    pages_fetched,
    raw_rows,
    accepted_rows,
    exhaustion_reason,
    points,
):
    if not 0 <= accepted_rows <= raw_rows:
        raise ValueError("Umami acquisition row accounting was invalid")
    normalized_points = tuple(
        point
        if point.completeness is completeness
        else replace(point, completeness=completeness)
        for point in points
    )
    acquisition = AcquisitionSlice(
        slice_key=slice_key,
        metric_family=metric_family,
        start=start,
        end=end,
        completeness=completeness,
        data_state=data_state,
        provider_scope=provider_scope,
        request_dimensions=tuple(request_dimensions),
        provider_aggregation=provider_aggregation,
        pages_fetched=pages_fetched,
        raw_rows=raw_rows,
        accepted_rows=accepted_rows,
        rejected_rows=raw_rows - accepted_rows,
        exhaustion_reason=exhaustion_reason,
    )
    return AcquisitionBatch(acquisition, normalized_points)


def _headline_windows(start, end, timezone):
    zone = UTC if timezone == "UTC" else ZoneInfo(timezone)
    cursor = start
    while cursor < end:
        next_date = min(
            cursor.date() + timedelta(days=_UMAMI_HEADLINE_MAX_DAYS),
            end.date(),
        )
        chunk_end = datetime.combine(next_date, time.min, zone)
        yield cursor, chunk_end
        cursor = chunk_end


def _umami_window_query(start, end, timezone, *, unit=None):
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000) - 1
    if end_ms < start_ms:
        raise ValueError("Umami request window was shorter than one millisecond")
    query = {
        "startAt": start_ms,
        "endAt": end_ms,
        "timezone": timezone,
    }
    if unit is not None:
        query["unit"] = unit
    return urlencode(query)


def _metric_rows(value) -> list[dict]:
    rows = value.get("data", value) if isinstance(value, dict) else value
    if (
        not isinstance(rows, list)
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise ValueError("Umami returned invalid expanded metrics")
    return rows


def _headline_series(series, metric, start_day, end_day, timezone):
    label = "pageview" if metric == "umami.pageviews" else "daily visitor"
    parsed = []
    seen_days: set[date] = set()
    for row in series:
        if not isinstance(row, dict) or "x" not in row:
            raise ValueError(f"Umami {label} series row was invalid")
        day = _series_day(row["x"], timezone)
        if not (start_day <= day < end_day):
            raise ValueError("Umami headline date was outside the request")
        if day in seen_days:
            raise ValueError("Umami headline series repeated a date")
        count = nonnegative_integral_count(row.get("y"))
        if count is None:
            raise ValueError(f"Umami {label} count was invalid")
        seen_days.add(day)
        parsed.append((day, count))
    return tuple(parsed)


def _required_count(mapping, key, label):
    if key not in mapping:
        raise ValueError(f"Umami {label} count was invalid")
    value = mapping[key]
    if isinstance(value, dict):
        if "value" not in value:
            raise ValueError(f"Umami {label} count was invalid")
        value = value["value"]
    count = nonnegative_integral_count(value)
    if count is None:
        raise ValueError(f"Umami {label} count was invalid")
    return count


def _expanded_identity(kind, row):
    name = row.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    if kind != "region":
        return name
    country = row.get("country")
    if not isinstance(country, str) or not country.strip():
        return None
    return country.strip().upper(), name


def _compatibility_specs(kind):
    if kind == "path":
        return (
            ("pageviews", "umami.route-pageviews"),
            ("visits", "umami.route-visits"),
        )
    if kind == "entry":
        return (("visits", "umami.entry-visits"),)
    if kind == "exit":
        return (("visits", "umami.exit-visits"),)
    metric = _UMAMI_ROUTE_METRICS.get(kind)
    return (("visits", metric),) if metric is not None else ()


def _dimension_completeness(
    exhaustive,
    rejected,
    start,
    end,
    trustworthy_start,
    trustworthy_end,
    timezone,
):
    if (
        not exhaustive
        or rejected
        or start < trustworthy_start
        or end > trustworthy_end
    ):
        return Completeness.UNKNOWN
    return _snapshot_completeness(start, end, timezone)


def _snapshot_completeness(start, end, timezone):
    zone = UTC if timezone == "UTC" else ZoneInfo(timezone)
    today_start = datetime.combine(datetime.now(zone).date(), time.min, zone)
    return (
        Completeness.PROVISIONAL
        if end > today_start
        else Completeness.FINAL
    )


def _path_pageview_dimensions(row, site, options):
    route = normalize_route(
        row.get("name"),
        site.canonical_url,
        allow_query_parameters=options.allowed_query_parameters,
        exclusions=options.exclusions,
        path_only=True,
    )
    return {"route": route} if route is not None else None


def _normalized_dimension(kind, row, site, options):
    value = row.get("name")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None

    if kind in {"path", "entry", "exit"}:
        route = normalize_route(
            value,
            site.canonical_url,
            allow_query_parameters=options.allowed_query_parameters,
            exclusions=options.exclusions,
        )
        return ("route", route, {"route": route}) if route is not None else None

    if kind == "country":
        country = _country_code(value)
        dimensions = (
            {"country_code": country, "country_code_system": "iso-alpha2"}
            if country is not None
            else None
        )
        return ("country_code", country, dimensions) if country is not None else None

    if kind == "region":
        country = _country_code(row.get("country"))
        region = value.upper()
        if country is None or _REGION.fullmatch(region) is None:
            return None
        short_region = (
            region[len(country) + 1 :]
            if region.startswith(f"{country}-")
            else region
        )
        canonical = f"{country}-{short_region}"
        return (
            "region_code",
            canonical,
            {
                "country_code": country,
                "country_code_system": "iso-alpha2",
                "region_code": short_region,
            },
        )

    if kind == "referrer":
        dimensions = sanitize_referrer(
            value,
            site.canonical_url,
            approved_domains=options.approved_referrer_domains,
            allow_query_parameters=options.allowed_query_parameters,
            exclusions=options.exclusions,
        )
        if dimensions is None:
            return None
        value_kind, normalized = next(iter(dimensions.items()))
        return value_kind, normalized, None

    if kind == "domain":
        normalized = safe_domain(value)
        return ("domain", normalized, {"domain": normalized}) if normalized else None

    if kind == "hostname":
        normalized = safe_domain(value)
        return ("hostname", normalized, None) if normalized else None

    if kind == "title":
        normalized = safe_public_label(value, maximum=160)
        return (
            ("page_title", normalized, {"page_title": normalized})
            if normalized
            else None
        )

    if kind == "channel":
        normalized = safe_public_label(value, maximum=80)
        return (
            ("channel", normalized, {"channel": normalized})
            if normalized
            else None
        )

    if kind == "device":
        normalized = safe_public_label(value, maximum=40)
        if normalized is None:
            return None
        normalized = normalized.casefold()
        compatibility = (
            {"device": normalized}
            if normalized in {"desktop", "mobile", "tablet"}
            else None
        )
        return "device", normalized, compatibility

    if kind in {"browser", "os"}:
        normalized = safe_public_label(value, maximum=80)
        return (kind, normalized, None) if normalized else None

    if kind == "language":
        normalized = value.casefold()
        return (
            ("language", normalized, None)
            if _LANGUAGE.fullmatch(normalized) is not None
            else None
        )

    if kind == "screen":
        normalized = value.casefold()
        return (
            ("screen", normalized, None)
            if _SCREEN.fullmatch(normalized) is not None
            else None
        )

    if kind == "tag":
        normalized = safe_public_label(value, maximum=80)
        return ("tag", normalized, None) if normalized else None

    if kind == "event":
        normalized = safe_public_label(value, maximum=128)
        return ("event_name", normalized, None) if normalized else None

    return None


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
    if isinstance(value, bool):
        raise ValueError("Umami returned an unsupported series timestamp")
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
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("Umami returned a timezone-less series timestamp")
            return parsed.astimezone(zone).date()
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000, UTC).astimezone(zone).date()
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("Umami returned an unsupported series timestamp") from exc
    raise ValueError("Umami returned an unsupported series timestamp")
