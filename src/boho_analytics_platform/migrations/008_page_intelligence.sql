-- Schema version 8. Privacy-bounded Page Intelligence catalog, daily facts,
-- and versioned path-only clustering schemes.

CREATE TABLE page_catalog (
  page_id TEXT NOT NULL PRIMARY KEY
    CHECK(length(page_id) = 64 AND page_id NOT GLOB '*[^0-9a-f]*'),
  client_id TEXT NOT NULL CHECK(length(client_id) BETWEEN 1 AND 512),
  site_id TEXT NOT NULL CHECK(length(site_id) BETWEEN 1 AND 512),
  route TEXT NOT NULL CHECK(
    length(route) BETWEEN 1 AND 2048
    AND substr(route, 1, 1) = '/'
    AND instr(route, '://') = 0
    AND instr(route, char(10)) = 0
    AND instr(route, char(13)) = 0
    AND instr(route, char(0)) = 0
  ),
  first_seen_at TEXT NOT NULL CHECK(julianday(first_seen_at) IS NOT NULL),
  last_seen_at TEXT NOT NULL CHECK(julianday(last_seen_at) IS NOT NULL),
  UNIQUE(site_id, route)
);

CREATE INDEX page_catalog_site_seen
  ON page_catalog(site_id, last_seen_at DESC, page_id);

CREATE TABLE page_catalog_sources (
  page_id TEXT NOT NULL,
  source TEXT NOT NULL CHECK(source IN (
    'sitemap','search-console','umami','google-analytics','site-graph'
  )),
  first_seen_at TEXT NOT NULL CHECK(julianday(first_seen_at) IS NOT NULL),
  last_seen_at TEXT NOT NULL CHECK(julianday(last_seen_at) IS NOT NULL),
  current_member INTEGER NOT NULL DEFAULT 1 CHECK(current_member IN (0,1)),
  PRIMARY KEY(page_id, source),
  FOREIGN KEY(page_id) REFERENCES page_catalog(page_id) ON DELETE CASCADE
);

CREATE TABLE page_catalog_index_links (
  site_id TEXT NOT NULL,
  page_id TEXT NOT NULL,
  url_hash TEXT NOT NULL
    CHECK(length(url_hash) = 64 AND url_hash NOT GLOB '*[^0-9a-f]*'),
  inventory_hash TEXT NOT NULL
    CHECK(length(inventory_hash) = 64 AND inventory_hash NOT GLOB '*[^0-9a-f]*'),
  last_seen_at TEXT NOT NULL CHECK(julianday(last_seen_at) IS NOT NULL),
  PRIMARY KEY(site_id, page_id, url_hash),
  UNIQUE(site_id, url_hash),
  FOREIGN KEY(page_id) REFERENCES page_catalog(page_id) ON DELETE CASCADE,
  FOREIGN KEY(site_id, url_hash)
    REFERENCES index_coverage_url_status(site_id, url_hash) ON DELETE CASCADE
);

CREATE TABLE page_daily (
  page_id TEXT NOT NULL,
  date_label TEXT NOT NULL CHECK(
    length(date_label) = 10 AND date(date_label) = date_label
  ),
  source TEXT NOT NULL CHECK(source IN (
    'search-console','umami','google-analytics'
  )),
  search_type TEXT NOT NULL DEFAULT '' CHECK(length(search_type) <= 32),
  pageviews INTEGER CHECK(
    pageviews IS NULL OR (typeof(pageviews) = 'integer' AND pageviews >= 0)
  ),
  visits INTEGER CHECK(
    visits IS NULL OR (typeof(visits) = 'integer' AND visits >= 0)
  ),
  sessions INTEGER CHECK(
    sessions IS NULL OR (typeof(sessions) = 'integer' AND sessions >= 0)
  ),
  engaged_sessions INTEGER CHECK(
    engaged_sessions IS NULL OR
    (typeof(engaged_sessions) = 'integer' AND engaged_sessions >= 0)
  ),
  engagement_seconds REAL CHECK(
    engagement_seconds IS NULL OR engagement_seconds >= 0
  ),
  key_events REAL CHECK(key_events IS NULL OR key_events >= 0),
  clicks INTEGER CHECK(
    clicks IS NULL OR (typeof(clicks) = 'integer' AND clicks >= 0)
  ),
  impressions INTEGER CHECK(
    impressions IS NULL OR (typeof(impressions) = 'integer' AND impressions >= 0)
  ),
  position_weight REAL CHECK(position_weight IS NULL OR position_weight >= 0),
  completeness TEXT NOT NULL CHECK(completeness IN (
    'realtime','provisional','final','unknown'
  )),
  data_state TEXT NOT NULL CHECK(length(data_state) BETWEEN 1 AND 64),
  provider_timezone TEXT NOT NULL CHECK(length(provider_timezone) BETWEEN 1 AND 128),
  observed_at TEXT NOT NULL CHECK(julianday(observed_at) IS NOT NULL),
  source_facts_hash TEXT NOT NULL
    CHECK(length(source_facts_hash) = 64 AND source_facts_hash NOT GLOB '*[^0-9a-f]*'),
  materialized_at TEXT NOT NULL CHECK(julianday(materialized_at) IS NOT NULL),
  PRIMARY KEY(page_id, date_label, source, search_type),
  CHECK(
    (source = 'search-console' AND search_type != ''
      AND clicks IS NOT NULL AND impressions IS NOT NULL
      AND pageviews IS NULL AND visits IS NULL AND sessions IS NULL
      AND engaged_sessions IS NULL AND engagement_seconds IS NULL
      AND key_events IS NULL)
    OR
    (source = 'umami' AND search_type = ''
      AND clicks IS NULL AND impressions IS NULL AND position_weight IS NULL
      AND sessions IS NULL AND engaged_sessions IS NULL
      AND engagement_seconds IS NULL AND key_events IS NULL
      AND (pageviews IS NOT NULL OR visits IS NOT NULL))
    OR
    (source = 'google-analytics' AND search_type = ''
      AND clicks IS NULL AND impressions IS NULL AND position_weight IS NULL
      AND visits IS NULL
      AND (pageviews IS NOT NULL OR sessions IS NOT NULL
        OR engaged_sessions IS NOT NULL OR engagement_seconds IS NOT NULL
        OR key_events IS NOT NULL))
  ),
  FOREIGN KEY(page_id) REFERENCES page_catalog(page_id) ON DELETE CASCADE
);

CREATE INDEX page_daily_window_source
  ON page_daily(date_label, source, search_type, page_id);

CREATE TABLE page_materialization_runs (
  id TEXT NOT NULL PRIMARY KEY
    CHECK(length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'),
  site_scope TEXT NOT NULL CHECK(length(site_scope) BETWEEN 1 AND 4096),
  started_at TEXT NOT NULL CHECK(julianday(started_at) IS NOT NULL),
  finished_at TEXT CHECK(finished_at IS NULL OR julianday(finished_at) IS NOT NULL),
  status TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
  source_facts INTEGER NOT NULL DEFAULT 0
    CHECK(typeof(source_facts) = 'integer' AND source_facts >= 0),
  pages_seen INTEGER NOT NULL DEFAULT 0
    CHECK(typeof(pages_seen) = 'integer' AND pages_seen >= 0),
  daily_cells INTEGER NOT NULL DEFAULT 0
    CHECK(typeof(daily_cells) = 'integer' AND daily_cells >= 0),
  source_facts_hash TEXT CHECK(
    source_facts_hash IS NULL OR
    (length(source_facts_hash) = 64 AND source_facts_hash NOT GLOB '*[^0-9a-f]*')
  ),
  error_category TEXT CHECK(length(error_category) BETWEEN 1 AND 60)
);

CREATE INDEX page_materialization_latest
  ON page_materialization_runs(started_at DESC);

CREATE TABLE page_schemes (
  scheme_id TEXT NOT NULL PRIMARY KEY CHECK(
    length(scheme_id) BETWEEN 1 AND 64
    AND scheme_id GLOB '[a-z]*'
    AND scheme_id NOT GLOB '*[^a-z0-9-]*'
  ),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 128),
  mode TEXT NOT NULL CHECK(mode IN ('exclusive','multilabel')),
  created_at TEXT NOT NULL CHECK(julianday(created_at) IS NOT NULL)
);

CREATE TABLE page_scheme_versions (
  version_id TEXT NOT NULL PRIMARY KEY
    CHECK(length(version_id) = 64 AND version_id NOT GLOB '*[^0-9a-f]*'),
  scheme_id TEXT NOT NULL,
  version_number INTEGER NOT NULL CHECK(
    typeof(version_number) = 'integer' AND version_number >= 1
  ),
  definition_json TEXT NOT NULL CHECK(json_valid(definition_json)),
  definition_hash TEXT NOT NULL
    CHECK(length(definition_hash) = 64 AND definition_hash NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL CHECK(julianday(created_at) IS NOT NULL),
  UNIQUE(scheme_id, version_number),
  UNIQUE(scheme_id, definition_hash),
  FOREIGN KEY(scheme_id) REFERENCES page_schemes(scheme_id)
);

CREATE TRIGGER page_scheme_versions_no_update
BEFORE UPDATE ON page_scheme_versions BEGIN
  SELECT RAISE(ABORT, 'page scheme versions are immutable');
END;

CREATE TRIGGER page_scheme_versions_no_delete
BEFORE DELETE ON page_scheme_versions BEGIN
  SELECT RAISE(ABORT, 'page scheme versions cannot be deleted');
END;

CREATE TABLE page_scheme_activations (
  id TEXT NOT NULL PRIMARY KEY
    CHECK(length(id) = 64 AND id NOT GLOB '*[^0-9a-f]*'),
  scheme_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  activated_at TEXT NOT NULL CHECK(julianday(activated_at) IS NOT NULL),
  reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 256),
  FOREIGN KEY(scheme_id) REFERENCES page_schemes(scheme_id),
  FOREIGN KEY(version_id) REFERENCES page_scheme_versions(version_id)
);

CREATE INDEX page_scheme_activations_latest
  ON page_scheme_activations(scheme_id, activated_at DESC, id DESC);

CREATE TRIGGER page_scheme_activations_no_update
BEFORE UPDATE ON page_scheme_activations BEGIN
  SELECT RAISE(ABORT, 'page scheme activations are immutable');
END;

CREATE TRIGGER page_scheme_activations_no_delete
BEFORE DELETE ON page_scheme_activations BEGIN
  SELECT RAISE(ABORT, 'page scheme activations cannot be deleted');
END;

CREATE TABLE page_scheme_assignments (
  version_id TEXT NOT NULL,
  page_id TEXT NOT NULL,
  cluster_id TEXT NOT NULL CHECK(
    length(cluster_id) BETWEEN 1 AND 64
    AND cluster_id GLOB '[a-z0-9]*'
    AND cluster_id NOT GLOB '*[^a-z0-9-]*'
  ),
  cluster_label TEXT NOT NULL CHECK(length(cluster_label) BETWEEN 1 AND 128),
  assigned_at TEXT NOT NULL CHECK(julianday(assigned_at) IS NOT NULL),
  PRIMARY KEY(version_id, page_id, cluster_id),
  FOREIGN KEY(version_id) REFERENCES page_scheme_versions(version_id),
  FOREIGN KEY(page_id) REFERENCES page_catalog(page_id) ON DELETE CASCADE
);

CREATE INDEX page_scheme_assignments_page
  ON page_scheme_assignments(page_id, version_id, cluster_id);
