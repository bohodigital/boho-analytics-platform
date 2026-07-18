# Deployment and operations

## Local and private-server shape

Use one virtual environment, one private TOML file, one SQLite state file, and two execution lanes:

- a timer invokes `boho-analytics sync` with a bounded window;
- a long-running service invokes `boho-analytics serve` on loopback.

The web process never contacts providers. The sync process never needs to bind a public port.

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

Upgrading an existing schema-v2 database preserves legacy forms facts but cuts reporting over to the
corrected site-day identity. Immediately run a bounded, source-backed sync for the configured D1 and
forms-inbox connections across the retained reporting horizon. Verify coverage and raw lineage counts
before re-enabling their timer; do not manufacture replacement values from the old date labels.

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

Test restores away from the live state path. A live restore requires `--confirm`, validates source
integrity, checkpoints the current WAL, and creates a `.pre-restore` backup before replacement:

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

1. Stop the web service and sync timer.
2. Reinstall the prior package or switch to the prior verified commit.
3. Restore the matching database backup if the schema changed.
4. Validate `db check`, start the web service, check `/healthz`, and run a known report.
5. Re-enable the timer only after a bounded fixture or provider sync succeeds.

Rollback never requires modifying trackers, provider data, form routing, or mailbox state.
