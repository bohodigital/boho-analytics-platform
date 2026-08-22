# Deployment and operations

This guide describes a generic private deployment. It does not assume a particular host, disk
vendor, domain, organization, or credential store.

## Recommended process separation

Run four independent lanes:

- `boho-analytics serve` provides the read-only dashboard on loopback;
- `boho-analytics sync` collects bounded provider windows;
- `boho-analytics index-coverage sync` advances the quota-bounded sitemap and URL Inspection census;
- `boho-analytics gsc-bulk sync` optionally mirrors Search Console bulk exports to a private Parquet
  lake.

The web process never contacts analytics providers. Provider syncs do not need a listening port.
The bulk-export lane neither reads nor writes the normalized SQLite database, and the web process
does not read the private Parquet lake.

Use a dedicated unprivileged service account, a restrictive umask, private configuration, a
credential manager, metadata-only logs, bounded memory and task limits, and explicit writable-path
allowlists. Keep the built-in HTTP listener on `127.0.0.1` unless it is placed behind an
authenticated HTTPS gateway that also enforces application authorization.

## First installation

```bash
python -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/boho-analytics --config /etc/analytics/platform.toml config validate
.venv/bin/boho-analytics --config /etc/analytics/platform.toml db init
.venv/bin/boho-analytics --config /etc/analytics/platform.toml probe \
  --connection example-connection
.venv/bin/boho-analytics --config /etc/analytics/platform.toml sync \
  --connection example-connection --days 7
.venv/bin/boho-analytics --config /etc/analytics/platform.toml db check
```

Connect one provider at a time. Verify its resource, scope, dates, timezone, metric definitions,
freshness, and completeness before scheduling it. A failed connector exits nonzero without erasing
facts already committed by other bindings. A successful empty read records query coverage without
inventing metric rows.

## External normalized-state storage

Large normalized state can live on a UUID-mounted filesystem while the configured application path
remains stable through a bind mount. Keep application state and scheduled backups in separate
private directories on the same reviewed filesystem.

Before a service opens SQLite, run:

```bash
python scripts/verify_runtime_storage.py \
  --state-path /var/lib/analytics/state \
  --required-mountpoint /mnt/analytics-data \
  --required-filesystem-uuid 00000000-0000-0000-0000-000000000000
```

The verifier requires both paths to be absolute real mountpoints, requires the state path to reside
on the configured filesystem, rejects symlink substitution, and checks the filesystem marker
`.boho-storage-<uuid>`. The marker is an operator-created, mode-restricted identity file; it is not
a credential.

In systemd, use `RequiresMountsFor`, `ConditionPathIsMountPoint`, and `ExecStartPre` so a missing
external filesystem cannot redirect writes to the host's root filesystem. Perform a storage cutover
only while readers, writers, and timers are stopped. Preserve the previous unit files, mount config,
database, and state directory until the new mount, database integrity, health endpoint, and one
bounded sync have all been verified.

## Database upgrades

Migrations are ordered, versioned, and applied by `db init`. Before upgrading an existing database:

1. Stop the dashboard, timers, and every writer.
2. Create an online backup with the currently trusted runtime.
3. Validate the backup with `db check` in a disposable location.
4. Install the reviewed new package in a separate environment.
5. Run `db init`, followed by `db check`, against a copy first.
6. Compare critical report totals and table counts before and after.
7. Upgrade the live database only after the copy passes.
8. Start the dashboard, verify `/healthz`, and enable writers one at a time.

Schema 8 adds Page Intelligence derived tables. After upgrading, materialize the configured
properties and verify the page catalog, provider-separated daily facts, sitemap fingerprints,
exclusive cluster reconciliation, and bounded evidence APIs:

```bash
boho-analytics --config /etc/analytics/platform.toml page-intelligence materialize
boho-analytics --config /etc/analytics/platform.toml db check
```

The schema-7 index census remains quota bounded. The default URL Inspection limit stays below the
provider's daily per-property quota, and the dashboard withholds indexed totals until the current
sitemap inventory is completely and freshly inspected.

## Scheduling provider syncs

For multiple properties, use `scripts/sync_runtime_by_site.sh` with explicit site and connection
allowlists:

```ini
Environment=BOHO_ANALYTICS_CLI=/opt/analytics/venv/bin/boho-analytics
Environment=BOHO_ANALYTICS_CONFIG=/etc/analytics/platform.toml
Environment="BOHO_ANALYTICS_SYNC_SITES=site-one site-two"
Environment="BOHO_ANALYTICS_SYNC_CONNECTIONS=umami search-console ga4"
Environment=BOHO_ANALYTICS_SYNC_DAYS=3
Environment=BOHO_ANALYTICS_SITE_TIMEOUT_SECONDS=3600
```

The wrapper starts one bounded process per property and continues after an individual timeout or
provider failure. Size the enclosing service timeout above the sum of the per-property limits.
Schedule short overlapping windows to absorb provider revisions, and add a measured longer refresh
only where quotas and runtime allow. Never trigger provider syncs from browser requests.

## Backup and restore

Create live backups through SQLite's online backup API:

```bash
boho-analytics --config /etc/analytics/platform.toml db backup \
  /var/backups/analytics/manual-before-upgrade.sqlite3
```

The bundled `scripts/backup_runtime.sh` writes scheduled backups only beneath a `scheduled/`
directory, validates the temporary database, atomically publishes it, and prunes only matching
scheduled backup names. Keep manual, pre-migration, and rollback evidence outside that directory.

Test restoration away from the live state path. A live restore requires `--confirm`, validates the
source and copied destination, checkpoints the current WAL, creates a `.pre-restore` backup, and
atomically replaces the database only after validation:

```bash
boho-analytics --config /etc/analytics/platform.toml db restore \
  /var/backups/analytics/verified.sqlite3 --confirm
boho-analytics --config /etc/analytics/platform.toml db check
```

Stop readers, writers, and timers before a live restore. Preserve the replaced database and matching
package until the health endpoint and a known report window pass.

## Private browser access

The simplest private remote-access path is an SSH local forward:

```bash
ssh -N -L 8787:127.0.0.1:8787 analytics-host
```

Then open `http://127.0.0.1:8787`. For shared or internet-reachable access, use authenticated HTTPS,
keep the origin private, validate identity at the application boundary, enforce property-level
authorization, rate-limit exports, and test revocation and origin-bypass failures. Do not expose the
built-in HTTP port directly.

## Release verification

Run the release verifier from the exact clean Git checkout:

```bash
python scripts/verify_release.py
python -m compileall -q src
python -m unittest discover -s tests -v
python -m build
python -m twine check --strict dist/*
```

The verifier requires a clean tree and recomputes the filesystem Git tree. For an exported tree
without `.git`, preserve the independently reviewed tree ID outside the export and pass it through
`--expected-tree`.

Set `BOHO_ANALYTICS_BUILD_COMMIT` and `BOHO_ANALYTICS_BUILD_TREE` in each deployed service so
`--version` and `/healthz` report the actual reviewed build.

The PyPI workflow is manual, tag-bound, and uses a protected environment with Trusted Publishing.
Before invoking it, verify that the final `vX.Y.Z` tag points to the reviewed release commit and that
the package version is exactly `X.Y.Z`. PyPI files are immutable; publish only a fully accepted final
artifact.

## Rollback

1. Stop the dashboard, timers, and every writer.
2. Preserve the failed database and logs for diagnosis.
3. Restore the verified pre-upgrade backup rather than attempting a destructive down migration.
4. Restore the exact matching package or commit.
5. Run `db check`, verify `/healthz`, and compare a known report window.
6. Re-enable writers one at a time and timers last.

Provider data, trackers, form routing, and mailbox state do not need modification for an application
rollback.
