# ADR 0006: Keep Search Console bulk export in a separate private lake

## Status

Accepted for implementation; production activation remains a separate authorization.

## Context

The Search Analytics API supports timely normalized reporting but can return top rows rather than a
complete high-dimensional result. Search Console's BigQuery export supplies complete aggregate
rows, including query and URL dimensions. Persisting those dimensions in the existing SQLite and
browser plane would contradict its privacy contract and make a small operational database absorb a
different scale and retention workload.

## Decision

Implement bulk export as a separate `gsc-bulk` CLI and schema-v1 private manifest. Read completed
property-specific BigQuery partitions with a dedicated least-privilege service account and mirror
them to immutable Parquet on a verified external filesystem. Do not implement it as a connector,
emit `MetricPoint` objects, write bulk rows to SQLite, or expose them through HTTP.

Each property declares the earliest provider date with paired export evidence and one known nonempty
identity-proof date. The application probes that date in both data tables before every sync, then
requires successful `ExportLog` evidence for both Search Console namespaces before accepting any
date. Every revision retains its `epoch_version`, complete successful log history from version zero,
and three per-partition query audits; a higher epoch is a new immutable partition. Writes use
BigQuery control totals, streaming Arrow batches, Parquet footer validation, checksums, private
staging/quarantine, `_SUCCESS`, and atomic publication.

Storage fails closed unless the configured path is beneath the exact mounted external filesystem,
the matching `/dev/disk/by-uuid/<uuid>` device and private UUID marker are present, ownership and
modes are private, symlinks are absent, and the free-space floor is met. The deployment adds systemd
mount dependencies and write-path isolation while deliberately retaining device visibility for the
direct UUID proof.

## Consequences

The company gains complete long-tail Search Console aggregate evidence for later opportunity and
wording analysis without weakening the browser database. It also assumes a new sensitive-data
asset: raw query and URL dimensions require separate reader credentials, disk protection, backup,
retention, deletion, monitoring, and incident handling. The current dashboard receives no new
BigQuery-derived metric until a later, separately reviewed semantic transformation is designed.

Google activation, IAM changes, billing exposure, timers, retention changes, and production cutover
remain operational approvals rather than consequences of merging this code.
