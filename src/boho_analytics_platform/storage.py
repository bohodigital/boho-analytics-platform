"""SQLite metric store with migrations, leases, ledgers, and recovery helpers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.resources import files
from pathlib import Path

from .models import CapabilitySnapshot, Completeness, MetricPoint, QueryWindow, TimeGrain


SCHEMA_VERSION = 3
MIGRATIONS = {1: "001_initial.sql", 2: "002_site_graph.sql", 3: "003_sync_coverage.sql"}
CURRENT_IDENTITY_VERSIONS = {"cloudflare-forms": 2, "forms-inbox": 2}


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


def _key(point: MetricPoint) -> str:
    identity = [
        point.client_id, point.site_id, point.source, point.metric, point.unit,
        _iso(point.start), _iso(point.end), point.grain.value, list(point.dimensions)
    ]
    identity_version = CURRENT_IDENTITY_VERSIONS.get(point.source, 1)
    if identity_version != 1:
        identity.append(identity_version)
    identity = json.dumps(identity, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


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
            db.execute(
                """UPDATE sync_runs SET finished_at=?,status=?,points_written=?,error_category=?,
                  error_message=?,result_kind=?,data_through=? WHERE id=?""",
                (
                    _iso(datetime.now(UTC)),
                    status,
                    points,
                    category,
                    safe_message,
                    result_kind,
                    _iso(data_through) if data_through else None,
                    run_id,
                ),
            )

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

    def set_watermark(self, binding_key: str, value: datetime) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO watermarks VALUES (?,?,?) ON CONFLICT(binding_key)
              DO UPDATE SET completed_through=excluded.completed_through,updated_at=excluded.updated_at""",
              (binding_key, _iso(value), _iso(datetime.now(UTC))))

    def integrity_check(self) -> str:
        with self.connect(readonly=True) as db:
            return str(db.execute("PRAGMA integrity_check").fetchone()[0])

    def backup(self, destination: str | Path) -> Path:
        target = Path(destination); target.parent.mkdir(parents=True, exist_ok=True)
        with self.connect(readonly=True) as source, closing(sqlite3.connect(target)) as output:
            source.backup(output)
        return target

    def restore(self, source: str | Path, *, confirmed: bool = False) -> None:
        if not confirmed: raise ValueError("restore requires explicit confirmation")
        source_path = Path(source)
        with closing(sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise ValueError("backup integrity check failed")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            with self.connect() as current: current.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.backup(self.path.with_suffix(self.path.suffix + ".pre-restore"))
        with closing(sqlite3.connect(source_path)) as input_db, closing(sqlite3.connect(self.path)) as output_db:
            input_db.backup(output_db)

    def enforce_retention(self, *, hourly_days: int, daily_days: int) -> int:
        now = datetime.now(UTC); hourly = _iso(now - timedelta(days=hourly_days)); daily = _iso(now - timedelta(days=daily_days))
        with self.connect() as db:
            cursor = db.execute("DELETE FROM metric_facts WHERE (grain='hour' AND end_at<?) OR (grain='day' AND end_at<?)", (hourly, daily))
            return cursor.rowcount
