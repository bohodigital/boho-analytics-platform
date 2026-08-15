# Search Console BigQuery bulk-export runbook

## Purpose and boundary

The Search Analytics API remains the source for bounded, normalized operational facts in SQLite
and the dashboard. Search Console bulk export is a separate private evidence lane:

```text
Search Console -> property-specific BigQuery dataset -> gsc-bulk -> private Parquet on the Pi
```

It retains Google's complete aggregate export rows, including non-anonymized query text and URL
dimensions as supplied by Google; provider-anonymized fields remain blank. Those rows never become
`MetricPoint` objects, SQLite facts, browser responses, CSV exports, or log messages. The dashboard
does not yet query the Parquet lake. See
[ADR 0006](adr/0006-search-console-private-bulk-lake.md).

## 1. Google Cloud and Search Console activation

Activation is a manual, separately authorized account change. For each property:

1. Select one billed Google Cloud project and choose the BigQuery location before starting. Dataset
   location is difficult to change later.
2. Enable both the BigQuery API and BigQuery Storage API; Search Console requires both before it
   can configure the export. The Pi reader's `use_storage_api` setting independently selects its
   download path and additional reader role.
3. Grant Google's export principal
   `search-console-data-export@system.gserviceaccount.com`:
   `roles/bigquery.jobUser` and `roles/bigquery.dataEditor`.
4. As a verified Search Console property owner, enable bulk export under the property's settings.
   Give every property a distinct dataset name beginning with `searchconsole`.
5. Wait for the first successful paired export, which can take up to 48 hours. Google does not
   bulk-backfill dates before activation, so use the Search Analytics API for older history. Set
   `first_export_date` to the earliest provider data date that actually has successful paired
   `ExportLog` rows, not the UI setup date. Select a known nonempty exported date as
   `identity_proof_date`; every probe and sync will use it to prove the property mapping.

Do not change the exported table schemas. During commissioning, leave partition expiration unset.
Any later cloud-retention reduction is a separate cost/recovery decision; Google requires an
expiration of at least 14 days if one is configured.

Google references: [setup](https://support.google.com/webmasters/answer/12917675),
[table schema](https://support.google.com/webmasters/answer/12917991),
[query guidance](https://support.google.com/webmasters/answer/12917174), and
[export monitoring](https://support.google.com/webmasters/answer/12919198).

## 2. Dedicated reader identity

Create a separate service account for the Pi reader. It is not the Search Console OAuth identity
and must not reuse the GA4/Search Analytics credential. Grant only:

- `roles/bigquery.jobUser` on the billed query project;
- `roles/bigquery.dataViewer` on each property dataset; and
- optionally `roles/bigquery.readSessionUser` on the project when the Storage API is enabled.

The application requests Google's `cloud-platform` credential scope, but IAM permissions above are
the actual authority boundary. No additional Search Console OAuth scope and no Google Trends scope
is involved.

Install the optional runtime dependencies in the reviewed release environment:

```bash
python -m pip install '.[bigquery]'
```

For production, load the canonical service-account JSON as an encrypted systemd credential and set
`warehouse.credential_ref` to `systemd:boho-analytics-bigquery-reader`. A wrapper credential whose
`service_account_json` field contains the JSON string is also supported. Never place either form in
the manifest, repository, command line, or logs.

## 3. Private manifest

Copy [`examples/gsc-bulk.example.yaml`](../examples/gsc-bulk.example.yaml) to a root-owned private
configuration path and replace every placeholder. The schema-v1 YAML is independent of
`platform.toml` and SQLite.

Important fields:

- `warehouse.project_id` and `location`: exact billed project and dataset location;
- `credential_ref`: opaque dedicated-reader credential reference;
- `maximum_bytes_billed`: per-query fail-closed cost ceiling;
- `use_storage_api`: false unless its API and reader role are both enabled;
- `storage.root`: private lake root strictly below `required_mountpoint`;
- `required_filesystem_uuid`: stable filesystem UUID, never `/dev/sda1` or another probe-order name;
- `minimum_free_bytes`: write refusal floor;
- `properties`: exact site ID, Search Console property string, unique `searchconsole*` dataset,
  earliest paired `first_export_date`, and a known nonempty `identity_proof_date`.

`gsc-bulk validate` reads only the manifest. It deliberately does not open credentials, storage, or
BigQuery:

```bash
boho-analytics gsc-bulk validate --manifest /private/gsc-bulk.yaml
```

## 4. Seagate storage contract

Use a dedicated root beneath the Seagate mount. The public examples use:

```text
/srv/analytics-data/boho-analytics-platform/bigquery
```

The populated private manifest must substitute the Pi's actual UUID-mounted Seagate path.

The mount must be persistent by filesystem UUID. On the mounted filesystem, create one empty marker
named `.boho-storage-<uuid>` owned by the bulk service user with mode `0600`; create the lake root
and all intermediate directories for that user with mode `0700`. The marker is an
operator-provisioned disk identity, not a secret.

Before every probe, sync, status, or verify operation, the application proves that:

- the required mountpoint is a real, separate mounted filesystem;
- neither the mountpoint nor lake path traverses a symlink or nested foreign filesystem;
- the private UUID marker is on that filesystem and has no group/other permissions;
- the lake root is owned by the service user, private, writable, and on the mounted device;
- the expected UUID device exists under `/dev/disk/by-uuid` and matches the mounted device; and
- configured free space remains available.

Failure stops before a lake write. There is no fallback to the Pi's root/SD filesystem.
`RequiresMountsFor` and `ConditionPathIsMountPoint` in systemd are additional controls, not
substitutes for the application checks.

The raw layout is immutable by Google revision:

```text
raw/v1/site=<site>/table=<namespace>/provider_date=<date>/
  epoch_version=<n>/part-00000.parquet
  epoch_version=<n>/manifest.json
  epoch_version=<n>/_SUCCESS
  current.json
```

These are source export records, not unique analytical rows. Search Console can emit repeated keys;
every later consumer must aggregate compatible dimensions and use impression-weighted position
math rather than selecting or averaging individual records.

`manifest.json` records the exact source identity and query-result schema, full successful
`ExportLog` history through the accepted revision, control totals, all three partition query-job
identities and bytes processed/billed, file size, and SHA-256 checksum. Empty partitions have a
manifest and `_SUCCESS` but no fabricated row. Downloads remain in private `staging/` until
validated and atomically renamed; failed downloads move to private `quarantine/`.

Partition manifests are materialization lineage, not a complete billing ledger. Identity-proof and
revision-discovery jobs—and jobs that fail before a partition is published—do not have a partition
manifest. Enable and retain Google Cloud Audit Logs and billing export for the query project as the
authoritative record of every submitted job, failure, and total BigQuery cost; reconcile that record
to the job IDs stored with successful local partitions.

The Seagate lake needs its own backup, retention, encryption, and physical-access policy. SQLite
backups do not include it. Quarantine growth and free space must be monitored.

## 5. Probe, sync, and verify

Probe storage, all three required BigQuery tables, and the known nonempty identity date for each
configured property:

```bash
boho-analytics gsc-bulk probe --manifest /private/gsc-bulk.yaml
```

Mirror a bounded settled window:

```bash
boho-analytics gsc-bulk sync --manifest /private/gsc-bulk.yaml \
  --days 7 --end-lag-days 3
boho-analytics gsc-bulk status --manifest /private/gsc-bulk.yaml
boho-analytics gsc-bulk verify --manifest /private/gsc-bulk.yaml
```

Dates are Pacific Search Console dates and the end is exclusive. A daily seven-day refresh catches
ordinary lag; add a measured periodic revision scan, up to the 366-day command limit, after cost and
runtime observation. Every query is partition/date bounded and has the configured bytes ceiling.

A date is eligible only when the latest successful `ExportLog` rows exist for both
`searchdata_site_impression` and `searchdata_url_impression`. Missing log evidence is reported as
`export-log-incomplete`, returns nonzero, and is never interpreted as zero traffic. Higher
`epoch_version` values create new immutable partitions; an identical revision is verified instead
of downloaded again. Status reports each namespace separately, paired versus unpaired dates,
continuous-through dates, and staging/quarantine usage. Google does not place failed attempts in
`ExportLog`, so also monitor Search Console export status and Cloud Logging. Persistent failures
can eventually stop exports.

Sync, status, and full verification hold the same exclusive lake lock. Lock acquisition also
repairs an interrupted durable current-pointer publication or quarantines a stale pointer temp
before the operation continues.

## 6. Candidate systemd controls

The reviewed release path and build identity must be substituted at deployment time. This is a
candidate, not an installed production unit:

```ini
[Unit]
Description=Boho Analytics Search Console BigQuery bulk mirror
Wants=network-online.target
After=network-online.target
RequiresMountsFor=/srv/analytics-data
ConditionPathIsMountPoint=/srv/analytics-data

[Service]
Type=oneshot
User=bohopi
Group=bohopi
WorkingDirectory=/opt/boho-analytics-platform/<reviewed-release>/source
ExecStart=/opt/boho-analytics-platform/<reviewed-release>/venv-prod/bin/boho-analytics gsc-bulk sync --manifest /etc/boho-analytics-platform/gsc-bulk.yaml --days 7 --end-lag-days 3
TimeoutStartSec=60m
LoadCredentialEncrypted=boho-analytics-bigquery-reader:/etc/credstore.encrypted/boho-analytics-bigquery-reader.cred
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths=/srv/analytics-data/boho-analytics-platform/bigquery
MemoryMax=1G
TasksMax=128
```

The bulk unit deliberately omits `PrivateDevices=yes` because direct visibility of the configured
`/dev/disk/by-uuid/<uuid>` identity is a hard application requirement. Keep the unit unprivileged,
capability-free, and write-confined as above. Configure the timer only after a manual `validate`,
`probe`, bounded `sync`, and `verify` all pass against live data.

## Acceptance and rollback

Production acceptance requires all of the following:

- every property's expected location and three tables pass `probe`;
- both namespaces exist for each accepted provider date;
- downloaded control totals equal BigQuery controls, Parquet footer rows and checksums verify, and
  `_SUCCESS` plus current pointers are valid;
- every lake payload path is on the Seagate and the Pi root filesystem is a different device;
- wrong/missing storage identity, permissive modes, symlinks, low space, and incomplete export logs
  fail closed;
- CLI output and journal logs contain bounded metadata only; and
- bulk query and URL rows are absent from SQLite and browser responses.

BigQuery bulk totals are complete aggregate-export evidence; they need not equal top-row Search
Analytics API detail. Compare them only under explicitly compatible dimensions and semantics.

Rollback is independent of the dashboard lane: disable the bulk timer, revoke or unload the reader
credential, and leave immutable Parquet evidence intact for investigation. The API-to-SQLite sync
and loopback dashboard continue unchanged.
