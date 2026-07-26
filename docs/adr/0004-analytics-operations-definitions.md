# ADR 0004: Separate immutable definition versions from activation history

- Status: Accepted
- Date: 2026-07-26

## Context

Goals, segments, alert rules, and report subscriptions change how stored evidence is interpreted.
Editing them in place would silently change historical results. Keeping activation timestamps on a
version row would also make reactivation either mutate history or create duplicate content rows.
A browser editor would expand the read-only trust boundary.

## Decision

Definitions originate in strict private declarative configuration. Validation is side-effect free
and constructs a sanitized canonical object. Raw configuration, comments, unknown fields,
credentials, addresses, and private identifiers are not stored.

Schema 5 separates:

- immutable version rows, deduplicated by scoped key and normalized-content hash; and
- append-only activation rows, with one current activation per scoped key.

Identical active content is a no-op. Identical retired content reuses its version and adds a new
activation. Changed content adds a version and activation while retiring the prior activation in
one transaction. Historical consumers retain the exact version they used.

All future evaluation must use the trusted active-fact/reporting selector rather than treating all
retained `metric_facts` identities as active evidence.

The browser may inspect sanitized state but cannot validate, activate, retire, annotate,
acknowledge, suppress, synchronize, send, or modify recipients.

## Consequences

Definition rollback is auditable reactivation rather than an invisible edit. The schema gains two
generic tables and no consumer-specific tables. Operators use validated CLI or private
configuration workflows. Recipient addresses and credentials remain outside both the repository
and SQLite.
