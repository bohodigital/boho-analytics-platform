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
  sanitized `metadata_json`, and UTC `created_at`.
- Uniqueness: both natural and reuse identities are unique; the parent also has a referenced-key
  uniqueness constraint on `(id, scope_key, definition_type, definition_key)`.
- State: versions have no mutable state and are never updated.
- Foreign keys: none; this is the parent record.
- Deletion: application APIs expose no delete. Future consumers use `ON DELETE RESTRICT`.
- Indexes: reuse identity plus `(scope_key, definition_type, definition_key, version)`.
- Limits: keys, JSON bytes, nesting, arrays, and strings are bounded before insertion and reinforced
  by SQL length/check constraints; JSON must be valid.
- Retention and backup: retained with the database for the lifetime of referenced history and
  included in the supported online backup.

Allowed definition types are the closed set `goal`, `segment`, `alert_rule`, and
`report_subscription`. Expanding that set requires a schema and contract review.

## `analytics_definition_activations`

Purpose: retained activation history and the current-version selector for future consumers.

- Primary key: deterministic text `id`.
- Natural identity: activation event identity derived from version identity and activation time.
- Columns: `definition_version_id`, repeated bounded scope/type/key for enforceable scoped
  uniqueness, UTC `activated_at`, and nullable UTC `retired_at`.
- Foreign key: the composite `(definition_version_id, scope_key, definition_type, definition_key)`
  references the matching version tuple `(id, scope_key, definition_type, definition_key)` with
  `ON DELETE RESTRICT`, so repeated scoped identity cannot drift from its version.
- State: current when `retired_at IS NULL`, otherwise retired.
- Mutability: every field is immutable except one permitted monotonic transition of `retired_at`
  from null to a UTC timestamp.
- Uniqueness: a partial unique index permits exactly one current activation for each scoped key.
- Checks: retirement follows activation; storage and migration tests reject any update other than
  the permitted retirement transition.
- Deletion: application APIs expose no delete.
- Indexes: current scoped lookup, version history, and scoped activation chronology.
- Retention and backup: retained history is included in the supported online backup.

Activation retires the prior current row and inserts the new row in one transaction. Identical
active content is a no-op. Reactivating a retired version inserts a new activation row. Explicit
retirement changes only the current row's `retired_at`. Missing version identities, unknown scoped
keys, already inactive keys, version/content collisions, invalid input, and transaction
interruptions fail without changing either table.

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
5. Copy production to a private acceptance location.
6. Record counts and deterministic sampled hashes for every required pre-existing fact, sync,
   watermark, forms-lineage, and graph table.
7. Run migration 005 against the copy only.
8. Require schema 5, integrity `ok`, no foreign-key errors, unchanged prior counts and hashes, and
   two empty definition tables.
9. Re-run the migration through the runner and prove an idempotent no-op.
10. Inject interruption inside migration and activation transactions and prove atomic rollback.
11. Prove the schema-5 candidate opens the copy and exact v0.2.0 refuses it.
12. Restore the schema-4 backup to a separate disposable path and run integrity and known-report
    checks.

The same migration must succeed on an empty database initialized through the supported runner.

## Production rollback

No production migration is authorized by this document. If a later, separately approved cutover
fails:

1. stop every writer and service;
2. preserve the failed schema-5 database for investigation;
3. restore the verified online schema-4 backup;
4. restore the exact v0.2.0 environment at commit
   `4a3bfa9e8a4346578263ee74dd227e6230ccc7c3`;
5. run integrity, foreign-key, and known-report verification;
6. enable timers last.

There is no in-place down migration. Dropping schema-5 tables cannot prove that prior state remained
unchanged and is not an acceptable primary rollback.
