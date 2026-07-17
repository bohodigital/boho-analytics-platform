"""Dependency-light command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__
from .config import ConfigError, load_config
from .engine import SyncEngine
from .models import QueryWindow
from .reporting import ReportService, to_csv
from .site_graph.manifest import ManifestError, load_manifest
from .site_graph.analysis import PROJECTION_LAYERS, compile_graph
from .site_graph.dashboard import SiteGraphReportService
from .site_graph.ingest import IngestError, ingest_repository, inspect_repository
from .site_graph.storage import SiteGraphStore
from .storage import LockBusy, SQLiteMetricStore
from .web import serve


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="boho-analytics", description="Operate a local Boho Analytics Platform installation.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default="platform.toml", help="schema-v2 TOML configuration")
    commands = parser.add_subparsers(dest="command")
    config = commands.add_parser("config", help="configuration operations"); config.add_subparsers(dest="config_command").add_parser("validate")
    db = commands.add_parser("db", help="database operations"); db_commands = db.add_subparsers(dest="db_command")
    db_commands.add_parser("init"); db_commands.add_parser("check")
    backup = db_commands.add_parser("backup"); backup.add_argument("destination")
    restore = db_commands.add_parser("restore"); restore.add_argument("source"); restore.add_argument("--confirm", action="store_true")
    probe = commands.add_parser("probe", help="test configured read-only capabilities"); probe.add_argument("--connection", action="append")
    sync = commands.add_parser("sync", help="collect a bounded window"); _window_args(sync); sync.add_argument("--connection", action="append")
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
    return parser


def _window_args(parser):
    parser.add_argument("--start", help="inclusive local date (YYYY-MM-DD)"); parser.add_argument("--end", help="exclusive local date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, help="completed days ending today; cannot be combined with start/end")


def _window(args, timezone: str, default_days: int) -> QueryWindow:
    if args.days is not None and (args.start or args.end): raise ValueError("--days cannot be combined with --start or --end")
    zone = UTC if timezone == "UTC" else ZoneInfo(timezone)
    if args.start or args.end:
        if not args.start or not args.end: raise ValueError("--start and --end must be provided together")
        start_date = datetime.fromisoformat(args.start).date(); end_date = datetime.fromisoformat(args.end).date()
    else:
        days = args.days if args.days is not None else default_days
        if days < 1 or days > 3650: raise ValueError("--days must be from 1 to 3650")
        today = datetime.now(zone).date()
        end_date = today; start_date = end_date - timedelta(days=days)
    return QueryWindow(datetime.combine(start_date, time.min, zone), datetime.combine(end_date, time.min, zone), timezone)


def _emit(value, *, error=False):
    print(json.dumps(value, sort_keys=True), file=sys.stderr if error else sys.stdout)


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
                _emit(SiteGraphReportService(graph_store).summary(site_key=args.site, selected_page=args.page, layers=layers))
                return 0
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
        if args.command == "probe":
            results = SyncEngine(config, store).probe(set(args.connection or [])); _emit([{"connection_id": r.connection_id, "site_id": r.site_id, "status": r.status, "points": r.points, "error_category": r.error_category} for r in results])
            return 0 if all(item.status == "success" for item in results) else 1
        if args.command == "sync":
            window = _window(args, config.platform.default_timezone, config.platform.default_sync_days)
            results = SyncEngine(config, store).sync(window, set(args.connection or [])); _emit([{"connection_id": r.connection_id, "site_id": r.site_id, "status": r.status, "points": r.points, "error_category": r.error_category} for r in results])
            return 0 if all(item.status == "success" for item in results) else 1
        if args.command == "report":
            definition, _, _, _ = ReportService(config, store).definition(args.report_id, args.subreport)
            report = ReportService(config, store).render(args.report_id, _window(args, config.platform.default_timezone, definition.default_window_days), args.subreport)
            content = json.dumps(report, indent=2, sort_keys=True) + "\n" if args.format == "json" else to_csv(report)
            if args.output: Path(args.output).write_text(content, encoding="utf-8", newline="")
            else: print(content, end="")
            return 0
        if args.command == "serve": serve(config, store); return 0
        return 2
    except (ConfigError, ManifestError, IngestError, ValueError, RuntimeError, LockBusy) as exc:
        _emit({"ok": False, "error": str(exc)}, error=True); return 2


if __name__ == "__main__": raise SystemExit(main())
