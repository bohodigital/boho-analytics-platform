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


def _metric(identifier: str, source: str, unit: str, aggregation: str, description: str) -> MetricDefinition:
    return MetricDefinition(identifier, source, unit, aggregation, description)


METRICS = {item.id: item for item in (
    _metric("umami.pageviews", "umami", "count", "sum", "Page views recorded by Umami."),
    _metric("umami.sessions", "umami", "count", "sum", "Sessions recorded by Umami."),
    _metric("umami.visitors", "umami", "count", "window", "Unique visitors for the exact sync window."),
    _metric("umami.visits", "umami", "count", "window", "Visits for the exact sync window."),
    _metric("umami.bounces", "umami", "count", "window", "Bounced visits for the exact sync window."),
    _metric("umami.total-time", "umami", "seconds", "window", "Total visit time for the exact sync window."),
    _metric("cloudflare.requests", "cloudflare", "count", "sum", "Estimated eyeball HTTP requests at the edge."),
    _metric("cloudflare.visits", "cloudflare", "count", "sum", "Estimated Cloudflare visits."),
    _metric("cloudflare.bytes", "cloudflare", "bytes", "sum", "Estimated edge response bytes."),
    _metric("google.active-users", "google-analytics", "count", "sum", "GA4 daily active users; do not deduplicate across days."),
    _metric("google.sessions", "google-analytics", "count", "sum", "GA4 sessions."),
    _metric("google.pageviews", "google-analytics", "count", "sum", "GA4 screen and page views."),
    _metric("google.events", "google-analytics", "count", "sum", "GA4 event count."),
    _metric("google.key-events", "google-analytics", "count", "sum", "GA4 configured key events."),
    _metric("search.clicks", "search-console", "count", "sum", "Search Console clicks."),
    _metric("search.impressions", "search-console", "count", "sum", "Search Console impressions."),
    _metric("search.ctr", "search-console", "ratio", "weighted", "Search Console row CTR; not additive."),
    _metric("search.position", "search-console", "position", "weighted", "Search Console average position; not additive."),
    _metric("forms.submissions", "cloudflare-forms", "count", "sum", "Durably stored form submissions."),
    _metric("forms.pending", "cloudflare-forms", "count", "sum", "Stored submissions whose notification is pending."),
    _metric("forms.sent", "cloudflare-forms", "count", "sum", "Stored submissions whose notification is marked sent."),
    _metric("forms.failed", "cloudflare-forms", "count", "sum", "Stored submissions whose notification is marked failed."),
    _metric("forms.inbox-deliveries", "forms-inbox", "count", "sum", "Matching notification messages observed in the configured mailbox."),
    _metric("forms.inbox-unread", "forms-inbox", "count", "latest", "Matching notification messages without a Seen flag."),
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
