# Changelog

All notable changes will be documented here. The project follows Semantic Versioning after
the first stable release.

## Unreleased

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
