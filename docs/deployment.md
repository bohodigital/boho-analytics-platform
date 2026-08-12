# Deployment and operations

## Local and private-server shape

Use one reviewed release environment and three isolated execution lanes:

- a timer invokes `boho-analytics sync` with a bounded window;
- a daily timer invokes `boho-analytics index-coverage sync` to advance a quota-bounded sitemap and
  URL Inspection census;
- a long-running service invokes `boho-analytics serve` on loopback; and
- a separate timer invokes `boho-analytics gsc-bulk sync` with its own private YAML, dedicated
  credential, cost limit, and Seagate-backed lake.

The web process never contacts providers. Neither sync process needs to bind a public port. The
bulk lane does not read or write SQLite and the web lane does not read the bulk lake.

Recommended private-server controls:

- dedicated unprivileged user and restrictive umask;
- configuration readable only by that user;
- systemd credentials or another non-file-in-repository secret source;
- state and backup directories writable only where needed;
- read-only access to the mail index for forms inbox monitoring;
- `NoNewPrivileges`, protected home/system paths, private temporary storage, and memory limits;
- metadata-only logs and a bounded scheduled sync window;
- SSH port forwarding for access.

Example tunnel from a workstation:

```bash
ssh -N -L 8787:127.0.0.1:8787 analytics-host
```

Then open `http://127.0.0.1:8787` locally.

## First deployment sequence

```bash
python -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/boho-analytics --config /private/platform.toml config validate
.venv/bin/boho-analytics --config /private/platform.toml db init
.venv/bin/boho-analytics --config /private/platform.toml probe --connection one-connection
.venv/bin/boho-analytics --config /private/platform.toml sync --connection one-connection --days 7
.venv/bin/boho-analytics --config /private/platform.toml db check
```

Connect one provider at a time. Confirm reported scope, resource, dates, and metric meaning before
enabling its timer. A failed connector returns a nonzero command status while successful bindings
remain committed and independently visible. A connector exception returns nonzero. A successful
empty provider read records `result_kind=empty`, advances binding progress, and provides query
coverage without inserting invented metric facts.

The forms-evidence-v3 release upgrades the database marker to schema version 4 and preserves prior
forms facts as inactive lineage. Before upgrade, back up the schema-v3 database. Configure the D1
connection's verified `source_retention_days` and the inbox binding's independently established
`observation_start` plus any reserved synthetic subject exclusions. Validate and probe the config,
then run source-backed D1 syncs only for completed site-local days inside retention and inbox syncs
only across the established observation horizon. Verify current identity counts, partial historical
coverage, and raw lineage before re-enabling the timer. Never derive identity-v3 replacement rows
from identity-v2 zeroes.

The schema-5 definition-registry upgrade is a separately reviewed additive migration. Before an
authorized cutover, stop every writer and reader service, create and verify an online schema-4
backup, retain the exact v0.2.0 environment at commit
`4a3bfa9e8a4346578263ee74dd227e6230ccc7c3`, and test the ordered migration on a truly empty
database, a fresh schema-4 database, and an exact online-backup copy of production. For every
legacy table, compare deterministic full-table fingerprints and row counts before and after; the
three new definition tables (`analytics_definition_versions`, `analytics_definition_activations`,
and `analytics_definition_retirements`) must initially be empty. Re-run initialization to prove an
idempotent no-op, verify integrity and foreign keys, and prove the older runtime refuses schema 5.
This repository change does not authorize a production migration.

The schema-6 acquisition-provenance upgrade is also additive, but it cuts Search Console and Umami
to identity version 2. Before an authorized cutover, stop writers, create and verify an online
schema-5 backup, run migration 006 on a disposable copy, and compare every pre-existing table's row
count and deterministic fingerprint. The two new provenance tables must initially be empty. After
upgrade, run fresh source-backed GSC and Umami syncs before treating those providers as current;
version-1 rows remain retained lineage and are not rewritten. Verify the pinned migration-006
tables, indexes, immutability triggers, record hashes, foreign keys, and a known report window before
re-enabling the timer. This repository change does not authorize that production migration or the
separate Search Console BigQuery export setup.

The schema-7 index-coverage upgrade adds only derived inventory, URL-fingerprint, and run-status
tables. Back up schema 6 before cutover, initialize the reviewed runtime, and verify integrity before
the first census. The census uses the existing Search Console read-only OAuth credential. It does
not require a BigQuery scope and does not store URL text or provider responses. Run it manually
before enabling a timer:

```bash
boho-analytics --config /private/platform.toml index-coverage sync \
  --per-property-limit 1900 --pause-seconds 0.12
boho-analytics --config /private/platform.toml index-coverage status
```

The default limit deliberately stays below Google's 2,000 inspection requests per property per
day. A property with more URLs completes across multiple daily runs. Until every current sitemap
URL has a verdict no older than 30 days, the dashboard shows inspection progress and withholds the
indexed total and percentage. Schedule refresh at 21 days so a completed census can be renewed
before it becomes stale. Sitemap count means unique same-host URLs in the current sitemap tree; it
does not mean crawled pages, rendered pages, or pages with search impressions.

Install the optional `bigquery` dependency group only in a release intended to run the bulk lane.
Before enabling its timer, complete the separately authorized Google setup and run:

```bash
boho-analytics gsc-bulk validate --manifest /private/gsc-bulk.yaml
boho-analytics gsc-bulk probe --manifest /private/gsc-bulk.yaml
boho-analytics gsc-bulk sync --manifest /private/gsc-bulk.yaml --days 1 --end-lag-days 3
boho-analytics gsc-bulk status --manifest /private/gsc-bulk.yaml
boho-analytics gsc-bulk verify --manifest /private/gsc-bulk.yaml
```

The bulk unit must add `RequiresMountsFor` and `ConditionPathIsMountPoint` for the Seagate and grant
write access only to its lake subtree. It must remain unprivileged with an empty capability set,
protected system/home paths, a restrictive umask, bounded memory/tasks, and encrypted systemd
credential delivery. Unlike the ordinary web and SQLite-sync units, do not set
`PrivateDevices=yes`: the application deliberately requires direct visibility of the configured
`/dev/disk/by-uuid/<uuid>` identity and fails closed without it. The complete activation, storage,
acceptance, and rollback contract is in the
[Search Console bulk-export runbook](gsc-bigquery-bulk-export.md).

Before any package build or release handoff, run `python scripts/verify_release.py` from the exact
reviewed Git checkout. The verifier requires a clean tracked and untracked status and independently
recomputes the filesystem Git tree. For a Git archive or another export without `.git`, preserve the
reviewed 40-character tree ID outside the export and run
`python scripts/verify_release.py --expected-tree <reviewed-tree-id>`; an export without that
independent identity fails closed. Modified, staged, deleted, or additional content cannot be
accepted merely because its path and file type are allowlisted.

Inject the exact reviewed Git identities into every web, sync, and backup unit so `--version` and
`/healthz` expose the running build rather than only a package label:

```ini
Environment=BOHO_ANALYTICS_BUILD_COMMIT=<full-commit-id>
Environment=BOHO_ANALYTICS_BUILD_TREE=<full-tree-id>
```

## Backup and restore

Use SQLite's online backup API rather than copying a live WAL database:

```bash
boho-analytics --config /private/platform.toml db backup /private/backups/analytics.db
```

The bundled scheduled-backup wrapper writes only beneath a `scheduled/` child directory and applies
retention only there. Keep pre-migration, preview, rollback, and manual evidence outside that child so
scheduled pruning cannot match it.

Test restores away from the live state path. A live restore requires `--confirm`; before copying it
validates source integrity, foreign keys, the schema marker, and every schema-5 definition's
canonical type-specific privacy contract and immutable hashes. It also requires the exact migration
005 table, index, and trigger definitions and recursively resolves every embedded typed version
reference. Schema-6 sources additionally require the exact migration-006 provenance schema and
valid acquisition-slice and normalized-observation hashes and linkage. A source schema outside the
running package's supported range is rejected before any copy, and the destination schema marker
must still match the source after copying.
Retained-version activation,
reuse, reference resolution, and current-definition reads repeat semantic, content-hash,
version-identity, and immutable version-record validation; current
activation use validates its referenced version before any retirement or successor activation and
also repeats immutable activation-record and append-only retirement-event validation. Restore holds
the validated source read snapshot through SQLite backup, checkpoints the current WAL, creates a
`.pre-restore` backup, validates the copied temporary destination, and only then atomically replaces
the target:

```bash
boho-analytics --config /private/platform.toml db restore /private/backups/analytics.db --confirm
boho-analytics --config /private/platform.toml db check
```

Stop the timer and web service before live restore. Preserve the previous package and database until
the health endpoint and a known report window have been verified.

## Scheduling

A typical schedule syncs the last 3 complete days daily to absorb provider revisions, plus a longer
weekly window where quotas allow. The global lease prevents overlapping writers and safely takes
over an expired lock. Do not schedule browser-driven syncs or unbounded backfills.

## Future remote web access

Do not open the built-in HTTP port directly. Place the loopback origin behind an authenticated HTTPS
proxy or private tunnel. Before any client-facing deployment, add and test application tenant roles,
origin-side identity validation, audit events, rate limits, export controls, and incident response.

## Rollback

1. Stop the web service, sync timer, and every other writer.
2. Preserve the failed database for investigation.
3. Restore the matching verified pre-migration backup; do not use an in-place destructive down
   migration.
4. Restore the exact prior package or verified commit. Schema-4 and schema-3 runtimes
   intentionally refuse newer database schemas.
5. Validate integrity, foreign keys, deterministic legacy-table fingerprints, `db check`, the
   health endpoint, and a known report.
6. Re-enable timers last, only after a bounded fixture or provider sync succeeds.

Rollback never requires modifying trackers, provider data, form routing, or mailbox state. Treat the
prior package's long-range forms coverage as a known data-trust limitation.
