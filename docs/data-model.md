# Data model

Configuration schema and SQLite schema evolve independently. Database schema version 6 contains:

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
- `acquisition_slices`: immutable per-request evidence: binding/run, bounded request family and
  dimensions, provider scope/aggregation/data state, interval, pages fetched, raw/accepted/rejected
  row counts, completeness, exhaustion reason, and an integrity hash.
- `metric_fact_observations`: immutable normalized fact versions linked to one acquisition slice.
  It retains what a provider sync reported at each observation without storing the raw response.
- `analytics_definition_versions`: immutable, sanitized, canonical definition versions with
  deterministic content, natural-identity, and full-record hashes.
- `analytics_definition_activations`: retained activation history with a composite reference to its
  version. Activation rows are fully immutable.
- `analytics_definition_retirements`: one immutable, hash-bound terminal event per retired
  activation. An activation is current only while no retirement event references it; insertion
  guards permit exactly one current activation per scoped key.

SQLite runs with foreign keys, WAL, normal synchronous mode, and a busy timeout. The design assumes a
single scheduled writer and multiple local readers. PostgreSQL is a measured future migration, not a
V1 requirement.

Migration 005 adds only those three registry tables and does not rewrite schema-4 facts, acquisition
coverage, sync history, watermarks, forms lineage, or graph evidence. Application validation
accepts only the closed `goal`, `segment`, `alert_rule`, and `report_subscription` types and
constructs canonical JSON from recognized fields before opening a write transaction. Identical
active content is a no-op; retired content is reactivated without duplicating its version; changed
content receives the next version while the prior activation is retired at the same transaction
timestamp. If the same version and public transaction timestamp recur, the activation identity uses
the first unused deterministic collision ordinal, so reactivation remains append-only and
restore-verifiable. A successor activation cannot precede the retirement that makes room for it;
all authority reads and integrity checks reject overlapping or non-monotonic activation intervals.
Omission is not retirement.

SQL checks, composite foreign keys, current-state insertion guards, and update/delete triggers
reinforce the application contract. Text primary keys are explicitly non-null, and canonical
`T`-separated UTC timestamps are compared both as normalized instants and canonical text so
sub-millisecond backwards retirement cannot pass. Definition integrity pins the exact tables,
indexes, and triggers installed by migration 005, recursively resolves every embedded typed version
reference, and validates the current version before any retirement or successor activation.
Restore holds one read snapshot while validating and copying, validates the copied destination,
and only then atomically makes it authoritative. No browser or feature-specific consumer is
included in the schema foundation.

Migration 006 adds acquisition slices and fact observations without rewriting prior facts, sync
runs, watermarks, or definition history. The current `metric_facts` table remains the read-optimized
latest snapshot. A provenance-aware connector writes its immutable request evidence, immutable fact
versions, and latest snapshot in one transaction. Update/delete triggers keep the new history
append-only; raw provider payloads, credentials, and arbitrary diagnostics are not stored.

## Metric facts

A fact contains no raw provider response. The natural identity is client, site, source, metric,
unit, interval, grain, canonical dimensions, and the source's active identity version.
Re-collecting the same identity updates the current snapshot. Provenance-aware GSC and Umami syncs
also append an immutable observation, so corrections and revisable provider results remain auditable.

Forms facts use identity version 3 after the source-retention, mailbox-observation, and synthetic-mail
trust corrections. Schema migration preserves version-1 and version-2 rows as audit lineage, while
normal queries expose only the active version. The version-4 schema marker prevents older code from
silently reactivating quarantined version-2 zeroes. A source-backed forms sync is therefore required
after upgrade; until it runs, coverage is honestly missing.

Search Console and Umami use identity version 2 for this acquisition cutover. Legacy Search Console
headline rows lack the now-required search type, provider data state, and aggregation identity.
Legacy Umami route-pageview rows may have relied on an unsupported expanded-metrics field selector.
Both remain in SQLite as lineage but are excluded from current reads until replaced by a fresh
source-backed sync.

Umami's pageview endpoint field named `sessions` is stored as `umami.daily-visitors`, not as an
additive session metric. Its values are daily uniques and must not be summed to estimate the exact
window visitor total; `umami.visitors` is the separately validated exact-window stats value.

Successful data-bearing and empty sync runs also form an acquisition-coverage ledger. Reporting
uses only successful runs from bindings that still exist in current configuration. The ledger proves
quiet daily cells for row-omitting providers; it never satisfies exact-window metrics or filtered
forms dimensions, which require explicit source facts.

The [metric catalog](../src/boho_analytics_platform/catalog.py) defines source, unit, aggregation,
and meaning. Unknown metrics, wrong units, and wrong non-fixture sources fail ingestion.

## Route observations and privacy boundary

Route analytics uses the same schema-6 deterministic metric-fact identity, latest snapshot,
immutable acquisition provenance,
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
stores raw URLs, unscreened Search Console queries, arbitrary event parameters, sessions, visitor/client or
distinct IDs, IP addresses, user agents, city locations, form payloads, email addresses, or phone
numbers. When query capture is explicitly enabled, bounded wording that passes the direct-identifier
screen may be stored; unsafe wording contributes only to a single redacted aggregate bucket.
Search Console high-dimensional facts are `UNKNOWN` completeness because the provider can return top
rows rather than a complete high-dimensional result set; their separately stored `data_state`
records whether finalized or provisional rows were requested.
Daily Search Console facts additionally retain the provider's Pacific date label and timezone.
Their fact interval is the platform's same-named site reporting-day bucket; the linked acquisition
slice is the exact `America/Los_Angeles` provider interval. A provisional `all`/`hourly_all` slice
can update returned rows but cannot use an omitted row as an authoritative deletion.

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

Definition JSON and metadata are bounded, strictly allowlisted, and sanitized. They may not contain
raw configuration, credentials, recipient addresses, full external URLs, raw queries, private
paths, provider payloads, message content, form payloads, or visitor/session identifiers. A future
delivery consumer may store only a keyed, non-reversible digest of the canonical recipient set or
a bounded count when operationally necessary; the digest key and recipient addresses remain
outside SQLite and the public repository. The public `AnalyticsDefinition` model never holds those
private values: a report-subscription supplies them only as call-scoped validation/package keyword
inputs. Construction privacy-screens and deep-freezes a detached copy of public content and
metadata, so caller mutation cannot add private material afterward. Consequently dataclass
conversion, copying, pickling, slots inspection, and the definition representation cannot serialize
recipient material. Only omitted metadata becomes `{}`; falsey lists, scalars, and every other
non-mapping value fail rather than being normalized. Validation rejects any caller-supplied
`recipient_set_id`, requires an ordinary bounded list or tuple of addresses matching the supported
ASCII dot-atom mailbox and per-label domain grammar, derives the HMAC-SHA-256 identifier, and
contributes only that identifier to canonical JSON. Python object secrecy is not treated as an
authorization boundary.
