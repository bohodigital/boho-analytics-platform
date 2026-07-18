# Data model

Configuration schema and SQLite schema evolve independently. Database schema version 3 contains:

- `metric_facts`: source-labeled aggregate facts with client/site scope, interval, grain, unit,
  canonical dimensions, completeness, observation time, identity version, and a deterministic
  SHA-256 upsert key.
- `capability_snapshots`: latest sanitized connector probe result.
- `sync_runs`: start/end, binding/source scope, requested window, status, result kind, actual
  data-through, point count, and sanitized error category.
- `sync_locks`: owner and expiry for safe stale-lock takeover.
- `watermarks`: binding progress. Non-empty reads retain their observed data-through instant;
  successful empty reads advance through the completed requested window.
- `schema_meta`: installed database version.

SQLite runs with foreign keys, WAL, normal synchronous mode, and a busy timeout. The design assumes a
single scheduled writer and multiple local readers. PostgreSQL is a measured future migration, not a
V1 requirement.

## Metric facts

A fact contains no raw provider response. The natural identity is client, site, source, metric,
unit, interval, grain, and canonical dimensions. Re-collecting the same identity updates value,
completeness, and observation time without creating duplicates.

Forms facts use identity version 2 after the UTC-instant-to-site-day correction. Schema migration
preserves version-1 rows as audit lineage, while normal queries expose only the active version. A
source-backed forms sync is therefore required after upgrade; until it runs, coverage is honestly
missing rather than double-counting adjacent legacy and corrected days.

Successful data-bearing and empty sync runs also form an acquisition-coverage ledger. Reporting
uses only successful runs from bindings that still exist in current configuration. The ledger proves
quiet daily cells for row-omitting providers; it never satisfies exact-window metrics or filtered
forms dimensions, which require explicit source facts.

The [metric catalog](../src/boho_analytics_platform/catalog.py) defines source, unit, aggregation,
and meaning. Unknown metrics, wrong units, and wrong non-fixture sources fail ingestion.

## Aggregation integrity

- Additive counts and bytes sum over the requested window.
- Search Console CTR is recomputed as clicks divided by impressions.
- CTR is withheld when any contributing impression cell lacks matching click evidence; a missing
  click fact is not interpreted as an observed zero.
- Search Console position is weighted by impressions.
- Inbox unread counts are daily facts and sum across the selected window.
- Exact-window Umami summary metrics are used only when their stored interval exactly matches the
  requested report interval.

These rules prevent ratios, positions, and overlapping unique-user windows from being summed.

## Privacy

The schema has no fields for credentials, form payloads, message bodies, email addresses, IPs, user
agents, visitor/session IDs, or Turnstile tokens. Form ID is an optional operational dimension; avoid
using IDs that themselves contain personal data.
