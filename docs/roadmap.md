# Roadmap

## Delivered in the connection-ready V1 beta

- Public/private boundary, threat model, strict configuration, release verification, and CI.
- SQLite migrations, catalog, idempotent ingestion, ledgers, capabilities, locks, retention,
  integrity, backup, and restore.
- Fixture-tested adapters for Umami, Cloudflare traffic, GA4, Search Console, forms D1, and forms
  inbox delivery evidence.
- Custom absolute windows, comparisons, saved reports, filtered subreports, JSON, CSV, forms
  reconciliation, and a hardened loopback-first web interface.

## V1 live-validation gate

- Connect one real least-privilege credential at a time.
- Confirm provider endpoint/version, resource discovery, account scopes, quotas, date boundaries,
  sampling/finality, and metric meaning.
- Compare a known provider window with platform output.
- Validate forms D1 counts against independent inbox evidence without inspecting content.
- Install as private-server service/timer, exercise backup/restore, and record a capacity baseline.

Exit: bounded live reports are reproducible, service restart is proven, and no secret/PII enters the
store, logs, exports, Git, or browser.

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
