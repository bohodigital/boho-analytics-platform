-- Schema version 2. Immutable, provenance-linked facts for the site graph.
CREATE TABLE IF NOT EXISTS site_graph_manifest_versions (
  id TEXT PRIMARY KEY,
  manifest_hash TEXT NOT NULL UNIQUE,
  site_key TEXT NOT NULL,
  schema_version INTEGER NOT NULL CHECK(schema_version = 1),
  canonical_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_graph_ingest_runs (
  id TEXT PRIMARY KEY,
  manifest_version_id TEXT NOT NULL REFERENCES site_graph_manifest_versions(id),
  site_key TEXT NOT NULL,
  analysis_mode TEXT NOT NULL CHECK(analysis_mode IN ('source-only','build')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed'))
);
CREATE INDEX IF NOT EXISTS site_graph_ingest_runs_recent
  ON site_graph_ingest_runs(site_key, started_at DESC);

CREATE TABLE IF NOT EXISTS site_graph_repository_snapshots (
  id TEXT PRIMARY KEY,
  ingest_run_id TEXT NOT NULL UNIQUE REFERENCES site_graph_ingest_runs(id),
  site_key TEXT NOT NULL,
  repository_identity TEXT NOT NULL,
  remote_url TEXT NOT NULL,
  revision TEXT NOT NULL,
  ref TEXT NOT NULL,
  clean INTEGER NOT NULL CHECK(clean IN (0,1)),
  content_hash TEXT NOT NULL,
  captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS site_graph_repository_revision
  ON site_graph_repository_snapshots(site_key, revision);

CREATE TABLE IF NOT EXISTS site_graph_page_facts (
  id TEXT PRIMARY KEY,
  repository_snapshot_id TEXT NOT NULL REFERENCES site_graph_repository_snapshots(id),
  fact_key TEXT NOT NULL,
  route TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  source_path TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  record_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(repository_snapshot_id, fact_key)
);
CREATE INDEX IF NOT EXISTS site_graph_page_route
  ON site_graph_page_facts(repository_snapshot_id, route);

CREATE TABLE IF NOT EXISTS site_graph_link_occurrences (
  id TEXT PRIMARY KEY,
  repository_snapshot_id TEXT NOT NULL REFERENCES site_graph_repository_snapshots(id),
  occurrence_key TEXT NOT NULL,
  source_page_fact_id TEXT NOT NULL REFERENCES site_graph_page_facts(id),
  raw_destination TEXT NOT NULL,
  canonical_destination TEXT NOT NULL,
  anchor_text TEXT NOT NULL,
  context_excerpt TEXT NOT NULL,
  source_location TEXT NOT NULL,
  landmark TEXT NOT NULL,
  layer TEXT NOT NULL CHECK(layer IN ('menu','breadcrumb','contextual','related','action','utility')),
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  repeated_template INTEGER NOT NULL CHECK(repeated_template IN (0,1)),
  crawlable INTEGER NOT NULL CHECK(crawlable IN (0,1)),
  nofollow INTEGER NOT NULL CHECK(nofollow IN (0,1)),
  external INTEGER NOT NULL CHECK(external IN (0,1)),
  fragment INTEGER NOT NULL CHECK(fragment IN (0,1)),
  action_kind TEXT,
  evidence_json TEXT NOT NULL,
  record_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(repository_snapshot_id, occurrence_key)
);
CREATE INDEX IF NOT EXISTS site_graph_link_source
  ON site_graph_link_occurrences(repository_snapshot_id, source_page_fact_id, layer);
CREATE INDEX IF NOT EXISTS site_graph_link_destination
  ON site_graph_link_occurrences(repository_snapshot_id, canonical_destination);

CREATE TABLE IF NOT EXISTS site_graph_page_entities (
  id TEXT PRIMARY KEY,
  repository_snapshot_id TEXT NOT NULL REFERENCES site_graph_repository_snapshots(id),
  page_fact_id TEXT NOT NULL REFERENCES site_graph_page_facts(id),
  entity_type TEXT NOT NULL,
  entity_value TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  evidence_json TEXT NOT NULL,
  UNIQUE(repository_snapshot_id, page_fact_id, entity_type, entity_value)
);

CREATE TABLE IF NOT EXISTS site_graph_page_roles (
  id TEXT PRIMARY KEY,
  repository_snapshot_id TEXT NOT NULL REFERENCES site_graph_repository_snapshots(id),
  page_fact_id TEXT NOT NULL REFERENCES site_graph_page_facts(id),
  role TEXT NOT NULL,
  journey_stage INTEGER NOT NULL CHECK(journey_stage BETWEEN 1 AND 5),
  rule_id TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  UNIQUE(repository_snapshot_id, page_fact_id, role, rule_id)
);

CREATE TABLE IF NOT EXISTS site_graph_edge_aggregates (
  id TEXT PRIMARY KEY,
  repository_snapshot_id TEXT NOT NULL REFERENCES site_graph_repository_snapshots(id),
  source_page_fact_id TEXT NOT NULL REFERENCES site_graph_page_facts(id),
  canonical_destination TEXT NOT NULL,
  layer TEXT NOT NULL CHECK(layer IN ('menu','breadcrumb','contextual','related','action','utility')),
  occurrence_count INTEGER NOT NULL CHECK(occurrence_count > 0),
  aggregate_json TEXT NOT NULL,
  UNIQUE(repository_snapshot_id, source_page_fact_id, canonical_destination, layer)
);

CREATE TABLE IF NOT EXISTS site_graph_snapshots (
  id TEXT PRIMARY KEY,
  site_key TEXT NOT NULL,
  repository_snapshot_id TEXT NOT NULL REFERENCES site_graph_repository_snapshots(id),
  manifest_version_id TEXT NOT NULL REFERENCES site_graph_manifest_versions(id),
  compiler_version TEXT NOT NULL,
  projection_name TEXT NOT NULL,
  goal_definition_hash TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(repository_snapshot_id, manifest_version_id, compiler_version, projection_name, goal_definition_hash, content_hash)
);
CREATE INDEX IF NOT EXISTS site_graph_snapshot_recent
  ON site_graph_snapshots(site_key, projection_name, created_at DESC);

CREATE TABLE IF NOT EXISTS site_graph_node_metrics (
  id TEXT PRIMARY KEY,
  graph_snapshot_id TEXT NOT NULL REFERENCES site_graph_snapshots(id),
  page_fact_id TEXT NOT NULL REFERENCES site_graph_page_facts(id),
  metric_name TEXT NOT NULL,
  metric_value REAL NOT NULL,
  algorithm TEXT NOT NULL,
  parameters_json TEXT NOT NULL,
  UNIQUE(graph_snapshot_id, page_fact_id, metric_name, algorithm)
);

CREATE TABLE IF NOT EXISTS site_graph_edge_metrics (
  id TEXT PRIMARY KEY,
  graph_snapshot_id TEXT NOT NULL REFERENCES site_graph_snapshots(id),
  edge_aggregate_id TEXT NOT NULL REFERENCES site_graph_edge_aggregates(id),
  metric_name TEXT NOT NULL,
  metric_value REAL NOT NULL,
  algorithm TEXT NOT NULL,
  parameters_json TEXT NOT NULL,
  UNIQUE(graph_snapshot_id, edge_aggregate_id, metric_name, algorithm)
);

CREATE TABLE IF NOT EXISTS site_graph_components (
  id TEXT PRIMARY KEY,
  graph_snapshot_id TEXT NOT NULL REFERENCES site_graph_snapshots(id),
  component_key TEXT NOT NULL,
  component_type TEXT NOT NULL,
  node_ids_json TEXT NOT NULL,
  edge_ids_json TEXT NOT NULL,
  algorithm TEXT NOT NULL,
  parameters_json TEXT NOT NULL,
  UNIQUE(graph_snapshot_id, component_key)
);

CREATE TABLE IF NOT EXISTS site_graph_findings (
  id TEXT PRIMARY KEY,
  graph_snapshot_id TEXT NOT NULL REFERENCES site_graph_snapshots(id),
  finding_key TEXT NOT NULL,
  finding_type TEXT NOT NULL,
  severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
  algorithm TEXT NOT NULL,
  parameters_json TEXT NOT NULL,
  affected_nodes_json TEXT NOT NULL,
  affected_edges_json TEXT NOT NULL,
  source_fact_keys_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  record_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(graph_snapshot_id, finding_key)
);
CREATE INDEX IF NOT EXISTS site_graph_findings_by_type
  ON site_graph_findings(graph_snapshot_id, finding_type, severity);
