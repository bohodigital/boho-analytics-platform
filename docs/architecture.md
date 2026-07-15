# Architecture

## Decision summary

Boho Analytics Platform starts as a modular monolith: one Python package, one sync command, one
web process, and one database. The modules have explicit contracts so providers, storage, and the
web surface can evolve independently without paying the operational cost of microservices.

The browser reads the local analytics store. It never queries providers directly.

```text
provider APIs -> connectors -> normalization -> metric store -> report engine -> web/API
                       ^                ^               ^
                 credentials       sync ledger      report cache
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

Scheduled syncs use idempotent upserts, bounded rolling refresh windows, per-connector locks,
pagination, exponential backoff with jitter, and a durable sync ledger. A browser request never
triggers an unbounded provider sync.

### Storage

SQLite in WAL mode is the first storage adapter. It uses a single writer, indexed time-series facts,
and migration-controlled schema changes. PostgreSQL becomes appropriate when measured write
contention, report concurrency, or dataset size exceeds the SQLite operating envelope. Provider
connectors and reporting code do not depend on SQLite-specific SQL.

### Reporting

The report engine accepts an explicit scope, window, timezone, grain, comparison, filters, metrics,
and sections. Saved reports and sub-reports compile to the same request model; they are not separate
query systems. See [`reporting-model.md`](reporting-model.md).

### Web and API

The first UI is server-rendered HTML with small progressive enhancements and locally vendored chart
assets. A versioned read-only API serves the same report engine. Production API documentation is
disabled unless explicitly enabled.

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
