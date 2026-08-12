-- Schema version 7. Privacy-bounded Search Console URL Inspection census.
--
-- Published URLs are discovered from public sitemaps, but only SHA-256 URL
-- fingerprints are retained. The current inventory and inspection cache are
-- mutable derived state; run records preserve operational history without
-- storing URL text or provider responses.
CREATE TABLE index_coverage_inventories (
  site_id TEXT NOT NULL PRIMARY KEY CHECK(length(site_id) BETWEEN 1 AND 512),
  inventory_hash TEXT NOT NULL
    CHECK(length(inventory_hash) = 64 AND inventory_hash NOT GLOB '*[^0-9a-f]*'),
  published_pages INTEGER NOT NULL
    CHECK(typeof(published_pages) = 'integer' AND published_pages >= 0),
  observed_at TEXT NOT NULL CHECK(julianday(observed_at) IS NOT NULL)
);

CREATE TABLE index_coverage_url_status (
  site_id TEXT NOT NULL CHECK(length(site_id) BETWEEN 1 AND 512),
  url_hash TEXT NOT NULL
    CHECK(length(url_hash) = 64 AND url_hash NOT GLOB '*[^0-9a-f]*'),
  inventory_hash TEXT NOT NULL
    CHECK(length(inventory_hash) = 64 AND inventory_hash NOT GLOB '*[^0-9a-f]*'),
  last_seen_at TEXT NOT NULL CHECK(julianday(last_seen_at) IS NOT NULL),
  verdict TEXT CHECK(verdict IN ('PASS','FAIL','NEUTRAL','UNKNOWN','VERDICT_UNSPECIFIED')),
  indexed INTEGER CHECK(indexed IN (0, 1)),
  inspected_at TEXT CHECK(inspected_at IS NULL OR julianday(inspected_at) IS NOT NULL),
  PRIMARY KEY(site_id, url_hash),
  CHECK(
    (verdict IS NULL AND indexed IS NULL AND inspected_at IS NULL)
    OR (verdict IS NOT NULL AND indexed IS NOT NULL AND inspected_at IS NOT NULL)
  ),
  FOREIGN KEY(site_id) REFERENCES index_coverage_inventories(site_id)
    ON DELETE CASCADE
);

CREATE INDEX index_coverage_current_status
  ON index_coverage_url_status(site_id, inventory_hash, inspected_at, indexed);

CREATE TABLE index_coverage_runs (
  id TEXT NOT NULL PRIMARY KEY
    CHECK(length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'),
  site_id TEXT NOT NULL CHECK(length(site_id) BETWEEN 1 AND 512),
  connection_id TEXT NOT NULL CHECK(length(connection_id) BETWEEN 1 AND 512),
  started_at TEXT NOT NULL CHECK(julianday(started_at) IS NOT NULL),
  finished_at TEXT CHECK(finished_at IS NULL OR julianday(finished_at) IS NOT NULL),
  status TEXT NOT NULL CHECK(status IN ('running','complete','partial','failed')),
  published_pages INTEGER CHECK(
    published_pages IS NULL
    OR (typeof(published_pages) = 'integer' AND published_pages >= 0)
  ),
  inspected_this_run INTEGER NOT NULL DEFAULT 0
    CHECK(typeof(inspected_this_run) = 'integer' AND inspected_this_run >= 0),
  error_category TEXT CHECK(length(error_category) BETWEEN 1 AND 60)
);

CREATE INDEX index_coverage_runs_latest
  ON index_coverage_runs(site_id, started_at DESC);
