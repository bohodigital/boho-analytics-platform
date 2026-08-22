from __future__ import annotations

import hashlib
import math
import sqlite3
import tempfile
import unittest
from contextlib import closing
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from boho_analytics_platform.storage import (
    MIGRATIONS,
    SCHEMA_VERSION,
    SQLiteMetricStore,
    _apply_migration,
)


def schema_version(path: Path) -> int:
    with closing(sqlite3.connect(path)) as db:
        return int(db.execute("SELECT version FROM schema_meta").fetchone()[0])


def table_fingerprints(path: Path) -> dict[str, tuple[int, str]]:
    with closing(sqlite3.connect(path)) as db:
        names = [
            row[0]
            for row in db.execute(
                """SELECT name
                     FROM sqlite_master
                    WHERE type='table' AND name NOT LIKE 'sqlite_%'
                      AND name != 'schema_meta'
                 ORDER BY name"""
            )
        ]
        output: dict[str, tuple[int, str]] = {}
        for name in names:
            columns = len(db.execute(f'PRAGMA table_info("{name}")').fetchall())
            rows = db.execute(
                f'SELECT * FROM "{name}" ORDER BY '
                + ",".join(str(index) for index in range(1, columns + 1))
            ).fetchall()
            digest = hashlib.sha256()
            for row in rows:
                for value in row:
                    if value is None:
                        encoded = b"n"
                    elif isinstance(value, int):
                        encoded = b"i" + str(value).encode("ascii")
                    elif isinstance(value, float):
                        if not math.isfinite(value):
                            raise AssertionError(
                                "SQLite fingerprint contains non-finite float"
                            )
                        encoded = b"f" + value.hex().encode("ascii")
                    elif isinstance(value, str):
                        encoded = b"s" + value.encode("utf-8", errors="strict")
                    elif isinstance(value, bytes):
                        encoded = b"y" + value
                    else:
                        raise AssertionError(
                            f"unsupported SQLite value type: {type(value).__name__}"
                        )
                    digest.update(len(encoded).to_bytes(8, "big"))
                    digest.update(encoded)
            output[name] = (len(rows), digest.hexdigest())
    return output


def create_schema4(path: Path) -> SQLiteMetricStore:
    store = SQLiteMetricStore(path)
    with store.connect() as db:
        for version in range(1, 5):
            migration = (
                files("boho_analytics_platform.migrations")
                .joinpath(MIGRATIONS[version])
                .read_text(encoding="utf-8")
            )
            _apply_migration(db, migration, version)
        db.execute(
            """INSERT INTO metric_facts(
                 point_key,client_id,site_id,source,metric,unit,start_at,end_at,grain,
                 value,dimensions_json,completeness,observed_at,updated_at,identity_version
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "legacy-point",
                "client",
                "site",
                "fixture",
                "test.views",
                "count",
                "2026-07-01T00:00:00+00:00",
                "2026-07-02T00:00:00+00:00",
                "day",
                "7",
                "{}",
                "final",
                "2026-07-03T00:00:00+00:00",
                "2026-07-03T00:00:00+00:00",
                1,
            ),
        )
        db.execute(
            """INSERT INTO sync_runs(
                 id,connection_id,site_id,started_at,status,points_written,binding_key,
                 source,window_start,window_end,result_kind
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "legacy-run",
                "connection",
                "site",
                "2026-07-03T00:00:00+00:00",
                "success",
                1,
                "binding",
                "fixture",
                "2026-07-01T00:00:00+00:00",
                "2026-07-02T00:00:00+00:00",
                "data",
            ),
        )
    return store


def create_schema5(path: Path) -> SQLiteMetricStore:
    store = create_schema4(path)
    with store.connect() as db:
        migration = (
            files("boho_analytics_platform.migrations")
            .joinpath(MIGRATIONS[5])
            .read_text(encoding="utf-8")
        )
        _apply_migration(db, migration, 5)
    return store


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_empty_database_runs_ordered_migrations_through_schema8(self) -> None:
        path = self.root / "empty.db"
        store = SQLiteMetricStore(path)
        store.initialize()
        self.assertEqual(SCHEMA_VERSION, 8)
        self.assertEqual(schema_version(path), 8)
        self.assertEqual(store.integrity_check(), "ok")
        with store.connect(readonly=True) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM analytics_definition_versions"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM analytics_definition_activations"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM analytics_definition_retirements"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM acquisition_slices").fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM metric_fact_observations"
                ).fetchone()[0],
                0,
            )
        before = path.read_bytes()
        store.initialize()
        self.assertEqual(path.read_bytes(), before)

    def test_schema4_migration_preserves_every_legacy_row_and_fingerprint(self) -> None:
        path = self.root / "copied-schema4.db"
        store = create_schema4(path)
        before = table_fingerprints(path)

        store.initialize()

        self.assertEqual(schema_version(path), 8)
        after = table_fingerprints(path)
        self.assertEqual({name: after[name] for name in before}, before)
        self.assertEqual(store.integrity_check(), "ok")
        with store.connect(readonly=True) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            new_counts = tuple(
                db.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "analytics_definition_versions",
                    "analytics_definition_activations",
                    "analytics_definition_retirements",
                    "acquisition_slices",
                    "metric_fact_observations",
                )
            )
        self.assertEqual(new_counts, (0, 0, 0, 0, 0))

        migrated = path.read_bytes()
        store.initialize()
        self.assertEqual(path.read_bytes(), migrated)

    def test_migration_interruption_rolls_back_schema_and_tables(self) -> None:
        path = self.root / "interrupted.db"
        store = create_schema4(path)
        before = table_fingerprints(path)
        migration = (
            files("boho_analytics_platform.migrations")
            .joinpath(MIGRATIONS[5])
            .read_text(encoding="utf-8")
        )
        with store.connect() as db:
            with self.assertRaises(sqlite3.OperationalError):
                _apply_migration(
                    db,
                    migration + "\nSELECT * FROM deliberately_missing_table;",
                    5,
                )
        self.assertEqual(schema_version(path), 4)
        self.assertEqual(table_fingerprints(path), before)
        with closing(sqlite3.connect(path)) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertNotIn("analytics_definition_versions", tables)
        self.assertNotIn("analytics_definition_activations", tables)
        self.assertNotIn("analytics_definition_retirements", tables)

    def test_schema6_preserves_schema5_rows_and_adds_immutable_normalized_history(self) -> None:
        path = self.root / "copied-schema5.db"
        store = create_schema5(path)
        before = table_fingerprints(path)

        store.initialize()

        self.assertEqual(schema_version(path), 8)
        after = table_fingerprints(path)
        self.assertEqual({name: after[name] for name in before}, before)
        with store.connect() as db:
            slice_columns = {
                row[1] for row in db.execute("PRAGMA table_info(acquisition_slices)")
            }
            observation_columns = {
                row[1]
                for row in db.execute("PRAGMA table_info(metric_fact_observations)")
            }
            self.assertTrue(
                {
                    "data_state",
                    "provider_scope",
                    "request_dimensions_json",
                    "provider_aggregation",
                }.issubset(slice_columns)
            )
            forbidden = {
                "raw_payload",
                "provider_payload",
                "payload_json",
                "response_json",
            }
            self.assertTrue(forbidden.isdisjoint(slice_columns))
            self.assertTrue(forbidden.isdisjoint(observation_columns))
            db.execute(
                """INSERT INTO acquisition_slices(
                     id,sync_run_id,binding_key,slice_key,metric_family,start_at,end_at,
                     completeness,data_state,provider_scope,request_dimensions_json,
                     provider_aggregation,pages_fetched,raw_rows,accepted_rows,
                     rejected_rows,exhaustion_reason,recorded_at,record_hash
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "a" * 64,
                    "legacy-run",
                    "binding",
                    "fixture.slice",
                    "fixture",
                    "2026-07-01T00:00:00+00:00",
                    "2026-07-02T00:00:00+00:00",
                    "final",
                    "final",
                    "web",
                    "[]",
                    "byProperty",
                    1,
                    0,
                    0,
                    0,
                    "empty_first_page",
                    "2026-07-03T00:00:00+00:00",
                    "b" * 64,
                ),
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                db.execute(
                    "UPDATE acquisition_slices SET raw_rows=1 WHERE id=?",
                    ("a" * 64,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot be deleted"):
                db.execute("DELETE FROM acquisition_slices WHERE id=?", ("a" * 64,))

    def test_schema6_interruption_rolls_back_both_provenance_tables(self) -> None:
        path = self.root / "interrupted-schema6.db"
        store = create_schema5(path)
        before = table_fingerprints(path)
        migration = (
            files("boho_analytics_platform.migrations")
            .joinpath(MIGRATIONS[6])
            .read_text(encoding="utf-8")
        )
        with store.connect() as db:
            with self.assertRaises(sqlite3.OperationalError):
                _apply_migration(
                    db,
                    migration + "\nSELECT * FROM deliberately_missing_table;",
                    6,
                )
        self.assertEqual(schema_version(path), 5)
        self.assertEqual(table_fingerprints(path), before)
        with closing(sqlite3.connect(path)) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertNotIn("acquisition_slices", tables)
        self.assertNotIn("metric_fact_observations", tables)

    def test_schema4_backup_restore_and_older_runtime_refusal(self) -> None:
        source = self.root / "source-schema4.db"
        store = create_schema4(source)
        baseline = table_fingerprints(source)
        backup = self.root / "pre-migration-schema4.db"
        store.backup(backup)

        copied = self.root / "acceptance-copy.db"
        SQLiteMetricStore(copied).restore(backup, confirmed=True)
        copied_store = SQLiteMetricStore(copied)
        copied_store.initialize()
        copied_fingerprints = table_fingerprints(copied)
        self.assertEqual(
            {name: copied_fingerprints[name] for name in baseline}, baseline
        )
        with patch("boho_analytics_platform.storage.SCHEMA_VERSION", 4):
            with self.assertRaisesRegex(
                RuntimeError, "database schema 8 is newer than supported 4"
            ):
                copied_store.initialize()

        restored = self.root / "restored-schema4.db"
        SQLiteMetricStore(restored).restore(backup, confirmed=True)
        self.assertEqual(schema_version(restored), 4)
        self.assertEqual(table_fingerprints(restored), baseline)
        with closing(sqlite3.connect(restored)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
