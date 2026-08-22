# Changelog

All notable changes are documented here. The project follows Semantic Versioning.

## 0.3.0 - 2026-08-22

### Added

- Added schema 8 Page Intelligence: a canonical path-only page catalog, provider-separated daily
  page facts, sitemap-fingerprint links, immutable materialization evidence, and versioned
  declarative clustering schemes.
- Added bounded Page Intelligence APIs for property summaries, pages, clusters, opportunities, and
  clustering schemes. Responses include filters, definitions, freshness, completeness,
  materialization identity, and pagination.
- Added dashboard cluster-share, low-CTR candidate, page-performance, property-level index coverage,
  and page-level evidence views backed by the same calculations as the JSON API.
- Added a privacy-bounded Search Console URL Inspection census. It inventories same-host sitemap
  URLs, stores fingerprints rather than URL text, advances within configured quotas, and withholds
  indexed totals until the current inventory is complete and fresh.
- Added schema 6 acquisition provenance and normalized observations for privacy-safe Search Console,
  GA4, and Umami route and dimension evidence.
- Added an optional Search Console BigQuery reader that mirrors complete aggregate-export revisions
  into a private immutable Parquet lake with cost ceilings, storage identity checks, checksums, and
  no path into SQLite or browser exports.
- Added a dependency-free runtime storage verifier, per-property sync wrapper, and validated atomic
  scheduled-backup wrapper.
- Added the schema 5 immutable Analytics Operations definition registry with closed validation,
  activation history, append-only retirement evidence, and restore-time integrity checks.

### Changed

- Rebuilt the dashboard as an all-in-one portfolio or single-property summary with clearer labels,
  provider-qualified values, multiple visual summaries, filled line charts, exact hover details,
  and persistent light and OLED-dark themes.
- Count-chart axes and count labels now use integers or compact `K`/`M`/`B` notation; fractional
  count tick marks are not emitted.
- Search Console surfaces, provider date bases, data state, aggregation, pagination, and
  high-dimensional completeness remain explicit instead of being blended into one total.
- GA4, Umami, Search Console, Cloudflare, forms, and index evidence remain provider separated.
  Cross-provider visitor or pageview totals are not silently summed or deduplicated.
- Successful provider syncs and completed index inventories automatically refresh their selected
  Page Intelligence scope.
- The per-property sync wrapper defaults to a 3,600-second bound and continues with later properties
  after an individual timeout or provider failure.
- External-storage and BigQuery documentation now describes generic UUID-mounted storage rather
  than any organization-specific host or hardware.

### Fixed

- Page Intelligence API reads return a bounded retryable HTTP 503 with `Retry-After` during SQLite
  write contention instead of dropping the connection or exposing storage details.
- Search Console CTR is recomputed from summed clicks and impressions, while average position is
  impression weighted. Unsupported position on Discover and Google News remains unavailable.
- Umami route visits remain visits rather than pageviews, and daily visitors remain a daily series
  rather than an invented report-window unique total.
- Incomplete provider cells remain absent or unknown instead of becoming synthetic zeroes.
- Scheduled SQLite backups validate a private temporary database and publish atomically; a failed or
  timed-out attempt cannot overwrite a verified backup.
- Route comparisons use mature complete overlapping dates only and disclose withholding reasons,
  source-only dates, evidence state, and reconciliation limits.

### Security and privacy

- Public examples use only placeholder resources and fixture data. The tracked tree contains no
  live property mappings, provider identifiers, credentials, database contents, raw provider
  payloads, visitor identifiers, or private URL/query evidence.
- Release verification binds a clean checkout to its exact Git tree and rejects unexpected files,
  generated state, private paths, secret-like values, credentialed remote targets, and
  organization-specific terms.

## 0.2.0 - 2026-07-25

### Added

- Added opt-in privacy-bounded route observations for GA4, Search Console, and Umami with bounded
  HTML, JSON, and CSV views.
- Added Site Graph manifests, immutable evidence storage, deterministic contextual compilation,
  goal-distance and component analysis, and an accessible structural dashboard.
- Added source-first static HTML and application-repository inspection with exact Git provenance,
  clean-worktree enforcement, occurrence-preserving link layers, and idempotent snapshot reuse.
- Added provider bindings, SQLite migrations, sync ledgers, watermarks, lease locking, retention,
  online backup, guarded restore, saved reports, previous-period comparisons, and CSV/JSON exports.

### Changed

- Rebuilt the landing experience around a summary, visual KPI cards, one dominant trend, an action
  rail, and progressively disclosed measurement evidence.
- Added corrected Graph Evidence Core 2.1 reconciliation coverage while preserving bounded SVG
  rendering and complete non-visual accounting.
- Added configurable maturity lag for default report windows and retained explicit historical
  windows with truthful partial-coverage warnings.

### Fixed

- Recomputed portfolio Search Console CTR and average position instead of summing ratios or
  averages.
- Replaced metric-presence completeness with per-site, source, metric, and date coverage.
- Prevented stale facts from removed bindings from entering reports and failed closed on unknown
  forms states.
- Preserved missing calendar dates as gaps rather than drawing continuous chart lines through them.
- Restored CI coverage, runtime commit/tree/schema identity, browser capture, strict Host handling,
  CSP, no-store responses, and bounded analytical query parsing.
