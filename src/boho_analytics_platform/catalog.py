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
    "umami": SourceSemantics("request-timezone", "provider-reported", "unknown"),
    "cloudflare": SourceSemantics("unverified-provider-date-bucket", "adaptive", "provisional"),
    "google-analytics": SourceSemantics("response-validated-property-timezone-or-unverified", "provider-reported", "unknown"),
    "search-console": SourceSemantics("America/Los_Angeles-provider-date-mapped-to-site-day", "provider-reported", "final-requested"),
    "cloudflare-forms": SourceSemantics("UTC-instant-filtered-configured-site-day", "exact-count", "snapshot"),
    "forms-inbox": SourceSemantics("UTC-instant-filtered-configured-site-day", "exact-count", "snapshot"),
    "fixture": SourceSemantics("fixture-declared", "fixture", "fixture"),
}


METRICS = {item.id: item for item in (
    _metric("umami.pageviews", "umami", "count", "sum", "Page views recorded by Umami."),
    _metric("umami.sessions", "umami", "count", "sum", "Sessions recorded by Umami."),
    _metric("umami.visitors", "umami", "count", "window", "Unique visitors for the exact sync window."),
    _metric("umami.visits", "umami", "count", "window", "Visits for the exact sync window."),
    _metric("umami.bounces", "umami", "count", "window", "Bounced visits for the exact sync window."),
    _metric("umami.total-time", "umami", "seconds", "window", "Total visit time for the exact sync window."),
    _metric("umami.country-visits", "umami", "count", "window", "Exact-window Umami visits grouped by ISO country code."),
    _metric("umami.region-visits", "umami", "count", "window", "Exact-window Umami visits grouped by country and region code."),
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
    _metric("cloudflare.requests", "cloudflare", "count", "sum", "Estimated eyeball HTTP requests at the edge."),
    _metric("cloudflare.visits", "cloudflare", "count", "sum", "Estimated Cloudflare visits."),
    _metric("cloudflare.bytes", "cloudflare", "bytes", "sum", "Estimated edge response bytes."),
    _metric("cloudflare.country-visits", "cloudflare", "count", "sum", "Estimated Cloudflare visits grouped by country."),
    _metric("google.active-users", "google-analytics", "count", "sum", "GA4 daily active users; do not deduplicate across days."),
    _metric("google.sessions", "google-analytics", "count", "sum", "GA4 sessions."),
    _metric("google.pageviews", "google-analytics", "count", "sum", "GA4 screen and page views."),
    _metric("google.events", "google-analytics", "count", "sum", "GA4 event count."),
    _metric("google.key-events", "google-analytics", "count", "sum", "GA4 configured key events."),
    _metric("google.country-sessions", "google-analytics", "count", "sum", "GA4 sessions grouped by country."),
    _metric("google.region-sessions", "google-analytics", "count", "sum", "GA4 sessions grouped by country and region."),
    _metric("google.landing-page-sessions", "google-analytics", "count", "sum", "GA4 sessions grouped by normalized landing page.", reportable=False, dimension_sets=(("route",),)),
    _metric("google.page-path-views", "google-analytics", "count", "sum", "GA4 views grouped by normalized page path.", reportable=False, dimension_sets=(("route",),)),
    _metric("google.page-title-views", "google-analytics", "count", "sum", "GA4 views grouped by bounded page title.", reportable=False, dimension_sets=(("page_title",),)),
    _metric("google.channel-sessions", "google-analytics", "count", "sum", "GA4 sessions grouped by default channel group.", reportable=False, dimension_sets=(("channel",),)),
    _metric("google.route-engaged-sessions", "google-analytics", "count", "sum", "GA4 engaged sessions grouped by normalized landing page.", reportable=False, dimension_sets=(("route",),)),
    _metric("google.route-engagement-seconds", "google-analytics", "seconds", "sum", "GA4 engagement seconds grouped by normalized landing page.", reportable=False, dimension_sets=(("route",),)),
    _metric("google.route-key-events", "google-analytics", "count", "sum", "GA4 key events grouped by normalized landing page.", reportable=False, dimension_sets=(("route",),)),
    _metric("google.referrer-sessions", "google-analytics", "count", "sum", "GA4 sessions with only sanitized internal routes or approved external domains.", reportable=False, dimension_sets=(("referrer_route",), ("referrer_domain",))),
    _metric("google.configured-event-count", "google-analytics", "count", "sum", "Configured GA4 event count without event parameters.", reportable=False, dimension_sets=(("event_name",),)),
    _metric("search.clicks", "search-console", "count", "sum", "Search Console clicks."),
    _metric("search.impressions", "search-console", "count", "sum", "Search Console impressions."),
    _metric("search.ctr", "search-console", "ratio", "weighted", "Search Console row CTR; not additive.",
        coverage_inputs=("search.clicks", "search.impressions")),
    _metric("search.position", "search-console", "position", "weighted", "Search Console average position; not additive.",
        coverage_inputs=("search.impressions", "search.position")),
    _metric("search.country-clicks", "search-console", "count", "sum", "Search Console clicks grouped by country."),
    _metric("search.route-clicks", "search-console", "count", "sum", "Search Console clicks grouped by normalized canonical page route and explicit observation scope.", reportable=False, dimension_sets=(
        ("data_state", "observation_scope", "route"),
        ("data_state", "device", "observation_scope", "route"),
        ("country_code", "country_code_system", "data_state", "observation_scope", "route"),
        ("data_state", "observation_scope", "route", "search_appearance"),
        ("data_state", "observation_scope", "query_cluster", "route"),
    )),
    _metric("search.route-impressions", "search-console", "count", "sum", "Search Console impressions grouped by normalized canonical page route and explicit observation scope.", reportable=False, dimension_sets=(
        ("data_state", "observation_scope", "route"),
        ("data_state", "device", "observation_scope", "route"),
        ("country_code", "country_code_system", "data_state", "observation_scope", "route"),
        ("data_state", "observation_scope", "route", "search_appearance"),
        ("data_state", "observation_scope", "query_cluster", "route"),
    )),
    _metric("search.route-ctr", "search-console", "ratio", "weighted", "Search Console route CTR; not additive.", coverage_inputs=("search.route-clicks", "search.route-impressions"), reportable=False, dimension_sets=(
        ("data_state", "observation_scope", "route"),
        ("data_state", "device", "observation_scope", "route"),
        ("country_code", "country_code_system", "data_state", "observation_scope", "route"),
        ("data_state", "observation_scope", "route", "search_appearance"),
        ("data_state", "observation_scope", "query_cluster", "route"),
    )),
    _metric("search.route-position", "search-console", "position", "weighted", "Search Console route position; not additive.", coverage_inputs=("search.route-impressions", "search.route-position"), reportable=False, dimension_sets=(
        ("data_state", "observation_scope", "route"),
        ("data_state", "device", "observation_scope", "route"),
        ("country_code", "country_code_system", "data_state", "observation_scope", "route"),
        ("data_state", "observation_scope", "route", "search_appearance"),
        ("data_state", "observation_scope", "query_cluster", "route"),
    )),
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
