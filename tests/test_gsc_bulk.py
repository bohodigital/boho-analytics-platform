from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from boho_analytics_platform.bulk_export.bigquery import (
    BigQueryBulkError,
    BigQueryBulkSource,
)
from boho_analytics_platform.bulk_export.config import (
    BulkExportConfigError,
    load_bulk_export_manifest,
)
from boho_analytics_platform.bulk_export.contracts import (
    ExportRevision,
    PartitionRead,
    PartitionTotals,
)
from boho_analytics_platform.bulk_export.engine import BulkExportEngine
from boho_analytics_platform.bulk_export.lake import BulkLakeError, SeagateBulkLake
from boho_analytics_platform.cli import _bulk_window, main


def manifest_text(root: Path, *, properties: str | None = None) -> str:
    properties = properties or """
  - site_id: example-site
    site_url: sc-domain:example.com
    dataset_id: searchconsole_example
    first_export_date: 2026-08-01
    identity_proof_date: 2026-08-01
"""
    return f"""schema_version: 1
warehouse:
  project_id: example-project
  location: US
  credential_ref: env:BIGQUERY_READER
  maximum_bytes_billed: 1073741824
  use_storage_api: false
storage:
  root: {root / 'lake'}
  required_mountpoint: {root}
  required_filesystem_uuid: 11111111-2222-3333-4444-555555555555
  minimum_free_bytes: 1073741824
  parquet_compression: zstd
  batch_rows: 50000
properties:
{properties}"""


class BulkManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "bulk.yaml"

    def load(self, mutate=lambda value: value):
        self.path.write_text(mutate(manifest_text(self.root)), encoding="utf-8")
        return load_bulk_export_manifest(self.path)

    def test_valid_manifest_keeps_bulk_storage_separate(self):
        manifest = self.load()
        self.assertEqual(manifest.schema_version, 1)
        self.assertEqual(manifest.properties[0].site_id, "example-site")
        self.assertEqual(manifest.storage.root, self.root / "lake")
        self.assertTrue(
            manifest.storage.identity_marker_name.endswith(
                "11111111-2222-3333-4444-555555555555"
            )
        )

    def test_inline_secret_and_unknown_fields_fail_closed(self):
        with self.assertRaisesRegex(BulkExportConfigError, "inline secret"):
            self.load(lambda value: value.replace(
                "  location: US", "  location: US\n  access_token: forbidden"
            ))
        with self.assertRaisesRegex(BulkExportConfigError, "unknown field"):
            self.load(lambda value: value.replace(
                "  batch_rows: 50000", "  batch_rows: 50000\n  fallback_to_sd: true"
            ))

    def test_storage_root_must_be_strictly_beneath_mount(self):
        with self.assertRaisesRegex(BulkExportConfigError, "strictly beneath"):
            self.load(lambda value: value.replace(
                f"  root: {self.root / 'lake'}", "  root: /tmp/not-the-seagate"
            ))

    def test_each_property_requires_a_distinct_searchconsole_dataset(self):
        duplicate = """
  - site_id: example-site
    site_url: sc-domain:example.com
    dataset_id: searchconsole_example
    first_export_date: 2026-08-01
    identity_proof_date: 2026-08-01
  - site_id: second-site
    site_url: sc-domain:second.example
    dataset_id: searchconsole_example
    first_export_date: 2026-08-01
    identity_proof_date: 2026-08-01
"""
        self.path.write_text(manifest_text(self.root, properties=duplicate), encoding="utf-8")
        with self.assertRaisesRegex(BulkExportConfigError, "distinct dataset"):
            load_bulk_export_manifest(self.path)

    def test_cli_validates_without_opening_credentials_or_storage(self):
        self.path.write_text(manifest_text(self.root), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["gsc-bulk", "validate", "--manifest", str(self.path)])
        self.assertEqual(status, 0)
        self.assertIn('"properties": ["example-site"]', output.getvalue())

    def test_status_and_verify_hold_the_lake_lock(self):
        manifest = self.load()

        class LockedLake:
            def __init__(self):
                self.events = []

            @contextmanager
            def lock(self):
                self.events.append("lock-enter")
                yield
                self.events.append("lock-exit")

            def status(self):
                self.events.append("status")
                return {"ok": True}

            def verify_all(self):
                self.events.append("verify")
                return {"ok": True}

        for command, action in (("status", "status"), ("verify", "verify")):
            with self.subTest(command=command):
                lake = LockedLake()
                with (
                    patch(
                        "boho_analytics_platform.bulk_export.config.load_bulk_export_manifest",
                        return_value=manifest,
                    ),
                    patch(
                        "boho_analytics_platform.bulk_export.lake.SeagateBulkLake",
                        return_value=lake,
                    ),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(
                        main(["gsc-bulk", command, "--manifest", str(self.path)]),
                        0,
                    )
                self.assertEqual(
                    lake.events,
                    ["lock-enter", action, "lock-exit"],
                )

    def test_yaml_timestamp_is_not_accepted_as_a_first_export_date(self):
        with self.assertRaisesRegex(BulkExportConfigError, "without a time"):
            self.load(lambda value: value.replace(
                "first_export_date: 2026-08-01",
                "first_export_date: 2026-08-01T00:00:00Z",
            ))

    def test_default_bulk_window_honors_an_explicit_lag(self):
        start, end = _bulk_window(SimpleNamespace(
            days=None,
            start=None,
            end=None,
            end_lag_days=10,
        ))
        expected_end = datetime.now(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=10)
        self.assertEqual(end, expected_end)
        self.assertEqual((end - start).days, 7)

    def test_explicit_bulk_dates_reject_an_ignored_lag(self):
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            _bulk_window(SimpleNamespace(
                days=None,
                start="2026-08-01",
                end="2026-08-03",
                end_lag_days=3,
            ))

    def test_ordinary_cli_import_does_not_require_posix_fcntl(self):
        script = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'fcntl':
        raise ModuleNotFoundError("No module named 'fcntl'", name='fcntl')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import boho_analytics_platform.cli
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_manifest_validation_does_not_require_posix_fcntl(self):
        self.path.write_text(manifest_text(self.root), encoding="utf-8")
        script = """
import builtins
import sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'fcntl':
        raise ModuleNotFoundError("No module named 'fcntl'", name='fcntl')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from boho_analytics_platform.cli import main
raise SystemExit(main(['gsc-bulk', 'validate', '--manifest', sys.argv[1]]))
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        completed = subprocess.run(
            [sys.executable, "-c", script, str(self.path)],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('"ok": true', completed.stdout)


class _Parameter:
    def __init__(self, name, field_type, value):
        self.name = name
        self.field_type = field_type
        self.value = value


class _QueryJobConfig:
    def __init__(self, **values):
        self.__dict__.update(values)


class _BigQueryModule:
    ScalarQueryParameter = _Parameter
    ArrayQueryParameter = _Parameter
    QueryJobConfig = _QueryJobConfig


class _RowIterator:
    def __init__(self, batches=(), schema=None):
        self.batches = batches
        self.arrow_arguments = None
        self.schema = schema or (
            SimpleNamespace(
                name="data_date",
                field_type="DATE",
                mode="NULLABLE",
                fields=(),
            ),
        )

    def to_arrow_iterable(self, **arguments):
        self.arrow_arguments = arguments
        return iter(self.batches)


class _Job:
    def __init__(self, rows, *, job_id="safe_job", processed=0, billed=0):
        self.rows = rows
        self.job_id = job_id
        self.total_bytes_processed = processed
        self.total_bytes_billed = billed
        self.result_arguments = None

    def result(self, **arguments):
        self.result_arguments = arguments
        return self.rows


class _Client:
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.queries = []

    @staticmethod
    def field(name, field_type):
        return SimpleNamespace(
            name=name,
            field_type=field_type,
            mode="NULLABLE",
            fields=(),
        )

    def table(self, table_name):
        common = (
            self.field("data_date", "DATE"),
            self.field("site_url", "STRING"),
            self.field("query", "STRING"),
            self.field("is_anonymized_query", "BOOLEAN"),
            self.field("is_anonymized_discover", "BOOLEAN"),
            self.field("country", "STRING"),
            self.field("search_type", "STRING"),
            self.field("device", "STRING"),
            self.field("clicks", "INTEGER"),
            self.field("impressions", "INTEGER"),
        )
        if table_name == "searchdata_site_impression":
            schema = common + (self.field("sum_top_position", "INTEGER"),)
        elif table_name == "searchdata_url_impression":
            schema = common + (
                self.field("url", "STRING"),
                self.field("sum_position", "INTEGER"),
            )
        else:
            schema = (
                self.field("agenda", "STRING"),
                self.field("namespace", "STRING"),
                self.field("data_date", "DATE"),
                self.field("epoch_version", "INTEGER"),
                self.field("publish_time", "TIMESTAMP"),
            )
        partitioning = (
            SimpleNamespace(field="data_date", type_="DAY")
            if table_name.startswith("searchdata_")
            else None
        )
        return SimpleNamespace(
            schema=schema,
            num_rows=12,
            time_partitioning=partitioning,
        )

    def query(self, sql, **arguments):
        self.queries.append((sql, arguments))
        return self.jobs.pop(0)

    def get_dataset(self, _dataset_id):
        return SimpleNamespace(location="US")

    def get_table(self, table_id):
        return self.table(table_id.rsplit(".", 1)[-1])


class BigQuerySourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        path = root / "bulk.yaml"
        path.write_text(manifest_text(root), encoding="utf-8")
        self.manifest = load_bulk_export_manifest(path)
        self.property = self.manifest.properties[0]

    def source(self, client):
        return BigQueryBulkSource(
            self.manifest,
            object(),
            client=client,
            bigquery_module=_BigQueryModule,
        )

    def test_service_account_credential_accepts_direct_or_wrapped_json(self):
        class Credential:
            def __init__(self, values):
                self.values = values

            def read(self, field):
                return self.values.get(field)

        direct = Credential({
            "type": b"service_account",
            "project_id": b"example-project",
            "client_email": b"reader@example-project.iam.gserviceaccount.com",
        })
        wrapped = Credential({
            "service_account_json": (
                b'{"type":"service_account","project_id":"example-project"}'
            )
        })
        self.assertEqual(
            BigQueryBulkSource._service_account_info(direct)["type"],
            "service_account",
        )
        self.assertEqual(
            BigQueryBulkSource._service_account_info(wrapped)["project_id"],
            "example-project",
        )

    def test_bigquery_source_rejects_an_unconfigured_property_object(self):
        unconfigured = type(self.property)(
            site_id="other-site",
            site_url="sc-domain:other.example",
            dataset_id="searchconsole_other",
            first_export_date=self.property.first_export_date,
            identity_proof_date=self.property.identity_proof_date,
        )
        with self.assertRaisesRegex(BigQueryBulkError, "unsupported.*table"):
            self.source(_Client([])).probe(unconfigured)

    def test_bigquery_integer_controls_do_not_truncate_fractional_values(self):
        client = _Client([_Job(({
            "partition_row_count": 1.5,
            "row_count": 1.5,
            "clicks": 4,
            "impressions": 20,
            "position_sum": 100,
        },))])
        revision = ExportRevision(
            "searchdata_site_impression",
            date(2026, 8, 2),
            0,
            datetime(2026, 8, 4, tzinfo=UTC),
        )
        with self.assertRaisesRegex(BigQueryBulkError, "row count"):
            self.source(client).read_partition(self.property, revision)

    def test_partition_read_rejects_a_naive_source_revision(self):
        revision = ExportRevision(
            "searchdata_site_impression",
            date(2026, 8, 2),
            0,
            datetime(2026, 8, 4),
        )
        with self.assertRaisesRegex(BigQueryBulkError, "invalid.*revision"):
            self.source(_Client([])).read_partition(self.property, revision)

    def test_export_log_query_is_parameterized_and_partition_bounded(self):
        publish_time = datetime(2026, 8, 4, 8, tzinfo=UTC)
        client = _Client([_Job(({
            "namespace": "searchdata_site_impression",
            "data_date": date(2026, 8, 2),
            "epoch_version": 1,
            "publish_time": publish_time,
        },))])
        revisions = self.source(client).revisions(
            self.property, date(2026, 8, 1), date(2026, 8, 3)
        )
        self.assertEqual(revisions[0].epoch_version, 1)
        sql, arguments = client.queries[0]
        self.assertIn("data_date >= @start_date", sql)
        self.assertIn("data_date < @end_date", sql)
        self.assertIn("ROW_NUMBER() OVER", sql)
        self.assertIn("ORDER BY epoch_version DESC, publish_time DESC", sql)
        self.assertNotIn("MAX(epoch_version)", sql)
        self.assertNotIn("2026-08-01", sql)
        self.assertEqual(
            arguments["job_config"].maximum_bytes_billed, 1_073_741_824
        )

    def test_export_log_requires_an_exact_date_and_aware_publish_time(self):
        invalid_rows = (
            {
                "namespace": "searchdata_site_impression",
                "data_date": datetime(2026, 8, 2, tzinfo=UTC),
                "epoch_version": 0,
                "publish_time": datetime(2026, 8, 4, tzinfo=UTC),
            },
            {
                "namespace": "searchdata_site_impression",
                "data_date": date(2026, 8, 2),
                "epoch_version": 0,
                "publish_time": datetime(2026, 8, 4),
            },
        )
        for row in invalid_rows:
            with self.subTest(row=row):
                with self.assertRaisesRegex(BigQueryBulkError, "invalid row"):
                    self.source(_Client([_Job((row,))])).revisions(
                        self.property, date(2026, 8, 1), date(2026, 8, 3)
                    )

    def test_partition_read_filters_exact_date_and_property_and_preserves_arrow_stream(self):
        totals_job = _Job(({
            "partition_row_count": 3,
            "row_count": 3,
            "clicks": 4,
            "impressions": 20,
            "position_sum": 100,
        },), processed=10, billed=20)
        rows = _RowIterator(("batch-one", "batch-two"))
        raw_job = _Job(rows, processed=30, billed=40)
        post_read_job = _Job(({
            "agenda": "SEARCHDATA",
            "namespace": "searchdata_site_impression",
            "data_date": date(2026, 8, 2),
            "epoch_version": 0,
            "publish_time": datetime(2026, 8, 4, tzinfo=UTC),
        },))
        client = _Client([totals_job, raw_job, post_read_job])
        revision = ExportRevision(
            "searchdata_site_impression",
            date(2026, 8, 2),
            0,
            datetime(2026, 8, 4, tzinfo=UTC),
        )
        result = self.source(client).read_partition(self.property, revision)
        self.assertEqual(result.expected_totals, PartitionTotals(
            3, 4, 20, Decimal("100")
        ))
        self.assertEqual(tuple(result.batches), ("batch-one", "batch-two"))
        self.assertEqual(
            [item["bytes_processed"] for item in result.query_audit],
            [10, 30, 0],
        )
        self.assertEqual(
            [item["bytes_billed"] for item in result.query_audit],
            [20, 40, 0],
        )
        for sql, _arguments in client.queries[:2]:
            self.assertIn("data_date = @data_date", sql)
            self.assertIn("site_url", sql)
            self.assertNotIn(self.property.site_url, sql)
        self.assertIn("ExportLog", client.queries[2][0])
        self.assertIn("ORDER BY epoch_version, publish_time", client.queries[2][0])
        self.assertEqual(raw_job.result_arguments, {"page_size": 50000})
        self.assertEqual(rows.arrow_arguments["max_queue_size"], 1)
        self.assertEqual(result.source_schema[0]["name"], "data_date")
        self.assertEqual(result.export_log_history[0]["epoch_version"], 0)

    def test_probe_verifies_dataset_location_and_three_required_tables(self):
        identity = {"partition_rows": 12, "matching_rows": 12}
        client = _Client([_Job((identity,)), _Job((identity,))])
        value = self.source(client).probe(self.property)
        self.assertEqual(value["location"], "US")
        self.assertEqual(set(value["tables"]), {
            "searchdata_site_impression", "searchdata_url_impression", "ExportLog"
        })
        self.assertEqual(
            value["tables"]["searchdata_site_impression"]["partition_field"],
            "data_date",
        )
        self.assertTrue(
            value["tables"]["searchdata_url_impression"]["identity"]["verified"]
        )

    def test_probe_rejects_wrong_property_dataset_mapping(self):
        identity = {"partition_rows": 12, "matching_rows": 0}
        client = _Client([_Job((identity,))])
        with self.assertRaisesRegex(BigQueryBulkError, "does not match"):
            self.source(client).probe(self.property)

    def test_probe_rejects_lookalike_table_schema(self):
        client = _Client([])
        original = client.table

        def missing_required(table_name):
            table = original(table_name)
            if table_name == "searchdata_site_impression":
                table.schema = tuple(
                    field for field in table.schema if field.name != "clicks"
                )
            return table

        client.table = missing_required
        with self.assertRaisesRegex(BigQueryBulkError, "required Search Console schema"):
            self.source(client).probe(self.property)

    def test_probe_requires_the_discover_anonymization_field(self):
        identity = {"partition_rows": 12, "matching_rows": 12}
        client = _Client([_Job((identity,))])
        original = client.table

        def missing_discover_flag(table_name):
            table = original(table_name)
            if table_name == "searchdata_url_impression":
                table.schema = tuple(
                    field
                    for field in table.schema
                    if field.name != "is_anonymized_discover"
                )
            return table

        client.table = missing_discover_flag
        with self.assertRaisesRegex(BigQueryBulkError, "required Search Console schema"):
            self.source(client).probe(self.property)

    def test_partition_lineage_uses_the_query_result_schema(self):
        totals = _Job(({
            "partition_row_count": 0,
            "row_count": 0,
            "clicks": 0,
            "impressions": 0,
            "position_sum": 0,
        },))
        rows = _RowIterator((), schema=(_Client.field("snapshot_field", "STRING"),))
        post = _Job(({
            "agenda": "SEARCHDATA",
            "namespace": "searchdata_site_impression",
            "data_date": date(2026, 8, 2),
            "epoch_version": 0,
            "publish_time": datetime(2026, 8, 4, tzinfo=UTC),
        },))
        client = _Client([totals, _Job(rows), post])
        revision = ExportRevision(
            "searchdata_site_impression",
            date(2026, 8, 2),
            0,
            datetime(2026, 8, 4, tzinfo=UTC),
        )
        result = self.source(client).read_partition(self.property, revision)
        tuple(result.batches)
        self.assertEqual(result.source_schema[0]["name"], "snapshot_field")

    def test_post_read_epoch_change_aborts_the_stream(self):
        totals = _Job(({
            "partition_row_count": 0,
            "row_count": 0,
            "clicks": 0,
            "impressions": 0,
            "position_sum": 0,
        },))
        rows = _RowIterator(())
        client = _Client([totals, _Job(rows), _Job(({
            "agenda": "SEARCHDATA",
            "namespace": "searchdata_site_impression",
            "data_date": date(2026, 8, 2),
            "epoch_version": 1,
            "publish_time": datetime(2026, 8, 5, tzinfo=UTC),
        },))])
        revision = ExportRevision(
            "searchdata_site_impression",
            date(2026, 8, 2),
            0,
            datetime(2026, 8, 4, tzinfo=UTC),
        )
        result = self.source(client).read_partition(self.property, revision)
        with self.assertRaisesRegex(BigQueryBulkError, "changed during"):
            tuple(result.batches)


class _FakeSource:
    def __init__(self, revisions, *, probe_error=None):
        self._revisions = tuple(revisions)
        self.probe_error = probe_error
        self.probes = []
        self.reads = []

    def revisions(self, _property, _start, _end):
        return self._revisions

    def probe(self, property_config):
        self.probes.append(property_config.site_id)
        if self.probe_error is not None:
            raise self.probe_error
        return {"ok": True}

    def read_partition(self, _property, revision):
        self.reads.append(revision)
        return PartitionRead(
            (),
            (),
            PartitionTotals(0, 0, 0, Decimal(0)),
            [{"role": "fake", "job_id": "job", "bytes_processed": 0, "bytes_billed": 0}],
            [{
                "agenda": "SEARCHDATA",
                "namespace": revision.namespace,
                "data_date": revision.data_date.isoformat(),
                "epoch_version": revision.epoch_version,
                "publish_time": revision.publish_time.isoformat(),
            }],
        )


class _FakeLake:
    def __init__(self, current=None):
        self.current = current or {}
        self.writes = []

    @contextmanager
    def lock(self):
        yield

    def current_epoch(self, property_config, revision):
        return self.current.get((property_config.site_id, revision.namespace, revision.data_date))

    def verify_partition(self, _property, _revision):
        return {"totals": {"row_count": 7}}

    def write_partition(self, property_config, revision, _read):
        self.writes.append((property_config.site_id, revision.namespace, revision.epoch_version))
        return {"status": "written", "totals": {"row_count": 0}}


class BulkEngineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        path = root / "bulk.yaml"
        path.write_text(manifest_text(root), encoding="utf-8")
        self.manifest = load_bulk_export_manifest(path)
        self.day = date(2026, 8, 2)
        self.published = datetime(2026, 8, 4, tzinfo=UTC)

    def revision(self, namespace, epoch=0):
        return ExportRevision(namespace, self.day, epoch, self.published)

    def test_waits_for_both_export_namespaces(self):
        source = _FakeSource([self.revision("searchdata_site_impression")])
        lake = _FakeLake()
        results = BulkExportEngine(self.manifest, source, lake).sync(
            self.day, self.day.replace(day=3)
        )
        self.assertEqual(results[0].status, "export-log-incomplete")
        self.assertIn("searchdata_url_impression", results[0].error_category)
        self.assertEqual(source.reads, [])
        self.assertEqual(lake.writes, [])
        self.assertEqual(source.probes, ["example-site"])

    def test_sync_never_publishes_before_property_identity_probe(self):
        source = _FakeSource(
            [
                self.revision("searchdata_site_impression"),
                self.revision("searchdata_url_impression"),
            ],
            probe_error=BigQueryBulkError("mapping mismatch"),
        )
        lake = _FakeLake()
        results = BulkExportEngine(self.manifest, source, lake).sync(
            self.day, self.day.replace(day=3)
        )
        self.assertEqual([item.status for item in results], ["failed"])
        self.assertEqual(source.reads, [])
        self.assertEqual(lake.writes, [])

    def test_same_epoch_is_verified_and_new_epoch_is_immutable(self):
        revisions = [
            self.revision("searchdata_site_impression", 2),
            self.revision("searchdata_url_impression", 2),
        ]
        current = {
            ("example-site", "searchdata_site_impression", self.day): 2,
            ("example-site", "searchdata_url_impression", self.day): 1,
        }
        source = _FakeSource(revisions)
        lake = _FakeLake(current)
        results = BulkExportEngine(self.manifest, source, lake).sync(
            self.day, self.day.replace(day=3)
        )
        self.assertEqual([item.status for item in results], ["current", "written"])
        self.assertEqual(lake.writes, [
            ("example-site", "searchdata_url_impression", 2)
        ])

    def test_windows_are_bounded(self):
        engine = BulkExportEngine(self.manifest, _FakeSource([]), _FakeLake())
        with self.assertRaisesRegex(ValueError, "366"):
            engine.sync(date(2025, 1, 1), date(2026, 2, 1))


class _TestLake(SeagateBulkLake):
    def preflight(self, *, create=False):
        if create:
            self.config.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.config.root.chmod(0o700)
        if not self.config.root.is_dir():
            raise BulkLakeError("test lake is unavailable")
        return {"ok": True, "root": str(self.config.root)}


class BulkLakeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        path = self.root / "bulk.yaml"
        path.write_text(manifest_text(self.root), encoding="utf-8")
        self.manifest = load_bulk_export_manifest(path)
        self.property = self.manifest.properties[0]
        self.revision = ExportRevision(
            "searchdata_site_impression",
            date(2026, 8, 2),
            0,
            datetime(2026, 8, 4, tzinfo=UTC),
        )

    @contextmanager
    def simulated_mount(
        self,
        *,
        marker=True,
        marker_mode=0o600,
        marker_owner=True,
        uuid_exists=True,
        uuid_matches=True,
        available_bytes=2_000_000_000,
    ):
        marker_path = self.root / self.manifest.storage.identity_marker_name
        if marker:
            marker_path.touch(mode=0o600)
            marker_path.chmod(marker_mode)
        real_exists = Path.exists
        real_stat = Path.stat
        real_lstat = Path.lstat
        device_path = (
            Path("/dev/disk/by-uuid")
            / self.manifest.storage.required_filesystem_uuid
        )
        mount_device = 4242

        def value(original, **changes):
            fields = {
                "st_mode": original.st_mode,
                "st_uid": original.st_uid,
                "st_dev": original.st_dev,
                "st_rdev": getattr(original, "st_rdev", 0),
                "st_size": original.st_size,
            }
            fields.update(changes)
            return SimpleNamespace(**fields)

        def fake_exists(path):
            if path == device_path:
                return uuid_exists
            return real_exists(path)

        def fake_stat(path, *args, **kwargs):
            if path == device_path:
                return SimpleNamespace(st_rdev=(mount_device if uuid_matches else 9999))
            original = real_stat(path, *args, **kwargs)
            if path == self.root.parent:
                return value(original, st_dev=1111)
            if path == self.root or self.root in path.parents:
                return value(original, st_dev=mount_device)
            return original

        def fake_lstat(path, *args, **kwargs):
            original = real_lstat(path, *args, **kwargs)
            if path == marker_path:
                return value(
                    original,
                    st_dev=mount_device,
                    st_uid=(original.st_uid if marker_owner else original.st_uid + 1),
                )
            if path == self.root or self.root in path.parents:
                return value(original, st_dev=mount_device)
            return original

        with (
            patch("os.path.ismount", return_value=True),
            patch.object(Path, "exists", fake_exists),
            patch.object(Path, "stat", fake_stat),
            patch.object(Path, "lstat", fake_lstat),
            patch("os.statvfs", return_value=SimpleNamespace(
                f_bavail=available_bytes,
                f_frsize=1,
            )),
        ):
            yield

    @staticmethod
    def batch():
        import pyarrow as arrow

        return arrow.record_batch({
            "data_date": [date(2026, 8, 2), date(2026, 8, 2)],
            "site_url": ["sc-domain:example.com", "sc-domain:example.com"],
            "query": ["private wording", ""],
            "is_anonymized_query": [False, True],
            "country": ["USA", "USA"],
            "search_type": ["web", "web"],
            "device": ["DESKTOP", "MOBILE"],
            "clicks": [3, 1],
            "impressions": [10, 10],
            "sum_top_position": [40, 60],
        })

    @staticmethod
    def url_batch():
        import pyarrow as arrow

        return arrow.record_batch({
            "data_date": [date(2026, 8, 2), date(2026, 8, 2)],
            "site_url": ["sc-domain:example.com", "sc-domain:example.com"],
            "url": ["https://example.com/one", "https://example.com/two"],
            "query": ["private wording", ""],
            "is_anonymized_query": [False, True],
            "is_anonymized_discover": [False, False],
            "country": ["USA", "USA"],
            "search_type": ["WEB", "WEB"],
            "device": ["DESKTOP", "MOBILE"],
            "clicks": [3, 1],
            "impressions": [10, 10],
            "sum_position": [40, 60],
        })

    def read(self, *, batches=None, expected=None, revision=None):
        batches = (self.batch(),) if batches is None else tuple(batches)
        expected = expected or PartitionTotals(2, 4, 20, Decimal(100))
        revision = revision or self.revision
        schema_batch = batches[0] if batches else (
            self.url_batch()
            if revision.namespace == "searchdata_url_impression"
            else self.batch()
        )
        source_schema = tuple({
            "name": field.name,
            "type": str(field.type),
            "mode": "NULLABLE",
            "fields": (),
        } for field in schema_batch.schema)
        return PartitionRead(
            batches=batches,
            source_schema=source_schema,
            expected_totals=expected,
            query_audit=[
                {
                    "role": role,
                    "job_id": f"safe_job_{index}",
                    "bytes_processed": 100 if role == "partition-data" else 0,
                    "bytes_billed": 100 if role == "partition-data" else 0,
                }
                for index, role in enumerate((
                    "partition-controls",
                    "partition-data",
                    "export-log-post-read",
                ))
            ],
            export_log_history=[
                {
                    "agenda": "SEARCHDATA",
                    "namespace": revision.namespace,
                    "data_date": revision.data_date.isoformat(),
                    "epoch_version": epoch,
                    "publish_time": (
                        revision.publish_time - timedelta(
                            days=revision.epoch_version - epoch
                        )
                    ).isoformat(),
                }
                for epoch in range(revision.epoch_version + 1)
            ],
        )

    def test_real_preflight_refuses_an_ordinary_directory_as_the_mount(self):
        marker = self.root / self.manifest.storage.identity_marker_name
        marker.touch(mode=0o600)
        with self.assertRaisesRegex(BulkLakeError, "not mounted"):
            SeagateBulkLake(self.manifest).preflight(create=True)
        self.assertFalse(self.manifest.storage.root.exists())

    def test_preflight_requires_exact_uuid_marker_and_free_space(self):
        lake = SeagateBulkLake(self.manifest)
        with self.simulated_mount():
            self.assertTrue(lake.preflight(create=True)["ok"])

        self.manifest.storage.root.rmdir()
        with self.simulated_mount(uuid_exists=False):
            with self.assertRaisesRegex(BulkLakeError, "UUID device"):
                lake.preflight(create=True)
        with self.simulated_mount(uuid_matches=False):
            with self.assertRaisesRegex(BulkLakeError, "does not match"):
                lake.preflight(create=True)
        with self.simulated_mount(marker_mode=0o640):
            with self.assertRaisesRegex(BulkLakeError, "marker is invalid"):
                lake.preflight(create=True)
        with self.simulated_mount(marker_owner=False):
            with self.assertRaisesRegex(BulkLakeError, "marker is invalid"):
                lake.preflight(create=True)
        marker_path = self.root / self.manifest.storage.identity_marker_name
        marker_path.unlink(missing_ok=True)
        with self.simulated_mount(marker=False):
            with self.assertRaisesRegex(BulkLakeError, "marker is missing"):
                lake.preflight(create=True)
        with self.simulated_mount(available_bytes=1):
            with self.assertRaisesRegex(BulkLakeError, "free-space floor"):
                lake.preflight(create=True)

    def test_preflight_rejects_a_symlinked_lake_path(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        self.manifest.storage.root.symlink_to(target, target_is_directory=True)
        with self.simulated_mount():
            with self.assertRaisesRegex(BulkLakeError, "symlinks"):
                SeagateBulkLake(self.manifest).preflight(create=True)

    def test_partition_paths_reject_unconfigured_or_unsafe_source_identity(self):
        lake = _TestLake(self.manifest)
        unsafe_revision = ExportRevision(
            "../outside",
            self.revision.data_date,
            0,
            self.revision.publish_time,
        )
        with self.assertRaisesRegex(BulkLakeError, "source identity"):
            lake.partition_path(self.property, unsafe_revision)

        unconfigured_property = type(self.property)(
            site_id="other-site",
            site_url="sc-domain:other.example",
            dataset_id="searchconsole_other",
            first_export_date=self.property.first_export_date,
            identity_proof_date=self.property.identity_proof_date,
        )
        with self.assertRaisesRegex(BulkLakeError, "source identity"):
            lake.partition_path(unconfigured_property, self.revision)

    def test_new_lake_directories_fsync_each_parent(self):
        lake = _TestLake(self.manifest)
        lake.preflight(create=True)
        first = self.manifest.storage.root / "durability-one"
        second = first / "durability-two"

        with patch(
            "boho_analytics_platform.bulk_export.lake._fsync_directory"
        ) as fsync_directory:
            lake._private_directory(second)

        self.assertEqual(
            [item.args[0] for item in fsync_directory.call_args_list],
            [self.manifest.storage.root, first],
        )

    def test_private_directories_reject_a_nested_foreign_filesystem(self):
        lake = _TestLake(self.manifest)
        lake.preflight(create=True)
        foreign = self.manifest.storage.root / "foreign-mount"
        foreign.mkdir(mode=0o700)
        real_lstat = Path.lstat

        def foreign_lstat(path, *args, **kwargs):
            value = real_lstat(path, *args, **kwargs)
            if path == foreign:
                fields = {
                    name: getattr(value, name)
                    for name in (
                        "st_mode", "st_ino", "st_dev", "st_nlink", "st_uid",
                        "st_gid", "st_size", "st_atime", "st_mtime", "st_ctime",
                    )
                }
                fields["st_dev"] = value.st_dev + 1
                return os.stat_result(tuple(fields[name] for name in fields))
            return value

        with patch.object(Path, "lstat", foreign_lstat):
            with self.assertRaisesRegex(BulkLakeError, "required filesystem"):
                lake._private_directory(foreign, create=False)

    def test_lock_file_must_be_private_and_must_not_be_a_symlink(self):
        lake = _TestLake(self.manifest)
        lake.preflight(create=True)
        lock_dir = self.manifest.storage.root / "locks"
        lake._private_directory(lock_dir)
        target = self.manifest.storage.root / "unexpected-lock-target"
        target.touch(mode=0o600)
        (lock_dir / "gsc-bulk.lock").symlink_to(target)

        with self.assertRaisesRegex(BulkLakeError, "lock file is invalid"):
            with lake.lock():
                pass

    def test_writes_private_atomic_parquet_and_verifies_it(self):
        lake = _TestLake(self.manifest)
        with patch(
            "boho_analytics_platform.bulk_export.lake._fsync_directory"
        ) as fsync_directory:
            value = lake.write_partition(self.property, self.revision, self.read())
        self.assertEqual(value["status"], "written")
        partition = lake.partition_path(self.property, self.revision)
        fsynced = [item.args[0] for item in fsync_directory.call_args_list]
        self.assertIn(self.manifest.storage.root / "staging", fsynced)
        self.assertIn(partition.parent, fsynced)
        parquet_path = partition / "part-00000.parquet"
        self.assertTrue((partition / "_SUCCESS").is_file())
        self.assertEqual(parquet_path.stat().st_mode & 0o777, 0o600)
        import pyarrow.parquet as parquet

        table = parquet.ParquetFile(parquet_path).read()
        self.assertEqual(table.num_rows, 2)
        self.assertIn("query", table.column_names)
        self.assertEqual(table["query"].to_pylist()[0], "private wording")
        verified = lake.verify_partition(self.property, self.revision)
        self.assertEqual(verified["totals"]["clicks"], 4)
        repeated = lake.write_partition(self.property, self.revision, self.read())
        self.assertEqual(repeated["status"], "existing")

    def test_revised_epoch_is_a_new_partition_and_updates_current_pointer(self):
        lake = _TestLake(self.manifest)
        lake.write_partition(self.property, self.revision, self.read())
        revised = ExportRevision(
            self.revision.namespace,
            self.revision.data_date,
            1,
            datetime(2026, 8, 5, tzinfo=UTC),
        )
        lake.write_partition(self.property, revised, self.read(revision=revised))
        self.assertTrue(lake.partition_path(self.property, self.revision).is_dir())
        self.assertTrue(lake.partition_path(self.property, revised).is_dir())
        self.assertEqual(lake.current_epoch(self.property, revised), 1)

    def test_existing_partition_repairs_an_interrupted_current_pointer_publish(self):
        lake = _TestLake(self.manifest)
        lake.write_partition(self.property, self.revision, self.read())
        current = lake._date_root(self.property, self.revision) / "current.json"
        current.unlink()

        repeated = lake.write_partition(self.property, self.revision, self.read())

        self.assertEqual(repeated["status"], "existing")
        self.assertEqual(lake.current_epoch(self.property, self.revision), 0)

    def test_existing_epoch_must_match_the_live_export_publish_time(self):
        lake = _TestLake(self.manifest)
        lake.write_partition(self.property, self.revision, self.read())
        conflicting = ExportRevision(
            self.revision.namespace,
            self.revision.data_date,
            self.revision.epoch_version,
            self.revision.publish_time + timedelta(hours=1),
        )

        with self.assertRaisesRegex(BulkLakeError, "publish time"):
            lake.write_partition(
                self.property,
                conflicting,
                self.read(revision=conflicting),
            )

    def test_lock_recovers_a_durable_temporary_current_pointer(self):
        lake = _TestLake(self.manifest)
        lake.write_partition(self.property, self.revision, self.read())
        date_root = lake._date_root(self.property, self.revision)
        current = date_root / "current.json"
        temporary = date_root / ".current-crash.tmp"
        current.replace(temporary)

        with self.assertRaises(BulkLakeError):
            lake.verify_all()
        with lake.lock():
            self.assertTrue(current.is_file())

        self.assertFalse(temporary.exists())
        self.assertEqual(lake.current_epoch(self.property, self.revision), 0)
        self.assertEqual(lake.verify_all()["current_pointers"], 1)

    def test_lock_quarantines_a_redundant_temporary_current_pointer(self):
        lake = _TestLake(self.manifest)
        lake.write_partition(self.property, self.revision, self.read())
        date_root = lake._date_root(self.property, self.revision)
        current = date_root / "current.json"
        temporary = date_root / ".current-stale.tmp"
        temporary.write_bytes(current.read_bytes())
        temporary.chmod(0o600)

        with lake.lock():
            pass

        self.assertFalse(temporary.exists())
        quarantined = tuple(
            (self.manifest.storage.root / "quarantine").glob(
                "current-pointer-*.tmp"
            )
        )
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(lake.verify_all()["current_pointers"], 1)

    def test_current_pointer_lstat_errors_are_sanitized(self):
        lake = _TestLake(self.manifest)
        lake.write_partition(self.property, self.revision, self.read())
        current = lake._date_root(self.property, self.revision) / "current.json"
        real_lstat = Path.lstat

        def failing_lstat(path, *args, **kwargs):
            if path == current:
                raise PermissionError("sensitive operating-system detail")
            return real_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", failing_lstat):
            with self.assertRaisesRegex(BulkLakeError, "pointer is unavailable"):
                lake.current_epoch(self.property, self.revision)

    def test_existing_partition_never_downgrades_a_newer_current_pointer(self):
        lake = _TestLake(self.manifest)
        lake.write_partition(self.property, self.revision, self.read())
        revised = ExportRevision(
            self.revision.namespace,
            self.revision.data_date,
            1,
            datetime(2026, 8, 5, tzinfo=UTC),
        )
        lake.write_partition(self.property, revised, self.read(revision=revised))

        with self.assertRaisesRegex(BulkLakeError, "newer"):
            lake.write_partition(self.property, self.revision, self.read())
        self.assertEqual(lake.current_epoch(self.property, revised), 1)

    def test_zero_row_export_is_a_completed_manifest_without_fake_rows(self):
        lake = _TestLake(self.manifest)
        value = lake.write_partition(
            self.property,
            self.revision,
            self.read(
                batches=(),
                expected=PartitionTotals(0, 0, 0, Decimal(0)),
            ),
        )
        self.assertEqual(value["totals"]["row_count"], 0)
        self.assertEqual(value["files"], [])
        self.assertTrue(
            (lake.partition_path(self.property, self.revision) / "_SUCCESS").is_file()
        )

    def test_control_mismatch_never_publishes_success_and_is_quarantined(self):
        lake = _TestLake(self.manifest)
        with patch(
            "boho_analytics_platform.bulk_export.lake._fsync_directory"
        ) as fsync_directory:
            with self.assertRaisesRegex(BulkLakeError, "totals"):
                lake.write_partition(
                    self.property,
                    self.revision,
                    self.read(expected=PartitionTotals(2, 99, 20, Decimal(100))),
                )
        self.assertFalse(lake.partition_path(self.property, self.revision).exists())
        quarantines = tuple((self.manifest.storage.root / "quarantine").iterdir())
        self.assertEqual(len(quarantines), 1)
        self.assertFalse((quarantines[0] / "_SUCCESS").exists())
        fsynced = [item.args[0] for item in fsync_directory.call_args_list]
        self.assertIn(self.manifest.storage.root / "staging", fsynced)
        self.assertIn(self.manifest.storage.root / "quarantine", fsynced)

    def test_invalid_query_or_export_lineage_never_publishes(self):
        lake = _TestLake(self.manifest)
        invalid_query_audit = self.read()
        invalid_query_audit.query_audit.pop()
        with self.assertRaisesRegex(BulkLakeError, "query audit"):
            lake.write_partition(
                self.property,
                self.revision,
                invalid_query_audit,
            )
        self.assertFalse(lake.partition_path(self.property, self.revision).exists())

        invalid_export_log = self.read()
        invalid_export_log.export_log_history[0]["epoch_version"] = 1
        with self.assertRaisesRegex(BulkLakeError, "ExportLog history"):
            lake.write_partition(
                self.property,
                self.revision,
                invalid_export_log,
            )
        self.assertFalse(lake.partition_path(self.property, self.revision).exists())
        quarantines = tuple((self.manifest.storage.root / "quarantine").iterdir())
        self.assertEqual(len(quarantines), 2)
        self.assertTrue(all(not (item / "_SUCCESS").exists() for item in quarantines))

    def test_checksum_tampering_is_detected(self):
        lake = _TestLake(self.manifest)
        lake.write_partition(self.property, self.revision, self.read())
        parquet_path = lake.partition_path(
            self.property, self.revision
        ) / "part-00000.parquet"
        with parquet_path.open("ab") as handle:
            handle.write(b"tamper")
        with self.assertRaisesRegex(BulkLakeError, "checksum"):
            lake.verify_partition(self.property, self.revision)

    def test_manifest_metric_tampering_is_detected(self):
        lake = _TestLake(self.manifest)
        lake.write_partition(self.property, self.revision, self.read())
        manifest_path = lake.partition_path(
            self.property, self.revision
        ) / "manifest.json"
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["totals"]["clicks"] = 999
        manifest_path.write_text(json.dumps(value), encoding="utf-8")
        manifest_path.chmod(0o600)

        with self.assertRaisesRegex(BulkLakeError, "totals"):
            lake.verify_partition(self.property, self.revision)

    def test_historical_settings_verify_after_current_configuration_changes(self):
        lake = _TestLake(self.manifest)
        written = lake.write_partition(self.property, self.revision, self.read())
        self.assertEqual(written["compression"], "zstd")
        self.assertEqual(written["maximum_bytes_billed"], 1_073_741_824)

        manifest_path = self.root / "bulk-updated.yaml"
        manifest_path.write_text(
            manifest_text(self.root)
            .replace("maximum_bytes_billed: 1073741824", "maximum_bytes_billed: 2147483648")
            .replace("parquet_compression: zstd", "parquet_compression: snappy"),
            encoding="utf-8",
        )
        updated_manifest = load_bulk_export_manifest(manifest_path)
        verified = _TestLake(updated_manifest).verify_partition(
            updated_manifest.properties[0], self.revision
        )

        self.assertEqual(verified["compression"], "zstd")
        self.assertEqual(verified["maximum_bytes_billed"], 1_073_741_824)

    def test_verify_rejects_permissive_or_undeclared_payloads(self):
        lake = _TestLake(self.manifest)
        lake.write_partition(self.property, self.revision, self.read())
        partition = lake.partition_path(self.property, self.revision)
        parquet_path = partition / "part-00000.parquet"
        parquet_path.chmod(0o640)
        with self.assertRaisesRegex(BulkLakeError, "private regular file"):
            lake.verify_partition(self.property, self.revision)
        parquet_path.chmod(0o600)
        extra = partition / "undeclared.bin"
        extra.write_bytes(b"unexpected")
        extra.chmod(0o600)
        with self.assertRaisesRegex(BulkLakeError, "undeclared"):
            lake.verify_partition(self.property, self.revision)

    def test_verify_all_rejects_unrecognized_empty_raw_directories(self):
        lake = _TestLake(self.manifest)
        lake.write_partition(self.property, self.revision, self.read())
        unexpected = self.manifest.storage.root / "raw" / "v1" / "unexpected"
        lake._private_directory(unexpected)

        with self.assertRaisesRegex(BulkLakeError, "unrecognized raw"):
            lake.verify_all()

    def test_verify_all_requires_a_valid_current_pointer(self):
        lake = _TestLake(self.manifest)
        lake.write_partition(self.property, self.revision, self.read())
        current = lake._date_root(self.property, self.revision) / "current.json"
        current.unlink()
        with self.assertRaisesRegex(BulkLakeError, "current pointers"):
            lake.verify_all()

        lake.write_partition(self.property, self.revision, self.read())
        current.write_text(
            json.dumps({
                "epoch_version": 99,
                "manifest": "epoch_version=99/manifest.json",
            }),
            encoding="utf-8",
        )
        current.chmod(0o600)
        with self.assertRaisesRegex(BulkLakeError, "newest epoch"):
            lake.verify_all()

    def test_status_discloses_each_namespace_and_unpaired_dates(self):
        lake = _TestLake(self.manifest)
        lake.write_partition(self.property, self.revision, self.read())
        status = lake.status()["sites"][0]
        self.assertEqual(status["paired_dates"], 0)
        self.assertEqual(status["unpaired_dates"], 1)
        self.assertEqual(
            status["namespaces"]["searchdata_site_impression"]["current_dates"],
            1,
        )
        self.assertEqual(
            status["namespaces"]["searchdata_url_impression"]["current_dates"],
            0,
        )

        url_revision = ExportRevision(
            "searchdata_url_impression",
            self.revision.data_date,
            0,
            self.revision.publish_time,
        )
        lake.write_partition(
            self.property,
            url_revision,
            self.read(
                revision=url_revision,
                batches=(self.url_batch(),),
            ),
        )
        paired = lake.status()["sites"][0]
        self.assertEqual(paired["paired_dates"], 1)
        self.assertEqual(paired["unpaired_dates"], 0)
        self.assertEqual(paired["latest_paired_data_date"], "2026-08-02")
        verified = lake.verify_all()
        self.assertEqual(verified["partitions"], 2)
        self.assertEqual(verified["current_pointers"], 2)

    def test_status_exposes_abandoned_staging_and_quarantine_usage(self):
        lake = _TestLake(self.manifest)
        lake.preflight(create=True)
        staging = self.manifest.storage.root / "staging" / "gsc-abandoned"
        lake._private_directory(staging)
        partial = staging / "part-00000.parquet"
        partial.write_bytes(b"partial")
        partial.chmod(0o600)
        quarantine = self.manifest.storage.root / "quarantine" / "gsc-failed"
        lake._private_directory(quarantine)
        failed = quarantine / "part-00000.parquet"
        failed.write_bytes(b"failed")
        failed.chmod(0o600)

        status = lake.status()
        self.assertEqual(status["staging"]["entries"], 1)
        self.assertEqual(status["staging"]["bytes"], 7)
        self.assertEqual(status["quarantine"]["entries"], 1)
        self.assertEqual(status["quarantine"]["bytes"], 6)

    def test_writer_close_failure_still_attempts_quarantine(self):
        lake = _TestLake(self.manifest)

        class FailingWriter:
            def __init__(self, *_args, **_kwargs):
                pass

            def write_batch(self, _batch):
                raise OSError("simulated write failure")

            def close(self):
                raise OSError("simulated close failure")

        with patch("pyarrow.parquet.ParquetWriter", FailingWriter):
            with self.assertRaisesRegex(BulkLakeError, "quarantined"):
                lake.write_partition(self.property, self.revision, self.read())
        self.assertEqual(
            len(tuple((self.manifest.storage.root / "quarantine").iterdir())),
            1,
        )
        self.assertEqual(
            len(tuple((self.manifest.storage.root / "staging").iterdir())),
            0,
        )


if __name__ == "__main__":
    unittest.main()
