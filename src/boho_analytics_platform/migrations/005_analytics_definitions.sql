-- Schema version 5. Additive immutable Analytics Operations definition registry.
CREATE TABLE analytics_definition_versions (
  id TEXT NOT NULL PRIMARY KEY
    CHECK(length(id) = 64 AND id NOT GLOB '*[^0-9a-f]*'),
  scope_key TEXT NOT NULL
    CHECK(length(scope_key) BETWEEN 1 AND 128),
  definition_type TEXT NOT NULL
    CHECK(definition_type IN ('goal','segment','alert_rule','report_subscription')),
  definition_key TEXT NOT NULL
    CHECK(length(definition_key) BETWEEN 1 AND 128),
  version INTEGER NOT NULL
    CHECK(typeof(version) = 'integer' AND version > 0),
  content_hash TEXT NOT NULL
    CHECK(length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'),
  content_json TEXT NOT NULL
    CHECK(
      length(CAST(content_json AS BLOB)) BETWEEN 2 AND 32768
      AND json_valid(content_json)
      AND json_type(content_json) = 'object'
    ),
  metadata_json TEXT NOT NULL
    CHECK(
      length(CAST(metadata_json AS BLOB)) BETWEEN 2 AND 4096
      AND json_valid(metadata_json)
      AND json_type(metadata_json) = 'object'
    ),
  created_at TEXT NOT NULL
    CHECK(
      julianday(created_at) IS NOT NULL
      AND substr(created_at, 11, 1) = 'T'
      AND substr(created_at, -6) = '+00:00'
      AND (
        length(created_at) = 25
        OR (length(created_at) = 32 AND substr(created_at, 20, 1) = '.')
      )
    ),
  record_hash TEXT NOT NULL
    CHECK(length(record_hash) = 64 AND record_hash NOT GLOB '*[^0-9a-f]*'),
  UNIQUE(scope_key, definition_type, definition_key, version),
  UNIQUE(scope_key, definition_type, definition_key, content_hash),
  UNIQUE(id, scope_key, definition_type, definition_key)
);

CREATE INDEX analytics_definition_versions_reuse
  ON analytics_definition_versions(
    scope_key, definition_type, definition_key, content_hash
  );
CREATE INDEX analytics_definition_versions_history
  ON analytics_definition_versions(
    scope_key, definition_type, definition_key, version
  );

CREATE TABLE analytics_definition_activations (
  id TEXT NOT NULL PRIMARY KEY
    CHECK(length(id) = 64 AND id NOT GLOB '*[^0-9a-f]*'),
  definition_version_id TEXT NOT NULL
    CHECK(
      length(definition_version_id) = 64
      AND definition_version_id NOT GLOB '*[^0-9a-f]*'
    ),
  scope_key TEXT NOT NULL
    CHECK(length(scope_key) BETWEEN 1 AND 128),
  definition_type TEXT NOT NULL
    CHECK(definition_type IN ('goal','segment','alert_rule','report_subscription')),
  definition_key TEXT NOT NULL
    CHECK(length(definition_key) BETWEEN 1 AND 128),
  activated_at TEXT NOT NULL
    CHECK(
      julianday(activated_at) IS NOT NULL
      AND substr(activated_at, 11, 1) = 'T'
      AND substr(activated_at, -6) = '+00:00'
      AND (
        length(activated_at) = 25
        OR (length(activated_at) = 32 AND substr(activated_at, 20, 1) = '.')
      )
    ),
  record_hash TEXT NOT NULL
    CHECK(length(record_hash) = 64 AND record_hash NOT GLOB '*[^0-9a-f]*'),
  FOREIGN KEY(
    definition_version_id, scope_key, definition_type, definition_key
  ) REFERENCES analytics_definition_versions(
    id, scope_key, definition_type, definition_key
  ) ON DELETE RESTRICT,
  UNIQUE(id, scope_key, definition_type, definition_key, activated_at)
);

CREATE INDEX analytics_definition_activations_version_history
  ON analytics_definition_activations(definition_version_id, activated_at);
CREATE INDEX analytics_definition_activations_scoped_history
  ON analytics_definition_activations(
    scope_key, definition_type, definition_key, activated_at
  );

CREATE TABLE analytics_definition_retirements (
  id TEXT NOT NULL PRIMARY KEY
    CHECK(length(id) = 64 AND id NOT GLOB '*[^0-9a-f]*'),
  activation_id TEXT NOT NULL UNIQUE
    CHECK(length(activation_id) = 64 AND activation_id NOT GLOB '*[^0-9a-f]*'),
  scope_key TEXT NOT NULL
    CHECK(length(scope_key) BETWEEN 1 AND 128),
  definition_type TEXT NOT NULL
    CHECK(definition_type IN ('goal','segment','alert_rule','report_subscription')),
  definition_key TEXT NOT NULL
    CHECK(length(definition_key) BETWEEN 1 AND 128),
  activated_at TEXT NOT NULL
    CHECK(
      julianday(activated_at) IS NOT NULL
      AND substr(activated_at, 11, 1) = 'T'
      AND substr(activated_at, -6) = '+00:00'
      AND (
        length(activated_at) = 25
        OR (length(activated_at) = 32 AND substr(activated_at, 20, 1) = '.')
      )
    ),
  retired_at TEXT NOT NULL
    CHECK(
      julianday(retired_at) IS NOT NULL
      AND substr(retired_at, 11, 1) = 'T'
      AND substr(retired_at, -6) = '+00:00'
      AND (
        length(retired_at) = 25
        OR (length(retired_at) = 32 AND substr(retired_at, 20, 1) = '.')
      )
      AND julianday(retired_at) >= julianday(activated_at)
      AND retired_at >= activated_at
    ),
  record_hash TEXT NOT NULL
    CHECK(length(record_hash) = 64 AND record_hash NOT GLOB '*[^0-9a-f]*'),
  FOREIGN KEY(
    activation_id, scope_key, definition_type, definition_key, activated_at
  ) REFERENCES analytics_definition_activations(
    id, scope_key, definition_type, definition_key, activated_at
  ) ON DELETE RESTRICT
);

CREATE INDEX analytics_definition_retirements_scoped_history
  ON analytics_definition_retirements(
    scope_key, definition_type, definition_key, retired_at
  );

CREATE TRIGGER analytics_definition_versions_no_update
BEFORE UPDATE ON analytics_definition_versions
BEGIN
  SELECT RAISE(ABORT, 'definition versions are immutable');
END;

CREATE TRIGGER analytics_definition_versions_no_delete
BEFORE DELETE ON analytics_definition_versions
BEGIN
  SELECT RAISE(ABORT, 'definition versions cannot be deleted');
END;

CREATE TRIGGER analytics_definition_activations_no_update
BEFORE UPDATE ON analytics_definition_activations
BEGIN
  SELECT RAISE(ABORT, 'definition activations are immutable');
END;

CREATE TRIGGER analytics_definition_activations_no_delete
BEFORE DELETE ON analytics_definition_activations
BEGIN
  SELECT RAISE(ABORT, 'definition activations cannot be deleted');
END;

CREATE TRIGGER analytics_definition_activations_one_current
BEFORE INSERT ON analytics_definition_activations
WHEN EXISTS (
  SELECT 1
    FROM analytics_definition_activations AS activation
   WHERE activation.scope_key = NEW.scope_key
     AND activation.definition_type = NEW.definition_type
     AND activation.definition_key = NEW.definition_key
     AND NOT EXISTS (
       SELECT 1
         FROM analytics_definition_retirements AS retirement
        WHERE retirement.activation_id = activation.id
     )
)
BEGIN
  SELECT RAISE(ABORT, 'a current definition activation already exists');
END;

CREATE TRIGGER analytics_definition_retirements_no_update
BEFORE UPDATE ON analytics_definition_retirements
BEGIN
  SELECT RAISE(ABORT, 'definition retirements are immutable');
END;

CREATE TRIGGER analytics_definition_retirements_no_delete
BEFORE DELETE ON analytics_definition_retirements
BEGIN
  SELECT RAISE(ABORT, 'definition retirements cannot be deleted');
END;
