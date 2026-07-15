# Data model

Configuration schema and SQLite schema evolve independently. V1 database schema version 1 contains:

- `metric_facts`: source-labeled aggregate facts with client/site scope, interval, grain, unit,
  canonical dimensions, completeness, observation time, and a deterministic SHA-256 upsert key.
- `capability_snapshots`: latest sanitized connector probe result.
- `sync_runs`: start/end, binding scope, status, point count, and sanitized error category.
- `sync_locks`: owner and expiry for safe stale-lock takeover.
- `watermarks`: last completed end instant for each binding.
- `schema_meta`: installed database version.

SQLite runs with foreign keys, WAL, normal synchronous mode, and a busy timeout. The design assumes a
single scheduled writer and multiple local readers. PostgreSQL is a measured future migration, not a
V1 requirement.

## Metric facts

A fact contains no raw provider response. The natural identity is client, site, source, metric,
unit, interval, grain, and canonical dimensions. Re-collecting the same identity updates value,
completeness, and observation time without creating duplicates.

The [metric catalog](../src/boho_analytics_platform/catalog.py) defines source, unit, aggregation,
and meaning. Unknown metrics, wrong units, and wrong non-fixture sources fail ingestion.

## Aggregation integrity

- Additive counts and bytes sum over the requested window.
- Search Console CTR is recomputed as clicks divided by impressions.
- Search Console position is weighted by impressions.
- Latest-state metrics select the newest contributing point.
- Exact-window Umami summary metrics are used only when their stored interval exactly matches the
  requested report interval.

These rules prevent ratios, positions, and overlapping unique-user windows from being summed.

## Privacy

The schema has no fields for credentials, form payloads, message bodies, email addresses, IPs, user
agents, visitor/session IDs, or Turnstile tokens. Form ID is an optional operational dimension; avoid
using IDs that themselves contain personal data.
