# Changelog

All notable changes will be documented here. The project follows Semantic Versioning after
the first stable release.

## Unreleased

### Fixed

- Replaced metric-presence completeness with per-site, source, metric, and date coverage; reports,
  health views, comparisons, and CSV exports now disclose missing evidence and provider semantics.
- Recomputed portfolio Search Console CTR and average position from clicks/impressions instead of
  summing ratios and averages, withheld CTR when click evidence is missing, and preserved unknown
  forms states instead of fabricating zeroes.
- Stopped silently switching visitor definitions or substituting invalid series metrics; strict,
  bounded date windows now return a controlled client error rather than underflowing the server.
- Made empty sync results warnings that do not advance watermarks and recorded binding, requested
  window, result kind, and actual data-through provenance in the sync ledger.
- Corrected forms/mail local-day grouping, made Search Console's Pacific date basis explicit without
  changing historical fact identities, marked adaptive Cloudflare facts provisional, and upgraded
  probes from token checks to configured-resource reads where supported.
- Added a non-destructive forms identity cutover that preserves legacy facts as lineage, aggregates
  same-day D1 rows before upsert, and exposes only corrected source-backed facts to reports.
- Removed invented and template/resource Site Graph pages, retained unresolved targets as evidence,
  and made graph compilation publish all derived state atomically.
- Restored the test suite to CI, added runtime commit/tree/schema identity, and prevented charts from
  drawing continuous lines or area fills across missing calendar dates.
- Confined scheduled-backup retention to a dedicated directory, validated blank query and configured
  metric inputs strictly, and restored browser capture for provenance-bearing health responses and
  seeded Site Graph pages.

### Added

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
