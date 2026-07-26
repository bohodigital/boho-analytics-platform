# ADR 0004: Activate analytics operations as immutable definitions

- Status: Accepted
- Date: 2026-07-25

## Context

Goals, segments, alert rules, and report subscriptions change how stored evidence is interpreted.
Editing them in place would silently change historical reports, incidents, and deliveries. A
browser editor would also expand the current read-only trust boundary and create a second path for
configuration and recipient mutation.

## Decision

Definitions originate in strict private declarative configuration. Validation is side-effect free.
Activation stores an immutable canonical snapshot with a stable key, version, content hash,
source-package hash, validation result, activation time, and retirement time.

Changed content creates a new version. Historical evaluations and deliveries retain their original
version references. The browser may inspect sanitized definition state but may not validate,
activate, retire, acknowledge, suppress, execute, or deliver.

Recipient addresses and credentials remain outside the public repository and are not copied into
the analytics database. Subscriptions persist only a private recipient-set reference, hash, and
count.

## Consequences

Reports can disclose exactly which business definition produced a result, and rollback becomes an
auditable activation rather than an invisible edit. Operators must use validated CLI or private
configuration workflows for changes. The database gains definition history and activation
transactions, but the web application retains its existing read-only security model.
