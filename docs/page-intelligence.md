# Page Intelligence

Page Intelligence turns normalized route observations and sitemap index census
records into one auditable page-analysis layer. It is a derived evidence system,
not another provider and not a replacement for raw provider facts.

## Grain and meanings

`page_catalog` has one row per property and normalized internal route. It may be
discovered from Search Console, Umami, GA4, a sitemap, or the structural site
graph. Full public URLs, hostnames, Search Console query text, provider payloads,
visitor/session identifiers, and credentials are not stored in the catalog.

`page_daily` has one row per page, provider date, source, and Search Console
surface. Sources never share metric columns conceptually:

- Search Console uses only ordinary `observation_scope=page` route facts.
  Device, country, search-appearance, and query-cluster rows are overlapping
  slices and are excluded. CTR is recomputed as summed clicks divided by summed
  impressions. Position is weighted by impressions.
- Umami route pageviews and visits remain Umami measurements.
- GA4 route pageviews, landing-page sessions, engaged sessions, engagement
  seconds, and key events remain GA4 measurements.
- Index coverage remains a current sitemap URL census. Internal routes join to
  the existing SHA-256 URL fingerprints. Indexed totals and percentages are
  withheld until the current sitemap inventory is fully inspected; partial
  progress and observed indexed URLs remain explicit.

Missing provider/date/page cells are absent. The materializer never creates a
zero merely because a provider omitted a top row. Each cell carries
completeness, provider state/timezone, observation time, materialization time,
and a hash of contributing normalized fact identities.

## Materialization

Initialize or migrate the database, then rebuild selected properties or the
portfolio:

```bash
boho-analytics --config /path/to/platform.toml db init
boho-analytics --config /path/to/platform.toml page-intelligence materialize
boho-analytics --config /path/to/platform.toml page-intelligence materialize --site example-site
```

A successful ordinary `sync` automatically materializes its selected property
scope. A successful index-coverage inventory also updates sitemap membership and
URL-fingerprint links. Materialization runs are recorded with counts, status,
timestamps, and a contributing-facts hash.

## Read-only evidence API

The loopback dashboard serves bounded JSON envelopes:

- `/api/v1/page-intelligence/properties`
- `/api/v1/page-intelligence/pages`
- `/api/v1/page-intelligence/clusters`
- `/api/v1/page-intelligence/opportunities`
- `/api/v1/page-intelligence/schemes`

Analytical endpoints accept half-open `start`/`end` dates, repeatable `site`, a
Search Console `search_type`, and where relevant an active `scheme`. Page and
opportunity reads are bounded. Every response includes the exact filters,
metric definitions, materialization state, provider freshness, completeness
limits, pagination, and data. The dashboard calls the same service methods, so
it cannot silently apply a different CTR, position, or cluster formula.

The opportunities endpoint is a diagnostic high-impression/low-CTR candidate
set. It does not forecast uplift or establish causality. Its candidate pool is
capped and discloses whether that pool was truncated.

## Clustering schemes

Schemes are versioned UTF-8 JSON, at most 64 KiB. They can use either the
built-in path-section strategy or ordered path-prefix/path-glob rules. Schemes
cannot contain SQL, Python, arbitrary regex, URLs, credentials, or executable
code. Versions and activation/rollback events are immutable.

```bash
boho-analytics --config /path/to/platform.toml page-intelligence scheme validate --file scheme.json
boho-analytics --config /path/to/platform.toml page-intelligence scheme preview --file scheme.json --site example-site
boho-analytics --config /path/to/platform.toml page-intelligence scheme apply --file scheme.json --reason "editorial taxonomy v1"
boho-analytics --config /path/to/platform.toml page-intelligence scheme activate --scheme editorial-map --version 1 --reason "rollback"
```

An exclusive scheme assigns exactly one cluster per in-scope page and permits
reconcilable shares. A multilabel scheme may assign several clusters; the API
discloses overlap and withholds shares. MCP is read-only: scheme writes stay on
the operator CLI.

## Validation

```bash
python -m unittest tests.test_page_intelligence
python -m unittest tests.test_migrations tests.test_storage tests.test_index_coverage
python -m unittest tests.test_web
python -m unittest discover tests
```

Before migration, create and verify a SQLite backup. After migration, run
`db check`, `verify_page_intelligence_integrity`, materialize every configured
property, reconcile exclusive cluster totals to page controls, compare API and
dashboard values, and inspect desktop and mobile dashboard renders. Size the
per-property sync timeout from measured provider volume; the bundled wrapper
defaults to 3,600 seconds and keeps property failures isolated.
