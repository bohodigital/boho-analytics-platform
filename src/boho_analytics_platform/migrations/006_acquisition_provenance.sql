-- Schema version 6. Immutable normalized acquisition provenance.
--
-- These tables retain provider-request semantics and every normalized fact
-- version. Raw provider responses, credentials, and arbitrary metadata are
-- intentionally outside this database contract.
CREATE TABLE acquisition_slices (
  id TEXT NOT NULL PRIMARY KEY
    CHECK(length(id) = 64 AND id NOT GLOB '*[^0-9a-f]*'),
  sync_run_id TEXT NOT NULL,
  binding_key TEXT NOT NULL
    CHECK(length(binding_key) BETWEEN 1 AND 2048),
  slice_key TEXT NOT NULL
    CHECK(
      length(slice_key) BETWEEN 1 AND 128
      AND substr(slice_key, 1, 1) GLOB '[A-Za-z0-9]'
      AND slice_key NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
  metric_family TEXT NOT NULL
    CHECK(
      length(metric_family) BETWEEN 1 AND 128
      AND substr(metric_family, 1, 1) GLOB '[A-Za-z0-9]'
      AND metric_family NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
  start_at TEXT NOT NULL
    CHECK(julianday(start_at) IS NOT NULL),
  end_at TEXT NOT NULL
    CHECK(
      julianday(end_at) IS NOT NULL
      AND julianday(end_at) > julianday(start_at)
    ),
  completeness TEXT NOT NULL
    CHECK(completeness IN ('realtime','provisional','final','unknown')),
  data_state TEXT NOT NULL
    CHECK(
      length(data_state) BETWEEN 1 AND 128
      AND substr(data_state, 1, 1) GLOB '[A-Za-z0-9]'
      AND data_state NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
  provider_scope TEXT NOT NULL
    CHECK(
      length(provider_scope) BETWEEN 1 AND 128
      AND substr(provider_scope, 1, 1) GLOB '[A-Za-z0-9]'
      AND provider_scope NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
  request_dimensions_json TEXT NOT NULL
    CHECK(
      length(CAST(request_dimensions_json AS BLOB)) BETWEEN 2 AND 4096
      AND json_valid(request_dimensions_json)
      AND json_type(request_dimensions_json) = 'array'
    ),
  provider_aggregation TEXT NOT NULL
    CHECK(
      length(provider_aggregation) BETWEEN 1 AND 128
      AND substr(provider_aggregation, 1, 1) GLOB '[A-Za-z0-9]'
      AND provider_aggregation NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
  pages_fetched INTEGER NOT NULL
    CHECK(typeof(pages_fetched) = 'integer' AND pages_fetched > 0),
  raw_rows INTEGER NOT NULL
    CHECK(typeof(raw_rows) = 'integer' AND raw_rows >= 0),
  accepted_rows INTEGER NOT NULL
    CHECK(typeof(accepted_rows) = 'integer' AND accepted_rows >= 0),
  rejected_rows INTEGER NOT NULL
    CHECK(
      typeof(rejected_rows) = 'integer'
      AND rejected_rows >= 0
      AND accepted_rows + rejected_rows = raw_rows
      AND (completeness != 'final' OR rejected_rows = 0)
    ),
  exhaustion_reason TEXT NOT NULL
    CHECK(
      length(exhaustion_reason) BETWEEN 1 AND 128
      AND substr(exhaustion_reason, 1, 1) GLOB '[A-Za-z0-9]'
      AND exhaustion_reason NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
  recorded_at TEXT NOT NULL
    CHECK(julianday(recorded_at) IS NOT NULL),
  record_hash TEXT NOT NULL
    CHECK(length(record_hash) = 64 AND record_hash NOT GLOB '*[^0-9a-f]*'),
  FOREIGN KEY(sync_run_id) REFERENCES sync_runs(id) ON DELETE RESTRICT,
  UNIQUE(sync_run_id, binding_key, slice_key),
  UNIQUE(id, sync_run_id, binding_key)
);

CREATE INDEX acquisition_slices_run
  ON acquisition_slices(sync_run_id, binding_key, metric_family, start_at, end_at);

CREATE TABLE metric_fact_observations (
  id TEXT NOT NULL PRIMARY KEY
    CHECK(length(id) = 64 AND id NOT GLOB '*[^0-9a-f]*'),
  acquisition_slice_id TEXT NOT NULL,
  sync_run_id TEXT NOT NULL,
  binding_key TEXT NOT NULL
    CHECK(length(binding_key) BETWEEN 1 AND 2048),
  point_key TEXT NOT NULL
    CHECK(length(point_key) = 64 AND point_key NOT GLOB '*[^0-9a-f]*'),
  client_id TEXT NOT NULL CHECK(length(client_id) BETWEEN 1 AND 512),
  site_id TEXT NOT NULL CHECK(length(site_id) BETWEEN 1 AND 512),
  source TEXT NOT NULL CHECK(length(source) BETWEEN 1 AND 128),
  metric TEXT NOT NULL CHECK(length(metric) BETWEEN 1 AND 256),
  unit TEXT NOT NULL CHECK(length(unit) BETWEEN 1 AND 64),
  start_at TEXT NOT NULL CHECK(julianday(start_at) IS NOT NULL),
  end_at TEXT NOT NULL
    CHECK(
      julianday(end_at) IS NOT NULL
      AND julianday(end_at) > julianday(start_at)
    ),
  grain TEXT NOT NULL
    CHECK(grain IN ('minute','hour','day','week','month','total')),
  value TEXT NOT NULL CHECK(length(value) > 0),
  dimensions_json TEXT NOT NULL
    CHECK(
      length(CAST(dimensions_json AS BLOB)) BETWEEN 2 AND 32768
      AND json_valid(dimensions_json)
      AND json_type(dimensions_json) = 'object'
    ),
  completeness TEXT NOT NULL
    CHECK(completeness IN ('realtime','provisional','final','unknown')),
  observed_at TEXT NOT NULL CHECK(julianday(observed_at) IS NOT NULL),
  identity_version INTEGER NOT NULL
    CHECK(typeof(identity_version) = 'integer' AND identity_version > 0),
  recorded_at TEXT NOT NULL CHECK(julianday(recorded_at) IS NOT NULL),
  record_hash TEXT NOT NULL
    CHECK(length(record_hash) = 64 AND record_hash NOT GLOB '*[^0-9a-f]*'),
  FOREIGN KEY(acquisition_slice_id, sync_run_id, binding_key)
    REFERENCES acquisition_slices(id, sync_run_id, binding_key)
    ON DELETE RESTRICT,
  FOREIGN KEY(sync_run_id) REFERENCES sync_runs(id) ON DELETE RESTRICT,
  UNIQUE(acquisition_slice_id, point_key)
);

CREATE INDEX metric_fact_observations_point_history
  ON metric_fact_observations(point_key, observed_at, recorded_at);
CREATE INDEX metric_fact_observations_run
  ON metric_fact_observations(sync_run_id, binding_key, acquisition_slice_id);

CREATE TRIGGER acquisition_slices_no_update
BEFORE UPDATE ON acquisition_slices
BEGIN
  SELECT RAISE(ABORT, 'acquisition slices are immutable');
END;

CREATE TRIGGER acquisition_slices_no_delete
BEFORE DELETE ON acquisition_slices
BEGIN
  SELECT RAISE(ABORT, 'acquisition slices cannot be deleted');
END;

CREATE TRIGGER metric_fact_observations_no_update
BEFORE UPDATE ON metric_fact_observations
BEGIN
  SELECT RAISE(ABORT, 'metric fact observations are immutable');
END;

CREATE TRIGGER metric_fact_observations_no_delete
BEFORE DELETE ON metric_fact_observations
BEGIN
  SELECT RAISE(ABORT, 'metric fact observations cannot be deleted');
END;
