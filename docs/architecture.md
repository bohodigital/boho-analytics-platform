# Architecture

## Decision summary

Boho Analytics Platform is one modular Python package with two deliberately independent data lanes.
The normalized reporting lane uses SQLite; the private Search Console bulk lane uses Parquet. The
modules have explicit contracts so providers, storage, and the web surface can evolve independently
without paying the operational cost of microservices.

The browser reads the local analytics store. It never queries providers directly.

```text
provider APIs -> explicit sync -> connectors -> catalog validation -> SQLite -> reports -> web/API
                       ^                ^               ^
                 credentials       sync ledger       absolute windows

Search Console -> BigQuery datasets -> gsc-bulk -> private external-disk Parquet lake
```

## Public core and private deployment

The public repository owns:

- Configuration and domain schemas.
- Provider connector contracts and generic adapters.
- Metric catalog and normalization rules.
- Storage migrations and repository interfaces.
- Report definitions, query planning, rendering, and exports.
- Web application, authentication hooks, and security middleware.
- Generic service and container examples.

A private deployment owns:

- Client and property mappings.
- Provider resource identifiers when they reveal private account structure.
- Credential references and the credential-provider choice.
- The private bulk-export YAML, property/dataset mappings, first-export and identity-proof dates,
  dedicated reader identity, external storage root, schedules, retention, and backup policy.
- Local ports, hostnames, schedules, retention, backup destinations, and access policies.
- Private saved reports and generated report artifacts.

The public package must not import a private deployment module. Private deployments configure or
extend the public package through documented interfaces.

## Modules

### Configuration

Loads a versioned, non-secret TOML document and fails closed on unknown fields, invalid references,
duplicate identifiers, unsupported schema versions, inline secret keys, and invalid timezones.

### Credential providers

Resolve opaque references such as `env:NAME`, `systemd:NAME`, or plugin-defined schemes. Provider
credentials are leased only to the connector performing a sync and must never enter report rows,
configuration dumps, browser responses, or exception text.

### Connectors

Each connector implements two core operations:

- `probe`: discover authentication state, accessible resources, available metric groups, retention,
  quota hints, and warnings.
- `collect`: retrieve a bounded window and emit normalized metric points.

Missing scopes and unavailable plan features are persisted capability states.

### Metric catalog

Every metric has a stable identifier, source, unit, aggregation rule, allowed grains, completeness
semantics, and plain-language definition. A metric is not exposed until those properties exist.
Cross-provider metrics remain separate unless an explicitly documented derived metric combines them.

### Ingestion

Scheduled syncs use idempotent upserts, bounded rolling refresh windows, a lease-based writer lock,
bounded retry/backoff, watermarks, and a durable sync ledger. Provider endpoints whose selected V1
grain can exceed one response must add explicit pagination before that scope is enabled. A browser
request never triggers a provider sync.

### Storage

SQLite in WAL mode is the first storage adapter. It uses a single writer, indexed time-series facts,
and migration-controlled schema changes. PostgreSQL becomes appropriate when measured write
contention, report concurrency, or dataset size exceeds the SQLite operating envelope. Provider
connectors and reporting code do not depend on SQLite-specific SQL.

Search Console bulk export is not a connector or SQLite storage adapter. `gsc-bulk` reads the two
property-specific data tables plus their successful `ExportLog` histories and writes revision-aware
immutable Parquet under a verified external filesystem. Unscreened query and URL dimensions remain
in that private lake; the report engine and web process have no reader for it. See
[`gsc-bigquery-bulk-export.md`](gsc-bigquery-bulk-export.md) and
[ADR 0006](adr/0006-search-console-private-bulk-lake.md).

### Reporting

The report engine accepts an explicit scope, window, timezone, grain, comparison, filters, metrics,
and sections. Saved reports and sub-reports compile to the same request model; they are not separate
query systems. See [`reporting-model.md`](reporting-model.md).

### Analytics operations foundation

Schema 5 is reserved for a generic immutable-definition registry. It does not itself add goals,
segments, annotations, alerts, delivery, or browser features. Definition versions and activation
history are separate so reactivation never rewrites history. Future consumers must select evidence
through the existing active-fact/reporting layer; retained historical identities are not all active.

Goal providers remain separate observations. Future segment compilers must use bounded
provider-specific mappings and fail when a predicate cannot be honored. Future writer processes
remain CLI or scheduler operations under the global writer lease.

See [`analytics-operations-contracts.md`](analytics-operations-contracts.md) and
[`analytics-operations-compatibility.md`](analytics-operations-compatibility.md).

### Web and API

The UI is server-rendered HTML with same-origin CSS and a small same-origin canvas renderer. KPI cards,
interactive plots, accessible daily-value fallbacks, freshness, comparison tables, and report tools
are all rendered from the same provider-neutral result used by the versioned read-only API. The page
remains usable when JavaScript is disabled. Production API documentation is disabled.

The web layer performs no state-changing operations. `/api/v1/report` and `/api/v1/report.csv` expose
aggregate reports; `/api/v1/series` and `/api/v1/series.csv` expose a selected stored daily series and
optional preceding-period comparison. Provider sync remains a CLI/timer operation.

Any future Analytics Operations HTML, JSON, or CSV route inherits this boundary. HTTP may inspect
sanitized state but cannot activate definitions, add annotations, acknowledge or suppress alerts,
trigger sync, send reports, modify recipients, or mutate the database.

### Forms monitoring

Cloudflare D1 submission state is authoritative for accepted forms. The mailbox adapter is separate
delivery evidence. Both connectors aggregate at source and are structurally prevented from placing
submission or message content in the metric store. See [`forms-monitoring.md`](forms-monitoring.md).

## Deployment modes

- **Local development:** fixture or explicitly configured provider access, local SQLite, loopback web.
- **Private server:** scheduled sync and loopback web, normally reached through SSH forwarding.
- **Authenticated web:** HTTPS reverse proxy or private tunnel, upstream identity plus application
  authorization, tenant-scoped queries, and audit logging.

The deployment mode changes adapters and policy, not domain or report logic.

## Compatibility policy

- Configuration and database schemas are versioned independently.
- Public Python contracts may change during `0.x`, but deprecations should span at least one minor
  release once third-party connectors exist.
- Stored provider payloads are not treated as a stable internal API.
- Connector fixtures record provider version and capture date.
- Database migrations are forward-only and backups are required before destructive migrations.
