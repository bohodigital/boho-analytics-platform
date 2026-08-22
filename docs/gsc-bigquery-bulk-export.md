# Search Console BigQuery bulk-export operator guide

## Purpose and boundary

The Search Analytics API remains the source for bounded normalized facts in SQLite and the
dashboard. Search Console bulk export is a separate private evidence lane:

```text
Search Console -> property-specific BigQuery dataset -> gsc-bulk -> private Parquet lake
```

It retains Google's aggregate export rows, including non-anonymized query text and URL dimensions
as supplied by Google. Provider-anonymized fields remain blank. Bulk rows never become
`MetricPoint` objects, SQLite facts, browser responses, CSV exports, or log messages. See
[ADR 0006](adr/0006-search-console-private-bulk-lake.md).

## 1. Google Cloud and Search Console setup

This setup changes a billed cloud account and should be performed only by an authorized account
owner. For each Search Console property:

1. Select the billed Google Cloud project and BigQuery location before starting. Dataset location
   is difficult to change later.
2. Enable the BigQuery API and BigQuery Storage API. The reader's `use_storage_api` setting
   independently selects the download path and requires its additional role.
3. Grant Google's export principal
   `search-console-data-export@system.gserviceaccount.com` the roles
   `roles/bigquery.jobUser` and `roles/bigquery.dataEditor`.
4. As a verified Search Console property owner, enable bulk export and assign a distinct dataset
   name beginning with `searchconsole`.
5. Wait for the first successful paired export. Google does not backfill dates before activation.
   Set `first_export_date` to the earliest provider date with successful paired `ExportLog` rows,
   and choose a known nonempty exported date as `identity_proof_date`.

Do not change the exported table schemas. Leave partition expiration unset while commissioning.
Any later retention reduction should account for cost, recovery, and Google's minimum expiration.

Google references: [setup](https://support.google.com/webmasters/answer/12917675),
[table schema](https://support.google.com/webmasters/answer/12917991),
[query guidance](https://support.google.com/webmasters/answer/12917174), and
[export monitoring](https://support.google.com/webmasters/answer/12919198).

## 2. Dedicated reader identity

Create a separate service account for the bulk reader. Do not reuse the GA4 or Search Analytics API
credential. Grant only:

- `roles/bigquery.jobUser` on the billed query project;
- `roles/bigquery.dataViewer` on each property dataset;
- optionally `roles/bigquery.readSessionUser` when the Storage API is enabled.

The application requests Google's `cloud-platform` credential scope, while IAM remains the actual
authority boundary. No additional Search Console OAuth or Google Trends scope is involved.

Install the optional dependencies:

```bash
python -m pip install '.[bigquery]'
```

Load the service-account JSON through a credential manager and set `warehouse.credential_ref` to a
reference such as `systemd:analytics-bigquery-reader`. A wrapper JSON object whose
`service_account_json` field contains the service-account document is also accepted. Never place
either credential form in the manifest, repository, command line, or logs.

## 3. Private manifest

Copy [`examples/gsc-bulk.example.yaml`](../examples/gsc-bulk.example.yaml) to a private path and
replace every placeholder. The schema-v1 YAML is independent of `platform.toml` and SQLite.

Important fields:

- `warehouse.project_id` and `location`: exact billed project and dataset location;
- `credential_ref`: opaque dedicated-reader credential reference;
- `maximum_bytes_billed`: per-query fail-closed cost ceiling;
- `use_storage_api`: false unless its API and reader role are both enabled;
- `storage.root`: private lake root strictly below `required_mountpoint`;
- `required_filesystem_uuid`: stable filesystem UUID, never a probe-order device name;
- `minimum_free_bytes`: write-refusal floor;
- `properties`: exact local site ID, Search Console property string, unique `searchconsole*`
  dataset, paired `first_export_date`, and a known nonempty `identity_proof_date`.

Manifest validation reads neither credentials nor BigQuery:

```bash
boho-analytics gsc-bulk validate --manifest /etc/analytics/gsc-bulk.yaml
```

## 4. External-storage contract

Use a dedicated private root beneath a persistent UUID-mounted filesystem. For example:

```text
/mnt/analytics-data/bigquery
```

Create one empty marker named `.boho-storage-<uuid>` on the mounted filesystem. The marker must be
owned by the bulk service user with mode `0600`; the lake root and intermediate directories should
use mode `0700`. The marker is an operator-provisioned disk identity, not a credential.

Before every probe, sync, status, or verify operation, the application proves that:

- the required mountpoint is a real separate mounted filesystem;
- neither mountpoint nor lake path traverses a symlink or nested foreign filesystem;
- the UUID marker is on that filesystem and has no group or other permissions;
- the lake root is private, writable, owned by the service user, and on the mounted device;
- the expected UUID device matches the mounted device;
- configured free space remains available.

Failure stops before a lake write. There is no fallback to the host's root filesystem.
`RequiresMountsFor` and `ConditionPathIsMountPoint` are additional controls, not substitutes for the
application checks.

The layout is immutable by Google revision:

```text
raw/v1/site=<site>/table=<namespace>/provider_date=<date>/
  epoch_version=<n>/part-00000.parquet
  epoch_version=<n>/manifest.json
  epoch_version=<n>/_SUCCESS
  current.json
```

These are source export records, not unique analytical rows. Search Console can emit repeated keys;
consumers must aggregate compatible dimensions and use impression-weighted position math.

Each partition manifest records source identity, result schema, successful `ExportLog` history,
control totals, query-job identities, bytes processed and billed, file size, and SHA-256 checksum.
Empty partitions receive a manifest and `_SUCCESS` without a fabricated row. Downloads remain in
private `staging/` until validated and atomically published; failed downloads move to private
`quarantine/`.

Partition manifests are lineage, not a complete billing ledger. Retain Cloud Audit Logs and billing
export for the query project as the authoritative record of every job and total cost, and reconcile
those records to locally retained successful job IDs.

Back up, retain, encrypt, and monitor the Parquet lake separately. SQLite backups do not include it.

## 5. Probe, sync, and verify

Probe storage, all required tables, and each property's known identity date:

```bash
boho-analytics gsc-bulk probe --manifest /etc/analytics/gsc-bulk.yaml
```

Mirror a bounded settled window:

```bash
boho-analytics gsc-bulk sync --manifest /etc/analytics/gsc-bulk.yaml \
  --days 7 --end-lag-days 3
boho-analytics gsc-bulk status --manifest /etc/analytics/gsc-bulk.yaml
boho-analytics gsc-bulk verify --manifest /etc/analytics/gsc-bulk.yaml
```

Dates use Search Console's Pacific date basis and the end is exclusive. A daily overlapping refresh
captures ordinary lag. Add a wider revision scan only after measuring query cost and runtime. Every
query is date bounded and subject to `maximum_bytes_billed`.

A date is eligible only when the latest successful `ExportLog` rows exist for both
`searchdata_site_impression` and `searchdata_url_impression`. Missing log evidence returns nonzero
as `export-log-incomplete`; it is never interpreted as zero traffic. Higher `epoch_version` values
produce new immutable partitions, while an identical revision is verified in place.

Sync, status, and verification share one exclusive lake lock. Lock acquisition repairs an
interrupted current-pointer publication or quarantines its stale temporary state before continuing.

## 6. Example systemd service

Substitute reviewed package, config, storage, and credential paths:

```ini
[Unit]
Description=Analytics Search Console BigQuery bulk mirror
Wants=network-online.target
After=network-online.target
RequiresMountsFor=/mnt/analytics-data
ConditionPathIsMountPoint=/mnt/analytics-data

[Service]
Type=oneshot
User=analytics
Group=analytics
WorkingDirectory=/opt/analytics/current/source
ExecStart=/opt/analytics/current/venv/bin/boho-analytics gsc-bulk sync --manifest /etc/analytics/gsc-bulk.yaml --days 7 --end-lag-days 3
TimeoutStartSec=60m
LoadCredentialEncrypted=analytics-bigquery-reader:/etc/credstore.encrypted/analytics-bigquery-reader.cred
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
ReadWritePaths=/mnt/analytics-data/bigquery
MemoryMax=1G
TasksMax=128
```

The unit deliberately omits `PrivateDevices=yes` because the application verifies the configured
`/dev/disk/by-uuid/<uuid>` identity. Keep it unprivileged, capability-free, and write-confined.
Enable a timer only after manual `validate`, `probe`, bounded `sync`, and `verify` commands pass.

## Acceptance and rollback

Accept the lane only when:

- each property's expected location and three tables pass `probe`;
- both namespaces exist for every accepted provider date;
- downloaded totals equal BigQuery controls and Parquet rows/checksums verify;
- `_SUCCESS` files and current pointers are valid;
- every payload path resides on the required filesystem and not the host root;
- wrong identity, permissive modes, symlinks, low space, and incomplete export logs fail closed;
- CLI and service logs contain bounded metadata only;
- query and URL rows are absent from SQLite and browser responses.

BigQuery bulk totals are complete aggregate-export evidence; they need not equal top-row Search
Analytics API details. Compare only compatible dimensions and semantics.

Rollback is independent of the dashboard lane: disable the bulk timer, unload or revoke the reader
credential, and preserve immutable Parquet evidence for diagnosis. The API-to-SQLite sync and
loopback dashboard continue unchanged.
