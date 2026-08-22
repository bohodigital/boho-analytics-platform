"""Atomic, private Parquet storage guarded by an exact mounted-filesystem identity."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from .config import (
    BulkExportManifest,
    SearchConsolePropertyConfig,
)
from .contracts import SEARCHDATA_TABLES, ExportRevision, PartitionRead, PartitionTotals


_SUPPORTED_PARQUET_COMPRESSION = frozenset({"gzip", "snappy", "zstd"})
_MINIMUM_QUERY_CEILING = 10_485_760
_MAXIMUM_QUERY_CEILING = 54_975_581_388_800


class BulkLakeError(RuntimeError):
    """Raised when the private bulk lake cannot prove a safe durable write."""


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    payload = (
        json.dumps(value, indent=2, sort_keys=True, separators=(",", ": "))
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _decimal(value: object) -> Decimal:
    candidate = Decimal(str(value if value is not None else 0))
    if not candidate.is_finite() or candidate < 0:
        raise BulkLakeError("bulk partition contains an invalid position sum")
    return candidate


class BulkLake:
    """Store immutable bulk partitions only on the verified external filesystem."""

    def __init__(self, manifest: BulkExportManifest) -> None:
        self.manifest = manifest
        self.config = manifest.storage

    def _path_chain_has_symlink(self, path: Path, stop: Path) -> bool:
        current = path
        while current != stop:
            if current.exists() and current.is_symlink():
                return True
            if current.parent == current:
                return True
            current = current.parent
        return stop.is_symlink()

    def _private_directory(self, path: Path, *, create: bool = True) -> None:
        """Create or verify lake-owned directories without permissive parents."""

        try:
            relative = path.relative_to(self.config.required_mountpoint)
        except ValueError as exc:
            raise BulkLakeError("bulk lake directory escaped the required mountpoint") from exc
        try:
            mount_device = self.config.required_mountpoint.stat().st_dev
        except OSError as exc:
            raise BulkLakeError("required bulk storage mountpoint is unavailable") from exc
        current = self.config.required_mountpoint
        for part in relative.parts:
            current = current / part
            if not current.exists():
                if not create:
                    raise BulkLakeError("bulk lake directory is missing")
                current.mkdir(mode=0o700)
                _fsync_directory(current.parent)
            value = current.lstat()
            if (
                not stat.S_ISDIR(value.st_mode)
                or current.is_symlink()
                or value.st_uid != os.geteuid()
                or stat.S_IMODE(value.st_mode) != 0o700
                or value.st_dev != mount_device
            ):
                raise BulkLakeError(
                    "bulk lake directories must be private, service-owned, and on the required filesystem"
                )

    def _private_file(self, path: Path, label: str) -> os.stat_result:
        try:
            value = path.lstat()
        except OSError as exc:
            raise BulkLakeError(f"{label} is missing") from exc
        if (
            not stat.S_ISREG(value.st_mode)
            or path.is_symlink()
            or value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_dev != self.config.root.stat().st_dev
        ):
            raise BulkLakeError(
                f"{label} must be a private regular file owned by the service user"
            )
        return value

    def _private_tree_inventory(self, path: Path) -> dict[str, object]:
        if not path.exists():
            return {
                "entries": 0,
                "directories": 0,
                "files": 0,
                "bytes": 0,
                "paths": set(),
                "directory_paths": set(),
            }
        self._private_directory(path, create=False)
        entry_count = len(tuple(path.iterdir()))
        directory_count = file_count = byte_count = 0
        paths: set[Path] = set()
        directory_paths: set[Path] = set()
        for root_name, directories, files in os.walk(path, followlinks=False):
            root = Path(root_name)
            self._private_directory(root, create=False)
            for directory in directories:
                candidate = root / directory
                self._private_directory(candidate, create=False)
                directory_count += 1
                directory_paths.add(candidate)
            for filename in files:
                candidate = root / filename
                value = self._private_file(candidate, "bulk lake file")
                file_count += 1
                byte_count += value.st_size
                paths.add(candidate)
        return {
            "entries": entry_count,
            "directories": directory_count,
            "files": file_count,
            "bytes": byte_count,
            "paths": paths,
            "directory_paths": directory_paths,
        }

    def preflight(self, *, create: bool = False) -> dict[str, object]:
        """Prove the configured lake is on the expected mounted filesystem."""

        mountpoint = self.config.required_mountpoint
        root = self.config.root
        try:
            mount_resolved = mountpoint.resolve(strict=True)
        except OSError as exc:
            raise BulkLakeError("required bulk storage mountpoint is unavailable") from exc
        if mount_resolved != mountpoint or mountpoint.is_symlink():
            raise BulkLakeError("required bulk storage mountpoint must not be a symlink")
        if not os.path.ismount(mountpoint):
            raise BulkLakeError("required bulk storage filesystem is not mounted")
        mount_stat = mountpoint.stat()
        if mount_stat.st_dev == mountpoint.parent.stat().st_dev:
            raise BulkLakeError("bulk storage mountpoint is not a separate filesystem")

        marker = mountpoint / self.config.identity_marker_name
        try:
            marker_stat = marker.lstat()
        except OSError as exc:
            raise BulkLakeError("bulk storage filesystem identity marker is missing") from exc
        if (
            not stat.S_ISREG(marker_stat.st_mode)
            or marker.is_symlink()
            or marker_stat.st_dev != mount_stat.st_dev
            or marker_stat.st_uid != os.geteuid()
            or stat.S_IMODE(marker_stat.st_mode) != 0o600
        ):
            raise BulkLakeError("bulk storage filesystem identity marker is invalid")

        device_identity = Path("/dev/disk/by-uuid") / self.config.required_filesystem_uuid
        if not device_identity.exists():
            raise BulkLakeError("required bulk storage UUID device is unavailable")
        try:
            if device_identity.stat().st_rdev != mount_stat.st_dev:
                raise BulkLakeError(
                    "mounted bulk storage device does not match the required UUID"
                )
        except OSError as exc:
            raise BulkLakeError("bulk storage device identity cannot be verified") from exc

        try:
            root.relative_to(mountpoint)
        except ValueError as exc:
            raise BulkLakeError("bulk lake root escaped the required mountpoint") from exc
        if self._path_chain_has_symlink(root, mountpoint):
            raise BulkLakeError("bulk lake path must not contain symlinks")

        if create:
            try:
                self._private_directory(root)
            except OSError as exc:
                raise BulkLakeError("bulk lake root could not be created") from exc
        if not root.is_dir():
            raise BulkLakeError("bulk lake root is unavailable")
        root_stat = root.stat()
        if root_stat.st_dev != mount_stat.st_dev:
            raise BulkLakeError("bulk lake root is not on the required filesystem")
        if root_stat.st_uid != os.geteuid() or stat.S_IMODE(root_stat.st_mode) != 0o700:
            raise BulkLakeError("bulk lake root must be private and owned by the service user")
        if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
            raise BulkLakeError("bulk lake root is not writable by the service user")

        filesystem = os.statvfs(root)
        available_bytes = filesystem.f_bavail * filesystem.f_frsize
        if available_bytes < self.config.minimum_free_bytes:
            raise BulkLakeError("bulk storage is below its configured free-space floor")
        return {
            "ok": True,
            "root": str(root),
            "mountpoint": str(mountpoint),
            "filesystem_uuid": self.config.required_filesystem_uuid,
            "available_bytes": available_bytes,
        }

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.preflight(create=True)
        lock_dir = self.config.root / "locks"
        self._private_directory(lock_dir)
        lock_path = lock_dir / "gsc-bulk.lock"
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise BulkLakeError("bulk lake lock file is invalid") from exc
        try:
            lock_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != os.geteuid()
                or stat.S_IMODE(lock_stat.st_mode) != 0o600
                or lock_stat.st_dev != self.config.root.stat().st_dev
            ):
                raise BulkLakeError("bulk lake lock file is invalid")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BulkLakeError("another Search Console bulk sync is active") from exc
            self._recover_current_temps()
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _date_root(
        self,
        property_config: SearchConsolePropertyConfig,
        revision: ExportRevision,
    ) -> Path:
        if (
            property_config not in self.manifest.properties
            or revision.namespace not in SEARCHDATA_TABLES
            or type(revision.data_date) is not date
            or isinstance(revision.epoch_version, bool)
            or not isinstance(revision.epoch_version, int)
            or revision.epoch_version < 0
            or not isinstance(revision.publish_time, datetime)
            or revision.publish_time.tzinfo is None
            or revision.publish_time.utcoffset() is None
        ):
            raise BulkLakeError("bulk partition source identity is invalid")
        return (
            self.config.root
            / "raw"
            / "v1"
            / f"site={property_config.site_id}"
            / f"table={revision.namespace}"
            / f"provider_date={revision.data_date.isoformat()}"
        )

    def partition_path(
        self,
        property_config: SearchConsolePropertyConfig,
        revision: ExportRevision,
    ) -> Path:
        return self._date_root(property_config, revision) / (
            f"epoch_version={revision.epoch_version}"
        )

    def _current_value(
        self,
        property_config: SearchConsolePropertyConfig,
        revision: ExportRevision,
    ) -> dict[str, object] | None:
        current = self._date_root(property_config, revision) / "current.json"
        try:
            current.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BulkLakeError("bulk partition current pointer is unavailable") from exc
        self._private_directory(current.parent, create=False)
        self._private_file(current, "bulk partition current pointer")
        return self._read_pointer_value(current, "bulk partition current pointer")

    @staticmethod
    def _read_pointer_value(path: Path, label: str) -> dict[str, object]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BulkLakeError(f"{label} is invalid") from exc
        if not isinstance(value, dict) or set(value) != {"epoch_version", "manifest"}:
            raise BulkLakeError(f"{label} is invalid")
        epoch = value.get("epoch_version")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise BulkLakeError(f"{label} is invalid")
        if value.get("manifest") != f"epoch_version={epoch}/manifest.json":
            raise BulkLakeError(f"{label} is invalid")
        return value

    def _quarantine_current_temp(self, path: Path) -> None:
        quarantine_parent = self.config.root / "quarantine"
        self._private_directory(quarantine_parent)
        destination = quarantine_parent / f"current-pointer-{uuid.uuid4().hex}.tmp"
        source_parent = path.parent
        os.replace(path, destination)
        _fsync_directory(source_parent)
        _fsync_directory(quarantine_parent)

    def _recover_current_temps(self) -> None:
        """Repair or quarantine interrupted current-pointer publications."""

        raw = self.config.root / "raw" / "v1"
        if not raw.exists():
            return
        self._private_directory(raw, create=False)
        temporary_files = tuple(sorted(
            raw.glob("site=*/table=*/provider_date=*/.current-*.tmp")
        ))
        date_roots = sorted({path.parent for path in temporary_files})
        for date_root in date_roots:
            property_config, namespace, data_date = self._parse_date_root(date_root)
            self._private_directory(date_root, create=False)
            completed_epochs: set[int] = set()
            for success in sorted(date_root.glob("epoch_version=*/_SUCCESS")):
                partition = success.parent
                try:
                    epoch = int(partition.name.removeprefix("epoch_version="))
                except ValueError as exc:
                    raise BulkLakeError(
                        "bulk lake contains an unrecognized partition path"
                    ) from exc
                if partition.name != f"epoch_version={epoch}" or epoch < 0:
                    raise BulkLakeError("bulk lake contains an unrecognized partition path")
                self.verify_partition(
                    property_config,
                    ExportRevision(namespace, data_date, epoch, datetime.now(UTC)),
                    match_publish_time=False,
                )
                completed_epochs.add(epoch)
            newest_epoch = max(completed_epochs) if completed_epochs else None

            recoverable: list[Path] = []
            for temporary in sorted(
                path for path in temporary_files if path.parent == date_root
            ):
                self._private_file(
                    temporary, "bulk partition temporary current pointer"
                )
                try:
                    value = self._read_pointer_value(
                        temporary, "bulk partition temporary current pointer"
                    )
                except BulkLakeError:
                    self._quarantine_current_temp(temporary)
                    continue
                if newest_epoch is not None and value["epoch_version"] == newest_epoch:
                    recoverable.append(temporary)
                else:
                    self._quarantine_current_temp(temporary)

            current_error: BulkLakeError | None = None
            try:
                current_value = self._current_value(
                    property_config,
                    ExportRevision(
                        namespace,
                        data_date,
                        newest_epoch if newest_epoch is not None else 0,
                        datetime.now(UTC),
                    ),
                )
            except BulkLakeError as exc:
                current_error = exc
                current_value = None

            if recoverable and (
                current_value is None
                or current_value["epoch_version"] != newest_epoch
            ):
                selected = recoverable.pop(0)
                os.replace(selected, date_root / "current.json")
                _fsync_directory(date_root)
                current_error = None
            for temporary in recoverable:
                self._quarantine_current_temp(temporary)
            if current_error is not None:
                raise current_error

    def current_epoch(
        self,
        property_config: SearchConsolePropertyConfig,
        revision: ExportRevision,
    ) -> int | None:
        value = self._current_value(property_config, revision)
        if value is None:
            return None
        return int(value["epoch_version"])

    def _sum_batch(self, batch: object, namespace: str) -> PartitionTotals:
        try:
            import pyarrow.compute as compute
        except ImportError as exc:
            raise BulkLakeError(
                "Parquet bulk storage requires the bigquery optional dependency"
            ) from exc
        position_field = (
            "sum_top_position"
            if namespace == "searchdata_site_impression"
            else "sum_position"
        )

        def value(field: str) -> object:
            index = batch.schema.get_field_index(field)
            if index < 0:
                raise BulkLakeError(f"bulk partition omitted required field {field}")
            column = batch.column(index)
            minimum = compute.min(column).as_py() if len(column) else None
            if minimum is not None and Decimal(str(minimum)) < 0:
                raise BulkLakeError("bulk partition contains a negative metric")
            result = compute.sum(column).as_py()
            return 0 if result is None else result

        def count(field: str) -> int:
            candidate = value(field)
            if isinstance(candidate, bool) or not isinstance(candidate, int):
                raise BulkLakeError("bulk partition contains a non-integral count")
            return candidate

        return PartitionTotals(
            row_count=int(batch.num_rows),
            clicks=count("clicks"),
            impressions=count("impressions"),
            position_sum=_decimal(value(position_field)),
        )

    @staticmethod
    def _add_totals(left: PartitionTotals, right: PartitionTotals) -> PartitionTotals:
        return PartitionTotals(
            row_count=left.row_count + right.row_count,
            clicks=left.clicks + right.clicks,
            impressions=left.impressions + right.impressions,
            position_sum=left.position_sum + right.position_sum,
        )

    def _verify_totals(self, actual: PartitionTotals, expected: PartitionTotals) -> None:
        if actual != expected:
            raise BulkLakeError("downloaded partition totals did not match BigQuery controls")

    def write_partition(
        self,
        property_config: SearchConsolePropertyConfig,
        revision: ExportRevision,
        partition_read: PartitionRead,
    ) -> dict[str, object]:
        """Write one immutable epoch and publish it only after full validation."""

        self.preflight(create=True)
        final_path = self.partition_path(property_config, revision)
        if final_path.exists():
            manifest = self.verify_partition(property_config, revision)
            current_epoch = self.current_epoch(property_config, revision)
            if current_epoch is not None and current_epoch > revision.epoch_version:
                raise BulkLakeError(
                    "refusing to replace a newer bulk partition current pointer"
                )
            if current_epoch != revision.epoch_version:
                self._publish_current(property_config, revision, final_path)
            return {"status": "existing", **manifest}

        staging_parent = self.config.root / "staging"
        quarantine_parent = self.config.root / "quarantine"
        self._private_directory(staging_parent)
        self._private_directory(quarantine_parent)
        staging = staging_parent / f"gsc-{uuid.uuid4().hex}"
        self._private_directory(staging)
        part_path = staging / "part-00000.parquet"
        writer = None
        schema = None
        totals = PartitionTotals(0, 0, 0, Decimal(0))
        try:
            try:
                import pyarrow.parquet as parquet
            except ImportError as exc:
                raise BulkLakeError(
                    "Parquet bulk storage requires the bigquery optional dependency"
                ) from exc
            for batch in partition_read.batches:
                if not hasattr(batch, "schema") or not hasattr(batch, "num_rows"):
                    raise BulkLakeError("BigQuery returned an invalid Arrow batch")
                if schema is None:
                    schema = batch.schema
                    descriptor = os.open(
                        part_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    os.close(descriptor)
                    writer = parquet.ParquetWriter(
                        part_path,
                        schema,
                        compression=self.config.parquet_compression,
                        use_dictionary=True,
                        write_statistics=True,
                    )
                elif not batch.schema.equals(schema, check_metadata=False):
                    raise BulkLakeError("BigQuery changed schema within one partition")
                writer.write_batch(batch)
                totals = self._add_totals(
                    totals, self._sum_batch(batch, revision.namespace)
                )
            if writer is not None:
                writer.close()
                writer = None
                os.chmod(part_path, 0o600)
                with part_path.open("rb") as handle:
                    os.fsync(handle.fileno())
            self._verify_totals(totals, partition_read.expected_totals)

            files: list[dict[str, object]] = []
            if part_path.is_file():
                parquet_file = parquet.ParquetFile(part_path)
                if parquet_file.metadata.num_rows != totals.row_count:
                    raise BulkLakeError("Parquet footer row count did not match controls")
                files.append(
                    {
                        "name": part_path.name,
                        "bytes": part_path.stat().st_size,
                        "sha256": _sha256(part_path),
                    }
                )
            source_schema = self._normalized_source_schema(
                partition_read.source_schema
            )
            query_audit = self._normalized_query_audit(
                partition_read.query_audit,
                self.manifest.warehouse.maximum_bytes_billed,
            )
            export_log_history = self._normalized_export_log(
                partition_read.export_log_history,
                revision,
                revision.publish_time,
            )
            manifest_value: dict[str, object] = {
                "schema_version": 1,
                "source": "google-search-console-bigquery",
                "project_id": self.manifest.warehouse.project_id,
                "dataset_id": property_config.dataset_id,
                "site_id": property_config.site_id,
                "site_url": property_config.site_url,
                "namespace": revision.namespace,
                "data_date": revision.data_date.isoformat(),
                "provider_timezone": "America/Los_Angeles",
                "epoch_version": revision.epoch_version,
                "publish_time": revision.publish_time.astimezone(UTC).isoformat(),
                "created_at": datetime.now(UTC).isoformat(),
                "format": "parquet",
                "compression": self.config.parquet_compression,
                "totals": totals.json_value(),
                "source_schema": source_schema,
                "queries": query_audit,
                "maximum_bytes_billed": self.manifest.warehouse.maximum_bytes_billed,
                "export_log": export_log_history,
                "files": files,
            }
            _write_json(staging / "manifest.json", manifest_value)
            success = staging / "_SUCCESS"
            descriptor = os.open(success, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.fsync(descriptor)
            os.close(descriptor)
            _fsync_directory(staging)

            self._private_directory(final_path.parent)
            os.replace(staging, final_path)
            _fsync_directory(staging_parent)
            _fsync_directory(final_path.parent)
            self._publish_current(property_config, revision, final_path)
            return {"status": "written", **manifest_value}
        except Exception as original:
            cleanup_failed = False
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    cleanup_failed = True
            if staging.exists():
                try:
                    quarantine = quarantine_parent / staging.name
                    os.replace(staging, quarantine)
                    _fsync_directory(staging_parent)
                    _fsync_directory(quarantine_parent)
                except Exception:
                    cleanup_failed = True
            if cleanup_failed:
                raise BulkLakeError(
                    "bulk partition failed and could not be completely quarantined"
                ) from original
            raise

    def _publish_current(
        self,
        property_config: SearchConsolePropertyConfig,
        revision: ExportRevision,
        final_path: Path,
    ) -> None:
        date_root = self._date_root(property_config, revision)
        temporary = date_root / f".current-{uuid.uuid4().hex}.tmp"
        _write_json(
            temporary,
            {
                "epoch_version": revision.epoch_version,
                "manifest": str((final_path / "manifest.json").relative_to(date_root)),
            },
        )
        os.replace(temporary, date_root / "current.json")
        _fsync_directory(date_root)

    @staticmethod
    def _manifest_totals(value: object) -> PartitionTotals:
        if not isinstance(value, dict) or set(value) != {
            "row_count", "clicks", "impressions", "position_sum"
        }:
            raise BulkLakeError("bulk partition manifest totals are invalid")
        counts: list[int] = []
        for field in ("row_count", "clicks", "impressions"):
            candidate = value[field]
            if (
                isinstance(candidate, bool)
                or not isinstance(candidate, int)
                or candidate < 0
            ):
                raise BulkLakeError("bulk partition manifest totals are invalid")
            counts.append(candidate)
        if not isinstance(value["position_sum"], str):
            raise BulkLakeError("bulk partition manifest totals are invalid")
        return PartitionTotals(
            row_count=counts[0],
            clicks=counts[1],
            impressions=counts[2],
            position_sum=_decimal(value["position_sum"]),
        )

    @staticmethod
    def _valid_job_id(value: object) -> bool:
        return (
            isinstance(value, str)
            and 1 <= len(value) <= 1024
            and all(character.isalnum() or character in "_-" for character in value)
        )

    @staticmethod
    def _normalized_source_schema(value: object) -> list[dict[str, object]]:
        def normalize_level(items: object, depth: int) -> list[dict[str, object]]:
            if (
                not isinstance(items, (list, tuple))
                or not items
                or len(items) > 4096
                or depth > 8
            ):
                raise BulkLakeError("bulk partition source schema is invalid")
            normalized: list[dict[str, object]] = []
            names: set[str] = set()
            for item in items:
                if not isinstance(item, Mapping) or set(item) != {
                    "name", "type", "mode", "fields"
                }:
                    raise BulkLakeError("bulk partition source schema is invalid")
                name = item["name"]
                field_type = item["type"]
                mode = item["mode"]
                children = item["fields"]
                if (
                    not isinstance(name, str)
                    or not name
                    or len(name) > 1024
                    or name in names
                    or not isinstance(field_type, str)
                    or not field_type
                    or len(field_type) > 128
                    or not isinstance(mode, str)
                    or not mode
                    or len(mode) > 128
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for text in (name, field_type, mode)
                        for character in text
                    )
                    or not isinstance(children, (list, tuple))
                ):
                    raise BulkLakeError("bulk partition source schema is invalid")
                names.add(name)
                normalized.append({
                    "name": name,
                    "type": field_type,
                    "mode": mode,
                    "fields": normalize_level(children, depth + 1) if children else [],
                })
            return normalized

        return normalize_level(value, 0)

    def _normalized_query_audit(
        self, value: object, maximum_bytes_billed: int
    ) -> list[dict[str, object]]:
        expected_roles = {
            "partition-controls", "partition-data", "export-log-post-read"
        }
        if not isinstance(value, (list, tuple)) or len(value) != len(expected_roles):
            raise BulkLakeError("bulk partition query audit is invalid")
        normalized: list[dict[str, object]] = []
        observed_roles: set[str] = set()
        for item in value:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"role", "job_id", "bytes_processed", "bytes_billed"}
                or item["role"] not in expected_roles
                or item["role"] in observed_roles
                or not self._valid_job_id(item["job_id"])
            ):
                raise BulkLakeError("bulk partition query audit is invalid")
            observed_roles.add(str(item["role"]))
            for metric in ("bytes_processed", "bytes_billed"):
                if (
                    isinstance(item[metric], bool)
                    or not isinstance(item[metric], int)
                    or item[metric] < 0
                ):
                    raise BulkLakeError("bulk partition query audit is invalid")
            if item["bytes_billed"] > maximum_bytes_billed:
                raise BulkLakeError("bulk partition query audit exceeded its recorded ceiling")
            normalized.append(dict(item))
        if observed_roles != expected_roles:
            raise BulkLakeError("bulk partition query audit is invalid")
        return normalized

    @staticmethod
    def _normalized_export_log(
        value: object,
        revision: ExportRevision,
        expected_publish_time: datetime,
    ) -> list[dict[str, object]]:
        if (
            not isinstance(value, (list, tuple))
            or len(value) != revision.epoch_version + 1
        ):
            raise BulkLakeError("bulk partition ExportLog history is invalid")
        normalized: list[dict[str, object]] = []
        publish_times: list[datetime] = []
        for expected_epoch, item in enumerate(value):
            if (
                not isinstance(item, Mapping)
                or set(item) != {
                    "agenda", "namespace", "data_date", "epoch_version", "publish_time"
                }
                or item["agenda"] != "SEARCHDATA"
                or item["namespace"] != revision.namespace
                or item["data_date"] != revision.data_date.isoformat()
                or isinstance(item["epoch_version"], bool)
                or not isinstance(item["epoch_version"], int)
                or item["epoch_version"] != expected_epoch
            ):
                raise BulkLakeError("bulk partition ExportLog history is invalid")
            try:
                published = datetime.fromisoformat(item["publish_time"])
            except (TypeError, ValueError) as exc:
                raise BulkLakeError("bulk partition ExportLog history is invalid") from exc
            if published.tzinfo is None or published.utcoffset() is None:
                raise BulkLakeError("bulk partition ExportLog history is invalid")
            published = published.astimezone(UTC)
            publish_times.append(published)
            normalized.append({
                "agenda": "SEARCHDATA",
                "namespace": revision.namespace,
                "data_date": revision.data_date.isoformat(),
                "epoch_version": expected_epoch,
                "publish_time": published.isoformat(),
            })
        if publish_times[-1] != expected_publish_time.astimezone(UTC):
            raise BulkLakeError("bulk partition ExportLog history is invalid")
        return normalized

    def _parse_date_root(
        self, date_root: Path
    ) -> tuple[SearchConsolePropertyConfig, str, date]:
        raw = self.config.root / "raw" / "v1"
        if date_root.parent.parent.parent != raw:
            raise BulkLakeError("bulk lake contains an unrecognized partition path")
        site_part = date_root.parent.parent.name
        table_part = date_root.parent.name
        date_part = date_root.name
        if (
            not site_part.startswith("site=")
            or not table_part.startswith("table=")
            or not date_part.startswith("provider_date=")
        ):
            raise BulkLakeError("bulk lake contains an unrecognized partition path")
        site_id = site_part.removeprefix("site=")
        namespace = table_part.removeprefix("table=")
        if namespace not in SEARCHDATA_TABLES:
            raise BulkLakeError("bulk lake contains an unrecognized partition table")
        try:
            data_date = date.fromisoformat(date_part.removeprefix("provider_date="))
            property_config = next(
                item for item in self.manifest.properties if item.site_id == site_id
            )
        except (StopIteration, ValueError) as exc:
            raise BulkLakeError("bulk lake contains an unrecognized partition path") from exc
        if date_part != f"provider_date={data_date.isoformat()}":
            raise BulkLakeError("bulk lake contains an unrecognized partition path")
        return property_config, namespace, data_date

    def verify_partition(
        self,
        property_config: SearchConsolePropertyConfig,
        revision: ExportRevision,
        *,
        match_publish_time: bool = True,
    ) -> dict[str, object]:
        path = self.partition_path(property_config, revision)
        self._private_directory(path, create=False)
        success_path = path / "_SUCCESS"
        manifest_path = path / "manifest.json"
        success_stat = self._private_file(success_path, "bulk partition success marker")
        self._private_file(manifest_path, "bulk partition manifest")
        if success_stat.st_size != 0:
            raise BulkLakeError("bulk partition success marker is invalid")
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BulkLakeError("bulk partition manifest is invalid") from exc
        expected_fields = {
            "schema_version", "source", "project_id", "dataset_id", "site_id",
            "site_url", "namespace", "data_date", "provider_timezone",
            "epoch_version", "publish_time", "created_at", "format", "compression",
            "totals", "source_schema", "queries", "maximum_bytes_billed",
            "export_log", "files",
        }
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise BulkLakeError("bulk partition manifest is invalid")
        expected_identity = {
            "schema_version": 1,
            "source": "google-search-console-bigquery",
            "project_id": self.manifest.warehouse.project_id,
            "site_id": property_config.site_id,
            "dataset_id": property_config.dataset_id,
            "site_url": property_config.site_url,
            "namespace": revision.namespace,
            "data_date": revision.data_date.isoformat(),
            "provider_timezone": "America/Los_Angeles",
            "epoch_version": revision.epoch_version,
            "format": "parquet",
        }
        if any(value.get(key) != expected for key, expected in expected_identity.items()):
            raise BulkLakeError("bulk partition manifest identity does not match its path")
        stored_compression = value["compression"]
        if (
            not isinstance(stored_compression, str)
            or stored_compression not in _SUPPORTED_PARQUET_COMPRESSION
        ):
            raise BulkLakeError("bulk partition compression lineage is invalid")
        stored_query_ceiling = value["maximum_bytes_billed"]
        if (
            isinstance(stored_query_ceiling, bool)
            or not isinstance(stored_query_ceiling, int)
            or not _MINIMUM_QUERY_CEILING
            <= stored_query_ceiling
            <= _MAXIMUM_QUERY_CEILING
        ):
            raise BulkLakeError("bulk partition query ceiling lineage is invalid")
        parsed_timestamps: dict[str, datetime] = {}
        for timestamp_field in ("publish_time", "created_at"):
            try:
                parsed_timestamp = datetime.fromisoformat(value[timestamp_field])
            except (TypeError, ValueError) as exc:
                raise BulkLakeError("bulk partition manifest timestamp is invalid") from exc
            if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
                raise BulkLakeError("bulk partition manifest timestamp is invalid")
            parsed_timestamps[timestamp_field] = parsed_timestamp.astimezone(UTC)
        if match_publish_time:
            if (
                revision.publish_time.tzinfo is None
                or revision.publish_time.utcoffset() is None
                or parsed_timestamps["publish_time"]
                != revision.publish_time.astimezone(UTC)
            ):
                raise BulkLakeError(
                    "bulk partition publish time does not match the source revision"
                )

        source_schema = self._normalized_source_schema(value["source_schema"])
        source_names = [str(field["name"]) for field in source_schema]
        self._normalized_query_audit(value["queries"], stored_query_ceiling)
        self._normalized_export_log(
            value["export_log"],
            revision,
            parsed_timestamps["publish_time"],
        )

        files = value.get("files")
        if not isinstance(files, list):
            raise BulkLakeError("bulk partition manifest files are invalid")
        actual_totals = PartitionTotals(0, 0, 0, Decimal(0))
        declared_names: set[str] = set()
        try:
            import pyarrow.parquet as parquet
        except ImportError as exc:
            raise BulkLakeError(
                "Parquet verification requires the bigquery optional dependency"
            ) from exc
        for item in files:
            if not isinstance(item, dict) or set(item) != {"name", "bytes", "sha256"}:
                raise BulkLakeError("bulk partition file declaration is invalid")
            name = item["name"]
            if (
                not isinstance(name, str)
                or Path(name).name != name
                or not name.endswith(".parquet")
                or name in declared_names
                or isinstance(item["bytes"], bool)
                or not isinstance(item["bytes"], int)
                or item["bytes"] < 0
                or not isinstance(item["sha256"], str)
                or len(item["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in item["sha256"])
            ):
                raise BulkLakeError("bulk partition file name is invalid")
            declared_names.add(name)
            file_path = path / name
            file_stat = self._private_file(file_path, "bulk partition Parquet file")
            if (
                file_stat.st_size != item["bytes"]
                or _sha256(file_path) != item["sha256"]
            ):
                raise BulkLakeError("bulk partition file checksum did not verify")
            parquet_file = parquet.ParquetFile(file_path)
            if list(parquet_file.schema_arrow.names) != source_names:
                raise BulkLakeError("Parquet schema does not match the recorded source schema")
            for row_group_index in range(parquet_file.metadata.num_row_groups):
                row_group = parquet_file.metadata.row_group(row_group_index)
                for column_index in range(row_group.num_columns):
                    codec = row_group.column(column_index).compression
                    if (
                        not isinstance(codec, str)
                        or codec.casefold() != stored_compression
                    ):
                        raise BulkLakeError(
                            "Parquet compression does not match its recorded lineage"
                        )
            file_totals = PartitionTotals(0, 0, 0, Decimal(0))
            for batch in parquet_file.iter_batches(batch_size=self.config.batch_rows):
                file_totals = self._add_totals(
                    file_totals, self._sum_batch(batch, revision.namespace)
                )
            if parquet_file.metadata.num_rows != file_totals.row_count:
                raise BulkLakeError("Parquet footer row count did not verify")
            actual_totals = self._add_totals(actual_totals, file_totals)
        actual_entries = {item.name for item in path.iterdir()}
        if actual_entries != {"manifest.json", "_SUCCESS", *declared_names}:
            raise BulkLakeError("bulk partition contains an undeclared file")
        expected_totals = self._manifest_totals(value.get("totals"))
        if actual_totals != expected_totals:
            raise BulkLakeError("bulk partition manifest totals did not verify")
        return value

    def verify_all(self) -> dict[str, object]:
        self.preflight(create=False)
        raw = self.config.root / "raw" / "v1"
        if not raw.exists():
            return {
                "ok": True,
                "partitions": 0,
                "current_pointers": 0,
                "rows": 0,
                "bytes": 0,
                "staging": self._inventory_summary(
                    self._private_tree_inventory(self.config.root / "staging")
                ),
                "quarantine": self._inventory_summary(
                    self._private_tree_inventory(self.config.root / "quarantine")
                ),
            }
        raw_inventory = self._private_tree_inventory(raw)
        partitions = rows = byte_count = 0
        groups: dict[Path, list[int]] = {}
        declared_files: set[Path] = set()
        declared_directories: set[Path] = set()
        for success in sorted(raw.glob("site=*/table=*/provider_date=*/epoch_version=*/_SUCCESS")):
            path = success.parent
            try:
                property_config, namespace, data_date = self._parse_date_root(path.parent)
                epoch = int(path.name.removeprefix("epoch_version="))
                if path.name != f"epoch_version={epoch}" or epoch < 0:
                    raise ValueError
                value = self.verify_partition(
                    property_config,
                    ExportRevision(namespace, data_date, epoch, datetime.now(UTC)),
                    match_publish_time=False,
                )
            except ValueError as exc:
                raise BulkLakeError("bulk lake contains an unrecognized partition path") from exc
            groups.setdefault(path.parent, []).append(epoch)
            declared_files.update(item for item in path.iterdir() if item.is_file())
            current_directory = path
            while current_directory != raw:
                declared_directories.add(current_directory)
                current_directory = current_directory.parent
            partitions += 1
            rows += int(value["totals"]["row_count"])
            byte_count += sum(int(item["bytes"]) for item in value["files"])

        current_files = tuple(sorted(raw.glob("site=*/table=*/provider_date=*/current.json")))
        if {path.parent for path in current_files} != set(groups):
            raise BulkLakeError("bulk lake current pointers do not cover completed partitions")
        for current in current_files:
            property_config, namespace, data_date = self._parse_date_root(current.parent)
            expected_epoch = max(groups[current.parent])
            revision = ExportRevision(
                namespace, data_date, expected_epoch, datetime.now(UTC)
            )
            value = self._current_value(property_config, revision)
            if value is None or value["epoch_version"] != expected_epoch:
                raise BulkLakeError("bulk lake current pointer does not select the newest epoch")
            declared_files.add(current)
        if (
            raw_inventory["paths"] != declared_files
            or raw_inventory["directory_paths"] != declared_directories
        ):
            raise BulkLakeError("bulk lake contains an unrecognized raw path")
        return {
            "ok": True,
            "partitions": partitions,
            "current_pointers": len(current_files),
            "rows": rows,
            "bytes": byte_count,
            "staging": self._inventory_summary(
                self._private_tree_inventory(self.config.root / "staging")
            ),
            "quarantine": self._inventory_summary(
                self._private_tree_inventory(self.config.root / "quarantine")
            ),
        }

    @staticmethod
    def _inventory_summary(value: dict[str, object]) -> dict[str, int]:
        return {
            key: int(value[key])
            for key in ("entries", "directories", "files", "bytes")
        }

    def status(self) -> dict[str, object]:
        storage = self.preflight(create=False)
        sites: list[dict[str, object]] = []
        for property_config in self.manifest.properties:
            base = self.config.root / "raw" / "v1" / f"site={property_config.site_id}"
            current_files = (
                tuple(base.glob("table=*/provider_date=*/current.json"))
                if base.exists()
                else ()
            )
            dates_by_namespace = {namespace: set() for namespace in SEARCHDATA_TABLES}
            for current in current_files:
                parsed_property, namespace, data_date = self._parse_date_root(current.parent)
                if parsed_property != property_config:
                    raise BulkLakeError("bulk lake current pointer has the wrong site")
                revision = ExportRevision(namespace, data_date, 0, datetime.now(UTC))
                value = self._current_value(property_config, revision)
                if value is None:
                    raise BulkLakeError("bulk lake current pointer is missing")
                selected = ExportRevision(
                    namespace,
                    data_date,
                    int(value["epoch_version"]),
                    datetime.now(UTC),
                )
                selected_path = self.partition_path(property_config, selected)
                self._private_directory(selected_path, create=False)
                self._private_file(
                    selected_path / "manifest.json", "bulk partition manifest"
                )
                self._private_file(
                    selected_path / "_SUCCESS", "bulk partition success marker"
                )
                dates_by_namespace[namespace].add(data_date)
            paired_dates = set.intersection(*dates_by_namespace.values())
            union_dates = set.union(*dates_by_namespace.values())
            complete_through = None
            cursor = property_config.first_export_date
            while cursor in paired_dates:
                complete_through = cursor
                cursor += timedelta(days=1)
            sites.append(
                {
                    "site_id": property_config.site_id,
                    "first_export_date": property_config.first_export_date.isoformat(),
                    "namespaces": {
                        namespace: {
                            "current_dates": len(dates),
                            "latest_data_date": max(dates).isoformat() if dates else None,
                        }
                        for namespace, dates in dates_by_namespace.items()
                    },
                    "paired_dates": len(paired_dates),
                    "unpaired_dates": len(union_dates - paired_dates),
                    "latest_paired_data_date": (
                        max(paired_dates).isoformat() if paired_dates else None
                    ),
                    "continuous_through": (
                        complete_through.isoformat() if complete_through else None
                    ),
                }
            )
        return {
            "ok": True,
            "storage": storage,
            "sites": sites,
            "staging": self._inventory_summary(
                self._private_tree_inventory(self.config.root / "staging")
            ),
            "quarantine": self._inventory_summary(
                self._private_tree_inventory(self.config.root / "quarantine")
            ),
        }
