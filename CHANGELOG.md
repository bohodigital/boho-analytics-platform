# Changelog

All notable changes will be documented here. The project follows Semantic Versioning after
the first stable release.

## Unreleased

### Added

- Added a schema-7 per-property Google index census. Public sitemap trees define the published URL
  denominator, Google URL Inspection supplies the indexed verdict, raw URLs are reduced to durable
  SHA-256 fingerprints at rest, daily quotas are bounded, and the dashboard withholds indexed totals
  and percentages until the current inventory is completely and freshly inspected.

- Added a separate Search Console BigQuery bulk-export reader and private immutable Parquet lake.
  Strict property/dataset identity proofs, paired and complete `ExportLog` revision history,
  bounded query costs, streaming Arrow reads, control-total and checksum verification, atomic
  publication, and hard UUID-mounted external-storage checks keep complete query/URL aggregates out
  of SQLite and browser exports.
- Added provider-correct route pageview acquisition: GA4 uses `pagePath` with
  `screenPageViews`, while Umami parses the pageview and visit fields returned together by one
  paginated `metrics/expanded?type=path` request. Safe rows from an unproven pagination boundary
  remain `UNKNOWN`; route visits are never relabeled as pageviews.
- Expanded Search Console acquisition with explicit search type, data state, and aggregation on
  every request; per-day bounded geography, page, query, page/query, device, country, appearance,
  and query-cluster views; privacy-screened query wording; and a recent `hourly_all` provisional
  feed. `search_types = ["all"]` keeps all six Google search surfaces in separate scopes, while
  `search_console_dimensions = ["all"]` expands the supported breakdowns. High-dimensional Search
  Analytics rows never claim exhaustive coverage.
- Expanded Umami v3 acquisition to validate and retain every privacy-safe aggregate measure for
  explicitly enabled dimension families, require and reconcile the complete stats response, paginate configured
  geography and website discovery, and collect timezone-bound configured event series without
  event properties.
- Added schema-6 immutable acquisition slices and normalized fact observations. Provider scope,
  request dimensions, aggregation, data state, page/row/rejection counts, and exhaustion evidence
  are retained alongside a read-optimized current fact snapshot; raw provider payloads remain out
  of SQLite acquisition storage.
- Added GA4/Umami pageview comparisons over mature complete overlapping dates only, with separate
  provider coverage, source-only dates, paired totals, difference, ratio, low-volume and evidence
  states, exact route-to-headline reconciliation, and explicit withholding reasons. HTML, JSON,
  and CSV use the same non-blended model; no paired period is non-comparable with null totals.

- Added the schema-5 immutable Analytics Operations definition registry: three additive tables,
  closed type-specific validation, canonical content and record hashes, retained activation and
  append-only retirement history, explicit retirement/reactivation, package-wide transactions,
  and restore-time integrity verification. No goal, segment, alert, subscription delivery,
  connector, report, or browser consumer is activated by this storage foundation.
- Hardened the schema-5 candidate against embedded private paths, raw configuration/comment text,
  sensitive field aliases, Unicode address and scheme-relative URL forms, unsafe nested regex
  repetition, caller-supplied recipient digests, same-timestamp reactivation collisions, nullable text
  primary keys, and lexical timestamp-order bypasses. The final privacy boundary also rejects
  quoted-key TOML, delimiter-adjacent comments, combining-mark email forms, base64url JWT endings,
  recipient identifiers not freshly derived from validated private inputs, serializable private
  input state, hostile sequence subclasses, raw stripped non-ASCII recipient forms before
  normalization or case-folding, semantically invalid
  restored definitions, foreign-key-invalid backups, and semantically valid metadata tampering
  before retained-row activation, reuse, reference resolution, or current-definition reads.
  Current activation use also recomputes immutable activation and retirement-event hashes before
  returning, reusing, replacing, reactivating, or retiring the record. Recipient parsing now
  enforces an explicit ASCII dot-atom mailbox and per-label domain grammar. Semantic validation
  also enforces canonical goal date bounds, a real 24-hour clock for alert quiet periods, and every
  scalar or list member of internal-route segment predicates. Public definition mappings are now
  construction-screened and deeply immutable; ratio/denominator and every alert-rule conditional
  field relationship fail closed. Current versions and recursive embedded references are validated
  before every authority mutation. Integrity pins the complete migration-005 schema object set, and
  restore validates and copies one protected snapshot into a post-validated temporary database
  before atomic replacement. Reactivation chronology cannot overlap prior intervals, internal-route
  values reject backslashes and controls, falsey non-mapping metadata is no longer defaulted away,
  and restore refuses schemas outside the running package's supported range.
- Bound release verification to the exact reviewed Git tree. Clean checkouts are verified against
  `HEAD`, exported trees require an independently supplied tree ID, and modified, staged, deleted,
  or additional allowlisted content now fails closed.

### Changed

- Reworked the loopback analytics dashboard around an operational first view: provider-qualified
  KPIs, exact report-cell coverage, a unit-aware primary trend, per-site performance, concise action
  items, and progressively disclosed reconciliation and data-health evidence. The interface no
  longer calls coverage confidence or trust, and it distinguishes observed partial totals, true
  zeroes, new activity, and unavailable comparisons. KPI totals disclose contributing/configured
  site scope; geography copy follows the active provider payload; lower-is-better metrics are
  line-only; and pointer inspection no longer floods assistive-technology live regions.
- Search Console reporting selects and discloses one search surface before aggregation. Umami daily
  unique visitors remain a daily series instead of being presented as a report-window unique total;
  dimensioned geography stays in its dimension-aware consumer.

### Fixed

- Corrected Umami's pageview-response `sessions` semantics to `umami.daily-visitors`, interpreted
  timezone-less midnight series labels in the explicitly requested timezone, retained partial
  first-observed days as `UNKNOWN`, and required all documented exact-window stats fields instead
  of accepting silent empty success. Inclusive provider end timestamps now stop one millisecond
  before each platform-exclusive boundary.
- Made Search Console metrics surface-aware: Discover and Google News no longer fail when Google
  omits position, never persist a synthetic zero position, skip unsupported query wording reads,
  and present average position as unavailable instead of complete or missing on those surfaces.
- Preserved Search Console's Pacific date on every daily fact, used `dataState=all` metadata to
  separate fresh provisional headline rows from settled detail, prevented incomplete empty
  snapshots from deleting current facts, and raised the implicit Search Console pagination plan to
  two 25,000-row pages plus a terminal exhaustion call. Hourly requests remain provisional whenever
  an incomplete marker exists, and pagination reaches the exact 50,000-row offset even when the
  configured page size does not divide that ceiling.

- Corrected GA4 country totals so sessions are summed across all returned regions instead of the
  last region overwriting the country fact.

- Treated Umami's reported date range as an event extent and clamped route acquisition to
  conservative whole-day bounds, preventing quiet trailing hours from failing otherwise valid
  headline and route bindings.

- Closed the provider-comparability acceptance findings: exact-half multi-date divergence is
  `unknown`; GA4 pagination requires exact requested headers, row arity, and bounded values; Umami
  detects overlapping raw page identities without double-counting; and retained invalid pageview
  facts make report cells incomplete instead of zero.
- Added a fail-closed, no-schema-bump pageview acquisition cutover. Fresh GA4 and Umami syncs mark the
  existing ledger result-kind field, while untouched legacy `data`/`empty` rows cannot authorize
  coverage, attribution, or quiet zeroes. Series and plot paths skip route materialization and
  provider-comparison attribution.
- Bounded provider count parsing by raw length, significant digits, exponent, and integer size;
  made normalized collision, daily, paired, and ratio arithmetic exact and revalidated; and refined
  reserved-label privacy filtering with a conservative lexical vocabulary that admits clear content
  slugs while rejecting opaque UUID, hex, JWT/dotted, base64, and encoded-separator tokens.
- Projected requested calendar dates into each binding's configured timezone across sync and
  reporting, so mixed-timezone sites retain exact local-midnight provider/ledger intervals and
  matching bounded fact queries, coverage cells, series labels, and comparisons.
- Failed provider acquisition closed when GA4 omits `rowCount`, when Umami availability does not
  contain the exact requested interval, or when a provider returns an out-of-window date or an
  invalid pageview count. Retained safe route rows remain `UNKNOWN` whenever completeness is not
  proven.
- Rejected opaque identity-bearing route segments and encoded separators without retaining them,
  required explicit pageview series and exact site-local acquisition windows, rejected ambiguous or
  adjacent-partial binding-run evidence, rejected out-of-window headline and Search Console route
  dates, excluded facts that cannot be attributed to the latest current-binding snapshot, prevented
  retained stale routes from entering a later snapshot, and required mature exact site-local daily cells for comparison and route
  reconciliation without rewriting historical facts or changing the schema.
- Preserved discontinuous provider comparison ranges in HTML so its date disclosures remain aligned
  with the JSON, CSV, and CLI comparison model.

### Documentation

- Recorded the provider pageview acquisition, mature-overlap comparison, evidence vocabulary,
  and reconciliation rules in the provider guide, operations contract, and ADR 0005.

- Defined reusable Analytics Operations contracts, a three-table additive schema-5 foundation,
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
