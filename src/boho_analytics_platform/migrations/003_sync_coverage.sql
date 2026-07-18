-- Schema version 3. Additive sync provenance for coverage and empty-result honesty.
-- Forms day identity changed from a UTC date label to the configured site day.
-- Preserve legacy derived facts as lineage, while current readers select only
-- the active identity version after source-backed re-ingestion.
ALTER TABLE metric_facts ADD COLUMN identity_version INTEGER NOT NULL DEFAULT 1;
CREATE INDEX IF NOT EXISTS metric_identity_version
  ON metric_facts(source, identity_version, site_id, metric, start_at, end_at);

ALTER TABLE sync_runs ADD COLUMN binding_key TEXT;
ALTER TABLE sync_runs ADD COLUMN source TEXT;
ALTER TABLE sync_runs ADD COLUMN window_start TEXT;
ALTER TABLE sync_runs ADD COLUMN window_end TEXT;
ALTER TABLE sync_runs ADD COLUMN result_kind TEXT;
ALTER TABLE sync_runs ADD COLUMN data_through TEXT;

CREATE INDEX IF NOT EXISTS sync_runs_coverage
  ON sync_runs(site_id, source, window_start, window_end, started_at DESC);
