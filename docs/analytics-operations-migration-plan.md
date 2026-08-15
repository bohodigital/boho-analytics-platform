# Schema-5 migration plan

Status: implementation contract; no migration is included in this documentation stage.

## Scope and consumers

Schema 5 is one additive migration providing only the immutable definition registry required by
future goal, segment, alert-rule, and report-subscription implementations. Those consumers do not
exist in this stage. Annotations, evaluations, incidents, delivery runs, and feature-specific
tables are explicitly deferred until an immediate reviewed consumer exists.

The migration must not rewrite or replace `metric_facts`, sync history, watermarks, acquisition
coverage, forms lineage, or graph evidence. It runs through the ordered migration runner in one
transaction. The application refuses databases newer than it supports.

## `analytics_definition_versions`

Purpose: immutable, sanitized definition content available to future registry consumers.

- Primary key: deterministic text `id`.
- Natural identity: `(scope_key, definition_type, definition_key, version)`.
- Reuse identity: `(scope_key, definition_type, definition_key, content_hash)`.
- Columns: bounded `scope_key`, `definition_type`, `definition_key`, positive integer `version`,
  64-character lowercase SHA-256 `content_hash`, bounded canonical `content_json`, bounded
  sanitized `metadata_json`, UTC `created_at`, and a 64-character lowercase SHA-256 `record_hash`.
- Record hash: SHA-256 over the canonical, length-delimited encoding of every persisted field except
  `record_hash`; identical rows therefore have identical record hashes across supported runtimes.
- Uniqueness: both natural and reuse identities are unique; the parent also has a referenced-key
  uniqueness constraint on `(id, scope_key, definition_type, definition_key)`.
- State: versions have no mutable state and are never updated.
- Foreign keys: none; this is the parent record.
- Deletion: application APIs expose no delete. Future consumers use `ON DELETE RESTRICT`.
- Indexes: reuse identity plus `(scope_key, definition_type, definition_key, version)`.
- Limits: keys, JSON bytes, nesting, arrays, and strings are bounded before insertion and reinforced
  by SQL length/check constraints; JSON must be valid.
- Retention, backup, and restore: retained with the database for the lifetime of referenced history,
  included in the supported online backup, and restored only as part of a complete database restore.
  Restore verification recomputes every version `record_hash` and rechecks referenced history; a
  partial table restore is unsupported.

Allowed definition types are the closed set `goal`, `segment`, `alert_rule`, and
`report_subscription`. Expanding that set requires a schema and contract review.

## `analytics_definition_activations`

Purpose: retained activation history and the current-version selector for future consumers.

- Primary key: deterministic text `id`.
- Natural identity: activation event identity derived from version identity and activation time.
- Columns: `definition_version_id`, repeated bounded scope/type/key for enforceable scoped
  identity, UTC `activated_at`, and a 64-character lowercase SHA-256 `record_hash`.
- Record hash: SHA-256 over the canonical, length-delimited encoding of the immutable activation
  identity fields, excluding `record_hash`.
- Foreign key: the composite `(definition_version_id, scope_key, definition_type, definition_key)`
  references the matching version tuple `(id, scope_key, definition_type, definition_key)` with
  `ON DELETE RESTRICT`, so repeated scoped identity cannot drift from its version.
- State: current when no `analytics_definition_retirements` row references the activation.
- Mutability: every field is immutable.
- Uniqueness: an insertion guard permits exactly one unretired activation for each scoped key.
- Checks: storage and migration tests reject every activation update.
- Deletion: application APIs expose no delete.
- Indexes: current scoped lookup, version history, and scoped activation chronology.
- Retention, backup, and restore: retained history is included in the supported online backup and
  restored only with its referenced version rows as part of a complete database restore. Restore
  verification recomputes activation `record_hash` values, validates the composite foreign key,
  and rejects a partial activation/version/retirement restore.

## `analytics_definition_retirements`

Purpose: immutable, append-only terminal history for activation ranges.

- Primary key: deterministic identity derived from activation identity and retirement time.
- Columns: referenced activation identity, repeated scope/type/key and activation time, UTC
  `retired_at`, and a full immutable `record_hash`.
- Integrity: a composite foreign key binds every repeated field to the exact activation; the
  retirement record hash covers the complete event.
- Mutability and deletion: update and delete triggers reject every change.
- Uniqueness: at most one retirement event may reference an activation.

Activation appends a retirement event for the prior current row and inserts the new row in one transaction. Identical
active content is a no-op. Reactivating a retired version inserts a new activation row. Explicit
retirement appends only a terminal event. Missing version identities, unknown scoped keys, already
inactive keys, version/content collisions, invalid input, and transaction interruptions fail
without changing any registry table.

## Stored-data boundary

Canonical JSON contains only fields recognized by the strict type schema. Metadata is a separate,
bounded allowlist. The storage API rejects credentials, email addresses, raw TOML, comments,
unknown private fields, full external URLs, raw queries, visitor/session identifiers, message
content, form payloads, private filesystem paths, and secret-shaped values. Neither JSON column is
an extension point for raw provider or private configuration data.

## Migration procedure

1. Stop scheduled writers and services.
2. Record exact application commit, package version, schema version, integrity, and writer state.
3. Create an online schema-4 backup through the supported backup API; open the copy and verify it.
4. Preserve the exact v0.2.0 environment at commit
   `4a3bfa9e8a4346578263ee74dd227e6230ccc7c3` separately.
5. Prepare three distinct cases: a truly empty database with no schema objects, a fresh database
   initialized only through schema 4, and an exact online-backup copy of production schema 4 in a
   private acceptance location.
6. For every legacy table in both schema-4 cases, record its count and a deterministic full-table
   fingerprint over every row in stable primary-key order. Sampled hashes are not sufficient.
7. Run the full ordered migration runner from no schema through schema 5 against the truly empty
   case. Run migration 005 through that runner against the fresh schema-4 case and the exact
   production copy.
8. Require schema 5, integrity `ok`, no foreign-key errors, and all three empty definition tables
   (versions, activations, and retirements) in all three cases. In both schema-4 cases,
   additionally require exactly unchanged legacy counts and full-table fingerprints.
9. Re-run the ordered runner in every case and prove an idempotent no-op.
10. Interrupt migration 005 inside its transaction for each schema-4 case. The transaction must
    roll back completely, schema 4 must remain authoritative, every legacy count and full-table
    fingerprint must remain unchanged, and all three new definition tables must remain absent.
11. In migrated schema-5 copies, seed bounded definition/version and activation fixtures, then
    interrupt multi-definition creation, replacement, retirement, and reactivation packages after
    each write step. All three definition tables must remain byte-for-byte equivalent to their
    respective pre-transaction contents; interruption must not require that pre-existing tables be
    empty.
12. Prove the schema-5 candidate opens every migrated case and exact v0.2.0 refuses each schema-5
    database.
13. Restore the schema-4 backup to a separate disposable path and run integrity, foreign-key,
    full-table fingerprint, and known-report checks.

## Production rollback

No production migration is authorized by this document. If a later, separately approved cutover
fails:

1. stop every writer and the web service;
2. preserve the failed schema-5 database for investigation;
3. restore the verified online schema-4 backup;
4. restore the exact v0.2.0 environment at commit
   `4a3bfa9e8a4346578263ee74dd227e6230ccc7c3`;
5. run integrity, foreign-key, and known-report verification;
6. start the web service and verify its health endpoint and a known report; and
7. enable timers last.

There is no in-place down migration. Dropping schema-5 tables cannot prove that prior state remained
unchanged and is not an acceptable primary rollback.
