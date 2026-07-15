-- Schema version 1. Loaded directly by the migration runner in storage.py.
CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS metric_facts (
  point_key TEXT PRIMARY KEY,
  client_id TEXT NOT NULL,
  site_id TEXT NOT NULL,
  source TEXT NOT NULL,
  metric TEXT NOT NULL,
  unit TEXT NOT NULL,
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  grain TEXT NOT NULL,
  value TEXT NOT NULL,
  dimensions_json TEXT NOT NULL,
  completeness TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS metric_query ON metric_facts(client_id, site_id, metric, start_at, end_at);
CREATE TABLE IF NOT EXISTS capability_snapshots (
  connection_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  probed_at TEXT NOT NULL,
  authentication_ok INTEGER NOT NULL,
  resources_json TEXT NOT NULL,
  metric_groups_json TEXT NOT NULL,
  max_lookback_days INTEGER,
  warnings_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_runs (
  id TEXT PRIMARY KEY,
  connection_id TEXT NOT NULL,
  site_id TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  points_written INTEGER NOT NULL DEFAULT 0,
  error_category TEXT,
  error_message TEXT
);
CREATE INDEX IF NOT EXISTS sync_runs_recent ON sync_runs(connection_id, started_at DESC);
CREATE TABLE IF NOT EXISTS sync_locks (
  lock_name TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watermarks (
  binding_key TEXT PRIMARY KEY,
  completed_through TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
