# Changelog

All notable changes will be documented here. The project follows Semantic Versioning after
the first stable release.

## Unreleased

### Documentation

- Defined reusable Analytics Operations contracts, a two-table additive schema-5 foundation,
  provider compatibility rules, trusted active-fact selection, recipient privacy, rollback, and
  threat controls. Private sequencing and site-specific inventories remain outside the public
  repository. No runtime behavior or database schema changed.

## 0.2.0 - 2026-07-25

### Changed

- Projected persisted Graph Evidence Core 2.1 reconciliation coverage and corrected structural
  findings through the CLI, Site Graph HTML, and JSON while retaining bounded SVG rendering and
  complete non-visual accounting. Structural metrics are withheld when selected display layers do
  not match the compiled contextual projection.
- Added a bounded, read-only route-observation HTML/JSON/CSV view for the accepted GA4, Search
  Console, and Umami aggregates, with provider-separated semantics, coverage, freshness, provider
  limitations, and privacy-safe filters.
- Rebuilt the analytics landing experience around a high-confidence summary, four visual KPI cards,
  one dominant area trend, and a compact action rail. Filters, raw data notes, operations evidence,
  and measurement gaps remain available without competing with the decisions the dashboard supports.
- Refined the responsive visual system with a clearer hierarchy, higher-contrast chart surface,
  coverage meter, calmer status colors, and single-column mobile layouts without adding browser
  dependencies or weakening the existing CSP.

### Fixed

- Removed compatibility-layer trap and bottleneck claims that the corrected Core 2.1 compiler does
  not establish; the legacy `orphans` summary key remains a true-orphan alias for compatible clients.
- Reconciled the existing Site Graph pan-and-zoom interaction with responsive SVG
  coordinates, lost pointer-capture cleanup, and Escape dismissal for pointer-pinned
  graph selections.
- Added configurable maturity lag for default report windows so provider-finalization delay does
  not make the dashboard's normal landing view look broken; explicit historical windows remain
  unchanged and retain truthful partial-coverage warnings.
- Excluded intentionally unconfigured site/provider combinations from coverage denominators and UI
  choices while retaining explicit `not_configured` diagnostics.
- Reused successful binding-window acquisition records to distinguish query-proven quiet dates from
  never-synced data; successful empty reads now advance binding progress without inventing facts.
- Compacted missing coverage into ranges, scoped series responses to the selected metric, suppressed
  incomplete comparison series, and fixed partial KPI, weighted fallback, and CSV context labels.
- Reconciled forms state transitions with retention-bounded daily zero facts and made inbox
  delivery/unread facts stable distinct-message sums, including zero days only after a trustworthy
  observation start.
- Fixed the native All-sites form value, early-date quick-link underflow, source/site option filtering,
  strict analytical query parsing, export filename collisions, and the same-origin favicon.
- Prevented stale facts from removed bindings from entering reports, rejected unsupported dashboard
  metric/site pairs, and failed closed on unknown D1 notification states.
- Replaced metric-presence completeness with per-site, source, metric, and date coverage; reports,
  health views, comparisons, and CSV exports now disclose missing evidence and provider semantics.
- Recomputed portfolio Search Console CTR and average position from clicks/impressions instead of
  summing ratios and averages, withheld CTR when click evidence is missing, and preserved unknown
  forms states instead of fabricating zeroes.
- Stopped silently switching visitor definitions or substituting invalid series metrics; strict,
  bounded date windows now return a controlled client error rather than underflowing the server.
- Recorded binding, requested window, result kind, and actual data-through provenance in the sync
  ledger. Successful empty reads now record acquisition coverage and advance binding progress.
- Corrected forms/mail local-day grouping, made Search Console's Pacific date basis explicit without
  changing historical fact identities, marked adaptive Cloudflare facts provisional, and upgraded
  probes from token checks to configured-resource reads where supported.
- Added non-destructive forms identity cutovers that preserve legacy facts as lineage, aggregate
  same-day D1 rows before upsert, quarantine retention-invalid historical zeroes, and expose only
  current source-backed facts to reports. Schema version 4 prevents unsafe old-code rollback.
- Removed invented and template/resource Site Graph pages, retained unresolved targets as evidence,
  and made graph compilation publish all derived state atomically.
- Restored the test suite to CI, added runtime commit/tree/schema identity, and prevented charts from
  drawing continuous lines or area fills across missing calendar dates.
- Confined scheduled-backup retention to a dedicated directory, validated blank query and configured
  metric inputs strictly, and restored browser capture for provenance-bearing health responses and
  seeded Site Graph pages.

### Added

- Added opt-in, privacy-bounded route observations for GA4, Search Console, and Umami. The shared
  route normalizer, bounded pagination/day limits, provider-specific fact catalog, and fixture tests
  preserve aggregate behavior and never store raw queries, identifiers, full referrers, or event payloads.

- Connection-ready V1 beta with Umami, Cloudflare traffic, GA4, Search Console, Cloudflare D1
  forms, read-only forms inbox, and sanitized fixture connectors.
- SQLite migrations, WAL mode, idempotent metrics, capability snapshots, sync ledgers, watermarks,
  stale-lock recovery, retention, integrity checks, backup, and guarded restore.
- Schema-v2 TOML configuration for reports, subreports, dimension filters, web policy, retention,
  provider bindings, and forms inbox monitoring.
- Saved reports with custom absolute windows, previous-period comparisons, weighted Search Console
  calculations, provider freshness, forms-pipeline reconciliation, JSON, and CSV.
- Server-rendered loopback dashboard and read-only V1 API with Host validation, CSP, no permissive
  CORS, no-store responses, and optional credential-referenced Basic authentication.
- Metric catalog enforcement, public-tree verification, and CI across supported Python versions.
- Site-graph manifest and immutable SQLite evidence contracts, deterministic contextual compilation,
  goal-distance and component analysis, CLI reporting, and a bounded accessible Site Graph dashboard.
- Source-first static HTML and vinext repository inspection/ingestion with exact Git provenance,
  clean-worktree enforcement, occurrence-preserving link layers, and idempotent snapshot reuse.
- Organic Site Graph SVG layout, full edge-accounting disclosure, complete edge table/CSV surfaces,
  and public graph-engine documentation.
