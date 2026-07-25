# Threat model

## Assets and boundaries

Assets are provider credentials, client/site mappings, normalized analytics, form-delivery state,
saved reports, route observations, repository-derived graph evidence, exports, and authorization
decisions. Trust boundaries are browser-to-web, web-to-
SQLite, sync-to-credential-provider, sync-to-provider, analytics-to-read-only-mail-index, and
public-package-to-private-deployment.

## V1 threats and controls

### Credential exposure

- TOML contains opaque references only; secret-like inline keys fail validation.
- Environment and systemd values are leased in non-printable objects and best-effort zeroed on close.
- Provider calls occur only in explicit sync/probe commands, never in browser code.
- Sync ledgers store exception categories and type names, not URLs, response bodies, or credentials.
- The release verifier rejects common token/key patterns, private paths, unexpected files, and
  generated artifacts.

Environment variables may be visible to same-user processes and some administrative tooling.
Prefer systemd credentials on a private server and use separate least-privilege provider tokens.

### Form and mailbox content disclosure

- D1 SQL names only date, form ID, notification status, and aggregate count.
- Mail SQLite is opened `mode=ro` with `query_only=ON`; only dates and aggregate counts are selected.
- Form payloads, message bodies/indexes, addresses, IPs, user agents, and tokens have no destination
  columns in the analytics schema.
- Fixtures recursively reject content-like fields.

Filtering on mailbox sender/subject still causes the local SQLite engine to evaluate those private
columns. Grant database-file access only to the analytics service user and do not place filter values
in the public example or logs.

### Browser attacks and accidental network exposure

- Loopback bind is the default; unauthenticated non-loopback bind fails configuration.
- Host allowlisting mitigates DNS rebinding and untrusted Host routing.
- CSP denies all resources except same-origin CSS, JavaScript, and API fetches; framing, sniffing, referrers, and caching are
  restricted; permissive CORS is absent.
- Web routes are read-only. Provider sync, restore, and configuration mutation have no HTTP route.
- Site Graph and route-observation requests cannot ingest, compile, build, crawl, or contact a
  provider. Their errors are sanitized and their HTML/JSON/CSV responses inherit the same Host,
  authentication, CSP, `no-store`, and no-permissive-CORS controls.
- Basic authentication uses constant-time comparison and credential references.

The built-in server does not provide TLS, tenant roles, brute-force protection, or an external
identity session. Basic auth over plain HTTP is unsafe outside a trusted loopback/tunnel boundary.
Any remote deployment requires an authenticated HTTPS proxy and additional application authorization.

### SSRF, provider abuse, and quotas

- Only administrator-controlled connection options determine provider URLs.
- HTTP accepts JSON only, limits response bytes and time, disables redirects, retries only bounded
  network/429/server failures, and never exposes arbitrary URL parameters from report requests.
- Provider mutations are not implemented. D1 uses a fixed parameterized `SELECT`; mailbox filters
  use fixed SQL clauses and bound parameters.

An administrator can intentionally configure an Umami URL on an internal network. Treat private
configuration write access as privileged and review changes before service restart.

### Corruption and misleading reporting

- WAL, deterministic idempotency keys, lease locks, sync ledgers, watermarks, integrity checks,
  online backup, and guarded restore protect local state.
- Catalog validation rejects unknown metric/source/unit combinations.
- Ratios and positions use explicit non-additive aggregation; providers stay source-labeled.
- Missing data and forms delivery gaps produce visible warnings rather than zero-filled success.
- Core 2.1 resolution coverage is counted from complete persisted evidence, independently of SVG
  caps. Corrected structural metrics are withheld when display layers do not match the compiled
  projection; unsupported trap and bottleneck claims are absent.
- Route-observation rows remain provider-separated and expose metric source, window, route,
  coverage, freshness, date basis, and limitations. Allowlisted dimensions exclude raw queries,
  query clusters, visitor/session identifiers, and full external referrer URLs.

Provider sampling, delayed finalization, account-specific access, D1 retention, and mailbox-sync lag
can still create discrepancies. Live connection testing must validate these assumptions.

## Future Internet deployment gate

Before client access, add tenant-scoped authorization at the report engine, identity-aware HTTPS,
audit events, rate limiting, export policy, session security, data-isolation tests, backup recovery,
and incident response. Public source inspection improves credibility; it is not a security guarantee.
