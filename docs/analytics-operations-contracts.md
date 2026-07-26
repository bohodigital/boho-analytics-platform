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
versions and activations are never deleted merely because a later version activates. An activation
row permits exactly one mutation: its `retired_at` may move once from null to the transaction's UTC
timestamp. Every other activation field is immutable.

Activation has these exact behaviors:

1. Identical content already active returns the existing version and current activation without a
   write.
2. Identical content previously retired reuses the immutable version row and creates a new
   activation record.
3. Changed content creates the next version and a new activation record, retiring the prior
   activation in the same transaction. One authoritative UTC transaction timestamp retires the old
   activation and starts the new one. Active ranges are half-open `[activated_at, retired_at)`, so
   the ranges meet at that timestamp without overlapping.
4. Explicit retirement of an active scoped key sets only its current activation's `retired_at`;
   the immutable version remains reusable.
5. Retirement of an unknown scoped key or a scoped key with no current activation fails without a
   write.
6. A matching digest with unequal canonical bytes is a collision and fails without a write.
7. Activation by a missing version identity, or activation from missing, invalid, unknown, private,
   or secret-shaped definition content, fails without a write.
8. Interruption before commit leaves the previous activation, retirement state, and all version
   history unchanged. Interruption tests are required for activation, replacement, and retirement.

Rollback of a definition is a new activation of a retained version, not an in-place edit or a
timestamp reversal.

## Trusted evidence selector

All goal, segment, alert, annotation-linked, and scheduled-report evaluation must consume the
existing trusted active-fact/reporting selection layer. Feature implementations may not query raw
`metric_facts` as though every retained identity version were active evidence.

This is a blocking implementation contract: no consumer may be accepted until tests prove that
historical form identities and other superseded identity versions cannot be selected or
double-counted, and that missing active evidence remains missing rather than falling back to a
retained identity. The selector must preserve source, metric, unit, scope, window, date basis,
coverage, completeness, and maturity metadata.

## Goal contract

Supported goal types are `page`, `event`, `form`, `download`, `outbound_action`, `revenue`, and
`composite`. A goal version defines its site scope, canonical source, canonical metric, unit,
aggregation, date basis, active date bounds, maturity lag, denominator, coverage requirement,
confidence, and optional provider bindings.

Exactly one binding is canonical. Zero or more bindings may corroborate it. Outputs show the
canonical observation and each corroborating observation separately; they never blend or sum
providers into one hidden count. A provider may not substitute for an unavailable canonical
source. Missing corroboration is `unknown`, not zero. An analytics event is never described as a
confirmed form delivery.

Every denominator identifies its metric, unit, scope, grain, window, date basis, completeness
policy, and zero-denominator behavior. Percentages are `unknown` when the canonical denominator is
missing or zero. The evaluation period must end at or before the source's disclosed mature-through
date; otherwise the result is `incomplete`. Coverage is disclosed for both numerator and
denominator and must satisfy the goal version's threshold.

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
set; its secret key remains outside SQLite and the public repository so an address dictionary
cannot reproduce it. SQLite may store only that identifier or a bounded recipient count when
operationally necessary. It stores no recipient list or message body. Public reports, browser
responses, and exports contain neither addresses nor private delivery identifiers.

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
