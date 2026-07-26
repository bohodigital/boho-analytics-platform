# Schema-5 migration plan

Status: implementation contract; no migration is included in this stage.

## Invariants

Schema 5 is one additive migration. It must not rewrite or replace `metric_facts`, sync history,
watermarks, acquisition coverage, forms lineage, or graph evidence. Migration execution uses the
existing ordered migration runner and one writer lease.

The application must refuse to run against a database newer than it supports. Version `0.2.0` must
not be restarted on an upgraded live database. Rollback therefore restores both the `0.2.0`
environment and the verified schema-4 backup.

## Proposed tables

### `analytics_definition_versions`

Primary immutable record for goals, segments, alert rules, and subscriptions:

- `id` primary key;
- `definition_kind`, `definition_key`, and integer `version`;
- `content_hash`, `canonical_json`, `source_config_hash`, and `validation_json`;
- `activated_at`, optional `retired_at`, and `created_at`;
- unique `(definition_kind, definition_key, version)`;
- unique `(definition_kind, definition_key, content_hash)`;
- partial unique active index on `(definition_kind, definition_key)` where `retired_at IS NULL`.

### `analytics_goals`

- `definition_version_id` primary key and foreign key;
- `site_id`, `display_name`, `description`, `goal_type`;
- `canonical_source`, `canonical_metric`, `unit`, optional `currency`;
- `active_from`, optional `active_through`;
- `denominator_json`, `aggregation_behavior`, and `confidence`;
- indexes on `(site_id, active_from, active_through)` and canonical metric.

### `analytics_goal_bindings`

- `id` primary key;
- `goal_definition_version_id` foreign key;
- `binding_role` constrained to `canonical` or `corroborating`;
- `source`, `metric`, `unit`, `rules_json`, and `compatibility_json`;
- unique `(goal_definition_version_id, binding_role, source, metric, rules_json)`;
- a trigger or activation-time invariant enforcing exactly one canonical binding.

### `analytics_segments`

- `definition_version_id` primary key and foreign key;
- `display_name`, `description`, `expression_json`, and `limits_json`;
- `compatibility_json`;
- no arbitrary SQL or field-name column.

### `analytics_annotations`

- `id` primary key;
- `site_id`, `category`, `start_at`, optional `end_at`;
- `title`, `description`, `source`;
- optional `commit_ref` and `deployment_ref`;
- `definition_hash`, optional deterministic `import_key`, and `created_at`;
- unique `(source, import_key)` when `import_key` is present;
- indexes on `(site_id, start_at)` and `(site_id, category, start_at)`.

### `analytics_alert_rules`

- `definition_version_id` primary key and foreign key;
- `site_id`, `rule_type`, `severity`, optional `goal_definition_version_id`,
  optional `segment_definition_version_id`;
- `rule_json`, `maturity_lag_seconds`, `minimum_baseline_periods`,
  `cooldown_seconds`, and `incomplete_policy`;
- indexes on `(site_id, rule_type)` and goal/segment references.

### `analytics_alert_evaluations`

- `id` primary key;
- `rule_definition_version_id` foreign key;
- `period_start`, `period_end`, `evaluated_at`, and `result`;
- `current_value`, optional `comparison_value`, `unit`, `completeness`, `coverage_json`,
  `evidence_json`, and `annotation_ids_json`;
- deterministic unique evaluation key for rule version, affected scope, and period;
- index on `(rule_definition_version_id, evaluated_at)`.

### `analytics_alert_incidents`

- `id` primary key and unique `incident_key`;
- `rule_definition_version_id` foreign key;
- `state`, `first_observed_at`, `latest_observed_at`, optional `resolved_at`;
- `latest_evaluation_id` foreign key;
- `affected_scope_json`, optional bounded `operator_note`, and `updated_at`;
- indexes on `(state, latest_observed_at)` and rule version.

### `analytics_report_subscriptions`

- `definition_version_id` primary key and foreign key;
- `site_scope_json`, `report_type`, `frequency`, `timezone`, `format_json`;
- `recipient_set_ref`, `recipient_set_hash`, `recipient_count`;
- `maturity_lag_seconds`, `incomplete_policy`, and `active`;
- no recipient-address or credential column.

### `analytics_delivery_runs`

- `id` primary key;
- `subscription_definition_version_id` foreign key;
- `period_start`, `period_end`, `idempotency_key`, `attempt`, and `state`;
- `recipient_set_hash`, `recipient_count`, optional `content_hash`,
  `attachment_json`, `started_at`, optional `finished_at`, optional `error_category`;
- unique successful `idempotency_key` and unique `(idempotency_key, attempt)`;
- index on `(subscription_definition_version_id, started_at)`.

All JSON columns contain canonical, bounded data validated before insertion. They are not extension
points for raw provider payloads.

## Migration procedure

1. Stop scheduled writers and delivery processes.
2. Record the exact application commit, package version, database schema, database integrity, and
   active writer state.
3. Create an online schema-4 backup and verify it by opening the copy and running integrity checks.
4. Preserve the `0.2.0` environment separately.
5. Copy the production database to an acceptance location.
6. Run the schema-5 migration against the copy.
7. Verify schema version, foreign keys, indexes, prior table row counts, representative fact hashes,
   sync/watermark continuity, forms lineage, and graph snapshot access.
8. Re-run the migration against the same copy and prove idempotent no-op behavior.
9. Exercise an injected interruption in a transaction and prove the database remains schema 4 or
   completes schema 5 atomically.
10. Run the complete suite, release verifier, package build, and bounded smoke queries.
11. Cut over only the exact accepted package and migration after explicit deployment approval.

## Acceptance evidence

The storage stage must provide:

- schema-4-to-5 fixture migration;
- repeated and interrupted migration tests;
- `PRAGMA integrity_check` and `PRAGMA foreign_key_check` results;
- before/after counts and deterministic samples for every pre-existing table;
- backup and restore evidence;
- old-code/new-schema refusal evidence;
- copied-production query and resource-use evidence; and
- exact rollback commands that identify the backup and preserved environment without exposing
  private paths.

## Rollback

Before production acceptance, rollback may discard only a disposable migrated copy.

After cutover failure:

1. stop every schema-5 writer;
2. preserve the failed database for investigation;
3. restore the verified schema-4 backup;
4. restore the exact `0.2.0` environment;
5. run integrity and known-report checks;
6. resume provider schedules only after confirmation; and
7. do not replay schema-5 alert or delivery jobs against schema 4.

There is no in-place down migration. Deleting schema-5 tables is not an acceptable production
rollback because it cannot prove that older state was untouched.
