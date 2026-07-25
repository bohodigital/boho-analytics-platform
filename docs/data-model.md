# Data model

Configuration schema and SQLite schema evolve independently. Database schema version 4 contains:

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

Forms facts use identity version 3 after the source-retention, mailbox-observation, and synthetic-mail
trust corrections. Schema migration preserves version-1 and version-2 rows as audit lineage, while
normal queries expose only the active version. The version-4 schema marker prevents older code from
silently reactivating quarantined version-2 zeroes. A source-backed forms sync is therefore required
after upgrade; until it runs, coverage is honestly missing.

Successful data-bearing and empty sync runs also form an acquisition-coverage ledger. Reporting
uses only successful runs from bindings that still exist in current configuration. The ledger proves
quiet daily cells for row-omitting providers; it never satisfies exact-window metrics or filtered
forms dimensions, which require explicit source facts.

The [metric catalog](../src/boho_analytics_platform/catalog.py) defines source, unit, aggregation,
and meaning. Unknown metrics, wrong units, and wrong non-fixture sources fail ingestion.

## Route observations and privacy boundary

Route analytics uses the same schema-4 deterministic metric-fact identity, upsert, provenance,
successful-empty coverage, watermarks, locks, retention, and integrity checks as existing aggregate
connectors. Search clicks are not GA4 sessions, GA4 sessions are not Umami visits, and a referrer
aggregate is not an exact link-click record.

Route observations are acquisition facts for dimension-aware consumers. They are intentionally not
selectable as ordinary saved-report metrics: the current headline reporter aggregates by metric,
site, source, and unit and would erase route/scope distinctions. Search Console page, breakdown, and
query-cluster facts retain an explicit observation scope and must never be summed across scopes.
Their CTR and position definitions remain weighted by route impressions rather than additive.

The shared normalizer stores internal routes only: it removes fragments, strips non-allowlisted query
parameters, canonicalizes percent encoding and trailing slashes, and rejects malformed, excluded, or
external URLs. External GA4 referrers can contribute only an explicitly allowlisted domain. No fact
stores raw URLs, raw Search Console queries, arbitrary event parameters, sessions, visitor/client or
distinct IDs, IP addresses, user agents, city locations, form payloads, email addresses, or phone
numbers. Search Console page facts are `UNKNOWN` completeness because the provider can return top
rows rather than a complete high-dimensional result set; their separately stored `data_state`
records that finalized rows were requested.

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
