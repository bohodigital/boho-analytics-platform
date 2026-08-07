"""Stable metric definitions and ingestion validation."""

from __future__ import annotations

from dataclasses import dataclass

from .models import MetricPoint


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    id: str
    source: str
    unit: str
    aggregation: str
    description: str
    coverage_inputs: tuple[str, ...]
    reportable: bool = True
    dimension_sets: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class SourceSemantics:
    """Honest provider-level interpretation metadata for reports and exports."""

    time_basis: str
    sampling: str
    data_state: str


def _metric(
    identifier: str,
    source: str,
    unit: str,
    aggregation: str,
    description: str,
    *,
    coverage_inputs: tuple[str, ...] | None = None,
    reportable: bool = True,
    dimension_sets: tuple[tuple[str, ...], ...] = (),
) -> MetricDefinition:
    return MetricDefinition(
        identifier,
        source,
        unit,
        aggregation,
        description,
        coverage_inputs or (identifier,),
        reportable,
        tuple(tuple(sorted(item)) for item in dimension_sets),
    )


SOURCE_SEMANTICS = {
    "umami": SourceSemantics(
        "request-timezone", "provider-reported", "revisable-provider-snapshot"
    ),
    "cloudflare": SourceSemantics("unverified-provider-date-bucket", "adaptive", "provisional"),
    "google-analytics": SourceSemantics("response-validated-property-timezone-or-unverified", "provider-reported", "unknown"),
    "search-console": SourceSemantics(
        "explicit-America/Los_Angeles-provider-date-mapped-to-site-reporting-day",
        "control-totals-plus-provider-top-rows",
        "request-labeled-final-or-provisional",
    ),
    "cloudflare-forms": SourceSemantics("UTC-instant-filtered-configured-site-day", "exact-count", "snapshot"),
    "forms-inbox": SourceSemantics("UTC-instant-filtered-configured-site-day", "exact-count", "snapshot"),
    "fixture": SourceSemantics("fixture-declared", "fixture", "fixture"),
}


_SEARCH_CONTROL_DIMENSIONS = (
    (
        "aggregation", "data_state", "provider_date", "provider_timezone",
        "search_type",
    ),
    ("aggregation", "data_state", "search_type"),
)
_SEARCH_COUNTRY_DIMENSIONS = (
    (
        "aggregation", "country_code", "country_code_system", "data_state",
        "provider_date", "provider_timezone", "search_type",
    ),
)
_SEARCH_ROUTE_DIMENSIONS = (
    (
        "aggregation", "data_state", "observation_scope", "provider_date",
        "provider_timezone", "route", "search_type",
    ),
    (
        "aggregation", "data_state", "device", "observation_scope", "route",
        "provider_date", "provider_timezone", "search_type",
    ),
    (
        "aggregation", "country_code", "country_code_system", "data_state",
        "observation_scope", "provider_date", "provider_timezone", "route",
        "search_type",
    ),
    (
        "aggregation", "data_state", "observation_scope", "provider_date",
        "provider_timezone", "route", "search_appearance", "search_type",
    ),
    (
        "aggregation", "data_state", "observation_scope", "query_cluster",
        "provider_date", "provider_timezone", "route", "search_type",
    ),
)
_SEARCH_QUERY_DIMENSIONS = (
    (
        "aggregation", "data_state", "observation_scope", "provider_date",
        "provider_timezone", "query_text", "query_visibility", "search_type",
    ),
)
_SEARCH_PAGE_QUERY_DIMENSIONS = (
    (
        "aggregation", "data_state", "observation_scope", "provider_date",
        "provider_timezone", "query_text", "query_visibility", "route",
        "search_type",
    ),
)
_UMAMI_GENERIC_DIMENSIONS = (
    ("dimension_type", "dimension_value", "dimension_value_kind"),
)
_COUNTRY_DIMENSIONS = (
    ("country_code", "country_code_system"),
)
_REGION_DIMENSIONS = (
    ("country_code", "country_code_system", "region_code"),
    ("country_code", "country_code_system", "region_name"),
)


METRICS = {item.id: item for item in (
    _metric("umami.pageviews", "umami", "count", "sum", "Page views recorded by Umami."),
    _metric("umami.daily-visitors", "umami", "count", "daily-unique", "Daily unique visitors from Umami's pageview series; do not add across days to estimate window uniques."),
    _metric("umami.visitors", "umami", "count", "window", "Unique visitors for the exact sync window."),
    _metric("umami.visits", "umami", "count", "window", "Visits for the exact sync window."),
    _metric("umami.bounces", "umami", "count", "window", "Bounced visits for the exact sync window."),
    _metric("umami.total-time", "umami", "seconds", "window", "Total visit time for the exact sync window."),
    _metric("umami.country-visits", "umami", "count", "window", "Exact-window Umami visits grouped by ISO country code.", reportable=False, dimension_sets=_COUNTRY_DIMENSIONS),
    _metric("umami.region-visits", "umami", "count", "window", "Exact-window Umami visits grouped by country and region code.", reportable=False, dimension_sets=_REGION_DIMENSIONS),
    _metric("umami.route-visits", "umami", "count", "sum", "Umami visits grouped by a normalized internal route.", reportable=False, dimension_sets=(("route",),)),
    _metric("umami.route-pageviews", "umami", "count", "sum", "Umami pageviews grouped by a normalized internal pathname; never sourced from visits.", reportable=False, dimension_sets=(("route",),)),
    _metric("umami.entry-visits", "umami", "count", "sum", "Umami visits grouped by a normalized entry route.", reportable=False, dimension_sets=(("route",),)),
    _metric("umami.exit-visits", "umami", "count", "sum", "Umami visits grouped by a normalized exit route.", reportable=False, dimension_sets=(("route",),)),
    _metric("umami.page-title-visits", "umami", "count", "sum", "Umami visits grouped by a bounded page title.", reportable=False, dimension_sets=(("page_title",),)),
    _metric("umami.channel-visits", "umami", "count", "sum", "Umami visits grouped by provider channel.", reportable=False, dimension_sets=(("channel",),)),
    _metric("umami.domain-visits", "umami", "count", "sum", "Umami visits grouped by provider domain.", reportable=False, dimension_sets=(("domain",),)),
    _metric("umami.device-visits", "umami", "count", "sum", "Umami visits grouped by provider device category.", reportable=False, dimension_sets=(("device",),)),
    _metric("umami.daily-country-visits", "umami", "count", "sum", "Daily Umami visits grouped by country code.", reportable=False, dimension_sets=(("country_code", "country_code_system"),)),
    _metric("umami.configured-event-count", "umami", "count", "sum", "Configured Umami event count without event parameters.", reportable=False, dimension_sets=(("event_name",),)),
    _metric("umami.dimension-pageviews", "umami", "count", "sum", "Daily Umami pageviews for one privacy-safe expanded dimension value.", reportable=False, dimension_sets=_UMAMI_GENERIC_DIMENSIONS),
    _metric("umami.dimension-visitors", "umami", "count", "daily-unique", "Daily Umami visitors for one privacy-safe expanded dimension value; do not deduplicate across days.", reportable=False, dimension_sets=_UMAMI_GENERIC_DIMENSIONS),
    _metric("umami.dimension-visits", "umami", "count", "sum", "Daily Umami visits for one privacy-safe expanded dimension value.", reportable=False, dimension_sets=_UMAMI_GENERIC_DIMENSIONS),
    _metric("umami.dimension-bounces", "umami", "count", "sum", "Daily Umami bounces for one privacy-safe expanded dimension value.", reportable=False, dimension_sets=_UMAMI_GENERIC_DIMENSIONS),
    _metric("umami.dimension-total-time", "umami", "seconds", "sum", "Daily Umami total time for one privacy-safe expanded dimension value.", reportable=False, dimension_sets=_UMAMI_GENERIC_DIMENSIONS),
    _metric("cloudflare.requests", "cloudflare", "count", "sum", "Estimated eyeball HTTP requests at the edge."),
    _metric("cloudflare.visits", "cloudflare", "count", "sum", "Estimated Cloudflare visits."),
    _metric("cloudflare.bytes", "cloudflare", "bytes", "sum", "Estimated edge response bytes."),
    _metric("cloudflare.country-visits", "cloudflare", "count", "sum", "Estimated Cloudflare visits grouped by country.", reportable=False, dimension_sets=_COUNTRY_DIMENSIONS),
    _metric("google.active-users", "google-analytics", "count", "sum", "GA4 daily active users; do not deduplicate across days."),
    _metric("google.sessions", "google-analytics", "count", "sum", "GA4 sessions."),
    _metric("google.pageviews", "google-analytics", "count", "sum", "GA4 screen and page views."),
    _metric("google.events", "google-analytics", "count", "sum", "GA4 event count."),
    _metric("google.key-events", "google-analytics", "count", "sum", "GA4 configured key events."),
    _metric("google.country-sessions", "google-analytics", "count", "sum", "GA4 sessions grouped by country.", reportable=False, dimension_sets=_COUNTRY_DIMENSIONS),
    _metric("google.region-sessions", "google-analytics", "count", "sum", "GA4 sessions grouped by country and region.", reportable=False, dimension_sets=_REGION_DIMENSIONS),
    _metric("google.landing-page-sessions", "google-analytics", "count", "sum", "GA4 sessions grouped by normalized landing page.", reportable=False, dimension_sets=(("route",),)),
    _metric("google.page-path-views", "google-analytics", "count", "sum", "GA4 views grouped by normalized page path.", reportable=False, dimension_sets=(("route",),)),
    _metric("google.page-title-views", "google-analytics", "count", "sum", "GA4 views grouped by bounded page title.", reportable=False, dimension_sets=(("page_title",),)),
    _metric("google.channel-sessions", "google-analytics", "count", "sum", "GA4 sessions grouped by default channel group.", reportable=False, dimension_sets=(("channel",),)),
    _metric("google.route-engaged-sessions", "google-analytics", "count", "sum", "GA4 engaged sessions grouped by normalized landing page.", reportable=False, dimension_sets=(("route",),)),
    _metric("google.route-engagement-seconds", "google-analytics", "seconds", "sum", "GA4 engagement seconds grouped by normalized landing page.", reportable=False, dimension_sets=(("route",),)),
    _metric("google.route-key-events", "google-analytics", "count", "sum", "GA4 key events grouped by normalized landing page.", reportable=False, dimension_sets=(("route",),)),
    _metric("google.referrer-sessions", "google-analytics", "count", "sum", "GA4 sessions with only sanitized internal routes or approved external domains.", reportable=False, dimension_sets=(("referrer_route",), ("referrer_domain",))),
    _metric("google.configured-event-count", "google-analytics", "count", "sum", "Configured GA4 event count without event parameters.", reportable=False, dimension_sets=(("event_name",),)),
    _metric("search.clicks", "search-console", "count", "sum", "Final or explicitly provisional Search Console property-level clicks for one search type and Pacific provider date.", dimension_sets=_SEARCH_CONTROL_DIMENSIONS),
    _metric("search.impressions", "search-console", "count", "sum", "Final or explicitly provisional Search Console property-level impressions for one search type and Pacific provider date.", dimension_sets=_SEARCH_CONTROL_DIMENSIONS),
    _metric("search.ctr", "search-console", "ratio", "weighted", "Search Console row CTR; not additive.",
        coverage_inputs=("search.clicks", "search.impressions"), dimension_sets=_SEARCH_CONTROL_DIMENSIONS),
    _metric("search.position", "search-console", "position", "weighted", "Search Console average position; not additive.",
        coverage_inputs=("search.impressions", "search.position"), dimension_sets=_SEARCH_CONTROL_DIMENSIONS),
    _metric("search.country-clicks", "search-console", "count", "sum", "Provider-limited Search Console clicks grouped by country.", reportable=False, dimension_sets=_SEARCH_COUNTRY_DIMENSIONS),
    _metric("search.country-impressions", "search-console", "count", "sum", "Provider-limited Search Console impressions grouped by country.", reportable=False, dimension_sets=_SEARCH_COUNTRY_DIMENSIONS),
    _metric("search.country-ctr", "search-console", "ratio", "weighted", "Provider-limited Search Console country CTR; not additive.", coverage_inputs=("search.country-clicks", "search.country-impressions"), reportable=False, dimension_sets=_SEARCH_COUNTRY_DIMENSIONS),
    _metric("search.country-position", "search-console", "position", "weighted", "Provider-limited Search Console country position, weighted by impressions.", coverage_inputs=("search.country-impressions", "search.country-position"), reportable=False, dimension_sets=_SEARCH_COUNTRY_DIMENSIONS),
    _metric("search.route-clicks", "search-console", "count", "sum", "Search Console clicks grouped by normalized canonical page route and explicit observation scope.", reportable=False, dimension_sets=_SEARCH_ROUTE_DIMENSIONS),
    _metric("search.route-impressions", "search-console", "count", "sum", "Search Console impressions grouped by normalized canonical page route and explicit observation scope.", reportable=False, dimension_sets=_SEARCH_ROUTE_DIMENSIONS),
    _metric("search.route-ctr", "search-console", "ratio", "weighted", "Search Console route CTR; not additive.", coverage_inputs=("search.route-clicks", "search.route-impressions"), reportable=False, dimension_sets=_SEARCH_ROUTE_DIMENSIONS),
    _metric("search.route-position", "search-console", "position", "weighted", "Search Console route position; not additive.", coverage_inputs=("search.route-impressions", "search.route-position"), reportable=False, dimension_sets=_SEARCH_ROUTE_DIMENSIONS),
    _metric("search.query-clicks", "search-console", "count", "sum", "Provider-limited Search Console clicks grouped by privacy-screened query wording.", reportable=False, dimension_sets=_SEARCH_QUERY_DIMENSIONS),
    _metric("search.query-impressions", "search-console", "count", "sum", "Provider-limited Search Console impressions grouped by privacy-screened query wording.", reportable=False, dimension_sets=_SEARCH_QUERY_DIMENSIONS),
    _metric("search.query-ctr", "search-console", "ratio", "weighted", "Provider-limited Search Console query CTR; not additive.", coverage_inputs=("search.query-clicks", "search.query-impressions"), reportable=False, dimension_sets=_SEARCH_QUERY_DIMENSIONS),
    _metric("search.query-position", "search-console", "position", "weighted", "Provider-limited Search Console query position, weighted by impressions.", coverage_inputs=("search.query-impressions", "search.query-position"), reportable=False, dimension_sets=_SEARCH_QUERY_DIMENSIONS),
    _metric("search.page-query-clicks", "search-console", "count", "sum", "Provider-limited Search Console clicks grouped by page and privacy-screened query wording.", reportable=False, dimension_sets=_SEARCH_PAGE_QUERY_DIMENSIONS),
    _metric("search.page-query-impressions", "search-console", "count", "sum", "Provider-limited Search Console impressions grouped by page and privacy-screened query wording.", reportable=False, dimension_sets=_SEARCH_PAGE_QUERY_DIMENSIONS),
    _metric("search.page-query-ctr", "search-console", "ratio", "weighted", "Provider-limited Search Console page-query CTR; not additive.", coverage_inputs=("search.page-query-clicks", "search.page-query-impressions"), reportable=False, dimension_sets=_SEARCH_PAGE_QUERY_DIMENSIONS),
    _metric("search.page-query-position", "search-console", "position", "weighted", "Provider-limited Search Console page-query position, weighted by impressions.", coverage_inputs=("search.page-query-impressions", "search.page-query-position"), reportable=False, dimension_sets=_SEARCH_PAGE_QUERY_DIMENSIONS),
    _metric("search.hourly-clicks", "search-console", "count", "sum", "Provisional Search Console hourly clicks for one explicit search type.", reportable=False, dimension_sets=_SEARCH_CONTROL_DIMENSIONS),
    _metric("search.hourly-impressions", "search-console", "count", "sum", "Provisional Search Console hourly impressions for one explicit search type.", reportable=False, dimension_sets=_SEARCH_CONTROL_DIMENSIONS),
    _metric("search.hourly-ctr", "search-console", "ratio", "weighted", "Provisional Search Console hourly CTR; not additive.", coverage_inputs=("search.hourly-clicks", "search.hourly-impressions"), reportable=False, dimension_sets=_SEARCH_CONTROL_DIMENSIONS),
    _metric("search.hourly-position", "search-console", "position", "weighted", "Provisional Search Console hourly position, weighted by impressions.", coverage_inputs=("search.hourly-impressions", "search.hourly-position"), reportable=False, dimension_sets=_SEARCH_CONTROL_DIMENSIONS),
    _metric("forms.submissions", "cloudflare-forms", "count", "sum", "Durably stored form submissions."),
    _metric("forms.pending", "cloudflare-forms", "count", "sum", "Stored submissions whose notification is pending."),
    _metric("forms.sent", "cloudflare-forms", "count", "sum", "Stored submissions whose notification is marked sent."),
    _metric("forms.failed", "cloudflare-forms", "count", "sum", "Stored submissions whose notification is marked failed."),
    _metric("forms.inbox-deliveries", "forms-inbox", "count", "sum", "Matching notification messages observed in the configured mailbox."),
    _metric("forms.inbox-unread", "forms-inbox", "count", "sum", "Matching notification messages received in the window without a Seen flag."),
)}


def validate_points(points: list[MetricPoint], *, fixture: bool = False) -> None:
    for point in points:
        definition = METRICS.get(point.metric)
        if definition is None:
            raise ValueError(f"connector emitted an unknown metric: {point.metric}")
        if point.unit != definition.unit:
            raise ValueError(f"connector emitted the wrong unit for {point.metric}")
        if not fixture and point.source != definition.source:
            raise ValueError(f"connector emitted the wrong source for {point.metric}")
        dimension_names = tuple(sorted(key for key, _value in point.dimensions))
        if definition.dimension_sets and dimension_names not in definition.dimension_sets:
            raise ValueError(f"connector emitted invalid dimensions for {point.metric}")
