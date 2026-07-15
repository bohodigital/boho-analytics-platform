# Roadmap

Roadmap phases are exit-gated. A later phase does not start merely because files for it exist.

## Phase 0: Public foundation

- Public/private boundary and threat model.
- Configuration and domain contracts.
- Release-tree verification and CI.
- Public repository and canonical server clone.

Exit: clean public history, tests pass, anonymous clone verified, and no private deployment data.

## Phase 1: Storage and ingestion kernel

- Versioned SQLite migrations and repository implementation.
- Sync ledger, locks, watermarks, retry categories, and retention.
- Metric catalog and capability persistence.
- Sanitized fixture harness for connector tests.

Exit: idempotent restart-safe ingestion kernel with backup/restore tests.

## Phase 2: Provider vertical slices

- Umami read-only connector and view-only access guidance.
- Google Analytics and Search Console connectors.
- Cloudflare GraphQL analytics and operational-resource connector.
- Capability discovery, quotas, backfills, and provider-semantic documentation.

Exit: the same bounded report can be reproduced from fixtures and validated against live provider
interfaces without exposing credentials.

## Phase 3: Reporting and internal web application

- Metric queries, comparisons, saved reports, and sub-reports.
- Portfolio and per-site views.
- Source, freshness, completeness, and capability indicators.
- CSV, JSON, and print-oriented outputs.
- Loopback authentication and web hardening.

Exit: private server deployment survives restart, provider outages, expired access, and restore.

## Phase 4: Operations and scale proof

- Scheduled rollups, report cache, retention enforcement, and performance benchmarks.
- Custom-window and large-history load tests.
- Alerting hooks and operational status reports.
- Measured SQLite-to-PostgreSQL decision gate.

Exit: documented capacity envelope and migration rehearsal.

## Phase 5: Optional client access

- Authenticated HTTPS deployment.
- Tenant-aware roles, audit events, and export controls.
- Client report bundles and delivery workflow.
- Security review and public-deployment runbook.

Exit: independent authorization and data-isolation tests plus explicit deployment approval.
