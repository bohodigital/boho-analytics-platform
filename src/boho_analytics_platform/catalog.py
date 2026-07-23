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
) -> MetricDefinition:
    return MetricDefinition(
        identifier,
        source,
        unit,
        aggregation,
        description,
        coverage_inputs or (identifier,),
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
    _metric("search.clicks", "search-console", "count", "sum", "Search Console clicks."),
    _metric("search.impressions", "search-console", "count", "sum", "Search Console impressions."),
    _metric("search.ctr", "search-console", "ratio", "weighted", "Search Console row CTR; not additive.",
        coverage_inputs=("search.clicks", "search.impressions")),
    _metric("search.position", "search-console", "position", "weighted", "Search Console average position; not additive.",
        coverage_inputs=("search.impressions", "search.position")),
    _metric("search.country-clicks", "search-console", "count", "sum", "Search Console clicks grouped by country."),
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
