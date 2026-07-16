# Roadmap

## Delivered in v0.1.0

- Public/private boundary, threat model, strict configuration, release verification, and CI.
- SQLite migrations, catalog, idempotent ingestion, ledgers, capabilities, locks, retention,
  integrity, backup, and restore.
- Fixture-tested adapters for Umami, Cloudflare traffic, GA4, Search Console, forms D1, and forms
  inbox delivery evidence.
- Custom absolute windows, comparisons, saved reports, filtered subreports, JSON, CSV, forms
  reconciliation, and a hardened loopback-first web interface.

## Production validation completed for v0.1.0

- A private Pi installation runs the web dashboard and bounded syncs from loopback-only systemd
  services with configuration and credentials outside the public repository.
- Release validation covers configuration, database integrity, backup, bounded provider sync,
  service restart, health checks, and browser rendering through an SSH tunnel.
- Public screenshots remain fixture-only. Private names, resource IDs, credentials, form content,
  mailbox content, and live metric values are not release artifacts.

Provider availability and historical depth remain account-specific. Operators must validate scopes,
quotas, date boundaries, sampling or finality, and metric meaning for every new account.

## Operational maturity

- Capability and stale-data status page, alerts, scheduled rollups, performance benchmarks, and a
  measured SQLite capacity envelope.
- More provider dimensions and cursor pagination where live accounts prove they are needed.
- Print/PDF report rendering only after HTML/CSV report contracts stabilize.
- PostgreSQL migration rehearsal only after evidence of write contention or data-volume pressure.

## Optional client access

- Authenticated HTTPS origin, tenant roles, independent authorization tests, audit events, rate
  limiting, export controls, and incident-response runbook.
- Client report bundles and scheduled delivery workflow with separate human approval.

Client access is not a V1 deployment mode and requires an explicit security review.
