# Analytics Operations contracts

This document freezes the reusable contracts required before schema-5 implementation. It defines
private configuration semantics, immutable activation, evidence selection, result language, and
the read/write boundary. It does not add runtime behavior.

## Definition package

Analytics Operations definitions originate in a private document with
`definitions_schema_version = 1`. That version is independent of both the application TOML schema
and the SQLite schema. Supported definition types are `goal`, `segment`, `alert_rule`, and
`report_subscription`. An annotation is an immutable ledger entry, not a reusable definition.

Every definition has:

- a stable, bounded `definition_key`;
- one supported `definition_type`;
- a bounded `scope_key`;
- a strictly validated, type-specific content object; and
- optional sanitized metadata.

Unknown fields, duplicate scoped keys, invalid references, unsupported operators, secret-shaped
values, email addresses, full external URLs, raw queries, private paths, message content, form
payloads, visitor or session identifiers, invalid timezones, and unsafe patterns fail validation.
Validation is side-effect free. Raw TOML, comments, unknown fields, recipient lists, credentials,
and private source configuration are never stored.

The public repository may contain placeholders only. Real site and provider identifiers,
recipients, credentials, operational paths, site-specific configuration, and private descriptions
remain outside it.

## Canonical serialization and immutable versions

After strict validation, the implementation constructs a new object containing only recognized
public fields. It serializes that object as UTF-8 JSON with lexicographically sorted object keys,
no insignificant whitespace, JSON booleans and null, and no non-finite numbers. Arrays preserve
their declared order. The SHA-256 digest of those exact bytes is the normalized-content hash.

A version record contains:

- definition type, stable key, and scope;
- a positive, monotonically increasing version for that scoped key;
- canonical JSON and its SHA-256 content hash;
- bounded sanitized metadata;
- creation time; and
- a deterministic natural identity derived from type, scope, key, version, and content hash.

Version rows are immutable. Activation history is stored separately and records activation and
retirement times. Exactly one activation may be current for a `(scope, type, key)`. Historical
versions, activations, and retirements are never deleted merely because a later version activates.
Activation rows are immutable. Retirement is a separate immutable event with a natural identity and
full-record hash bound to the referenced activation and the transaction's UTC timestamp.

Activation has these exact behaviors:

1. Identical content already active returns the existing version and current activation without a
   write.
2. Identical content previously retired reuses the immutable version row and creates a new
   activation record.
3. Changed content creates the next version and a new activation record, retiring the prior
   activation in the same transaction. One authoritative UTC transaction timestamp retires the old
   activation and starts the new one. Active ranges are half-open `[activated_at, retired_at)`, so
   the ranges meet at that timestamp without overlapping.
4. Explicit retirement of an active scoped key appends one retirement event for its current
   activation; the immutable activation and version remain reusable history.
5. Retirement of an unknown scoped key or a scoped key with no current activation fails without a
   write.
6. A matching digest with unequal canonical bytes is a collision and fails without a write.
7. Activation by a missing version identity, or activation from missing, invalid, unknown, private,
   or secret-shaped definition content, fails without a write.
8. Interruption before commit leaves the previous activation, retirement state, and all version
   history unchanged. Interruption tests are required for activation, replacement, and retirement.

Package application validates every definition and cross-reference before opening its write
transaction. Omission is not retirement: an active definition absent from a later package remains
active until an explicit, valid retirement operation names it. After validation, all new version
rows, explicit retirements, reactivations, and replacement activations for one package commit in one
transaction. Identical active definitions remain no-ops within that transaction. A collision,
missing reference, invalid definition, or interruption aborts the entire package, exposes no new
version row or partial activation set, and leaves the previously active package state unchanged.
Implementation tests must interrupt multi-definition packages after version insertion, retirement,
and activation steps and prove all-or-nothing visibility.

Rollback of a definition is a new activation of a retained version, not an in-place edit or a
timestamp reversal.

## Trusted evidence selector

All goal, segment, alert, annotation-linked, and scheduled-report evaluation must consume the
existing trusted active-fact/reporting selection layer. Feature implementations may not query raw
`metric_facts` as though every retained identity version were active evidence.

This is a blocking implementation contract: no consumer may be accepted until tests prove all of
the following:

- forms identity versions 1 and 2 remain excluded;
- the active forms identity version 3 remains included;
- retained lineage cannot be selected, double-counted, or inflate an outcome;
- successful-empty acquisition coverage cannot fabricate a goal observation; and
- unrelated provider facts and their selection behavior remain unchanged.

Missing active evidence remains missing rather than falling back to a retained identity. The
selector must preserve source, metric, unit, scope, window, date basis, coverage, completeness, and
maturity metadata.

## Provider pageview comparison contract

The pageview comparison is diagnostic evidence, not a canonical source selector. It retains two
independent measures:

- GA4 `google.pageviews`, with route control `google.page-path-views` sourced from
  `screenPageViews` grouped by normalized `pagePath`; and
- Umami `umami.pageviews`, with route control `umami.route-pageviews` sourced from
  `metrics/expanded?type=path&field=pageviews`.

Umami visits are never used as pageviews. A fixture, stale binding, alternate provider, or one
provider's value cannot substitute for the other. The service never combines, averages, rescales,
or silently prefers the two values.

A provider date is complete only when one current-binding sync-ledger run contains the exact whole
configured-site-local day; adjacent partial runs cannot be combined. One sync invocation may cover
multiple site timezones: the engine projects the requested calendar dates independently onto each
binding's configured local-midnight interval before acquisition and ledger recording. Reporting uses
that same per-site projection for bounded fact and ledger queries, coverage, series, and comparison.
The cell must already be
closed, and the provider must have returned its explicit pageview series, including an explicit
empty series for a quiet window. For GA4 and Umami, only fresh post-upgrade runs carrying the
existing-ledger `data:explicit-pageviews-v1` or `empty:explicit-pageviews-v1` result marker can prove
that contract. Legacy `data` and `empty` rows remain stored but cannot authorize coverage, fact
attribution, or quiet zeroes. Upgrades therefore require a fresh successful sync; there is no schema
bump, migration, or historical rewrite. Operational status continues to project the public
`data`/`empty` vocabulary. A retained final headline fact contributes a value only when its
observation timestamp falls inside exactly one successful data run, that run belongs to the current
binding, it contains the exact fact interval, and it is the latest successful snapshot for that
cell. Concurrent or otherwise ambiguous runs fail closed. Routes absent from a later complete
snapshot therefore cannot be mixed back in from an older retained fact. A successful empty
current-binding run may prove a quiet zero day, but it never makes a
retained fact from a replaced resource eligible. Historical fact bytes remain unchanged.
Observation boundaries and provider data-through limits still apply. A mature paired date is the
intersection of the two providers' complete dates inside the exact requested half-open report
window. Provider-only dates remain visible but do not enter paired totals.

Each site comparison exposes:

- each provider's earliest retained final headline date, data-through date, and compact complete-date
  ranges;
- paired, GA4-only, and Umami-only date ranges and counts, with first and last paired dates;
- separate provider totals over paired dates only, absolute difference, and GA4-to-Umami ratio;
- a low-volume warning when the two paired totals combine to fewer than 100 pageviews, evidence
  state, provider semantics, and explicit coverage limits; and
- route-to-headline reconciliation state and a bounded reason when reconciliation is withheld.

When there is no paired date, the evidence state is `non_comparable` and every paired numeric
total, difference, and ratio is null. Zero is never used to represent unavailable comparison
evidence. If Umami's paired total is genuinely zero, the provider totals and absolute difference
remain observations but the ratio is null.

The evidence states are:

- `aligned`: every paired daily value is equal;
- `within_expected_variation`: every non-aligned paired daily ratio remains within the disclosed band;
- `low_volume`: the paired totals combine to fewer than 100 pageviews;
- `isolated_divergence`: divergence is limited to a minority or the only paired date;
- `persistent_divergence`: divergence spans a majority of multiple paired dates;
- `unknown`: exactly half of multiple paired dates diverge, or another declared interpretation rule
  cannot classify the arithmetic safely;
- `coverage_mismatch`: at least one paired date exists but complete source-only dates also exist;
- `non_comparable`: no complete paired date exists.

These states describe evidence only. They never declare either provider correct and never prove a
cause for unusual traffic. Stored facts remain unchanged; suspected automated or synthetic traffic
is not silently removed.

Route reconciliation uses the same provider's headline and route pageview facts over exactly the
provider's complete dates. It is `reconciled` only when every date has final normalized route
facts, each fact starts and ends at that configured site's exact local calendar-day boundaries
inside the requested half-open window, independent of the report timezone, and every daily route
sum equals that date's headline pageviews.
Incomplete pagination,
rejected unsafe dimensions, absent route facts, disabled route acquisition, or a sum mismatch
withholds reconciliation with a reason. Safe rows returned before an unproven pagination boundary
remain facts with `UNKNOWN` completeness and cannot satisfy reconciliation.

GA4 and Umami pageview values must be non-negative integral counts. Boolean, negative, fractional,
malformed, NaN, or infinite values are rejected. Parsing bounds raw text at 128 characters,
significant digits at 38, absolute adjusted exponent at 37, and direct integer input at 127 bits;
ordinary scientific notation such as `1e3` remains valid. Rejected route rows make retained safe
rows `UNKNOWN`; invalid headline pageviews fail the acquisition run. Reporting independently
revalidates retained headline and route facts against the same finite, non-negative, integral,
bounded domain. Daily normalized collisions are summed with exact integers and revalidated before
persistence; an out-of-domain sum is omitted and makes the acquisition non-final. Reporting uses
exact integer accumulation for daily cells and paired arithmetic, and separately caps downstream
multi-date totals at 64 digits. An invalid retained fact or daily sum makes its cell incomplete and
cannot be converted to zero. The 0.8 and 1.25 evidence thresholds use exact integer cross-products,
not rounded Decimal division.

Rows that normalize to the same route are summed before persistence so normalization cannot discard
pageviews through fact-identity collisions. Reserved identity labels reject opaque UUID, long-hex,
JWT/dotted, padded or unpadded base64, and encoded-separator tokens. A conservative lexical
vocabulary retains clear lowercase content slugs such as `appointment-booking` and
`article-alpha` without admitting arbitrary base64url-shaped hyphenated strings.
High-cardinality route facts are queried only for the exact current reconciliation window. Series
and plot requests neither query nor materialize provider route metrics and do not execute comparison
construction; they still enforce the post-cutover acquisition marker for requested native headline
facts. Dedicated report and comparison paths retain the bounded current-window route query and
headline-only history query.

HTML, JSON, and report CSV are projections of this one model. Every surface preserves compact
discontinuous ranges; a first and last date never imply that intervening dates are present. CSV
comparison rows use
`record_type=provider_comparison`; metric rows remain `record_type=metric`.

## Goal contract

Supported goal types are `page`, `event`, `form`, `download`, `outbound_action`, `revenue`, and
`composite`. A goal version defines its site scope, canonical source, canonical metric, unit,
aggregation, date basis, active date bounds, maturity lag, denominator, coverage requirement,
confidence, and optional provider bindings.

Active date bounds, when present, are canonical `YYYY-MM-DD` calendar dates. An end bound cannot
precede its start bound.

Exactly one binding is canonical. Zero or more bindings may corroborate it. Outputs show the
canonical observation and each corroborating observation separately; they never blend or sum
providers into one hidden count. A provider may not substitute for an unavailable canonical
source. Missing corroboration is `unknown`, not zero. An analytics event is never described as a
confirmed form delivery.

Every denominator identifies its metric, unit, scope, grain, window, date basis, completeness
policy, and zero-denominator behavior. Percentages are `unknown` when the canonical denominator is
missing or zero. The evaluation period must end at or before the source's disclosed mature-through
date; otherwise the result is `incomplete`. Coverage is disclosed for both numerator and
denominator and must satisfy the goal version's threshold. `ratio` aggregation requires exactly
one complete denominator object; every other aggregation forbids a denominator.

Canonical-source, metric, unit, denominator, date-basis, maturity, aggregation, filter, or
reconciliation changes require a new definition version. Historical outputs keep the version that
produced them and are not silently rebased.

A goal may activate only when the metric catalog, active-fact selector, provider compatibility,
coverage rules, and a bounded private fixture establish its evidence. A configured event name is
not proven merely because a provider exposes aggregate events or key-event totals.

## Segment contract

A segment is a bounded expression tree using logical nodes `all`, `any`, and `not`. Initial
dimensions are `site`, `provider`, `route`, `landing_route`, `source`, `medium`, `channel`,
`campaign`, `device`, `country`, `region`, `goal`, `event`, `date`, and `completeness`. Initial
operators are `equals`, `not_equals`, `in`, `not_in`, `starts_with`, `ends_with`, `contains`,
`matches_safe_pattern`, `is_present`, and `is_missing`.

Depth, node count, list size, string length, and pattern complexity have fixed limits. Dimensions
are logical names mapped to catalog-approved fields, never arbitrary SQL or stored-field access.
Raw identity joins and cross-provider visitor/session joins are prohibited.

Every scalar or list-valued `route` and `landing_route` predicate is validated as an internal
pathname. Literal values cannot contain a query or fragment, and every member of `in` and `not_in`
is checked independently. Literals and patterns reject backslashes and control characters so no
consumer can reinterpret a stored pathname as a host-like or multi-line value.

Compilation is deterministic for the same definition, provider, catalog version, and requested
metric. Its result is `supported`, `partially_supported`, or `unsupported`, with bounded diagnostic
codes and the segment version. Unknown dimensions, operators, predicates, or provider mappings
fail. Unsupported predicates are never dropped. A report requiring one fails before querying.
`partially_supported` may summarize a multi-metric request only: every required metric still
receives its own compatibility result, and any unsupported required metric fails the entire request
before querying. The compiler never queries or returns a supported subset silently.

Segmented output discloses totals and coverage before and after filtering, compatibility state,
diagnostics, and the exact segment version. HTML, JSON, and CSV compile through the same request.

## Annotation contract

Annotations are immutable, bounded records. Categories are `deployment`, `content_release`,
`tracking_change`, `campaign`, `outage`, `provider_change`, `form_change`, `navigation_change`,
`pricing_change`, and `manual_note`.

An annotation records site scope, category, start and optional end, bounded title and description,
source, optional safe commit or deployment reference, creation time, and a deterministic import
key when imported. Importers require verified deployment or configuration evidence; a Git push
alone is not deployment proof. Duplicate source/import keys are idempotent. Text is rejected when
it contains secret-shaped or private-source material.

## Alert contract

Initial rule types are `sync_failure`, `stale_data`, `missing_binding`, `coverage_drop`,
`absolute_threshold`, `relative_change`, `zero_after_nonzero`, `cross_provider_divergence`, and
`goal_change`.

Each version fixes site and evidence scope, optional goal and segment versions, evaluation grain,
threshold or comparison, maturity lag, minimum baseline, quiet periods, incomplete-data policy,
cooldown, and severity. Evaluation consumes only the trusted active-fact selector.
Quiet-period boundaries use a canonical 24-hour `HH:MM` clock from `00:00` through `23:59`.

Conditional fields are closed by rule type. Fields not listed as required or forbidden remain
optional evidence selectors:

| Rule type | Required conditional fields | Forbidden conditional fields |
| --- | --- | --- |
| `sync_failure` | `source`, `threshold` | `comparison` |
| `stale_data` | `source`, `threshold` | `comparison` |
| `missing_binding` | `source`, `threshold` | `comparison` |
| `coverage_drop` | `threshold` | `comparison` |
| `absolute_threshold` | `threshold` | `comparison` |
| `relative_change` | `comparison` | `threshold` |
| `zero_after_nonzero` | `comparison` | `threshold` |
| `cross_provider_divergence` | `threshold` | `comparison`, `source` |
| `goal_change` | `goal_version_id`, `comparison` | `threshold`, `source` |

Evaluation results are `triggered`, `clear`, `suppressed_incomplete`, `suppressed_quiet`,
`insufficient_baseline`, or `error`. Immutable evidence records retain rule version, period,
values, completeness, coverage, bounded evidence, and related annotation identifiers. Continuing
evidence updates one stable incident rather than creating one incident per run.

Cross-provider results use `divergence` or `tracking_discrepancy` language and never imply semantic
equivalence.

## Subscription and recipient privacy contract

Frequencies are `daily`, `weekly`, `monthly`, and `quarterly`. A subscription fixes report type,
site scope, optional goal and segment versions, timezone, frequency, maturity lag,
incomplete-data policy, formats, and a non-reversible recipient-set identifier.

Recipient addresses and delivery credentials remain in private configuration and are resolved
only at execution. The recipient-set identifier is a keyed digest over a canonicalized recipient
set. Canonicalization accepts only an explicit ASCII dot-atom local part and domain labels that
begin and end with an alphanumeric character; empty atoms or labels, consecutive dots, boundary
hyphens, and overlong parts fail closed. The digest secret key remains outside SQLite and the
public repository so an address dictionary cannot reproduce it. SQLite may store only that
identifier or a bounded recipient count when operationally necessary. It stores no recipient list
or message body. Public reports, browser responses, and exports contain neither addresses nor
private delivery identifiers.

Delivery idempotency covers subscription version, intended period, formats, and recipient-set
identifier. A successful key cannot send twice. Retries are bounded and reuse the key. Materially
incomplete reports do not send unless the definition explicitly permits them.

## Operations result language

The result vocabulary is:

- `observed`: directly stored source evidence;
- `calculated`: deterministic arithmetic over disclosed observations;
- `compared`: current and reference values shown together;
- `inferred`: a bounded hypothesis supported by a rule or annotation;
- `unknown`: required evidence does not exist; and
- `incomplete`: evidence exists but does not cover the required scope or maturity.

All surfaces use one result model. Credentials, addresses, private paths, raw exceptions, raw
provider payloads, and unbounded evidence never enter it.

## Command and browser boundary

State-changing commands require private configuration, validate before writing, transact all
effects, fail closed, and support dry-run where practical.

No browser route may activate or retire definitions, add annotations, acknowledge or suppress
alerts, trigger sync, evaluate rules, send reports, modify recipients, or mutate the database.
Browser routes are read-only and retain Host validation, loopback defaults, optional
authentication, CSP, `no-store`, restrictive CORS, bounded responses, and sanitized errors.

## Database rollback boundary

Production rollback from schema 5 requires a verified pre-migration online schema-4 backup, the
exact v0.2.0 environment at commit `4a3bfa9e8a4346578263ee74dd227e6230ccc7c3`,
all writers and services stopped, backup restoration, integrity and foreign-key verification,
known-report verification, and timers enabled last. Older code must refuse schema 5. A destructive
down migration is not the primary rollback.
