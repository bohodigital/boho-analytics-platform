"""Dependency-light command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .build_info import version_string
from .config import ConfigError, load_config
from .credentials import ReferenceCredentialProvider
from .engine import SyncEngine
from .models import QueryWindow
from .reporting import ReportService, to_csv
from .site_graph.manifest import ManifestError, load_manifest
from .site_graph.analysis import PROJECTION_LAYERS, compile_graph
from .site_graph.reporting import SiteGraphDisplayReportService
from .site_graph.ingest import IngestError, ingest_repository, inspect_repository
from .site_graph.storage import SiteGraphStore
from .storage import LockBusy, SQLiteMetricStore
from .time_window import report_window
from .web import serve, site_graph_core21_projection


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="boho-analytics", description="Operate a local Boho Analytics Platform installation.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version_string()}")
    parser.add_argument("--config", default="platform.toml", help="schema-v2 TOML configuration")
    commands = parser.add_subparsers(dest="command")
    config = commands.add_parser("config", help="configuration operations"); config.add_subparsers(dest="config_command").add_parser("validate")
    db = commands.add_parser("db", help="database operations"); db_commands = db.add_subparsers(dest="db_command")
    db_commands.add_parser("init"); db_commands.add_parser("check")
    backup = db_commands.add_parser("backup"); backup.add_argument("destination")
    restore = db_commands.add_parser("restore"); restore.add_argument("source"); restore.add_argument("--confirm", action="store_true")
    probe = commands.add_parser("probe", help="test configured read-only capabilities"); probe.add_argument("--connection", action="append")
    sync = commands.add_parser("sync", help="collect a bounded window"); _window_args(sync); sync.add_argument("--connection", action="append"); sync.add_argument("--site", action="append")
    index_coverage = commands.add_parser(
        "index-coverage", help="census sitemap pages with Google URL Inspection"
    )
    index_commands = index_coverage.add_subparsers(dest="index_coverage_command")
    index_sync = index_commands.add_parser(
        "sync", help="advance the quota-bounded per-property index census"
    )
    index_sync.add_argument("--site", action="append")
    index_sync.add_argument("--per-property-limit", type=int, default=1900)
    index_sync.add_argument("--pause-seconds", type=float, default=0.12)
    index_sync.add_argument("--workers", type=int, default=16)
    index_sync.add_argument("--refresh-days", type=int, default=21)
    index_sync.add_argument("--freshness-days", type=int, default=30)
    index_status = index_commands.add_parser(
        "status", help="show the current per-property index census"
    )
    index_status.add_argument("--site", action="append")
    report = commands.add_parser("report", help="render a saved report"); report.add_argument("report_id"); report.add_argument("--subreport"); _window_args(report)
    report.add_argument("--format", choices=("json", "csv"), default="json"); report.add_argument("--output")
    commands.add_parser("serve", help="run the configured read-only web dashboard")
    site_graph = commands.add_parser("site-graph", help="site graph operations")
    site_graph_commands = site_graph.add_subparsers(dest="site_graph_command")
    manifest = site_graph_commands.add_parser("manifest", help="site graph manifest operations")
    manifest_commands = manifest.add_subparsers(dest="manifest_command")
    validate = manifest_commands.add_parser("validate", help="validate a site graph manifest")
    validate.add_argument("--manifest", required=True, help="path to the site graph YAML manifest")
    inspect_repo = site_graph_commands.add_parser("inspect-repo", help="inspect an authorized repository without changing it")
    inspect_repo.add_argument("--manifest", required=True, help="path to the site graph YAML manifest")
    inspect_repo.add_argument("--allow-dirty-snapshot", action="store_true", help="explicit non-production override; manifest must also permit dirty input")
    ingest = site_graph_commands.add_parser("ingest", help="persist immutable source-first repository facts")
    ingest.add_argument("--manifest", required=True, help="path to the site graph YAML manifest")
    ingest.add_argument("--database", required=True, help="initialized analytics SQLite database")
    ingest.add_argument("--allow-dirty-snapshot", action="store_true", help="explicit non-production override; manifest must also permit dirty input")
    compile_command = site_graph_commands.add_parser("compile", help="compile immutable graph facts into a projection")
    compile_command.add_argument("--database", required=True, help="initialized analytics SQLite database")
    compile_command.add_argument("--site", required=True, help="site graph key")
    compile_command.add_argument("--projection", choices=tuple(sorted(PROJECTION_LAYERS)), default="contextual")
    compile_command.add_argument("--latest", action="store_true", help="compile the latest immutable repository snapshot")
    graph_report = site_graph_commands.add_parser("report", help="render the normalized site graph dashboard summary")
    graph_report.add_argument("--database", required=True, help="initialized analytics SQLite database")
    graph_report.add_argument("--site", help="site graph key; defaults to the latest compiled site")
    graph_report.add_argument("--page", help="optional selected page route for a bounded two-hop neighborhood")
    graph_report.add_argument("--layer", action="append", choices=tuple(sorted(PROJECTION_LAYERS["full"])))
    graph_report.add_argument("--latest", action="store_true", help="report the latest compiled snapshot")
    graph_report.add_argument("--format", choices=("json",), default="json")
    gsc_bulk = commands.add_parser(
        "gsc-bulk", help="private Search Console BigQuery bulk lake operations"
    )
    gsc_bulk_commands = gsc_bulk.add_subparsers(dest="gsc_bulk_command")
    bulk_validate = gsc_bulk_commands.add_parser(
        "validate", help="validate a private bulk-export manifest"
    )
    bulk_validate.add_argument("--manifest", required=True)
    bulk_probe = gsc_bulk_commands.add_parser(
        "probe", help="verify storage and read-only BigQuery table access"
    )
    bulk_probe.add_argument("--manifest", required=True)
    bulk_probe.add_argument("--site", action="append")
    bulk_sync = gsc_bulk_commands.add_parser(
        "sync", help="mirror completed BigQuery revisions to private Parquet"
    )
    bulk_sync.add_argument("--manifest", required=True)
    bulk_sync.add_argument("--site", action="append")
    bulk_sync.add_argument("--start")
    bulk_sync.add_argument("--end")
    bulk_sync.add_argument("--days", type=int)
    bulk_sync.add_argument("--end-lag-days", type=int)
    bulk_status = gsc_bulk_commands.add_parser(
        "status", help="summarize locally current bulk partitions"
    )
    bulk_status.add_argument("--manifest", required=True)
    bulk_verify = gsc_bulk_commands.add_parser(
        "verify", help="verify every completed local bulk partition"
    )
    bulk_verify.add_argument("--manifest", required=True)
    page_intelligence = commands.add_parser(
        "page-intelligence", help="materialize and manage privacy-bounded page evidence"
    )
    page_commands = page_intelligence.add_subparsers(dest="page_intelligence_command")
    page_materialize = page_commands.add_parser(
        "materialize", help="rebuild the canonical page catalog and daily facts"
    )
    page_materialize.add_argument("--site", action="append")
    page_schemes = page_commands.add_parser(
        "scheme", help="validate and version declarative page clustering schemes"
    )
    scheme_commands = page_schemes.add_subparsers(dest="scheme_command")
    scheme_commands.add_parser("list", help="list active schemes")
    scheme_validate = scheme_commands.add_parser("validate", help="validate a JSON scheme")
    scheme_validate.add_argument("--file", required=True)
    scheme_preview = scheme_commands.add_parser("preview", help="preview assignments without writes")
    scheme_preview.add_argument("--file", required=True)
    scheme_preview.add_argument("--site")
    scheme_preview.add_argument("--limit", type=int, default=100)
    scheme_apply = scheme_commands.add_parser("apply", help="store and activate an immutable scheme version")
    scheme_apply.add_argument("--file", required=True)
    scheme_apply.add_argument("--reason", default="operator apply")
    scheme_activate = scheme_commands.add_parser("activate", help="activate or roll back to a stored version")
    scheme_activate.add_argument("--scheme", required=True)
    scheme_activate.add_argument("--version", required=True, type=int)
    scheme_activate.add_argument("--reason", required=True)
    return parser


def _window_args(parser):
    parser.add_argument("--start", help="inclusive local date (YYYY-MM-DD)"); parser.add_argument("--end", help="exclusive local date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, help="completed days ending today; cannot be combined with start/end")


def _window(
    args, timezone: str, default_days: int, default_end_lag_days: int = 0
) -> QueryWindow:
    if args.days is not None and (args.start or args.end): raise ValueError("--days cannot be combined with --start or --end")
    days = args.days if args.days is not None else default_days
    return report_window(
        timezone=timezone,
        default_days=days,
        default_end_lag_days=(0 if args.days is not None else default_end_lag_days),
        start=args.start,
        end=args.end,
    )


def _emit(value, *, error=False):
    print(json.dumps(value, sort_keys=True), file=sys.stderr if error else sys.stdout)


def _bulk_window(args):
    if args.days is not None and (args.start or args.end):
        raise ValueError("--days cannot be combined with --start or --end")
    if args.days is None and bool(args.start) != bool(args.end):
        raise ValueError("bulk sync requires both --start and --end")
    lag = 3 if args.end_lag_days is None else args.end_lag_days
    if args.start and args.end_lag_days is not None:
        raise ValueError("--end-lag-days cannot be combined with --start and --end")
    if args.days is not None:
        if not 1 <= args.days <= 366:
            raise ValueError("--days must be from 1 to 366")
        if not 0 <= lag <= 30:
            raise ValueError("--end-lag-days must be from 0 to 30")
        today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
        end = today - timedelta(days=lag)
        return end - timedelta(days=args.days), end
    if not args.start:
        if not 0 <= lag <= 30:
            raise ValueError("--end-lag-days must be from 0 to 30")
        today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
        end = today - timedelta(days=lag)
        return end - timedelta(days=7), end
    try:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = datetime.strptime(args.end, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("bulk sync dates must use YYYY-MM-DD") from exc
    if start.isoformat() != args.start or end.isoformat() != args.end:
        raise ValueError("bulk sync dates must use YYYY-MM-DD")
    return start, end


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser(); args = parser.parse_args(argv)
    if not args.command: parser.print_help(sys.stderr); return 2
    try:
        if args.command == "site-graph":
            if args.site_graph_command == "manifest" and args.manifest_command == "validate":
                manifest = load_manifest(args.manifest)
                _emit({"ok": True, **manifest.sanitized_summary()})
                return 0
            if args.site_graph_command == "compile":
                graph_store = SiteGraphStore(args.database)
                graph_store.initialize()
                _emit(compile_graph(graph_store, site_key=args.site, projection=args.projection))
                return 0
            if args.site_graph_command == "inspect-repo":
                manifest = load_manifest(args.manifest)
                _emit({"ok": True, **inspect_repository(
                    manifest, allow_dirty_snapshot=args.allow_dirty_snapshot
                ).sanitized_summary()})
                return 0
            if args.site_graph_command == "ingest":
                manifest = load_manifest(args.manifest)
                graph_store = SiteGraphStore(args.database)
                graph_store.initialize()
                _emit({"ok": True, **ingest_repository(
                    graph_store, manifest, allow_dirty_snapshot=args.allow_dirty_snapshot
                ).sanitized_summary()})
                return 0
            if args.site_graph_command == "report":
                graph_store = SiteGraphStore(args.database)
                graph_store.initialize()
                layers = tuple(args.layer or ("contextual", "related", "action"))
                payload = SiteGraphDisplayReportService(graph_store).summary(
                    site_key=args.site, selected_page=args.page, layers=layers
                )
                payload["evidence_core21"] = site_graph_core21_projection(
                    graph_store, payload, layers
                )
                if (
                    payload["evidence_core21"].get("available")
                    and payload["evidence_core21"]["structural_metrics"].get("available")
                ):
                    structural = payload["evidence_core21"]["structural_metrics"]
                    payload["overview"].update({
                        "orphans": structural["true_orphans"],
                        "true_orphans": structural["true_orphans"],
                        "contextual_orphans": structural["contextual_orphans"],
                        "contextual_dead_ends": structural["contextual_dead_ends"],
                        "menu_dependent_pages": structural["menu_dependent"],
                        "global_shell_dependent_pages": structural["global_shell_dependent"],
                    })
                    payload["overview"].pop("traps", None)
                    payload["overview"].pop("bottlenecks", None)
                _emit(payload)
                return 0
            return 2
        if args.command == "gsc-bulk":
            from .bulk_export.config import load_bulk_export_manifest

            if not args.gsc_bulk_command:
                return 2
            manifest = load_bulk_export_manifest(args.manifest)
            if args.gsc_bulk_command == "validate":
                _emit({
                    "ok": True,
                    "schema_version": manifest.schema_version,
                    "project_id": manifest.warehouse.project_id,
                    "location": manifest.warehouse.location,
                    "properties": [item.site_id for item in manifest.properties],
                    "storage_root": str(manifest.storage.root),
                    "required_mountpoint": str(manifest.storage.required_mountpoint),
                    "required_filesystem_uuid": manifest.storage.required_filesystem_uuid,
                })
                return 0
            try:
                from .bulk_export.bigquery import BigQueryBulkSource
                from .bulk_export.engine import BulkExportEngine
                from .bulk_export.lake import BulkLake
            except ModuleNotFoundError as exc:
                if exc.name == "fcntl":
                    raise RuntimeError(
                        "gsc-bulk requires a POSIX host with file-lock support"
                    ) from exc
                raise
            lake = BulkLake(manifest)
            if args.gsc_bulk_command == "status":
                with lake.lock():
                    payload = lake.status()
                _emit(payload)
                return 0
            if args.gsc_bulk_command == "verify":
                with lake.lock():
                    payload = lake.verify_all()
                _emit(payload)
                return 0
            selected = set(args.site or [])
            with ReferenceCredentialProvider().acquire(
                manifest.warehouse.credential_ref
            ) as credential:
                source = BigQueryBulkSource(manifest, credential)
                engine = BulkExportEngine(manifest, source, lake)
                if args.gsc_bulk_command == "probe":
                    _emit(engine.probe(selected))
                    return 0
                if args.gsc_bulk_command == "sync":
                    start, end = _bulk_window(args)
                    results = engine.sync(start, end, selected)
                    _emit([item.json_value() for item in results])
                    return 0 if all(
                        item.status not in {"failed", "export-log-incomplete"}
                        for item in results
                    ) else 1
            return 2
        config = load_config(args.config)
        if args.command == "config":
            if args.config_command != "validate": return 2
            _emit({"ok": True, "schema_version": config.schema_version, "clients": len(config.clients), "sites": len(config.sites),
                "connections": len(config.connections), "bindings": len(config.bindings), "reports": len(config.reports)})
            return 0
        store = SQLiteMetricStore(config.resolve_path(config.platform.state_path))
        if args.command == "db":
            if args.db_command == "init": store.initialize(); _emit({"ok": True, "path": str(store.path)}); return 0
            if args.db_command == "check":
                result = store.integrity_check(); _emit({"ok": result == "ok", "result": result}); return 0 if result == "ok" else 1
            if args.db_command == "backup": _emit({"ok": True, "path": str(store.backup(args.destination))}); return 0
            if args.db_command == "restore": store.restore(args.source, confirmed=args.confirm); _emit({"ok": True}); return 0
            return 2
        store.initialize()
        if args.command == "page-intelligence":
            from .page_intelligence import PageIntelligenceService, load_scheme

            service = PageIntelligenceService(config, store)
            if args.page_intelligence_command == "materialize":
                _emit(service.materialize(args.site))
                return 0
            if args.page_intelligence_command == "scheme":
                if args.scheme_command == "list":
                    _emit(service.schemes())
                    return 0
                if args.scheme_command == "validate":
                    definition = load_scheme(args.file)
                    _emit({"ok": True, "definition": definition})
                    return 0
                if args.scheme_command == "preview":
                    _emit(service.preview_scheme(
                        load_scheme(args.file), site_id=args.site, limit=args.limit
                    ))
                    return 0
                if args.scheme_command == "apply":
                    _emit(service.apply_scheme(load_scheme(args.file), reason=args.reason))
                    return 0
                if args.scheme_command == "activate":
                    _emit(service.activate_scheme_version(
                        args.scheme, args.version, reason=args.reason
                    ))
                    return 0
            return 2
        if args.command == "index-coverage":
            configured_sites = {item.id for item in config.sites}
            selected = set(args.site or [])
            unknown = selected - configured_sites
            if unknown:
                raise ValueError(f"unknown site id(s): {', '.join(sorted(unknown))}")
            site_ids = tuple(
                item.id for item in config.sites if not selected or item.id in selected
            )
            if args.index_coverage_command == "status":
                _emit({
                    "schema_version": 1,
                    "metric": "Google indexed pages / published sitemap URLs",
                    "freshness_days": 30,
                    "properties": store.query_index_coverage(site_ids),
                })
                return 0
            if args.index_coverage_command == "sync":
                from .index_coverage import IndexCoverageEngine

                results = IndexCoverageEngine(config, store).sync(
                    selected or None,
                    per_property_limit=args.per_property_limit,
                    pause_seconds=args.pause_seconds,
                    workers=args.workers,
                    refresh_days=args.refresh_days,
                    freshness_days=args.freshness_days,
                )
                _emit([item.json_value() for item in results])
                return 0 if all(item.status != "failed" for item in results) else 1
            return 2
        if args.command == "probe":
            results = SyncEngine(config, store).probe(set(args.connection or [])); _emit([{"connection_id": r.connection_id, "site_id": r.site_id, "status": r.status, "points": r.points, "error_category": r.error_category} for r in results])
            return 0 if all(item.status == "success" for item in results) else 1
        if args.command == "sync":
            window = _window(args, config.platform.default_timezone, config.platform.default_sync_days)
            results = SyncEngine(config, store).sync(
                window,
                set(args.connection or []),
                set(args.site or []),
            )
            if all(item.status == "success" for item in results):
                from .page_intelligence import PageIntelligenceService

                PageIntelligenceService(config, store).materialize(
                    set(args.site or []) or None
                )
            _emit([{"connection_id": r.connection_id, "site_id": r.site_id, "status": r.status, "points": r.points, "error_category": r.error_category} for r in results])
            return 0 if all(item.status == "success" for item in results) else 1
        if args.command == "report":
            definition, _, _, _ = ReportService(config, store).definition(args.report_id, args.subreport)
            active_definition = definition
            if args.subreport:
                active_definition = next(
                    item for item in definition.subreports if item.id == args.subreport
                )
            report = ReportService(config, store).render(
                args.report_id,
                _window(
                    args,
                    config.platform.default_timezone,
                    active_definition.default_window_days,
                    active_definition.default_end_lag_days,
                ),
                args.subreport,
            )
            content = json.dumps(report, indent=2, sort_keys=True) + "\n" if args.format == "json" else to_csv(report)
            if args.output: Path(args.output).write_text(content, encoding="utf-8", newline="")
            else: print(content, end="")
            return 0
        if args.command == "serve": serve(config, store); return 0
        return 2
    except (
        ConfigError, ManifestError, IngestError, ValueError, RuntimeError, LockBusy,
    ) as exc:
        _emit({"ok": False, "error": str(exc)}, error=True); return 2


if __name__ == "__main__": raise SystemExit(main())
