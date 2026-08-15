# Threat model

## Assets and boundaries

Assets are provider credentials, client/site mappings, normalized analytics, form-delivery state,
saved reports, route observations, repository-derived graph evidence, private BigQuery reader
configuration, Search Console bulk manifests and Parquet rows, exports, and authorization
decisions. Trust boundaries are browser-to-web, web-to-SQLite, sync-to-credential-provider,
sync-to-provider, analytics-to-read-only-mail-index, BigQuery-to-bulk-reader-to-external-lake, and
public-package-to-private-deployment.

## V1 threats and controls

### Credential exposure

- TOML contains opaque references only; secret-like inline keys fail validation.
- Environment and systemd values are leased in non-printable objects and best-effort zeroed on close.
- Provider calls occur only in explicit sync/probe commands, never in browser code.
- Sync ledgers store exception categories and type names, not URLs, response bodies, or credentials.
- The release verifier rejects common token/key patterns, private paths, unexpected files, and
  generated artifacts. It also recomputes the public filesystem's Git tree and requires it to match
  the clean reviewed `HEAD`; an exported tree has no implicit authority and must be checked against
  an independently supplied exact tree ID.

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

### Search Console bulk-export exposure and cost

- The bulk reader uses a dedicated service account with project job-user and dataset data-viewer
  access only; it does not reuse Search Console or GA4 credentials.
- A strict private manifest rejects inline credentials, unknown fields, duplicate dataset mappings,
  unbounded windows, and queries without a per-job bytes ceiling.
- Every property is proven against an operator-selected nonempty date in both exported data tables,
  preventing a wrong site-to-dataset mapping from masquerading as a legitimate zero.
- Query and URL dimensions remain only in the private lake. CLI output and logs contain bounded
  identities, counts, dates, revisions, and byte totals, never row values or provider responses.
- Publication requires paired successful `ExportLog` evidence, a stable revision during the read,
  matching BigQuery controls, Parquet footer totals, a checksum, and an atomic success marker.
- The lake verifies the exact filesystem UUID through `/dev/disk/by-uuid`, a private on-disk marker,
  ownership, modes, devices, and symlink-free paths. Any ambiguity, missing mount, low-space floor,
  unexpected file, corruption, or identity mismatch fails closed without falling back to the Pi
  root filesystem.
- Immutable revisions and full successful `ExportLog` history preserve provider corrections.
  Staging and quarantine usage, missing namespace pairs, continuity, freshness, free space, query
  bytes, and provider export health require monitoring.

Service-account theft can expose sensitive aggregate query and URL evidence and consume billable
query capacity. Disk theft or an over-broad backup can expose the same data. Production acceptance
therefore requires encrypted credential delivery, restrictive IAM, private modes, physical and
at-rest disk controls, a defined backup/retention/deletion policy, and tested revocation. The bulk
lake is intentionally absent from the browser plane and from SQLite backups.

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

## Analytics Operations threats and controls

### Malicious or unbounded definitions

- Goal, segment, alert, and subscription documents use strict schemas with unknown-field rejection.
- Segment dimensions and operators are allowlisted; logical depth, node count, list size, string
  length, and safe-pattern complexity are bounded.
- Logical dimensions compile to catalog-approved provider fields. Unsupported predicates fail
  before querying and are never silently omitted.
- Canonical JSON and content hashes make version reuse deterministic. Version and activation rows
  are immutable. Retirement is a separate, immutable, hash-bound terminal event, so one transaction
  appends retirement history before adding a successor activation.
- Deterministic length-delimited record hashes cover every immutable persisted field. SQL checks,
  composite foreign keys, current-state insertion guards, and update/delete triggers
  reinforce application validation. Integrity and restore pin the exact migration 005 tables,
  indexes, and triggers. Restore validates and copies one protected read snapshot, post-validates
  the copied destination, and only then atomically replaces the target. Retained-version activation
  and reuse, recursive reference resolution, and current-definition reads revalidate semantics and
  recompute every applicable immutable definition hash. Current activation use validates its
  referenced version and recomputes the immutable activation-record hash before the row can be
  returned, reused, replaced, reactivated, or retired. Retirement-event natural identities and full
  record hashes and non-overlapping activation chronology are revalidated on those paths and on
  restore.
- Package validation finishes before the write transaction. A collision, missing version,
  unknown retirement, invalid definition, or interruption rolls the entire package back.
- Stored JSON is bounded validated data, never arbitrary SQL, provider payloads, or executable
  templates.

### Recipient and delivery abuse

- Recipient addresses and SMTP credentials remain in private configuration.
- Header values, recipient sets, attachment names, attachment sizes, retry counts, and email sizes
  are validated and bounded.
- A future delivery store may retain only a keyed, non-reversible recipient-set digest or bounded
  count when operationally necessary. The digest key and recipient addresses remain outside SQLite
  and the public repository.
- Report-subscription validation does not accept a caller-supplied digest string or proof object.
  Private recipients and the digest key are call-scoped keyword inputs and are never fields on the
  public definition model. Construction privacy-screens and deep-freezes detached public mappings,
  so dataclass conversion, copying, pickling, slots inspection, repr, and later caller mutation
  cannot serialize or insert recipient material. Omitted metadata alone defaults to an empty
  mapping; falsey values of any other type fail closed. Validation accepts only a bounded ordinary
  list or tuple, rejects
  non-ASCII/confusable addresses, malformed dot-atoms, empty or boundary-hyphen domain labels, and
  hostile sequence subclasses, derives the HMAC-SHA-256 identifier itself, and writes only that
  identifier to canonical JSON. Python closures, object identity, and introspection are not treated
  as authorization boundaries; there is no issuer, seal, or public pre-derived-digest path to
  recover or forge.
- A deterministic idempotency key prevents duplicate successful sends across retries and restarts.
- Preview and filesystem sinks are the default acceptance paths; external delivery requires a
  separately configured scheduler and allowlisted recipients.
- Error records use sanitized categories and never include recipient addresses, credentials, raw
  messages, or provider responses.

### Misleading incidents and causal claims

- Rules disclose metric or goal version, comparison, maturity lag, baseline, coverage, and result.
- Incomplete periods suppress evaluation unless a rule explicitly permits incomplete evidence.
- Continuing evidence updates one stable incident; cooldown prevents repeated delivery.
- Cross-provider comparisons are labeled divergence or tracking discrepancy and retain both
  provider semantics.
- An annotation can establish coincidence, not causation. Explanations remain hypotheses unless a
  verified source record directly supports them.

### Annotation and cross-site leakage

- Annotation text and imported metadata are bounded and screened for secret-shaped and
  private-source content.
- Deployment import requires verified deployment evidence; a Git push is insufficient.
- Every goal, annotation, rule, evaluation, incident, subscription, and query retains explicit site
  scope.
- Browser responses omit private recipient data, paths, raw exceptions, and unbounded evidence.

### Browser mutation attempts

- Analytics Operations routes are read-only and inherit Host validation, loopback defaults,
  authentication, CSP, restrictive CORS, `no-store`, response bounds, and sanitized errors.
- There are no HTTP handlers for definition activation, annotation changes, rule evaluation,
  incident acknowledgement or suppression, subscription execution, recipient edits, or delivery.
- State-changing operations remain validated CLI or scheduler actions under the existing writer
  lease and transaction boundary.

## Future Internet deployment gate

Before client access, add tenant-scoped authorization at the report engine, identity-aware HTTPS,
audit events, rate limiting, export policy, session security, data-isolation tests, backup recovery,
and incident response. Public source inspection improves credibility; it is not a security guarantee.
