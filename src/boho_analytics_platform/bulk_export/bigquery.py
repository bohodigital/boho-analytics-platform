"""Read-only BigQuery adapter for Search Console bulk-export partitions."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from ..credentials import CredentialError
from .config import BulkExportManifest, SearchConsolePropertyConfig
from .contracts import (
    SEARCHDATA_TABLES,
    ExportRevision,
    PartitionRead,
    PartitionTotals,
)


_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,1024}$")
_SERVICE_ACCOUNT_FIELDS = (
    "type",
    "project_id",
    "private_key_id",
    "private_key",
    "client_email",
    "client_id",
    "auth_uri",
    "token_uri",
    "auth_provider_x509_cert_url",
    "client_x509_cert_url",
    "universe_domain",
)
_REQUIRED_TABLE_FIELDS = {
    "searchdata_site_impression": {
        "data_date": "DATE",
        "site_url": "STRING",
        "query": "STRING",
        "is_anonymized_query": "BOOLEAN",
        "country": "STRING",
        "search_type": "STRING",
        "device": "STRING",
        "impressions": "INTEGER",
        "clicks": "INTEGER",
        "sum_top_position": "INTEGER",
    },
    "searchdata_url_impression": {
        "data_date": "DATE",
        "site_url": "STRING",
        "url": "STRING",
        "query": "STRING",
        "is_anonymized_query": "BOOLEAN",
        "is_anonymized_discover": "BOOLEAN",
        "country": "STRING",
        "search_type": "STRING",
        "device": "STRING",
        "impressions": "INTEGER",
        "clicks": "INTEGER",
        "sum_position": "INTEGER",
    },
    "ExportLog": {
        "agenda": "STRING",
        "namespace": "STRING",
        "data_date": "DATE",
        "epoch_version": "INTEGER",
        "publish_time": "TIMESTAMP",
    },
}
_TYPE_ALIASES = {"INT64": "INTEGER", "BOOL": "BOOLEAN"}


class BigQueryBulkError(RuntimeError):
    """A sanitized BigQuery bulk-source failure."""


def _bounded_nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BigQueryBulkError(f"BigQuery returned invalid {label}")
    if value < 0:
        raise BigQueryBulkError(f"BigQuery returned invalid {label}")
    return value


def _decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value if value is not None else 0))
    except Exception as exc:
        raise BigQueryBulkError(f"BigQuery returned invalid {label}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise BigQueryBulkError(f"BigQuery returned invalid {label}")
    return parsed


def _schema_value(field: object) -> dict[str, object]:
    name = getattr(field, "name", None)
    field_type = getattr(field, "field_type", None)
    mode = getattr(field, "mode", None)
    if not all(isinstance(value, str) and value for value in (name, field_type, mode)):
        raise BigQueryBulkError("BigQuery returned an invalid table schema")
    children = tuple(_schema_value(item) for item in getattr(field, "fields", ()) or ())
    return {
        "name": name,
        "type": field_type,
        "mode": mode,
        "fields": children,
    }


def _field_type(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise BigQueryBulkError("BigQuery returned an invalid table schema")
    normalized = value.upper()
    return _TYPE_ALIASES.get(normalized, normalized)


def _validate_table_contract(table_name: str, table: object) -> dict[str, object]:
    schema = tuple(getattr(table, "schema", ()) or ())
    fields: dict[str, str] = {}
    for field in schema:
        name = getattr(field, "name", None)
        if not isinstance(name, str) or not name or name in fields:
            raise BigQueryBulkError("BigQuery table schema is invalid or duplicated")
        fields[name] = _field_type(getattr(field, "field_type", None))
    required = _REQUIRED_TABLE_FIELDS[table_name]
    if any(fields.get(name) != field_type for name, field_type in required.items()):
        raise BigQueryBulkError("BigQuery table does not match the required Search Console schema")
    if any(
        name.startswith("is_") and field_type != "BOOLEAN"
        for name, field_type in fields.items()
    ):
        raise BigQueryBulkError("BigQuery search-appearance fields must be boolean")
    partition_field = None
    if table_name in SEARCHDATA_TABLES:
        partitioning = getattr(table, "time_partitioning", None)
        partition_field = getattr(partitioning, "field", None)
        partition_type = getattr(partitioning, "type_", None)
        partition_type = getattr(partition_type, "name", partition_type)
        normalized_partition_type = str(partition_type).rsplit(".", 1)[-1].upper()
        if partition_field != "data_date" or normalized_partition_type != "DAY":
            raise BigQueryBulkError(
                "Search Console data tables must be daily partitioned by data_date"
            )
    return {
        "columns": len(schema),
        "rows": _bounded_nonnegative_int(
            getattr(table, "num_rows", 0) or 0, "table row count"
        ),
        "partition_field": partition_field,
    }


class BigQueryBulkSource:
    """Query only completed Search Console export partitions with a reader identity."""

    def __init__(
        self,
        manifest: BulkExportManifest,
        credential: object,
        *,
        client: object | None = None,
        storage_client: object | None = None,
        bigquery_module: object | None = None,
    ) -> None:
        self.manifest = manifest
        if bigquery_module is None:
            try:
                from google.cloud import bigquery as imported_bigquery
            except ImportError as exc:
                raise BigQueryBulkError(
                    "Search Console bulk export requires the bigquery optional dependency"
                ) from exc
            bigquery_module = imported_bigquery
        self.bigquery = bigquery_module
        credentials = None
        if client is None:
            credentials = self._service_account_credentials(credential)
            try:
                client = self.bigquery.Client(
                    project=manifest.warehouse.project_id,
                    location=manifest.warehouse.location,
                    credentials=credentials,
                )
            except Exception as exc:
                raise BigQueryBulkError("BigQuery reader client initialization failed") from exc
        self.client = client
        if manifest.warehouse.use_storage_api and storage_client is None:
            try:
                from google.cloud import bigquery_storage

                storage_client = bigquery_storage.BigQueryReadClient(
                    credentials=credentials
                )
            except ImportError as exc:
                raise BigQueryBulkError(
                    "BigQuery Storage reads require the bigquery-storage optional dependency"
                ) from exc
            except Exception as exc:
                raise BigQueryBulkError(
                    "BigQuery Storage reader client initialization failed"
                ) from exc
        self.storage_client = storage_client

    @staticmethod
    def _service_account_info(credential: object) -> dict[str, object]:
        raw = getattr(credential, "read")("service_account_json")
        if raw:
            try:
                info = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CredentialError(
                    "BigQuery reader service_account_json is not valid UTF-8 JSON"
                ) from exc
        else:
            info = {}
            for field in _SERVICE_ACCOUNT_FIELDS:
                value = getattr(credential, "read")(field)
                if value is None:
                    continue
                try:
                    info[field] = value.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise CredentialError(
                        "BigQuery reader credential contains a non-UTF-8 field"
                    ) from exc
        if not isinstance(info, dict) or info.get("type") != "service_account":
            raise CredentialError(
                "BigQuery bulk export requires a dedicated service-account JSON credential"
            )
        return info

    @classmethod
    def _service_account_credentials(cls, credential: object):
        info = cls._service_account_info(credential)
        try:
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_info(
                info,
                scopes=("https://www.googleapis.com/auth/cloud-platform",),
            )
        except ImportError as exc:
            raise CredentialError(
                "BigQuery reader credentials require the bigquery optional dependency"
            ) from exc
        except Exception as exc:
            raise CredentialError("BigQuery reader credential could not be loaded") from exc

    def _table_id(self, property_config: SearchConsolePropertyConfig, table: str) -> str:
        if (
            property_config not in self.manifest.properties
            or table not in {*SEARCHDATA_TABLES, "ExportLog"}
        ):
            raise BigQueryBulkError("unsupported Search Console bulk table")
        return (
            f"{self.manifest.warehouse.project_id}."
            f"{property_config.dataset_id}.{table}"
        )

    def _query_config(self, parameters: list[object]):
        return self.bigquery.QueryJobConfig(
            query_parameters=parameters,
            maximum_bytes_billed=self.manifest.warehouse.maximum_bytes_billed,
            use_query_cache=True,
        )

    def _query(self, sql: str, parameters: list[object]):
        try:
            return self.client.query(
                sql,
                job_config=self._query_config(parameters),
                location=self.manifest.warehouse.location,
            )
        except Exception as exc:
            raise BigQueryBulkError("BigQuery query submission failed") from exc

    @staticmethod
    def _job_audit(job: object, role: str) -> dict[str, object]:
        job_id = str(getattr(job, "job_id", ""))
        if not _SAFE_JOB_ID.fullmatch(job_id):
            raise BigQueryBulkError("BigQuery returned an invalid job identity")
        return {
            "role": role,
            "job_id": job_id,
            "bytes_processed": _bounded_nonnegative_int(
                getattr(job, "total_bytes_processed", 0) or 0,
                "processed byte count",
            ),
            "bytes_billed": _bounded_nonnegative_int(
                getattr(job, "total_bytes_billed", 0) or 0,
                "billed byte count",
            ),
        }

    def probe(self, property_config: SearchConsolePropertyConfig) -> dict[str, object]:
        """Verify the exact dataset location and required table contracts."""

        dataset_id = (
            f"{self.manifest.warehouse.project_id}.{property_config.dataset_id}"
        )
        try:
            dataset = self.client.get_dataset(dataset_id)
            location = str(getattr(dataset, "location", ""))
            if location.casefold() != self.manifest.warehouse.location.casefold():
                raise BigQueryBulkError(
                    "BigQuery dataset location does not match the manifest"
                )
            tables: dict[str, object] = {}
            for table_name in (*SEARCHDATA_TABLES, "ExportLog"):
                table = self.client.get_table(
                    self._table_id(property_config, table_name)
                )
                tables[table_name] = _validate_table_contract(table_name, table)
            for table_name in SEARCHDATA_TABLES:
                tables[table_name]["identity"] = self._property_identity_evidence(
                    property_config, table_name
                )
        except BigQueryBulkError:
            raise
        except Exception as exc:
            raise BigQueryBulkError(
                "BigQuery dataset or required tables are unavailable"
            ) from exc
        return {
            "site_id": property_config.site_id,
            "dataset_id": property_config.dataset_id,
            "location": location,
            "tables": tables,
        }

    def _property_identity_evidence(
        self,
        property_config: SearchConsolePropertyConfig,
        table_name: str,
    ) -> dict[str, object]:
        proof_date = property_config.identity_proof_date
        sql = f"""
SELECT COUNT(1) AS partition_rows,
       COUNTIF(site_url = @site_url) AS matching_rows
FROM `{self._table_id(property_config, table_name)}`
WHERE data_date = @proof_date
""".strip()
        parameters = [
            self.bigquery.ScalarQueryParameter("proof_date", "DATE", proof_date),
            self.bigquery.ScalarQueryParameter(
                "site_url", "STRING", property_config.site_url
            ),
        ]
        job = self._query(sql, parameters)
        try:
            rows = tuple(job.result())
        except Exception as exc:
            raise BigQueryBulkError(
                "BigQuery property identity query failed"
            ) from exc
        if len(rows) != 1:
            raise BigQueryBulkError("BigQuery property identity query was ambiguous")
        partition_rows = _bounded_nonnegative_int(
            rows[0]["partition_rows"], "property identity row count"
        )
        matching_rows = _bounded_nonnegative_int(
            rows[0]["matching_rows"], "property identity match count"
        )
        if partition_rows == 0:
            raise BigQueryBulkError(
                "Search Console property identity is not yet provable from exported rows"
            )
        if matching_rows != partition_rows:
            raise BigQueryBulkError(
                "Search Console property does not match its configured BigQuery dataset"
            )
        return {
            "verified": True,
            "rows_checked": partition_rows,
            "proof_date": proof_date.isoformat(),
        }

    def revisions(
        self,
        property_config: SearchConsolePropertyConfig,
        start: date,
        end: date,
    ) -> tuple[ExportRevision, ...]:
        sql = f"""
SELECT namespace, data_date, epoch_version, publish_time
FROM `{self._table_id(property_config, 'ExportLog')}`
WHERE data_date >= @start_date
  AND data_date < @end_date
  AND agenda = @agenda
  AND namespace IN UNNEST(@namespaces)
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY namespace, data_date
  ORDER BY epoch_version DESC, publish_time DESC
) = 1
ORDER BY data_date, namespace
""".strip()
        parameters = [
            self.bigquery.ScalarQueryParameter("start_date", "DATE", start),
            self.bigquery.ScalarQueryParameter("end_date", "DATE", end),
            self.bigquery.ArrayQueryParameter(
                "namespaces", "STRING", list(SEARCHDATA_TABLES)
            ),
            self.bigquery.ScalarQueryParameter("agenda", "STRING", "SEARCHDATA"),
        ]
        job = self._query(sql, parameters)
        try:
            rows = tuple(job.result())
        except Exception as exc:
            raise BigQueryBulkError("BigQuery ExportLog query failed") from exc
        revisions: list[ExportRevision] = []
        seen: set[tuple[str, date]] = set()
        for row in rows:
            namespace = row["namespace"]
            data_date = row["data_date"]
            publish_time = row["publish_time"]
            if (
                namespace not in SEARCHDATA_TABLES
                or type(data_date) is not date
                or not isinstance(publish_time, datetime)
                or publish_time.tzinfo is None
                or publish_time.utcoffset() is None
            ):
                raise BigQueryBulkError("BigQuery ExportLog returned an invalid row")
            publish_time = publish_time.astimezone(UTC)
            key = (namespace, data_date)
            if key in seen:
                raise BigQueryBulkError("BigQuery ExportLog returned duplicate revisions")
            seen.add(key)
            revisions.append(
                ExportRevision(
                    namespace=namespace,
                    data_date=data_date,
                    epoch_version=_bounded_nonnegative_int(
                        row["epoch_version"], "epoch version"
                    ),
                    publish_time=publish_time,
                )
            )
        return tuple(revisions)

    def read_partition(
        self,
        property_config: SearchConsolePropertyConfig,
        revision: ExportRevision,
    ) -> PartitionRead:
        if (
            revision.namespace not in SEARCHDATA_TABLES
            or type(revision.data_date) is not date
            or isinstance(revision.epoch_version, bool)
            or not isinstance(revision.epoch_version, int)
            or revision.epoch_version < 0
            or not isinstance(revision.publish_time, datetime)
            or revision.publish_time.tzinfo is None
            or revision.publish_time.utcoffset() is None
        ):
            raise BigQueryBulkError("invalid Search Console bulk revision")
        table_id = self._table_id(property_config, revision.namespace)
        position_field = (
            "sum_top_position"
            if revision.namespace == "searchdata_site_impression"
            else "sum_position"
        )
        parameters = [
            self.bigquery.ScalarQueryParameter(
                "data_date", "DATE", revision.data_date
            ),
            self.bigquery.ScalarQueryParameter(
                "site_url", "STRING", property_config.site_url
            ),
        ]
        where = "data_date = @data_date AND site_url = @site_url"
        totals_sql = f"""
SELECT COUNT(1) AS partition_row_count,
       COUNTIF(site_url = @site_url) AS row_count,
       COALESCE(SUM(IF(site_url = @site_url, clicks, 0)), 0) AS clicks,
       COALESCE(SUM(IF(site_url = @site_url, impressions, 0)), 0) AS impressions,
       COALESCE(SUM(IF(site_url = @site_url, {position_field}, 0)), 0) AS position_sum
FROM `{table_id}`
WHERE data_date = @data_date
""".strip()
        totals_job = self._query(totals_sql, parameters)
        try:
            total_rows = tuple(totals_job.result())
        except Exception as exc:
            raise BigQueryBulkError("BigQuery partition control query failed") from exc
        if len(total_rows) != 1:
            raise BigQueryBulkError("BigQuery partition control query was ambiguous")
        total_row = total_rows[0]
        partition_row_count = _bounded_nonnegative_int(
            total_row["partition_row_count"], "partition row count"
        )
        matching_row_count = _bounded_nonnegative_int(
            total_row["row_count"], "row count"
        )
        if partition_row_count != matching_row_count:
            raise BigQueryBulkError(
                "Search Console property does not match its configured BigQuery dataset"
            )
        expected = PartitionTotals(
            row_count=matching_row_count,
            clicks=_bounded_nonnegative_int(total_row["clicks"], "click total"),
            impressions=_bounded_nonnegative_int(
                total_row["impressions"], "impression total"
            ),
            position_sum=_decimal(total_row["position_sum"], "position sum"),
        )

        raw_sql = f"SELECT * FROM `{table_id}` WHERE {where}"
        raw_job = self._query(raw_sql, parameters)
        try:
            row_iterator = raw_job.result(
                page_size=self.manifest.storage.batch_rows
            )
            batches = row_iterator.to_arrow_iterable(
                bqstorage_client=self.storage_client,
                max_queue_size=1,
            )
            source_schema = tuple(
                _schema_value(field)
                for field in tuple(getattr(row_iterator, "schema", ()) or ())
            )
            if not source_schema:
                raise BigQueryBulkError("BigQuery query result omitted its schema")
        except Exception as exc:
            raise BigQueryBulkError("BigQuery partition read failed") from exc
        query_audit = [
            self._job_audit(totals_job, "partition-controls"),
            self._job_audit(raw_job, "partition-data"),
        ]
        export_log_history: list[dict[str, object]] = []
        return PartitionRead(
            batches=self._revision_checked_batches(
                property_config,
                revision,
                batches,
                query_audit,
                export_log_history,
            ),
            source_schema=source_schema,
            expected_totals=expected,
            query_audit=query_audit,
            export_log_history=export_log_history,
        )

    def _revision_checked_batches(
        self,
        property_config: SearchConsolePropertyConfig,
        revision: ExportRevision,
        batches: object,
        query_audit: list[dict[str, object]],
        export_log_history: list[dict[str, object]],
    ):
        for batch in batches:
            yield batch
        sql = f"""
SELECT agenda, namespace, data_date, epoch_version, publish_time
FROM `{self._table_id(property_config, 'ExportLog')}`
WHERE data_date = @data_date
  AND namespace = @namespace
  AND agenda = @agenda
ORDER BY epoch_version, publish_time
""".strip()
        parameters = [
            self.bigquery.ScalarQueryParameter(
                "data_date", "DATE", revision.data_date
            ),
            self.bigquery.ScalarQueryParameter(
                "namespace", "STRING", revision.namespace
            ),
            self.bigquery.ScalarQueryParameter("agenda", "STRING", "SEARCHDATA"),
        ]
        job = self._query(sql, parameters)
        try:
            rows = tuple(job.result())
        except Exception as exc:
            raise BigQueryBulkError(
                "BigQuery ExportLog post-read verification failed"
            ) from exc
        query_audit.append(self._job_audit(job, "export-log-post-read"))
        seen_epochs: set[int] = set()
        publish_by_epoch: dict[int, datetime] = {}
        for row in rows:
            epoch = _bounded_nonnegative_int(row["epoch_version"], "epoch version")
            publish_time = row["publish_time"]
            if (
                row["agenda"] != "SEARCHDATA"
                or row["namespace"] != revision.namespace
                or row["data_date"] != revision.data_date
                or not isinstance(publish_time, datetime)
                or publish_time.tzinfo is None
                or publish_time.utcoffset() is None
                or epoch in seen_epochs
            ):
                raise BigQueryBulkError("BigQuery ExportLog history is invalid")
            seen_epochs.add(epoch)
            publish_time = publish_time.astimezone(UTC)
            publish_by_epoch[epoch] = publish_time
            export_log_history.append({
                "agenda": "SEARCHDATA",
                "namespace": revision.namespace,
                "data_date": revision.data_date.isoformat(),
                "epoch_version": epoch,
                "publish_time": publish_time.isoformat(),
            })
        expected_epochs = set(range(revision.epoch_version + 1))
        if (
            seen_epochs != expected_epochs
            or publish_by_epoch.get(revision.epoch_version)
            != revision.publish_time.astimezone(UTC)
        ):
            raise BigQueryBulkError(
                "Search Console export revision changed during the partition read"
            )
