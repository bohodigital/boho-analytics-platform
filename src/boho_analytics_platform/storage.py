"""SQLite metric store with migrations, leases, ledgers, and recovery helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Callable

from .contracts import (
    PAGEVIEW_ACQUISITION_SOURCES,
    PAGEVIEW_DATA_RESULT_KIND,
    PAGEVIEW_EMPTY_RESULT_KIND,
    explicit_pageview_result_kind,
    mark_pageview_result_kind,
    public_result_kind,
)
from .models import (
    AcquisitionBatch,
    AcquisitionSlice,
    AnalyticsDefinition,
    CapabilitySnapshot,
    Completeness,
    DefinitionActivation,
    DefinitionChange,
    DefinitionIdentity,
    DefinitionPackageResult,
    DefinitionType,
    DefinitionValidationError,
    DefinitionVersion,
    MetricPoint,
    QueryWindow,
    TimeGrain,
    ValidatedDefinition,
    _validate_persisted_analytics_definition,
    validate_analytics_definition,
    validate_definition_identity,
)


SCHEMA_VERSION = 8
MIGRATIONS = {
    1: "001_initial.sql",
    2: "002_site_graph.sql",
    3: "003_sync_coverage.sql",
    4: "004_forms_evidence_v3.sql",
    5: "005_analytics_definitions.sql",
    6: "006_acquisition_provenance.sql",
    7: "007_index_coverage.sql",
    8: "008_page_intelligence.sql",
}
CURRENT_IDENTITY_VERSIONS = {
    "cloudflare-forms": 3,
    "forms-inbox": 3,
    "search-console": 2,
    "umami": 2,
}


def _apply_migration(db: sqlite3.Connection, migration: str, version: int) -> None:
    """Apply DDL and its schema-version update as one rollback-safe unit."""
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("migration version must be a positive integer")
    script = (
        "BEGIN IMMEDIATE;\n"
        f"{migration}\n"
        "DELETE FROM schema_meta;\n"
        f"INSERT INTO schema_meta(version) VALUES ({version});\n"
        "COMMIT;"
    )
    try:
        db.executescript(script)
    except sqlite3.Error:
        if db.in_transaction:
            db.rollback()
        raise


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _key_at_identity_version(point: MetricPoint, identity_version: int) -> str:
    identity = [
        point.client_id, point.site_id, point.source, point.metric, point.unit,
        _iso(point.start), _iso(point.end), point.grain.value, list(point.dimensions)
    ]
    if identity_version != 1:
        identity.append(identity_version)
    identity = json.dumps(identity, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _key(point: MetricPoint) -> str:
    return _key_at_identity_version(
        point, CURRENT_IDENTITY_VERSIONS.get(point.source, 1)
    )


def _digest_fields(fields: Sequence[object]) -> str:
    """Hash an unambiguous, runtime-stable sequence of persisted fields."""

    digest = hashlib.sha256()
    for field in fields:
        if field is None:
            encoded = b"n"
        elif isinstance(field, bool):
            encoded = b"b1" if field else b"b0"
        elif isinstance(field, int):
            encoded = b"i" + str(field).encode("ascii")
        elif isinstance(field, str):
            encoded = b"s" + field.encode("utf-8")
        elif isinstance(field, bytes):
            encoded = b"y" + field
        else:
            raise TypeError(
                f"unsupported persisted hash field type: {type(field).__name__}"
            )
        digest.update(len(encoded).to_bytes(8, byteorder="big", signed=False))
        digest.update(encoded)
    return digest.hexdigest()


def _definition_schema_rows(
    db: sqlite3.Connection,
) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        tuple(row)
        for row in db.execute(
            """SELECT type,name,tbl_name,sql
                 FROM sqlite_master
                WHERE name GLOB 'analytics_definition_*'
                  AND sql IS NOT NULL
                ORDER BY type,name"""
        ).fetchall()
    )


@lru_cache(maxsize=1)
def _canonical_definition_schema_rows(
) -> tuple[tuple[str, str, str, str], ...]:
    migration = (
        files("boho_analytics_platform.migrations")
        .joinpath(MIGRATIONS[5])
        .read_text(encoding="utf-8")
    )
    with closing(sqlite3.connect(":memory:")) as canonical:
        canonical.executescript(migration)
        return _definition_schema_rows(canonical)


def _acquisition_schema_rows(
    db: sqlite3.Connection,
) -> tuple[tuple[str, str, str, str], ...]:
    names = {
        "acquisition_slices",
        "acquisition_slices_run",
        "acquisition_slices_no_update",
        "acquisition_slices_no_delete",
        "metric_fact_observations",
        "metric_fact_observations_point_history",
        "metric_fact_observations_run",
        "metric_fact_observations_no_update",
        "metric_fact_observations_no_delete",
    }
    placeholders = ",".join("?" for _ in names)
    return tuple(
        tuple(row)
        for row in db.execute(
            f"""SELECT type,name,tbl_name,sql
                  FROM sqlite_master
                 WHERE name IN ({placeholders})
                   AND sql IS NOT NULL
                 ORDER BY type,name""",
            sorted(names),
        ).fetchall()
    )


@lru_cache(maxsize=1)
def _canonical_acquisition_schema_rows(
) -> tuple[tuple[str, str, str, str], ...]:
    migration = (
        files("boho_analytics_platform.migrations")
        .joinpath(MIGRATIONS[6])
        .read_text(encoding="utf-8")
    )
    with closing(sqlite3.connect(":memory:")) as canonical:
        canonical.executescript(migration)
        return _acquisition_schema_rows(canonical)


def _transaction_time(value: datetime | None = None) -> datetime:
    instant = value or datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("definition transaction timestamp must be timezone-aware")
    return instant.astimezone(UTC)


class DefinitionCollisionError(RuntimeError):
    pass


class DefinitionNotFoundError(LookupError):
    pass


class DefinitionNotActiveError(LookupError):
    pass


class DefinitionIntegrityError(RuntimeError):
    pass


class AcquisitionIntegrityError(RuntimeError):
    pass


class PageIntelligenceIntegrityError(RuntimeError):
    pass


class LockBusy(RuntimeError):
    pass


class SQLiteMetricStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self, *, readonly: bool = False):
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        target = f"file:{self.path.as_posix()}?mode=ro" if readonly else str(self.path)
        connection = sqlite3.connect(target, uri=readonly, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        if not readonly:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
        try:
            yield connection
            if not readonly:
                connection.commit()
        except Exception:
            if not readonly:
                connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            has_meta = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
            row = db.execute("SELECT version FROM schema_meta LIMIT 1").fetchone() if has_meta else None
            current = int(row[0]) if row else 0
            if current > SCHEMA_VERSION:
                raise RuntimeError(f"database schema {current} is newer than supported {SCHEMA_VERSION}")
            for version in range(current + 1, SCHEMA_VERSION + 1):
                migration = files("boho_analytics_platform.migrations").joinpath(MIGRATIONS[version]).read_text(encoding="utf-8")
                _apply_migration(db, migration, version)

    def begin_index_coverage_inventory(
        self,
        site_id: str,
        inventory_hash: str,
        url_hashes: Sequence[str],
        observed_at: datetime,
    ) -> None:
        """Replace current sitemap membership without retaining URL text."""

        unique_hashes = tuple(sorted(set(url_hashes)))
        if len(unique_hashes) != len(url_hashes):
            raise ValueError("index inventory contains duplicate URL hashes")
        if not re.fullmatch(r"[0-9a-f]{64}", inventory_hash):
            raise ValueError("invalid index inventory hash")
        if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in unique_hashes):
            raise ValueError("invalid index inventory URL hash")
        observed = _iso(observed_at)
        with self.connect() as db:
            db.execute(
                """INSERT INTO index_coverage_inventories(
                         site_id,inventory_hash,published_pages,observed_at
                       ) VALUES (?,?,?,?)
                       ON CONFLICT(site_id) DO UPDATE SET
                         inventory_hash=excluded.inventory_hash,
                         published_pages=excluded.published_pages,
                         observed_at=excluded.observed_at""",
                (site_id, inventory_hash, len(unique_hashes), observed),
            )
            db.executemany(
                """INSERT INTO index_coverage_url_status(
                         site_id,url_hash,inventory_hash,last_seen_at
                       ) VALUES (?,?,?,?)
                       ON CONFLICT(site_id,url_hash) DO UPDATE SET
                         inventory_hash=excluded.inventory_hash,
                         last_seen_at=excluded.last_seen_at""",
                (
                    (site_id, url_hash, inventory_hash, observed)
                    for url_hash in unique_hashes
                ),
            )

    def pending_index_coverage_hashes(
        self,
        site_id: str,
        inventory_hash: str,
        *,
        refresh_before: datetime,
        limit: int,
    ) -> tuple[str, ...]:
        if limit < 0:
            raise ValueError("index inspection limit cannot be negative")
        with self.connect(readonly=True) as db:
            rows = db.execute(
                """SELECT url_hash
                     FROM index_coverage_url_status
                    WHERE site_id=? AND inventory_hash=?
                      AND (inspected_at IS NULL OR inspected_at < ?)
                    ORDER BY inspected_at IS NOT NULL, inspected_at, url_hash
                    LIMIT ?""",
                (site_id, inventory_hash, _iso(refresh_before), limit),
            ).fetchall()
        return tuple(row["url_hash"] for row in rows)

    def record_index_coverage_inspection(
        self,
        site_id: str,
        inventory_hash: str,
        url_hash: str,
        verdict: str,
        inspected_at: datetime,
    ) -> None:
        if verdict not in {
            "PASS", "FAIL", "NEUTRAL", "UNKNOWN", "VERDICT_UNSPECIFIED"
        }:
            raise ValueError("invalid URL Inspection verdict")
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE index_coverage_url_status
                      SET verdict=?,indexed=?,inspected_at=?
                    WHERE site_id=? AND inventory_hash=? AND url_hash=?""",
                (
                    verdict,
                    1 if verdict == "PASS" else 0,
                    _iso(inspected_at),
                    site_id,
                    inventory_hash,
                    url_hash,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("URL is not in the current index inventory")

    def start_index_coverage_run(
        self, site_id: str, connection_id: str, started_at: datetime | None = None
    ) -> str:
        run_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                """INSERT INTO index_coverage_runs(
                         id,site_id,connection_id,started_at,status
                       ) VALUES (?,?,?,?, 'running')""",
                (run_id, site_id, connection_id, _iso(started_at or datetime.now(UTC))),
            )
        return run_id

    def finish_index_coverage_run(
        self,
        run_id: str,
        status: str,
        *,
        published_pages: int | None = None,
        inspected_this_run: int = 0,
        error_category: str | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        if status not in {"complete", "partial", "failed"}:
            raise ValueError("invalid index coverage run status")
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE index_coverage_runs
                      SET finished_at=?,status=?,published_pages=?,
                          inspected_this_run=?,error_category=?
                    WHERE id=? AND status='running'""",
                (
                    _iso(finished_at or datetime.now(UTC)),
                    status,
                    published_pages,
                    inspected_this_run,
                    error_category,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("index coverage run is not active")

    def mark_abandoned_index_coverage_runs(
        self, finished_at: datetime | None = None
    ) -> int:
        """Close runs left active after a process interruption."""

        with self.connect() as db:
            cursor = db.execute(
                """UPDATE index_coverage_runs
                      SET finished_at=?,status='partial',error_category='interrupted'
                    WHERE status='running'""",
                (_iso(finished_at or datetime.now(UTC)),),
            )
            return cursor.rowcount

    def query_index_coverage(
        self,
        site_ids: Sequence[str],
        *,
        fresh_after: datetime | None = None,
    ) -> list[dict[str, object]]:
        """Return current census facts; totals are withheld until fully inspected."""

        if not site_ids:
            return []
        cutoff = fresh_after or datetime.now(UTC) - timedelta(days=30)
        placeholders = ",".join("?" for _ in site_ids)
        with self.connect(readonly=True) as db:
            inventories = {
                row["site_id"]: row
                for row in db.execute(
                    f"""SELECT site_id,inventory_hash,published_pages,observed_at
                          FROM index_coverage_inventories
                         WHERE site_id IN ({placeholders})""",
                    tuple(site_ids),
                ).fetchall()
            }
            counts = {
                row["site_id"]: row
                for row in db.execute(
                    f"""SELECT s.site_id,
                                COUNT(*) AS inventory_rows,
                                SUM(CASE WHEN s.inspected_at >= ? THEN 1 ELSE 0 END)
                                  AS fresh_inspected,
                                SUM(CASE WHEN s.inspected_at >= ? AND s.indexed=1 THEN 1 ELSE 0 END)
                                  AS fresh_indexed,
                                MIN(CASE WHEN s.inspected_at >= ? THEN s.inspected_at END)
                                  AS oldest_fresh_inspection,
                                MAX(s.inspected_at) AS latest_inspection
                           FROM index_coverage_url_status AS s
                           JOIN index_coverage_inventories AS i
                             ON i.site_id=s.site_id AND i.inventory_hash=s.inventory_hash
                          WHERE s.site_id IN ({placeholders})
                          GROUP BY s.site_id""",
                    (_iso(cutoff), _iso(cutoff), _iso(cutoff), *site_ids),
                ).fetchall()
            }
            latest_runs = {
                row["site_id"]: row
                for row in db.execute(
                    f"""SELECT r.site_id,r.status,r.finished_at,r.error_category,
                                r.inspected_this_run
                           FROM index_coverage_runs AS r
                           JOIN (
                             SELECT site_id,MAX(started_at) AS started_at
                               FROM index_coverage_runs
                              WHERE site_id IN ({placeholders})
                              GROUP BY site_id
                           ) AS latest
                             ON latest.site_id=r.site_id
                            AND latest.started_at=r.started_at""",
                    tuple(site_ids),
                ).fetchall()
            }
        result = []
        for site_id in site_ids:
            inventory = inventories.get(site_id)
            count = counts.get(site_id)
            run = latest_runs.get(site_id)
            if inventory is None:
                result.append({
                    "site_id": site_id,
                    "status": "not_collected",
                    "published_pages": None,
                    "indexed_pages": None,
                    "indexed_percentage": None,
                    "inspection_progress": {"inspected": 0, "total": None},
                    "inventory_observed_at": None,
                    "oldest_fresh_inspection": None,
                    "latest_inspection": None,
                    "last_run": None,
                })
                continue
            published = int(inventory["published_pages"])
            inventory_rows = int(count["inventory_rows"] or 0) if count else 0
            inspected = int(count["fresh_inspected"] or 0) if count else 0
            indexed_so_far = int(count["fresh_indexed"] or 0) if count else 0
            complete = published > 0 and inventory_rows == published and inspected == published
            result.append({
                "site_id": site_id,
                "status": "complete" if complete else "empty" if published == 0 else "partial",
                "published_pages": published,
                "indexed_pages": indexed_so_far if complete else None,
                "indexed_percentage": (
                    round(indexed_so_far * 100 / published, 2) if complete else None
                ),
                "confirmed_indexed_so_far": indexed_so_far,
                "inspection_progress": {"inspected": inspected, "total": published},
                "inventory_observed_at": inventory["observed_at"],
                "oldest_fresh_inspection": (
                    count["oldest_fresh_inspection"] if count else None
                ),
                "latest_inspection": count["latest_inspection"] if count else None,
                "last_run": ({
                    "status": run["status"],
                    "finished_at": run["finished_at"],
                    "error_category": run["error_category"],
                    "inspected": int(run["inspected_this_run"]),
                } if run else None),
            })
        return result

    @staticmethod
    def _version_from_row(row: sqlite3.Row) -> DefinitionVersion:
        return DefinitionVersion(
            id=row["id"],
            scope_key=row["scope_key"],
            definition_type=DefinitionType(row["definition_type"]),
            definition_key=row["definition_key"],
            version=int(row["version"]),
            content_hash=row["content_hash"],
            content_json=row["content_json"],
            metadata_json=row["metadata_json"],
            created_at=_parse(row["created_at"]),
            record_hash=row["record_hash"],
        )

    @staticmethod
    def _activation_from_row(row: sqlite3.Row) -> DefinitionActivation:
        return DefinitionActivation(
            id=row["id"],
            definition_version_id=row["definition_version_id"],
            scope_key=row["scope_key"],
            definition_type=DefinitionType(row["definition_type"]),
            definition_key=row["definition_key"],
            activated_at=_parse(row["activated_at"]),
            retired_at=_parse(row["retired_at"]) if row["retired_at"] else None,
            record_hash=row["record_hash"],
        )

    @staticmethod
    def _validate_version_integrity(
        row: Mapping[str, object],
    ) -> ValidatedDefinition:
        try:
            validated = _validate_persisted_analytics_definition(
                definition_type=row["definition_type"],
                definition_key=row["definition_key"],
                scope_key=row["scope_key"],
                content_json=row["content_json"],
                metadata_json=row["metadata_json"],
            )
        except DefinitionValidationError as exc:
            raise DefinitionIntegrityError(
                f"definition version semantic validation failed: {row['id']}"
            ) from exc
        if validated.content_hash != row["content_hash"]:
            raise DefinitionIntegrityError(
                f"definition version content hash failed: {row['id']}"
            )
        expected_id = _digest_fields(
            (
                row["definition_type"],
                row["scope_key"],
                row["definition_key"],
                row["version"],
                row["content_hash"],
            )
        )
        expected_record_hash = _digest_fields(
            (
                row["id"],
                row["scope_key"],
                row["definition_type"],
                row["definition_key"],
                row["version"],
                row["content_hash"],
                row["content_json"],
                row["metadata_json"],
                row["created_at"],
            )
        )
        if expected_id != row["id"] or expected_record_hash != row["record_hash"]:
            raise DefinitionIntegrityError(
                f"definition version immutable integrity failed: {row['id']}"
            )
        return validated

    @staticmethod
    def _validate_activation_integrity(row: Mapping[str, object]) -> None:
        expected_record_hash = _digest_fields(
            (
                row["id"],
                row["definition_version_id"],
                row["scope_key"],
                row["definition_type"],
                row["definition_key"],
                row["activated_at"],
            )
        )
        if expected_record_hash != row["record_hash"]:
            raise DefinitionIntegrityError(
                f"definition activation integrity failed: {row['id']}"
            )

    @staticmethod
    def _validate_retirement_integrity(row: Mapping[str, object]) -> None:
        expected_id = _digest_fields((row["activation_id"], row["retired_at"]))
        expected_record_hash = _digest_fields(
            (
                row["id"],
                row["activation_id"],
                row["scope_key"],
                row["definition_type"],
                row["definition_key"],
                row["activated_at"],
                row["retired_at"],
            )
        )
        if expected_id != row["id"] or expected_record_hash != row["record_hash"]:
            raise DefinitionIntegrityError(
                f"definition retirement integrity failed: {row['id']}"
            )

    @staticmethod
    def _activation_row(
        db: sqlite3.Connection, activation_id: str
    ) -> sqlite3.Row | None:
        return db.execute(
            """SELECT
                 activation.*,
                 retirement.id AS retirement_id,
                 retirement.activation_id AS retirement_activation_id,
                 retirement.scope_key AS retirement_scope_key,
                 retirement.definition_type AS retirement_definition_type,
                 retirement.definition_key AS retirement_definition_key,
                 retirement.activated_at AS retirement_activated_at,
                 retirement.retired_at AS retired_at,
                 retirement.record_hash AS retirement_record_hash
               FROM analytics_definition_activations AS activation
               LEFT JOIN analytics_definition_retirements AS retirement
                 ON retirement.activation_id=activation.id
              WHERE activation.id=?""",
            (activation_id,),
        ).fetchone()

    @staticmethod
    def _validate_activation_history_row(row: Mapping[str, object]) -> None:
        SQLiteMetricStore._validate_activation_integrity(row)
        if row["retirement_id"] is None:
            return
        SQLiteMetricStore._validate_retirement_integrity(
            {
                "id": row["retirement_id"],
                "activation_id": row["retirement_activation_id"],
                "scope_key": row["retirement_scope_key"],
                "definition_type": row["retirement_definition_type"],
                "definition_key": row["retirement_definition_key"],
                "activated_at": row["retirement_activated_at"],
                "retired_at": row["retired_at"],
                "record_hash": row["retirement_record_hash"],
            }
        )

    @staticmethod
    def _validate_activation_chronology(
        rows: Sequence[Mapping[str, object]],
    ) -> None:
        grouped: dict[
            tuple[object, object, object],
            list[tuple[str, str | None, object]],
        ] = {}
        for row in rows:
            activated_at = row["activated_at"]
            retired_at = row["retired_at"]
            if not isinstance(activated_at, str) or (
                retired_at is not None and not isinstance(retired_at, str)
            ):
                raise DefinitionIntegrityError(
                    "definition activation chronology is invalid"
                )
            if retired_at is not None and retired_at < activated_at:
                raise DefinitionIntegrityError(
                    "definition activation chronology is invalid"
                )
            identity = (
                row["scope_key"],
                row["definition_type"],
                row["definition_key"],
            )
            grouped.setdefault(identity, []).append(
                (activated_at, retired_at, row["id"])
            )
        for intervals in grouped.values():
            intervals.sort(
                key=lambda interval: (
                    interval[0],
                    interval[1] is None,
                    interval[1] or "",
                    str(interval[2]),
                )
            )
            first = True
            prior_end: str | None = None
            for activated_at, retired_at, _ in intervals:
                if not first and (
                    prior_end is None or activated_at < prior_end
                ):
                    raise DefinitionIntegrityError(
                        "definition activation history overlaps or is non-monotonic"
                    )
                first = False
                prior_end = retired_at

    @staticmethod
    def _current_activation(
        db: sqlite3.Connection, identity: DefinitionIdentity
    ) -> sqlite3.Row | None:
        rows = db.execute(
            """SELECT
                 activation.*,
                 retirement.id AS retirement_id,
                 retirement.activation_id AS retirement_activation_id,
                 retirement.scope_key AS retirement_scope_key,
                 retirement.definition_type AS retirement_definition_type,
                 retirement.definition_key AS retirement_definition_key,
                 retirement.activated_at AS retirement_activated_at,
                 retirement.retired_at AS retired_at,
                 retirement.record_hash AS retirement_record_hash
               FROM analytics_definition_activations AS activation
               LEFT JOIN analytics_definition_retirements AS retirement
                 ON retirement.activation_id=activation.id
              WHERE activation.scope_key=?
                AND activation.definition_type=?
                AND activation.definition_key=?
              ORDER BY activation.activated_at,activation.id""",
            (
                identity.scope_key,
                identity.definition_type.value,
                identity.definition_key,
            ),
        ).fetchall()
        SQLiteMetricStore._validate_activation_chronology(rows)
        current: sqlite3.Row | None = None
        for row in rows:
            SQLiteMetricStore._validate_activation_history_row(row)
            if row["retirement_id"] is None:
                if current is not None:
                    raise DefinitionIntegrityError(
                        "multiple current definition activations exist"
                    )
                current = row
        if current is not None:
            version = db.execute(
                "SELECT * FROM analytics_definition_versions WHERE id=?",
                (current["definition_version_id"],),
            ).fetchone()
            if version is None:
                raise DefinitionIntegrityError(
                    "current activation references a missing definition version"
                )
            SQLiteMetricStore._validate_authoritative_version(db, version)
        return current

    @staticmethod
    def _validate_definition_references(
        db: sqlite3.Connection,
        definitions: Sequence[ValidatedDefinition],
        *,
        integrity_context: bool = False,
    ) -> None:
        pending = list(definitions)
        expected: dict[str, DefinitionType] = {}
        validated_reference_ids: set[str] = set()
        while pending:
            definition = pending.pop()
            content = json.loads(definition.content_json)
            references: list[tuple[str, DefinitionType]] = []
            if definition.definition_type is DefinitionType.GOAL:
                references.extend(
                    (version_id, DefinitionType.GOAL)
                    for version_id in content.get("goal_version_ids", ())
                )
            elif definition.definition_type is DefinitionType.ALERT_RULE:
                if "goal_version_id" in content:
                    references.append(
                        (content["goal_version_id"], DefinitionType.GOAL)
                    )
                if "segment_version_id" in content:
                    references.append(
                        (content["segment_version_id"], DefinitionType.SEGMENT)
                    )
            elif definition.definition_type is DefinitionType.REPORT_SUBSCRIPTION:
                references.extend(
                    (version_id, DefinitionType.GOAL)
                    for version_id in content.get("goal_version_ids", ())
                )
                if "segment_version_id" in content:
                    references.append(
                        (content["segment_version_id"], DefinitionType.SEGMENT)
                    )
            for version_id, definition_type in references:
                prior = expected.setdefault(version_id, definition_type)
                if prior is not definition_type:
                    if integrity_context:
                        raise DefinitionIntegrityError(
                            "definition version reference has conflicting expected types"
                        )
                    raise DefinitionNotFoundError(
                        "definition version reference has conflicting expected types"
                    )
                if version_id in validated_reference_ids:
                    continue
                row = db.execute(
                    """SELECT *
                         FROM analytics_definition_versions
                        WHERE id=?""",
                    (version_id,),
                ).fetchone()
                if row is None or row["definition_type"] != definition_type.value:
                    if integrity_context:
                        raise DefinitionIntegrityError(
                            f"referenced {definition_type.value} version does not exist"
                        )
                    raise DefinitionNotFoundError(
                        f"referenced {definition_type.value} version does not exist"
                    )
                validated_reference_ids.add(version_id)
                pending.append(SQLiteMetricStore._validate_version_integrity(row))

    @staticmethod
    def _validate_authoritative_version(
        db: sqlite3.Connection, row: Mapping[str, object]
    ) -> ValidatedDefinition:
        validated = SQLiteMetricStore._validate_version_integrity(row)
        SQLiteMetricStore._validate_definition_references(
            db,
            (validated,),
            integrity_context=True,
        )
        return validated

    @staticmethod
    def _validate_definition_schema(db: sqlite3.Connection) -> None:
        if _definition_schema_rows(db) != _canonical_definition_schema_rows():
            raise DefinitionIntegrityError(
                "definition schema enforcement verification failed"
            )

    @staticmethod
    def _retire_activation(
        db: sqlite3.Connection, activation: sqlite3.Row, retired_at: str
    ) -> DefinitionActivation:
        if activation["retirement_id"] is not None:
            raise DefinitionNotActiveError("definition activation is no longer current")
        retirement_id = _digest_fields((activation["id"], retired_at))
        record_hash = _digest_fields(
            (
                retirement_id,
                activation["id"],
                activation["scope_key"],
                activation["definition_type"],
                activation["definition_key"],
                activation["activated_at"],
                retired_at,
            )
        )
        try:
            db.execute(
                """INSERT INTO analytics_definition_retirements(
                     id,activation_id,scope_key,definition_type,definition_key,
                     activated_at,retired_at,record_hash
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    retirement_id,
                    activation["id"],
                    activation["scope_key"],
                    activation["definition_type"],
                    activation["definition_key"],
                    activation["activated_at"],
                    retired_at,
                    record_hash,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DefinitionNotActiveError(
                "definition activation is no longer current"
            ) from exc
        row = SQLiteMetricStore._activation_row(db, activation["id"])
        if row is None:
            raise DefinitionIntegrityError("retired activation disappeared")
        SQLiteMetricStore._validate_activation_history_row(row)
        return SQLiteMetricStore._activation_from_row(row)

    @staticmethod
    def _insert_activation(
        db: sqlite3.Connection, version: sqlite3.Row, activated_at: str
    ) -> DefinitionActivation:
        history = db.execute(
            """SELECT
                 activation.id,
                 activation.scope_key,
                 activation.definition_type,
                 activation.definition_key,
                 activation.activated_at,
                 retirement.retired_at
               FROM analytics_definition_activations AS activation
               LEFT JOIN analytics_definition_retirements AS retirement
                 ON retirement.activation_id=activation.id
              WHERE activation.scope_key=?
                AND activation.definition_type=?
                AND activation.definition_key=?""",
            (
                version["scope_key"],
                version["definition_type"],
                version["definition_key"],
            ),
        ).fetchall()
        SQLiteMetricStore._validate_activation_chronology(
            (
                *history,
                {
                    "id": "<pending>",
                    "scope_key": version["scope_key"],
                    "definition_type": version["definition_type"],
                    "definition_key": version["definition_key"],
                    "activated_at": activated_at,
                    "retired_at": None,
                },
            )
        )
        collision_ordinal = 0
        while True:
            identity_fields: tuple[object, ...] = (version["id"], activated_at)
            if collision_ordinal:
                identity_fields += (collision_ordinal,)
            activation_id = _digest_fields(identity_fields)
            occupied = db.execute(
                """SELECT definition_version_id,activated_at
                     FROM analytics_definition_activations
                    WHERE id=?""",
                (activation_id,),
            ).fetchone()
            if occupied is None:
                break
            if (
                occupied["definition_version_id"] != version["id"]
                or occupied["activated_at"] != activated_at
            ):
                raise DefinitionCollisionError(
                    "activation identity digest collides with unequal immutable fields"
                )
            collision_ordinal += 1
        record_hash = _digest_fields(
            (
                activation_id,
                version["id"],
                version["scope_key"],
                version["definition_type"],
                version["definition_key"],
                activated_at,
            )
        )
        db.execute(
            """INSERT INTO analytics_definition_activations(
                 id,definition_version_id,scope_key,definition_type,definition_key,
                 activated_at,record_hash
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                activation_id,
                version["id"],
                version["scope_key"],
                version["definition_type"],
                version["definition_key"],
                activated_at,
                record_hash,
            ),
        )
        row = SQLiteMetricStore._activation_row(db, activation_id)
        if row is None:
            raise DefinitionIntegrityError("inserted activation disappeared")
        SQLiteMetricStore._validate_activation_history_row(row)
        return SQLiteMetricStore._activation_from_row(row)

    @staticmethod
    def _apply_validated_definition(
        db: sqlite3.Connection,
        definition: ValidatedDefinition,
        *,
        transaction_time: str,
        step_hook: Callable[[str], None] | None,
    ) -> DefinitionChange:
        identity = DefinitionIdentity(
            definition.scope_key,
            definition.definition_type,
            definition.definition_key,
        )
        existing = db.execute(
            """SELECT *
                 FROM analytics_definition_versions
                WHERE scope_key=? AND definition_type=? AND definition_key=?
                  AND content_hash=?""",
            (
                identity.scope_key,
                identity.definition_type.value,
                identity.definition_key,
                definition.content_hash,
            ),
        ).fetchone()
        current = SQLiteMetricStore._current_activation(db, identity)
        if existing is not None:
            SQLiteMetricStore._validate_version_integrity(existing)
            if existing["content_json"].encode("utf-8") != definition.content_json.encode(
                "utf-8"
            ):
                raise DefinitionCollisionError(
                    "matching definition digest has unequal canonical bytes"
                )
            if current is not None and current["definition_version_id"] == existing["id"]:
                return DefinitionChange(
                    SQLiteMetricStore._version_from_row(existing),
                    SQLiteMetricStore._activation_from_row(current),
                    "unchanged",
                )
            if current is not None:
                SQLiteMetricStore._retire_activation(db, current, transaction_time)
                if step_hook:
                    step_hook("after_retirement")
            activation = SQLiteMetricStore._insert_activation(
                db, existing, transaction_time
            )
            if step_hook:
                step_hook("after_activation")
            return DefinitionChange(
                SQLiteMetricStore._version_from_row(existing),
                activation,
                "reactivated",
            )

        row = db.execute(
            """SELECT COALESCE(MAX(version), 0)
                 FROM analytics_definition_versions
                WHERE scope_key=? AND definition_type=? AND definition_key=?""",
            (
                identity.scope_key,
                identity.definition_type.value,
                identity.definition_key,
            ),
        ).fetchone()
        version_number = int(row[0]) + 1
        version_id = _digest_fields(
            (
                identity.definition_type.value,
                identity.scope_key,
                identity.definition_key,
                version_number,
                definition.content_hash,
            )
        )
        record_hash = _digest_fields(
            (
                version_id,
                identity.scope_key,
                identity.definition_type.value,
                identity.definition_key,
                version_number,
                definition.content_hash,
                definition.content_json,
                definition.metadata_json,
                transaction_time,
            )
        )
        db.execute(
            """INSERT INTO analytics_definition_versions(
                 id,scope_key,definition_type,definition_key,version,content_hash,
                 content_json,metadata_json,created_at,record_hash
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                version_id,
                identity.scope_key,
                identity.definition_type.value,
                identity.definition_key,
                version_number,
                definition.content_hash,
                definition.content_json,
                definition.metadata_json,
                transaction_time,
                record_hash,
            ),
        )
        if step_hook:
            step_hook("after_version")
        version_row = db.execute(
            "SELECT * FROM analytics_definition_versions WHERE id=?", (version_id,)
        ).fetchone()
        if current is not None:
            SQLiteMetricStore._retire_activation(db, current, transaction_time)
            if step_hook:
                step_hook("after_retirement")
        activation = SQLiteMetricStore._insert_activation(
            db, version_row, transaction_time
        )
        if step_hook:
            step_hook("after_activation")
        return DefinitionChange(
            SQLiteMetricStore._version_from_row(version_row),
            activation,
            "created",
        )

    def apply_definition_package(
        self,
        definitions: Iterable[AnalyticsDefinition],
        *,
        recipient_inputs: Mapping[
            DefinitionIdentity, tuple[Sequence[str], bytes]
        ] | None = None,
        retirements: Iterable[DefinitionIdentity] = (),
        transaction_time: datetime | None = None,
        _step_hook: Callable[[str], None] | None = None,
    ) -> DefinitionPackageResult:
        """Validate then atomically apply one definition package.

        An omitted definition remains active. Explicit retirements are required.
        The private hook exists only for deterministic transaction-interruption
        verification and runs after writes but before commit.
        """

        private_inputs: dict[
            DefinitionIdentity, tuple[Sequence[str], bytes]
        ] = {}
        if recipient_inputs is not None:
            if not isinstance(recipient_inputs, Mapping):
                raise ValueError("recipient_inputs must be a mapping")
            for raw_identity, values in recipient_inputs.items():
                identity = validate_definition_identity(raw_identity)
                if identity.definition_type is not DefinitionType.REPORT_SUBSCRIPTION:
                    raise ValueError(
                        "recipient inputs are valid only for report_subscription"
                    )
                if type(values) is not tuple or len(values) != 2:
                    raise ValueError(
                        "recipient inputs must contain recipient and digest-key pairs"
                    )
                if identity in private_inputs:
                    raise ValueError("recipient_inputs contains duplicate scoped keys")
                private_inputs[identity] = values
        prepared_items: list[ValidatedDefinition] = []
        for item in definitions:
            try:
                identity = validate_definition_identity(
                    DefinitionIdentity(
                        item.scope_key,
                        DefinitionType(item.definition_type),
                        item.definition_key,
                    )
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise DefinitionValidationError(
                    "definition identity is invalid"
                ) from exc
            private = private_inputs.pop(identity, None)
            if private is None:
                prepared_items.append(validate_analytics_definition(item))
            else:
                prepared_items.append(
                    validate_analytics_definition(
                        item,
                        recipient_set=private[0],
                        recipient_digest_key=private[1],
                    )
                )
        if private_inputs:
            raise ValueError(
                "recipient_inputs contains an identity absent from the package"
            )
        prepared = tuple(prepared_items)
        retirement_identities = tuple(
            validate_definition_identity(item) for item in retirements
        )
        definition_keys = [
            (item.scope_key, item.definition_type, item.definition_key)
            for item in prepared
        ]
        retirement_keys = [
            (item.scope_key, item.definition_type, item.definition_key)
            for item in retirement_identities
        ]
        if len(definition_keys) != len(set(definition_keys)):
            raise ValueError("definition package contains duplicate scoped keys")
        if len(retirement_keys) != len(set(retirement_keys)):
            raise ValueError("definition package contains duplicate retirements")
        if set(definition_keys) & set(retirement_keys):
            raise ValueError(
                "definition package cannot activate and explicitly retire the same scoped key"
            )
        with self.connect(readonly=True) as db:
            self._validate_definition_references(db, prepared)
        instant = _iso(_transaction_time(transaction_time))
        changes: list[DefinitionChange] = []
        retired: list[DefinitionActivation] = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_definition_references(db, prepared)
            for definition in prepared:
                changes.append(
                    self._apply_validated_definition(
                        db,
                        definition,
                        transaction_time=instant,
                        step_hook=_step_hook,
                    )
                )
            for identity in retirement_identities:
                current = self._current_activation(db, identity)
                if current is None:
                    raise DefinitionNotActiveError(
                        "definition is unknown or has no current activation"
                    )
                retired.append(self._retire_activation(db, current, instant))
                if _step_hook:
                    _step_hook("after_retirement")
        return DefinitionPackageResult(tuple(changes), tuple(retired))

    def activate_definition_version(
        self,
        version_id: str,
        *,
        transaction_time: datetime | None = None,
        _step_hook: Callable[[str], None] | None = None,
    ) -> DefinitionChange:
        """Activate a retained version as an explicit, auditable rollback."""

        if not isinstance(version_id, str) or not re.fullmatch(r"[0-9a-f]{64}", version_id):
            raise ValueError("version_id must be a lowercase SHA-256 identity")
        instant = _iso(_transaction_time(transaction_time))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute(
                "SELECT * FROM analytics_definition_versions WHERE id=?", (version_id,)
            ).fetchone()
            if version is None:
                raise DefinitionNotFoundError("definition version does not exist")
            self._validate_authoritative_version(db, version)
            identity = DefinitionIdentity(
                version["scope_key"],
                DefinitionType(version["definition_type"]),
                version["definition_key"],
            )
            current = self._current_activation(db, identity)
            if current is not None and current["definition_version_id"] == version_id:
                return DefinitionChange(
                    self._version_from_row(version),
                    self._activation_from_row(current),
                    "unchanged",
                )
            if current is not None:
                self._retire_activation(db, current, instant)
                if _step_hook:
                    _step_hook("after_retirement")
            activation = self._insert_activation(db, version, instant)
            if _step_hook:
                _step_hook("after_activation")
            return DefinitionChange(
                self._version_from_row(version), activation, "reactivated"
            )

    def retire_definition(
        self,
        identity: DefinitionIdentity,
        *,
        transaction_time: datetime | None = None,
        _step_hook: Callable[[str], None] | None = None,
    ) -> DefinitionActivation:
        result = self.apply_definition_package(
            (),
            retirements=(identity,),
            transaction_time=transaction_time,
            _step_hook=_step_hook,
        )
        return result.retired[0]

    def get_current_definition(
        self, identity: DefinitionIdentity
    ) -> tuple[DefinitionVersion, DefinitionActivation] | None:
        identity = validate_definition_identity(identity)
        with self.connect(readonly=True) as db:
            activation = self._current_activation(db, identity)
            if activation is None:
                return None
            version = db.execute(
                "SELECT * FROM analytics_definition_versions WHERE id=?",
                (activation["definition_version_id"],),
            ).fetchone()
            if version is None:
                raise DefinitionIntegrityError(
                    "current activation references a missing definition version"
                )
            self._validate_authoritative_version(db, version)
            return (
                self._version_from_row(version),
                self._activation_from_row(activation),
            )

    @staticmethod
    def _verify_definition_integrity_connection(
        db: sqlite3.Connection,
    ) -> dict[str, int]:
        SQLiteMetricStore._validate_definition_schema(db)
        foreign_key_errors = db.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise DefinitionIntegrityError("definition foreign-key verification failed")
        versions = db.execute(
            "SELECT * FROM analytics_definition_versions ORDER BY id"
        ).fetchall()
        activations = db.execute(
            "SELECT * FROM analytics_definition_activations ORDER BY id"
        ).fetchall()
        retirements = db.execute(
            "SELECT * FROM analytics_definition_retirements ORDER BY id"
        ).fetchall()
        validated_versions: list[ValidatedDefinition] = []
        for row in versions:
            validated_versions.append(
                SQLiteMetricStore._validate_version_integrity(row)
            )
        SQLiteMetricStore._validate_definition_references(
            db,
            validated_versions,
            integrity_context=True,
        )
        activation_identity_groups: dict[tuple[str, str], set[str]] = {}
        for row in activations:
            identity_fields = (row["definition_version_id"], row["activated_at"])
            activation_identity_groups.setdefault(identity_fields, set()).add(row["id"])
            SQLiteMetricStore._validate_activation_integrity(row)
        for row in retirements:
            SQLiteMetricStore._validate_retirement_integrity(row)
        retirement_by_activation = {
            row["activation_id"]: row["retired_at"] for row in retirements
        }
        SQLiteMetricStore._validate_activation_chronology(
            tuple(
                {
                    "id": row["id"],
                    "scope_key": row["scope_key"],
                    "definition_type": row["definition_type"],
                    "definition_key": row["definition_key"],
                    "activated_at": row["activated_at"],
                    "retired_at": retirement_by_activation.get(row["id"]),
                }
                for row in activations
            )
        )
        for identity_fields, actual_ids in activation_identity_groups.items():
            expected_ids = {_digest_fields(identity_fields)}
            for collision_ordinal in range(1, len(actual_ids)):
                expected_ids.add(_digest_fields((*identity_fields, collision_ordinal)))
            if actual_ids != expected_ids:
                raise DefinitionIntegrityError(
                    "definition activation identity sequence is not canonical"
                )
        current_counts: dict[tuple[str, str, str], int] = {}
        retired_ids = {row["activation_id"] for row in retirements}
        for row in activations:
            if row["id"] in retired_ids:
                continue
            identity = (
                row["scope_key"],
                row["definition_type"],
                row["definition_key"],
            )
            current_counts[identity] = current_counts.get(identity, 0) + 1
        if any(count != 1 for count in current_counts.values()):
            raise DefinitionIntegrityError(
                "multiple current definition activations exist"
            )
        return {
            "versions": len(versions),
            "activations": len(activations),
            "retirements": len(retirements),
        }

    def verify_definition_integrity(self) -> dict[str, int]:
        """Verify schema enforcement, immutable hashes, semantics, and references."""

        with self.connect(readonly=True) as db:
            db.execute("BEGIN")
            return self._verify_definition_integrity_connection(db)

    @staticmethod
    def _verify_acquisition_integrity_connection(
        db: sqlite3.Connection,
    ) -> dict[str, int]:
        """Verify the pinned schema and every immutable acquisition record."""

        if _acquisition_schema_rows(db) != _canonical_acquisition_schema_rows():
            raise AcquisitionIntegrityError(
                "acquisition provenance schema does not match migration 006"
            )

        def timestamp(value: object, label: str) -> datetime:
            if not isinstance(value, str):
                raise AcquisitionIntegrityError(f"{label} is not a timestamp")
            try:
                parsed = _parse(value)
            except ValueError as exc:
                raise AcquisitionIntegrityError(
                    f"{label} is not a timestamp"
                ) from exc
            if (
                parsed.tzinfo is None
                or parsed.utcoffset() is None
                or _iso(parsed) != value
            ):
                raise AcquisitionIntegrityError(
                    f"{label} is not a canonical UTC timestamp"
                )
            return parsed

        slices = db.execute(
            """SELECT acquisition.*,run.site_id AS run_site_id,
                      run.source AS run_source,run.binding_key AS run_binding_key,
                      run.window_start AS run_window_start,
                      run.window_end AS run_window_end
                 FROM acquisition_slices AS acquisition
                 JOIN sync_runs AS run ON run.id=acquisition.sync_run_id
                ORDER BY acquisition.id"""
        ).fetchall()
        slice_intervals: dict[str, tuple[datetime, datetime, str, str]] = {}
        for row in slices:
            try:
                raw_dimensions = json.loads(row["request_dimensions_json"])
                if not isinstance(raw_dimensions, list) or any(
                    not isinstance(item, str) for item in raw_dimensions
                ):
                    raise ValueError("request dimensions are invalid")
                dimensions = tuple(raw_dimensions)
                if json.dumps(
                    list(dimensions), separators=(",", ":"), ensure_ascii=True
                ) != row["request_dimensions_json"]:
                    raise ValueError("request dimensions are not canonical")
                start = timestamp(row["start_at"], "acquisition start")
                end = timestamp(row["end_at"], "acquisition end")
                timestamp(row["recorded_at"], "acquisition record time")
                acquisition = AcquisitionSlice(
                    row["slice_key"], row["metric_family"], start, end,
                    Completeness(row["completeness"]), row["data_state"],
                    row["provider_scope"], dimensions,
                    row["provider_aggregation"], int(row["pages_fetched"]),
                    int(row["raw_rows"]), int(row["accepted_rows"]),
                    int(row["rejected_rows"]), row["exhaustion_reason"],
                )
                expected_id = _digest_fields(
                    (row["sync_run_id"], row["binding_key"], acquisition.slice_key)
                )
                fields: tuple[object, ...] = (
                    row["id"], row["sync_run_id"], row["binding_key"],
                    acquisition.slice_key, acquisition.metric_family,
                    row["start_at"], row["end_at"],
                    acquisition.completeness.value, acquisition.data_state,
                    acquisition.provider_scope, row["request_dimensions_json"],
                    acquisition.provider_aggregation, acquisition.pages_fetched,
                    acquisition.raw_rows, acquisition.accepted_rows,
                    acquisition.rejected_rows, acquisition.exhaustion_reason,
                    row["recorded_at"],
                )
                run_start = timestamp(
                    row["run_window_start"], "sync run window start"
                )
                run_end = timestamp(row["run_window_end"], "sync run window end")
            except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
                raise AcquisitionIntegrityError(
                    "acquisition slice semantic validation failed"
                ) from exc
            if expected_id != row["id"] or _digest_fields(fields) != row["record_hash"]:
                raise AcquisitionIntegrityError(
                    "acquisition slice immutable integrity failed"
                )
            if (
                row["binding_key"] != row["run_binding_key"]
                or not row["run_site_id"]
                or not row["run_source"]
                or start >= run_end
                or end <= run_start
            ):
                raise AcquisitionIntegrityError(
                    "acquisition slice does not match its sync run"
                )
            slice_intervals[row["id"]] = (
                start, end, row["run_site_id"], row["run_source"]
            )

        observations = db.execute(
            """SELECT observation.*
                 FROM metric_fact_observations AS observation
                ORDER BY observation.id"""
        ).fetchall()
        for row in observations:
            try:
                raw_dimensions = json.loads(row["dimensions_json"])
                if not isinstance(raw_dimensions, dict) or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in raw_dimensions.items()
                ):
                    raise ValueError("metric dimensions are invalid")
                dimensions = tuple(sorted(raw_dimensions.items()))
                if json.dumps(
                    dict(dimensions), separators=(",", ":"), sort_keys=True,
                    ensure_ascii=True,
                ) != row["dimensions_json"]:
                    raise ValueError("metric dimensions are not canonical")
                start = timestamp(row["start_at"], "metric start")
                end = timestamp(row["end_at"], "metric end")
                observed_at = timestamp(row["observed_at"], "metric observation")
                timestamp(row["recorded_at"], "metric record time")
                point = MetricPoint(
                    row["client_id"], row["site_id"], row["source"],
                    row["metric"], row["unit"], start, end,
                    TimeGrain(row["grain"]), Decimal(row["value"]), dimensions,
                    Completeness(row["completeness"]), observed_at,
                )
                identity_version = int(row["identity_version"])
                expected_point_key = _key_at_identity_version(
                    point, identity_version
                )
                expected_id = _digest_fields(
                    (row["acquisition_slice_id"], row["point_key"])
                )
                fields = (
                    row["id"], row["acquisition_slice_id"], row["sync_run_id"],
                    row["binding_key"], row["point_key"], point.client_id,
                    point.site_id, point.source, point.metric, point.unit,
                    row["start_at"], row["end_at"], point.grain.value,
                    row["value"], row["dimensions_json"],
                    point.completeness.value, row["observed_at"],
                    identity_version, row["recorded_at"],
                )
                _slice_start, _slice_end, run_site, run_source = slice_intervals[
                    row["acquisition_slice_id"]
                ]
            except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
                raise AcquisitionIntegrityError(
                    "metric fact observation semantic validation failed"
                ) from exc
            if (
                expected_id != row["id"]
                or expected_point_key != row["point_key"]
                or _digest_fields(fields) != row["record_hash"]
            ):
                raise AcquisitionIntegrityError(
                    "metric fact observation immutable integrity failed"
                )
            if (
                start >= _slice_end
                or end <= _slice_start
                or point.site_id != run_site
                or point.source != run_source
            ):
                raise AcquisitionIntegrityError(
                    "metric fact observation does not match its acquisition slice"
                )

        return {"slices": len(slices), "observations": len(observations)}

    def verify_acquisition_integrity(self) -> dict[str, int]:
        """Verify schema enforcement, immutable hashes, and request linkage."""

        with self.connect(readonly=True) as db:
            db.execute("BEGIN")
            return self._verify_acquisition_integrity_connection(db)

    def record_acquisition_batches(
        self,
        run_id: str,
        binding_key: str,
        batches: Sequence[AcquisitionBatch],
        *,
        publish_current: bool = True,
    ) -> int:
        """Preserve request evidence; optionally publish the latest snapshot."""

        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be non-empty")
        if not isinstance(binding_key, str) or not binding_key:
            raise ValueError("binding_key must be non-empty")
        if len(binding_key.encode("utf-8")) > 2048:
            raise ValueError("binding_key is too long")
        if not isinstance(publish_current, bool):
            raise ValueError("publish_current must be a boolean")
        materialized = tuple(batches)
        if not materialized:
            raise ValueError("at least one acquisition batch is required")
        if any(not isinstance(batch, AcquisitionBatch) for batch in materialized):
            raise ValueError("batches must contain only AcquisitionBatch values")

        recorded_at = _iso(datetime.now(UTC))
        seen_slice_keys: set[str] = set()
        seen_point_keys: set[str] = set()
        slice_rows: list[tuple[object, ...]] = []
        observation_rows: list[tuple[object, ...]] = []
        fact_rows: list[tuple[object, ...]] = []
        batch_point_rows: list[tuple[AcquisitionBatch, MetricPoint, str]] = []

        for batch in materialized:
            acquisition = batch.slice
            if acquisition.slice_key in seen_slice_keys:
                raise ValueError(
                    f"duplicate acquisition slice key: {acquisition.slice_key}"
                )
            seen_slice_keys.add(acquisition.slice_key)
            request_dimensions_json = json.dumps(
                list(acquisition.request_dimensions),
                separators=(",", ":"),
                ensure_ascii=True,
            )
            if len(request_dimensions_json.encode("utf-8")) > 4096:
                raise ValueError("request_dimensions exceed the storage limit")
            slice_id = _digest_fields(
                (run_id, binding_key, acquisition.slice_key)
            )
            slice_fields: tuple[object, ...] = (
                slice_id,
                run_id,
                binding_key,
                acquisition.slice_key,
                acquisition.metric_family,
                _iso(acquisition.start),
                _iso(acquisition.end),
                acquisition.completeness.value,
                acquisition.data_state,
                acquisition.provider_scope,
                request_dimensions_json,
                acquisition.provider_aggregation,
                acquisition.pages_fetched,
                acquisition.raw_rows,
                acquisition.accepted_rows,
                acquisition.rejected_rows,
                acquisition.exhaustion_reason,
                recorded_at,
            )
            slice_rows.append((*slice_fields, _digest_fields(slice_fields)))

            for point in batch.points:
                point_key = _key(point)
                if point_key in seen_point_keys:
                    raise ValueError(
                        "duplicate metric point identity across acquisition batches"
                    )
                seen_point_keys.add(point_key)
                if not point.unit:
                    raise ValueError("metric point unit must be non-empty")
                dimensions_json = json.dumps(
                    dict(point.dimensions),
                    separators=(",", ":"),
                    sort_keys=True,
                    ensure_ascii=True,
                )
                if len(dimensions_json.encode("utf-8")) > 32768:
                    raise ValueError("metric point dimensions exceed the storage limit")
                identity_version = CURRENT_IDENTITY_VERSIONS.get(point.source, 1)
                observation_id = _digest_fields((slice_id, point_key))
                observation_fields: tuple[object, ...] = (
                    observation_id,
                    slice_id,
                    run_id,
                    binding_key,
                    point_key,
                    point.client_id,
                    point.site_id,
                    point.source,
                    point.metric,
                    point.unit,
                    _iso(point.start),
                    _iso(point.end),
                    point.grain.value,
                    str(point.value),
                    dimensions_json,
                    point.completeness.value,
                    _iso(point.observed_at),
                    identity_version,
                    recorded_at,
                )
                observation_rows.append(
                    (*observation_fields, _digest_fields(observation_fields))
                )
                fact_rows.append(
                    (
                        point_key,
                        point.client_id,
                        point.site_id,
                        point.source,
                        point.metric,
                        point.unit,
                        _iso(point.start),
                        _iso(point.end),
                        point.grain.value,
                        str(point.value),
                        dimensions_json,
                        point.completeness.value,
                        _iso(point.observed_at),
                        recorded_at,
                        identity_version,
                    )
                )
                batch_point_rows.append((batch, point, point_key))

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute(
                """SELECT site_id,status,binding_key,source,window_start,window_end
                     FROM sync_runs WHERE id=?""",
                (run_id,),
            ).fetchone()
            if run is None:
                raise ValueError("sync run does not exist")
            if run["status"] != "running":
                raise ValueError("acquisition batches require a running sync run")
            if run["binding_key"] != binding_key:
                raise ValueError("binding_key does not match the sync run")
            if not run["site_id"] or not run["source"]:
                raise ValueError("sync run must identify a site and source")
            if not run["window_start"] or not run["window_end"]:
                raise ValueError("sync run must identify an acquisition window")
            run_start = _parse(run["window_start"])
            run_end = _parse(run["window_end"])
            for batch in materialized:
                if (
                    batch.slice.start >= run_end
                    or batch.slice.end <= run_start
                ):
                    raise ValueError(
                        "acquisition slice must overlap the sync run window"
                    )
            for _batch, point, _point_key in batch_point_rows:
                if point.site_id != run["site_id"]:
                    raise ValueError("metric point site does not match the sync run")
                if point.source != run["source"]:
                    raise ValueError("metric point source does not match the sync run")

            if publish_current:
                # A provider snapshot can legitimately omit a row that existed
                # in an earlier revision. Retire only current snapshot rows
                # covered by the same binding/request scope; history remains.
                stale_point_keys: set[str] = set()
                for batch in materialized:
                    acquisition = batch.slice
                    # Fresh Search Console snapshots can be explicitly partial.
                    # Their missing rows are not authoritative deletions; a
                    # later final snapshot can retire them without losing the
                    # immutable observation history.
                    if (
                        acquisition.data_state in {"all", "hourly_all"}
                        and acquisition.completeness is not Completeness.FINAL
                    ):
                        continue
                    request_dimensions_json = json.dumps(
                        list(acquisition.request_dimensions),
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
                    prior = db.execute(
                        """SELECT observation.point_key,observation.grain,
                                  observation.start_at,observation.end_at,
                                  acquisition.start_at AS slice_start_at,
                                  acquisition.end_at AS slice_end_at
                             FROM metric_fact_observations AS observation
                             JOIN acquisition_slices AS acquisition
                               ON acquisition.id=observation.acquisition_slice_id
                            WHERE acquisition.binding_key=?
                              AND acquisition.metric_family=?
                              AND acquisition.provider_scope=?
                              AND acquisition.request_dimensions_json=?
                              AND acquisition.provider_aggregation=?""",
                        (
                            binding_key,
                            acquisition.metric_family,
                            acquisition.provider_scope,
                            request_dimensions_json,
                            acquisition.provider_aggregation,
                        ),
                    ).fetchall()
                    for row in prior:
                        if row["grain"] == TimeGrain.TOTAL.value:
                            covered = (
                                row["slice_start_at"] == _iso(acquisition.start)
                                and row["slice_end_at"] == _iso(acquisition.end)
                            )
                        else:
                            covered = (
                                _parse(row["start_at"]) < acquisition.end
                                and _parse(row["end_at"]) > acquisition.start
                            )
                        if covered:
                            stale_point_keys.add(row["point_key"])
                stale_point_keys.difference_update(seen_point_keys)
                if stale_point_keys:
                    db.executemany(
                        "DELETE FROM metric_facts WHERE point_key=?",
                        ((point_key,) for point_key in sorted(stale_point_keys)),
                    )

            db.executemany(
                """INSERT INTO acquisition_slices(
                     id,sync_run_id,binding_key,slice_key,metric_family,start_at,end_at,
                     completeness,data_state,provider_scope,request_dimensions_json,
                     provider_aggregation,pages_fetched,raw_rows,accepted_rows,
                     rejected_rows,exhaustion_reason,recorded_at,record_hash
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                slice_rows,
            )
            if observation_rows:
                db.executemany(
                    """INSERT INTO metric_fact_observations(
                         id,acquisition_slice_id,sync_run_id,binding_key,point_key,
                         client_id,site_id,source,metric,unit,start_at,end_at,grain,
                         value,dimensions_json,completeness,observed_at,identity_version,
                         recorded_at,record_hash
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    observation_rows,
                )
                if publish_current:
                    db.executemany(
                        """INSERT INTO metric_facts(
                             point_key,client_id,site_id,source,metric,unit,start_at,end_at,
                             grain,value,dimensions_json,completeness,observed_at,updated_at,
                             identity_version
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(point_key) DO UPDATE SET
                             value=excluded.value,
                             completeness=excluded.completeness,
                             observed_at=excluded.observed_at,
                             updated_at=excluded.updated_at""",
                        fact_rows,
                    )
        return len(fact_rows)

    def upsert(self, points: Iterable[MetricPoint]) -> int:
        now = _iso(datetime.now(UTC)); rows = []
        for point in points:
            rows.append((_key(point), point.client_id, point.site_id, point.source, point.metric, point.unit,
                _iso(point.start), _iso(point.end), point.grain.value, str(point.value),
                json.dumps(dict(point.dimensions), separators=(",", ":"), sort_keys=True),
                point.completeness.value, _iso(point.observed_at), now,
                CURRENT_IDENTITY_VERSIONS.get(point.source, 1)))
        if not rows: return 0
        with self.connect() as db:
            db.executemany("""INSERT INTO metric_facts(
              point_key,client_id,site_id,source,metric,unit,start_at,end_at,grain,value,
              dimensions_json,completeness,observed_at,updated_at,identity_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(point_key) DO UPDATE SET value=excluded.value, completeness=excluded.completeness,
              observed_at=excluded.observed_at, updated_at=excluded.updated_at""", rows)
        return len(rows)

    def query(self, *, client_id: str, site_ids: Sequence[str], metric_ids: Sequence[str], window: QueryWindow) -> list[MetricPoint]:
        if not site_ids or not metric_ids: return []
        sites = ",".join("?" for _ in site_ids); metrics = ",".join("?" for _ in metric_ids)
        sql = f"""SELECT * FROM metric_facts WHERE client_id=? AND site_id IN ({sites})
          AND metric IN ({metrics}) AND start_at>=? AND end_at<=? ORDER BY start_at, metric, site_id"""
        params = [client_id, *site_ids, *metric_ids, _iso(window.start), _iso(window.end)]
        with self.connect(readonly=True) as db:
            rows = db.execute(sql, params).fetchall()
        rows = [
            row for row in rows
            if int(row["identity_version"]) == CURRENT_IDENTITY_VERSIONS.get(row["source"], 1)
        ]
        return [MetricPoint(
            row["client_id"], row["site_id"], row["source"], row["metric"], row["unit"],
            _parse(row["start_at"]), _parse(row["end_at"]), TimeGrain(row["grain"]),
            Decimal(row["value"]), tuple(sorted(json.loads(row["dimensions_json"]).items())),
            Completeness(row["completeness"]), _parse(row["observed_at"])) for row in rows]

    def save_capability(self, snapshot: CapabilitySnapshot) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO capability_snapshots VALUES (?,?,?,?,?,?,?,?)
              ON CONFLICT(connection_id) DO UPDATE SET provider=excluded.provider, probed_at=excluded.probed_at,
              authentication_ok=excluded.authentication_ok, resources_json=excluded.resources_json,
              metric_groups_json=excluded.metric_groups_json, max_lookback_days=excluded.max_lookback_days,
              warnings_json=excluded.warnings_json""", (
                snapshot.connection_id, snapshot.provider, _iso(snapshot.probed_at), int(snapshot.authentication_ok),
                json.dumps(snapshot.resources), json.dumps(snapshot.metric_groups), snapshot.max_lookback_days,
                json.dumps(snapshot.warnings)))

    def start_run(
        self,
        connection_id: str,
        site_id: str | None,
        *,
        binding_key: str | None = None,
        source: str | None = None,
        window: QueryWindow | None = None,
    ) -> str:
        run_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                """INSERT INTO sync_runs(
                  id,connection_id,site_id,started_at,status,binding_key,source,window_start,window_end
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    connection_id,
                    site_id,
                    _iso(datetime.now(UTC)),
                    "running",
                    binding_key,
                    source,
                    _iso(window.start) if window else None,
                    _iso(window.end) if window else None,
                ),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        status: str,
        points: int = 0,
        category: str | None = None,
        message: str | None = None,
        *,
        result_kind: str | None = None,
        data_through: datetime | None = None,
    ) -> None:
        safe_message = message[:300] if message else None
        with self.connect() as db:
            source_row = db.execute(
                "SELECT source FROM sync_runs WHERE id=?", (run_id,)
            ).fetchone()
            stored_result_kind = mark_pageview_result_kind(
                source_row["source"] if source_row is not None else None,
                result_kind,
            ) if status == "success" else result_kind
            db.execute(
                """UPDATE sync_runs SET finished_at=?,status=?,points_written=?,error_category=?,
                  error_message=?,result_kind=?,data_through=? WHERE id=?""",
                (
                    _iso(datetime.now(UTC)),
                    status,
                    points,
                    category,
                    safe_message,
                    stored_result_kind,
                    _iso(data_through) if data_through else None,
                    run_id,
                ),
            )

    def query_sync_coverage(
        self,
        *,
        site_ids: Sequence[str],
        sources: Sequence[str],
        binding_keys: Sequence[str] | None,
        window: QueryWindow,
    ) -> list[dict[str, object]]:
        """Return successful acquisition intervals, optionally across all bindings.

        A successful provider query is distinct from the presence of events. The
        interval ledger lets reporting represent query-proven quiet dates without
        manufacturing metric facts or accepting stale runs from removed bindings.
        """

        if not site_ids or not sources or binding_keys == ():
            return []
        sites = ",".join("?" for _ in site_ids)
        provider_sources = ",".join("?" for _ in sources)
        binding_clause = ""
        params = [
            PAGEVIEW_DATA_RESULT_KIND,
            PAGEVIEW_EMPTY_RESULT_KIND,
            *site_ids,
            *sources,
        ]
        if binding_keys is not None:
            bindings = ",".join("?" for _ in binding_keys)
            binding_clause = f" AND binding_key IN ({bindings})"
            params.extend(binding_keys)
        sql = f"""SELECT site_id,source,binding_key,window_start,window_end,
                         result_kind,data_through,started_at,finished_at
                    FROM sync_runs
                   WHERE status='success'
                     AND result_kind IN ('data','empty',?,?)
                     AND site_id IN ({sites})
                     AND source IN ({provider_sources})
                     {binding_clause}
                     AND window_start IS NOT NULL
                     AND window_end IS NOT NULL
                     AND window_start < ?
                     AND window_end > ?
                ORDER BY window_start,window_end"""
        params.extend((_iso(window.end), _iso(window.start)))
        with self.connect(readonly=True) as db:
            rows = db.execute(sql, params).fetchall()
        return [
            {
                "site_id": row["site_id"],
                "source": row["source"],
                "binding_key": row["binding_key"],
                "window_start": _parse(row["window_start"]),
                "window_end": _parse(row["window_end"]),
                "result_kind": row["result_kind"],
                "data_through": _parse(row["data_through"]) if row["data_through"] else None,
                "started_at": _parse(row["started_at"]),
                "finished_at": _parse(row["finished_at"]) if row["finished_at"] else None,
            }
            for row in rows
            if (
                explicit_pageview_result_kind(row["source"], row["result_kind"])
                or (
                    row["source"] not in PAGEVIEW_ACQUISITION_SOURCES
                    and row["result_kind"] in {"data", "empty"}
                )
            )
        ]

    def query_latest_sync_status(
        self, *, binding_keys: Sequence[str]
    ) -> list[dict[str, object]]:
        """Return the newest safe operational status for current bindings only.

        Binding keys and provider error messages may contain resource identity or
        response detail, so neither leaves the storage boundary. Callers receive
        only fields suitable for an aggregated private operations dashboard.
        """

        if not binding_keys:
            return []
        bindings = ",".join("?" for _ in binding_keys)
        sql = f"""WITH ranked AS (
                    SELECT binding_key,connection_id,site_id,source,started_at,finished_at,
                           status,points_written,error_category,result_kind,
                           data_through,
                           ROW_NUMBER() OVER (
                               PARTITION BY binding_key
                               ORDER BY started_at DESC,id DESC
                           ) AS position
                      FROM sync_runs
                     WHERE binding_key IN ({bindings})
                )
                SELECT binding_key,connection_id,site_id,source,started_at,finished_at,
                       status,points_written,error_category,result_kind,data_through
                  FROM ranked
                 WHERE position=1
              ORDER BY site_id,source,connection_id"""
        with self.connect(readonly=True) as db:
            rows = db.execute(sql, list(binding_keys)).fetchall()
        binding_indexes = {key: index for index, key in enumerate(binding_keys)}
        return [
            {
                "binding_index": binding_indexes[row["binding_key"]],
                "connection_id": row["connection_id"],
                "site_id": row["site_id"],
                "source": row["source"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "status": row["status"],
                "points_written": int(row["points_written"]),
                "error_category": row["error_category"],
                "result_kind": public_result_kind(row["result_kind"]),
                "data_through": row["data_through"],
            }
            for row in rows
        ]

    def query_capability_summaries(
        self, *, connection_ids: Sequence[str]
    ) -> list[dict[str, object]]:
        """Return last-recorded provider capability limits without resource IDs.

        A capability row is a dated snapshot, not current authentication health.
        Failed probes are represented by sync runs and do not replace the last
        successful snapshot.
        """

        if not connection_ids:
            return []
        connections = ",".join("?" for _ in connection_ids)
        sql = f"""SELECT connection_id,provider,probed_at,resources_json,
                         metric_groups_json,max_lookback_days,warnings_json
                    FROM capability_snapshots
                   WHERE connection_id IN ({connections})
                ORDER BY provider,connection_id"""
        with self.connect(readonly=True) as db:
            rows = db.execute(sql, list(connection_ids)).fetchall()
        output = []
        for row in rows:
            resources = sorted(
                (str(item) for item in json.loads(row["resources_json"])),
                key=len,
                reverse=True,
            )
            warnings = []
            for item in json.loads(row["warnings_json"]):
                safe = str(item)
                for resource in resources:
                    if resource:
                        safe = safe.replace(resource, "[resource]")
                warnings.append(safe[:200])
            output.append({
                "connection_id": row["connection_id"],
                "provider": row["provider"],
                "probed_at": row["probed_at"],
                "metric_groups": list(json.loads(row["metric_groups_json"])),
                "max_lookback_days": row["max_lookback_days"],
                "warnings": warnings,
            })
        return output

    def acquire_lock(self, name: str, owner: str, lease_seconds: int = 900) -> None:
        now = datetime.now(UTC); expires = now + timedelta(seconds=lease_seconds)
        with self.connect() as db:
            db.execute("DELETE FROM sync_locks WHERE lock_name=? AND expires_at<=?", (name, _iso(now)))
            try:
                db.execute("INSERT INTO sync_locks VALUES (?,?,?,?)", (name, owner, _iso(now), _iso(expires)))
            except sqlite3.IntegrityError as exc:
                raise LockBusy(f"lock is already held: {name}") from exc

    def release_lock(self, name: str, owner: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM sync_locks WHERE lock_name=? AND owner_id=?", (name, owner))

    def renew_lock(self, name: str, owner: str, lease_seconds: int = 900) -> None:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=lease_seconds)
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE sync_locks SET expires_at=?
                    WHERE lock_name=? AND owner_id=? AND expires_at>?""",
                (_iso(expires), name, owner, _iso(now)),
            )
            if cursor.rowcount != 1:
                raise LockBusy(f"lock lease was lost: {name}")

    def set_watermark(self, binding_key: str, value: datetime) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO watermarks VALUES (?,?,?) ON CONFLICT(binding_key)
              DO UPDATE SET completed_through=excluded.completed_through,updated_at=excluded.updated_at""",
              (binding_key, _iso(value), _iso(datetime.now(UTC))))

    def integrity_check(self) -> str:
        with self.connect(readonly=True) as db:
            result = str(db.execute("PRAGMA integrity_check").fetchone()[0])
            if result != "ok":
                return result
            try:
                schema_row = db.execute(
                    "SELECT version FROM schema_meta LIMIT 1"
                ).fetchone()
                schema_version = int(schema_row[0]) if schema_row else 0
                if schema_version >= 5:
                    self._verify_definition_integrity_connection(db)
                if schema_version >= 6:
                    self._verify_acquisition_integrity_connection(db)
                if schema_version >= 8:
                    self._verify_page_intelligence_integrity_connection(db)
            except (
                AcquisitionIntegrityError,
                DefinitionIntegrityError,
                PageIntelligenceIntegrityError,
            ):
                return "application-integrity-error"
            return "ok"

    @staticmethod
    def _verify_page_intelligence_integrity_connection(
        db: sqlite3.Connection,
    ) -> dict[str, int]:
        pages = db.execute(
            "SELECT page_id,site_id,route FROM page_catalog ORDER BY page_id"
        ).fetchall()
        for row in pages:
            expected = hashlib.sha256(
                f"page-v1\0{row['site_id']}\0{row['route']}".encode("utf-8")
            ).hexdigest()
            if row["page_id"] != expected:
                raise PageIntelligenceIntegrityError("page catalog identity mismatch")
        versions = db.execute(
            """SELECT version_id,scheme_id,definition_json,definition_hash
                 FROM page_scheme_versions ORDER BY version_id"""
        ).fetchall()
        modes: dict[str, str] = {}
        for row in versions:
            try:
                definition = json.loads(row["definition_json"])
                canonical = json.dumps(
                    definition,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                mode = str(definition["mode"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PageIntelligenceIntegrityError(
                    "page scheme definition is invalid"
                ) from exc
            definition_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            version_id = hashlib.sha256(
                f"scheme-v1\0{row['scheme_id']}\0{definition_hash}".encode("utf-8")
            ).hexdigest()
            if (
                canonical != row["definition_json"]
                or definition_hash != row["definition_hash"]
                or version_id != row["version_id"]
                or mode not in {"exclusive", "multilabel"}
            ):
                raise PageIntelligenceIntegrityError("page scheme identity mismatch")
            modes[str(row["version_id"])] = mode
        mismatch = db.execute(
            """SELECT 1 FROM page_scheme_activations AS a
                 JOIN page_scheme_versions AS v ON v.version_id=a.version_id
                WHERE a.scheme_id!=v.scheme_id LIMIT 1"""
        ).fetchone()
        if mismatch:
            raise PageIntelligenceIntegrityError("page scheme activation mismatch")
        for row in db.execute(
            """SELECT version_id,page_id,COUNT(*) AS assignments
                 FROM page_scheme_assignments
                GROUP BY version_id,page_id HAVING COUNT(*)>1"""
        ).fetchall():
            if modes.get(str(row["version_id"])) == "exclusive":
                raise PageIntelligenceIntegrityError(
                    "exclusive page scheme has overlapping assignments"
                )
        mismatch = db.execute(
            """SELECT 1 FROM page_catalog_index_links AS l
                 JOIN page_catalog AS p ON p.page_id=l.page_id
                WHERE l.site_id!=p.site_id LIMIT 1"""
        ).fetchone()
        if mismatch:
            raise PageIntelligenceIntegrityError("page index link site mismatch")
        return {
            "pages": len(pages),
            "scheme_versions": len(versions),
            "daily_cells": int(db.execute("SELECT COUNT(*) FROM page_daily").fetchone()[0]),
        }

    def verify_page_intelligence_integrity(self) -> dict[str, int]:
        with self.connect(readonly=True) as db:
            return self._verify_page_intelligence_integrity_connection(db)

    def backup(self, destination: str | Path) -> Path:
        target = Path(destination)
        if target.resolve() == self.path.resolve():
            raise ValueError("backup source and target must be different paths")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.backup-",
            suffix=".db",
            dir=target.parent,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            with self.connect(readonly=True) as source:
                source.execute("BEGIN")
                schema_row = source.execute(
                    "SELECT version FROM schema_meta LIMIT 1"
                ).fetchone()
                if schema_row is None:
                    raise ValueError("backup source schema marker is missing")
                source_schema = int(schema_row[0])
                with closing(sqlite3.connect(temporary_path)) as output:
                    source.backup(output)
                    output.commit()
            self._validate_restored_path(temporary_path, source_schema)
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, target)
            return target
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
            for suffix in ("-wal", "-shm", "-journal"):
                temporary_sidecar = Path(f"{temporary_path}{suffix}")
                if temporary_sidecar.exists():
                    temporary_sidecar.unlink()

    @staticmethod
    def _validate_restore_connection(db: sqlite3.Connection) -> int:
        db.execute("PRAGMA foreign_keys=ON")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("backup integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("backup foreign-key check failed")
        schema_row = db.execute(
            "SELECT version FROM schema_meta LIMIT 1"
        ).fetchone()
        if schema_row is None:
            raise ValueError("backup schema marker is missing")
        source_schema = int(schema_row[0])
        if source_schema not in MIGRATIONS:
            raise ValueError(
                f"backup schema {source_schema} is outside supported range "
                f"1-{SCHEMA_VERSION}"
            )
        if source_schema >= 5:
            try:
                SQLiteMetricStore._verify_definition_integrity_connection(db)
            except DefinitionIntegrityError as exc:
                raise ValueError("backup definition integrity check failed") from exc
        if source_schema >= 6:
            try:
                SQLiteMetricStore._verify_acquisition_integrity_connection(db)
            except AcquisitionIntegrityError as exc:
                raise ValueError("backup acquisition integrity check failed") from exc
        if source_schema >= 8:
            try:
                SQLiteMetricStore._verify_page_intelligence_integrity_connection(db)
            except PageIntelligenceIntegrityError as exc:
                raise ValueError(
                    "backup page intelligence integrity check failed"
                ) from exc
        return source_schema

    @staticmethod
    def _validate_restored_path(path: Path, expected_schema: int) -> None:
        uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True)) as restored:
            restored.row_factory = sqlite3.Row
            restored.execute("BEGIN")
            actual_schema = SQLiteMetricStore._validate_restore_connection(restored)
            if actual_schema != expected_schema:
                raise ValueError("restored schema marker changed during copy")

    def restore(self, source: str | Path, *, confirmed: bool = False) -> None:
        if not confirmed: raise ValueError("restore requires explicit confirmation")
        source_path = Path(source)
        if source_path.resolve() == self.path.resolve():
            raise ValueError("restore source and target must be different paths")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.restore-",
            suffix=".db",
            dir=self.path.parent,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        source_uri = f"file:{source_path.as_posix()}?mode=ro"
        try:
            with closing(sqlite3.connect(source_uri, uri=True)) as input_db:
                input_db.row_factory = sqlite3.Row
                input_db.execute("BEGIN")
                source_schema = self._validate_restore_connection(input_db)
                if self.path.exists():
                    with self.connect() as current:
                        current.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    self.backup(
                        self.path.with_suffix(self.path.suffix + ".pre-restore")
                    )
                with closing(sqlite3.connect(temporary_path)) as output_db:
                    input_db.backup(output_db)
                    output_db.commit()
            self._validate_restored_path(temporary_path, source_schema)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self.path}{suffix}")
                if sidecar.exists():
                    sidecar.unlink()
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
            for suffix in ("-wal", "-shm"):
                temporary_sidecar = Path(f"{temporary_path}{suffix}")
                if temporary_sidecar.exists():
                    temporary_sidecar.unlink()

    def enforce_retention(self, *, hourly_days: int, daily_days: int) -> int:
        now = datetime.now(UTC); hourly = _iso(now - timedelta(days=hourly_days)); daily = _iso(now - timedelta(days=daily_days))
        with self.connect() as db:
            cursor = db.execute("DELETE FROM metric_facts WHERE (grain='hour' AND end_at<?) OR (grain='day' AND end_at<?)", (hourly, daily))
            return cursor.rowcount
