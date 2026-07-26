# Analytics Operations contracts

This document freezes the public contracts required before schema-5 implementation. It defines
private configuration semantics, immutable activation, result language, and the read/write
boundary. It does not add runtime behavior.

## Definition package

Analytics Operations definitions use a separate private document with
`definitions_schema_version = 1`. This version is independent of the application TOML schema and
the SQLite schema.

A definition package may contain:

- `goals`;
- `segments`;
- `alert_rules`; and
- `report_subscriptions`.

Annotations are ledger entries, not reusable definitions, but automatic configuration-change
annotations refer to activated definition versions.

Unknown fields, duplicate stable keys, invalid references, unsupported operators, secret-shaped
values, unsafe patterns, invalid timezones, and unsupported provider bindings fail validation.
Validation is side-effect free. Activation revalidates the exact bytes, canonicalizes them, computes
their hashes, and writes all affected versions in one transaction.

The public repository may contain placeholder examples only. Real site identifiers, provider
resource identifiers, recipients, credentials, operational paths, and private descriptions remain
outside the repository.

The canonical top-level shape is:

```toml
definitions_schema_version = 1

[[goals]]
goal_key = "example-confirmed-outcome"
site_id = "example-site"
display_name = "Confirmed outcome"
description = "Placeholder only."
goal_type = "form"
canonical_source = "cloudflare-forms"
canonical_metric = "forms.submissions"
unit = "count"
active_from = "2026-01-01"
aggregation_behavior = "sum"
confidence = "confirmed"

[[goals.bindings]]
role = "canonical"
source = "cloudflare-forms"
metric = "forms.submissions"
unit = "count"

[[segments]]
segment_key = "example-mobile"
display_name = "Mobile"
description = "Placeholder only."

[segments.expression]
dimension = "device"
operator = "equals"
value = "mobile"

[[alert_rules]]
rule_key = "example-stale-source"
site_id = "example-site"
rule_type = "stale_data"
severity = "warning"
maturity_lag = "P2D"
minimum_baseline_periods = 7
cooldown = "P1D"
incomplete_policy = "suppress"

[[report_subscriptions]]
subscription_key = "example-weekly-briefing"
report_type = "weekly_site_briefing"
site_ids = ["example-site"]
frequency = "weekly"
timezone = "Etc/UTC"
formats = ["html", "text"]
recipient_set_ref = "example-operators"
maturity_lag = "P2D"
incomplete_policy = "do_not_send"
enabled = false
```

Durations use a deliberately bounded ISO-8601 day/time subset. Calendar scheduling uses frequency
and site timezone rather than encoding calendar months as durations. Expression values are typed
scalars or bounded lists; a node may not mix logical children with a leaf predicate.

## Immutable activation

Every definition version records:

- a stable key and definition kind;
- a monotonically increasing version within that kind and key;
- canonical JSON;
- a SHA-256 content hash;
- a SHA-256 source-package hash;
- a bounded structured validation result;
- activation time;
- optional retirement time; and
- creation time.

Activation is idempotent by kind, stable key, and content hash. Re-activating identical content
returns the existing version. Changed content creates a new version and retires the previously
active version in the same transaction. Old evaluations and deliveries retain their original
version references; later activation never silently reinterprets them.

Only one version of a kind/key may be active. Retirement does not delete history. Rollback means
activating a new version whose content intentionally matches a prior version, not mutating the old
row or moving timestamps backward.

## Goal contract

Supported goal types are `page`, `event`, `form`, `download`, `outbound-action`, `revenue`, and
`composite`.

Each goal defines:

- `goal_key`, `site_id`, display name, bounded description, and goal type;
- canonical source, metric, unit, and optional currency;
- active date bounds;
- route or event rules;
- denominator and aggregation behavior;
- confidence label;
- provider bindings; and
- a definition-version reference.

Exactly one binding is canonical. Additional bindings are `corroborating`. A composite goal must
name every component and an explicit reconciliation rule. No rule may sum unlike providers into a
single hidden total.

Goal output reports canonical and corroborating observations separately and may calculate absolute
difference, percentage difference, coverage, and an instrumentation-discrepancy state. A percentage
difference is `unknown` when the canonical denominator is zero or missing.

A goal activates only when its canonical catalog metric exists, source and unit agree with the
catalog, the denominator is valid, historical coverage is disclosed, provider bindings are
compatible, and a fixture query distinguishes zero, missing, unknown, and incomplete.

The initial first-party package must be derived from verified configured events, routes, and forms.
The names in the product program are candidates, not proof that instrumentation exists. A candidate
remains inactive when the accepted fact catalog and a bounded private query cannot establish its
canonical source and provider bindings.

## Segment contract

A segment is a bounded expression tree. Initial logical nodes are `all`, `any`, and `not`; depth,
node count, list size, string length, and pattern complexity must have fixed implementation limits.

Initial logical dimensions are:

`site`, `provider`, `route`, `landing_route`, `source`, `medium`, `channel`, `campaign`, `device`,
`country`, `region`, `goal`, `event`, `date`, and `completeness`.

Initial operators are:

`equals`, `not_equals`, `in`, `not_in`, `starts_with`, `ends_with`, `contains`,
`matches_safe_pattern`, `is_present`, and `is_missing`.

Dimensions are logical names, not arbitrary stored-field access. A provider compiler maps them to
approved catalog dimensions and fact scopes. Every compile result is `supported`,
`partially_supported`, or `unsupported`, with bounded diagnostic codes. Unsupported predicates are
never dropped. A report that requires an unsupported predicate fails before querying.

Segmented output includes totals before and after filtering, coverage before and after filtering,
the exact segment definition version, and compatibility diagnostics. HTML, JSON, and CSV use the
same compiled request.

## Annotation contract

Supported categories are `deployment`, `content-release`, `tracking-change`, `campaign`, `outage`,
`provider-change`, `form-change`, `navigation-change`, `pricing-change`, and `manual-note`.

An annotation records a generated identifier, site, category, start, optional end, bounded title
and description, source, optional safe commit or deployment reference, creation time, definition
hash, and a deterministic import key when imported.

Importers accept only verified deployment or configuration records. A Git push alone is not proof
of deployment. Repeated import with the same source identity and import key is idempotent.
Descriptions are bounded and screened for secret-shaped or private-source content before storage.
All instants are stored as UTC with the original site timezone retained for display.

## Alert contract

Initial rule types are `sync_failure`, `stale_data`, `missing_binding`, `coverage_drop`,
`absolute_threshold`, `relative_change`, `zero_after_nonzero`, `cross_provider_divergence`, and
`goal_change`.

Every rule fixes its metric or goal, site scope, optional segment version, evaluation grain,
comparison or threshold, maturity lag, minimum baseline, quiet periods, incomplete-data policy,
cooldown, and severity.

Each evaluation is immutable and records the rule version, evaluated period, current and comparison
values, completeness, coverage, result, bounded evidence, and related annotation identifiers.
Allowed results are `triggered`, `clear`, `suppressed_incomplete`, `suppressed_quiet`,
`insufficient_baseline`, and `error`.

An incident has a stable key derived from rule version and affected scope. Its lifecycle is `new`,
`ongoing`, `resolved`, `suppressed`, or `acknowledged`. Continuing evidence updates one incident;
it does not create one incident per run. Acknowledgement and suppression are CLI-only state
transitions with bounded operator notes that contain no identity or secret material.

Cross-provider rules always use `divergence` or `tracking_discrepancy` language. They do not imply
that the compared provider measures should be equal.

## Subscription and delivery contract

Frequencies are `daily`, `weekly`, `monthly`, and `quarterly`. Formats are HTML email, plain-text
email, optional bounded CSV attachment, and filesystem preview.

A subscription defines report type, site scope, optional goal and segment versions, timezone,
frequency, maturity lag, incomplete-data policy, recipient-set reference, formats, and active
state. Recipient addresses and SMTP credentials remain private configuration and are resolved only
at execution. The database stores a recipient-set reference, recipient-set hash, and recipient
count, not addresses.

A delivery run records the subscription version, intended period, idempotency key, attempt number,
state, recipient-set hash/count, content hash, attachment metadata, timestamps, and sanitized error
category. States are `planned`, `previewed`, `sending`, `succeeded`, `retryable_failure`,
`permanent_failure`, and `disabled`.

The idempotency key covers subscription version, intended period, formats, and recipient-set hash.
A succeeded key cannot be sent again. Retries are bounded and reuse the same key. Reports with
materially incomplete evidence do not send unless the definition explicitly permits them.

Initial report types are:

- `daily_operations_digest`: provider failures and staleness, unresolved incidents, latest
  data-through, delivery failures, and configuration problems;
- `weekly_site_briefing`: provider-separated traffic and search changes, goal evidence, route
  movement, annotations, unresolved incidents, and data-quality warnings; and
- `monthly_portfolio_report`: cross-site performance, goal outcomes, acquisition, content
  performance, provider health, trends, and labeled investigation hypotheses.

Quarterly frequency uses an explicitly selected report type; it does not imply a separate template.

## Operations result contract

The operations service reads existing build, schema, integrity, backup, sync, watermark,
definition, goal, alert, annotation, scheduler, and delivery records. It does not collect provider
data or mutate state.

Result language is restricted to:

- `observed`: directly stored source evidence;
- `calculated`: deterministic arithmetic over disclosed observations;
- `compared`: current and reference values shown together;
- `inferred`: a bounded hypothesis supported by a rule or annotation;
- `unknown`: required evidence does not exist; and
- `incomplete`: evidence exists but does not cover the required scope.

The operations console and its HTML, JSON, and CSV surfaces use one result model. Recipient
addresses, credentials, private paths, raw exception text, and unbounded evidence never enter it.

## Command and browser boundary

State-changing commands require private configuration, validate before writing, use transactions,
report exact effects, fail closed, and support dry-run where practical.

Browser routes may list and inspect goals, segments, annotations, alerts, operations state, and
subscription status. They may not activate definitions, add or retire annotations, evaluate or
acknowledge incidents, execute or send subscriptions, synchronize providers, or edit recipients.
Existing Host validation, loopback defaults, optional authentication, CSP, `no-store`, restrictive
CORS, bounded responses, and sanitized errors remain mandatory.

Initial read-only HTML routes are `/goals`, `/segments`, `/annotations`, `/alerts`, `/operations`,
and `/subscriptions`. Initial JSON routes use the same names under `/api/v1/`. CSV exports cover
goals, goal reconciliation, annotations, alerts, and delivery history. Recipient addresses and
private operational identifiers are omitted by default from every browser export.
