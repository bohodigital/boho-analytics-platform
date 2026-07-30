"""Loopback-first, server-rendered dashboard and read-only report API."""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import html
import io
import json
import math
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlencode, urlsplit

from .build_info import build_identity
from .catalog import METRICS, SOURCE_SEMANTICS
from .credentials import ReferenceCredentialProvider, require_text
from .geography import GeographyService, SOURCE_CONFIG
from .models import QueryWindow
from .reporting import ReportService, to_csv, to_series_csv
from .site_graph.reporting import SiteGraphDisplayReportService
from .site_graph.storage import SiteGraphStore
from .time_window import report_window


SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "accelerometer=(), autoplay=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}

FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#17201d"/><path d="M17 15h18c10 0 15 4 15 12 0 5-3 8-7 10 5 1 8 5 8 11 0 9-6 13-17 13H17V15zm12 10v8h6c3 0 5-1 5-4s-2-4-5-4h-6zm0 17v9h7c3 0 5-2 5-5 0-2-2-4-5-4h-7z" fill="#ffd4c2"/></svg>"""

SITE_GRAPH_LAYERS = ("contextual", "related", "action", "menu", "breadcrumb", "utility")
ROUTE_OBSERVATION_METRICS = tuple(sorted(
    metric_id for metric_id, definition in METRICS.items()
    if not definition.reportable and definition.source in {
        "google-analytics", "search-console", "umami"
    }
))
ROUTE_OBSERVATION_DIMENSIONS = frozenset({
    "channel", "country_code", "country_code_system", "data_state", "device",
    "domain", "event_name", "observation_scope", "page_title", "referrer_domain",
    "referrer_route", "route", "search_appearance",
})
ROUTE_OBSERVATION_LIMIT = 500
ROUTE_OBSERVATION_MAX_DAYS = 366


def _reachable_routes(start, adjacency):
    if start not in adjacency:
        return set()
    seen = {start}
    pending = [start]
    while pending:
        route = pending.pop()
        for destination in sorted(adjacency[route]):
            if destination not in seen:
                seen.add(destination)
                pending.append(destination)
    return seen


def site_graph_core21_projection(graph_store, payload, layers):
    """Project persisted Core 2.1 evidence without exposing private provenance."""

    if payload.get("empty") or not payload.get("site"):
        return {"available": False, "reason": "no-compiled-snapshot"}
    with graph_store.connect(readonly=True) as db:
        graph = db.execute(
            """SELECT id,repository_snapshot_id,compiler_version
                 FROM site_graph_snapshots
                WHERE site_key=? AND projection_name='contextual'
                ORDER BY created_at DESC,id DESC LIMIT 1""",
            (payload["site"]["key"],),
        ).fetchone()
        if graph is None:
            return {"available": False, "reason": "no-compatible-selected-projection"}
        pages = db.execute(
            """SELECT id,route,evidence_json FROM site_graph_page_facts
                WHERE repository_snapshot_id=? ORDER BY route,id""",
            (graph["repository_snapshot_id"],),
        ).fetchall()
        entities = db.execute(
            """SELECT evidence_json FROM site_graph_page_entities
                WHERE repository_snapshot_id=? AND entity_type='core21-page'""",
            (graph["repository_snapshot_id"],),
        ).fetchall()
        links = db.execute(
            """SELECT source.route AS source_route,l.canonical_destination,l.layer,
                      l.crawlable,l.evidence_json
                 FROM site_graph_link_occurrences l
                 JOIN site_graph_page_facts source ON source.id=l.source_page_fact_id
                WHERE l.repository_snapshot_id=?
                ORDER BY source.route,l.canonical_destination,l.layer,l.id""",
            (graph["repository_snapshot_id"],),
        ).fetchall()
        finding_rows = db.execute(
            """SELECT finding_type,COUNT(*) AS count
                 FROM site_graph_findings WHERE graph_snapshot_id=?
                GROUP BY finding_type ORDER BY finding_type""",
            (graph["id"],),
        ).fetchall()
        reachable_goal_count = db.execute(
            """SELECT COUNT(DISTINCT page_fact_id) FROM site_graph_node_metrics
                WHERE graph_snapshot_id=? AND metric_name='goal_distance'
                  AND metric_value>=0""",
            (graph["id"],),
        ).fetchone()[0]

    candidates = {}
    batch_id = ""
    repository_revision = payload.get("revision", "")
    diagnostics = []
    for row in pages:
        try:
            evidence = json.loads(row["evidence_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(evidence, dict) or not evidence.get("evidence_batch_id"):
            continue
        batch_id = str(evidence["evidence_batch_id"])
        repository_revision = str(evidence.get("repository_revision", repository_revision))
        if isinstance(evidence.get("diagnostics"), list):
            diagnostics.extend(item for item in evidence["diagnostics"] if isinstance(item, dict))
        for candidate in evidence.get("candidate_evidence", ()):
            if isinstance(candidate, dict) and isinstance(candidate.get("candidate_id"), str):
                candidates[candidate["candidate_id"]] = candidate
    if not batch_id:
        return {"available": False, "reason": "legacy-evidence-snapshot"}

    entity_rows = []
    for row in entities:
        try:
            item = json.loads(row["evidence_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            entity_rows.append(item)
    topology = []
    relationship_count = 0
    for row in links:
        try:
            evidence = json.loads(row["evidence_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(evidence, dict) or evidence.get("evidence_batch_id") != batch_id:
            continue
        relationship_count += 1
        if (
            row["crawlable"]
            and evidence.get("topology_eligible") is True
            and row["canonical_destination"]
        ):
            topology.append((row["source_route"], row["canonical_destination"], row["layer"]))

    state_counts = {}
    for candidate in candidates.values():
        state = str(candidate.get("resolution_state", "unchecked"))
        state_counts[state] = state_counts.get(state, 0) + 1
    routes = {str(item.get("canonical_route")) for item in entity_rows if item.get("canonical_route")}
    incoming_full = {route: set() for route in routes}
    incoming_selected = {route: set() for route in routes}
    full = {route: set() for route in routes}
    selected = {route: set() for route in routes}
    without_menu = {route: set() for route in routes}
    for source, destination, layer in topology:
        if source not in routes or destination not in routes:
            continue
        full[source].add(destination)
        incoming_full[destination].add(source)
        if layer != "menu":
            without_menu[source].add(destination)
        if layer in layers:
            selected[source].add(destination)
            incoming_selected[destination].add(source)
    full_reachable = _reachable_routes("/", full)
    without_menu_reachable = _reachable_routes("/", without_menu)
    true_orphans = sorted(route for route in routes if route != "/" and not incoming_full[route])
    contextual_orphans = sorted(
        route for route in routes
        if route != "/" and incoming_full[route] and not incoming_selected[route]
    )
    dead_ends = sorted(route for route in routes if not selected[route])
    homepage_dependent = sorted(
        route for route in routes
        if route != "/" and incoming_full[route] == {"/"}
    )
    global_shell_dependent = sorted(
        route for route in routes
        if route != "/" and incoming_full[route]
        and all(
            layer in {"menu", "utility"}
            for source, destination, layer in topology
            if destination == route and source in routes
        )
    )
    contradictions = sorted(
        candidate.get("canonical_route", "")
        for candidate in candidates.values()
        if candidate.get("resolution_state") == "contradicted"
    )
    exclusions = sorted(
        candidate.get("canonical_route", "")
        for candidate in candidates.values()
        if candidate.get("resolution_state") == "excluded"
    )
    revision_mismatches = sum(
        item.get("code") == "revision-mismatch" for item in diagnostics
    )
    finding_counts = {row["finding_type"]: row["count"] for row in finding_rows}
    compatible_layers = set(layers) == {"contextual", "related", "action"}
    structural_metrics = (
        {
            "available": True,
            "selected_layers": sorted(layers),
            "pages": len(routes),
            "full_relationships": sum(len(destinations) for destinations in full.values()),
            "selected_relationships": sum(len(destinations) for destinations in selected.values()),
            "true_orphans": finding_counts.get("true_orphan", len(true_orphans)),
            "contextual_orphans": finding_counts.get(
                "contextual_orphan", len(contextual_orphans)
            ),
            "contextual_dead_ends": finding_counts.get(
                "contextual_dead_end", len(dead_ends)
            ),
            "menu_dependent": finding_counts.get(
                "menu_dependence", len(full_reachable - without_menu_reachable)
            ),
            "homepage_dependent": finding_counts.get(
                "homepage_dependence", len(homepage_dependent)
            ),
            "global_shell_dependent": finding_counts.get(
                "global_shell_dependence", len(global_shell_dependent)
            ),
            "selected_goal_reachable": reachable_goal_count,
            "withheld_full_goal_metrics": True,
            "withheld_reason": (
                "full-topology goal reachability is not carried by the compiled "
                "contextual display projection"
            ),
        }
        if compatible_layers and graph["compiler_version"].startswith("site-graph-core21-")
        else {
            "available": False,
            "selected_layers": list(layers),
            "reason": "displayed layers differ from the compiled contextual analysis projection",
        }
    )
    return {
        "available": True,
        "evidence_core": "2.1",
        "schema_version": 1,
        "batch_id": batch_id,
        "repository_revision": repository_revision,
        "freshness_basis": "exact-revision",
        "coverage": {
            "candidates": len(candidates),
            "entities": len(entity_rows),
            "relationships": relationship_count,
            "state_counts": dict(sorted(state_counts.items())),
            "unresolved": sum(
                state_counts.get(state, 0)
                for state in ("unresolved", "dynamic-unknown", "unchecked")
            ),
            "contradictions": len(contradictions),
            "exclusions": len(exclusions),
            "revision_mismatches": revision_mismatches,
            "complete_totals": True,
            "display_cap_applied": False,
        },
        "contradicted_routes": contradictions,
        "excluded_routes": exclusions,
        "structural_metrics": structural_metrics,
    }


METRIC_LABELS = {
    "umami.pageviews": "Page views",
    "umami.sessions": "Sessions",
    "umami.visitors": "Visitors",
    "umami.visits": "Visits",
    "umami.bounces": "Bounces",
    "umami.total-time": "Visit time",
    "umami.country-visits": "Country visits",
    "umami.region-visits": "Region visits",
    "cloudflare.requests": "Edge requests",
    "cloudflare.visits": "Edge visits",
    "cloudflare.bytes": "Response bytes",
    "cloudflare.country-visits": "Country edge visits",
    "google.active-users": "Active users",
    "google.sessions": "GA sessions",
    "google.pageviews": "GA page views",
    "google.events": "GA events",
    "google.key-events": "Key events",
    "google.country-sessions": "Country GA sessions",
    "google.region-sessions": "Region GA sessions",
    "search.clicks": "Search clicks",
    "search.impressions": "Search impressions",
    "search.ctr": "Search CTR",
    "search.position": "Average position",
    "search.country-clicks": "Country search clicks",
    "forms.submissions": "Form submissions",
    "forms.pending": "Pending notifications",
    "forms.sent": "Sent notifications",
    "forms.failed": "Failed notifications",
    "forms.inbox-deliveries": "Inbox deliveries",
    "forms.inbox-unread": "Unread notifications",
}

SOURCE_LABELS = {
    "umami": "Umami",
    "cloudflare": "Cloudflare",
    "google-analytics": "Google Analytics",
    "search-console": "Google Search Console",
    "cloudflare-forms": "Forms database",
    "forms-inbox": "Forms inbox",
    "fixture": "Fixture replay",
}

CHART_PRIORITY = (
    "umami.pageviews",
    "umami.sessions",
    "google.pageviews",
    "google.sessions",
    "cloudflare.visits",
    "cloudflare.requests",
    "search.impressions",
    "search.clicks",
    "forms.submissions",
    "forms.inbox-deliveries",
)

PORTFOLIO_SUMMARY = (
    ("Organic clicks", ("search.clicks",), "Clicks recorded by Google Search Console"),
    ("Umami page views", ("umami.pageviews",), "Umami-recorded page views"),
    ("Umami visits", ("umami.visits",), "Exact-window Umami visits"),
    ("GA sessions", ("google.sessions",), "Google Analytics sessions"),
)

TRAFFIC_SUMMARY = (
    ("Umami page views", ("umami.pageviews",), "Umami-recorded page views"),
    ("GA page views", ("google.pageviews",), "Google Analytics screen and page views"),
    ("Umami visitors", ("umami.visitors",), "Unique within each exact provider window; summed by site"),
    ("GA active-user days", ("google.active-users",), "Daily active users summed; not unique across days"),
)

SEARCH_SUMMARY = (
    ("Impressions", ("search.impressions",), "Search result visibility"),
    ("Clicks", ("search.clicks",), "Clicks recorded by Google Search Console"),
    ("CTR", ("search.ctr",), "Clicks per impression"),
    ("Average position", ("search.position",), "Impression-weighted rank"),
)

FORMS_SUMMARY = (
    ("Submissions", ("forms.submissions",), "Stored form records"),
    ("Inbox deliveries", ("forms.inbox-deliveries",), "Mailbox delivery evidence"),
    ("Pending", ("forms.pending",), "Awaiting notification"),
    ("Failed", ("forms.failed",), "Notification failures"),
)


BASE_CSS = """
:root{--ink:#17201d;--ink-2:#26312d;--paper:#f4f2ec;--surface:#fff;--line:#deddd5;--muted:#525a55;--accent:#e86d3d;--accent-soft:#fff0e8;--green:#1f7a5a;--green-soft:#e7f5ef;--amber:#a55c12;--amber-soft:#fff4dc;--red:#a43f35;--red-soft:#fdebe8;--shadow:0 12px 35px rgba(25,35,31,.07)}
*{box-sizing:border-box}html{max-width:100%;overflow-x:hidden;scroll-behavior:smooth}body{max-width:100%;margin:0;overflow-x:hidden;background:var(--paper);color:var(--ink);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:inherit}button,input,select{font:inherit}.skip-link{position:fixed;left:12px;top:-80px;z-index:20;background:#fff;padding:10px 14px;border-radius:8px}.skip-link:focus{top:12px}
.topbar{background:var(--ink);color:#fff}.topbar-inner{max-width:1240px;margin:auto;padding:18px 28px;display:flex;justify-content:space-between;gap:24px;align-items:center}.brand{display:flex;align-items:center;gap:12px}.brand-mark{display:grid;place-items:center;width:38px;height:38px;border:1px solid rgba(255,255,255,.28);border-radius:11px;color:#ffd4c2;font-weight:800;letter-spacing:-.04em}.brand strong{display:block;font-size:15px}.brand span{display:block;color:#aeb9b4;font-size:12px}.live-state{display:flex;align-items:center;gap:8px;color:#dfe9e5;font-size:13px}.live-dot{width:8px;height:8px;border-radius:50%;background:#4fd49c;box-shadow:0 0 0 4px rgba(79,212,156,.12)}
.shell{max-width:1240px;margin:auto;padding:34px 28px 48px}.hero{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:24px;align-items:end;margin-bottom:22px}.eyebrow{margin:0 0 7px;overflow-wrap:anywhere;color:var(--accent);font-size:12px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.hero h1{margin:0;font-size:clamp(29px,4vw,46px);line-height:1.08;letter-spacing:-.045em}.hero-copy{max-width:720px;margin:11px 0 0;color:var(--muted);font-size:16px}.coverage-badge{align-self:start;display:inline-flex;align-items:center;gap:8px;padding:9px 12px;border-radius:999px;background:var(--green-soft);color:var(--green);font-size:13px;font-weight:750}.coverage-badge.partial{background:var(--amber-soft);color:var(--amber)}
.report-nav,.subnav,.quick-links{display:flex;flex-wrap:wrap;gap:8px}.report-nav{margin:0 0 12px}.report-nav a,.subnav a,.quick-links a{padding:8px 11px;border:1px solid var(--line);border-radius:9px;background:rgba(255,255,255,.6);color:var(--ink-2);font-size:13px;font-weight:700;text-decoration:none}.report-nav a:hover,.subnav a:hover,.quick-links a:hover{background:#fff;border-color:#b9bab4}.report-nav a.active,.subnav a.active{background:var(--ink);border-color:var(--ink);color:#fff}.subnav{margin-bottom:16px}
.panel{min-width:0;background:var(--surface);border:1px solid var(--line);border-radius:17px;box-shadow:var(--shadow)}.control-panel{padding:18px;margin-bottom:18px}.panel-heading{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:14px}.panel-heading h2{margin:0;font-size:18px;letter-spacing:-.02em}.panel-heading p{margin:3px 0 0;color:var(--muted);font-size:13px}.filter-form{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr)) auto;gap:12px;align-items:end}.field{display:grid;min-width:0;gap:6px}fieldset.field{min-width:0;margin:0}.field span{font-size:12px;font-weight:750;color:var(--ink-2)}input,select{width:100%;min-height:42px;padding:9px 11px;border:1px solid #c8c9c3;border-radius:9px;background:#fff;color:var(--ink)}input:focus,select:focus,button:focus,a:focus{outline:3px solid rgba(232,109,61,.28);outline-offset:2px;border-color:var(--accent)}button{min-height:42px;padding:9px 16px;border:1px solid var(--ink);border-radius:9px;background:var(--ink);color:#fff;font-weight:800;cursor:pointer}button:hover{background:#283530}.tools-row{display:flex;justify-content:space-between;gap:14px;align-items:center;margin-top:14px;padding-top:14px;border-top:1px solid #ecebe5}.tools-label{color:var(--muted);font-size:12px;font-weight:750;text-transform:uppercase;letter-spacing:.08em}
.alerts{display:grid;gap:9px;margin:0 0 18px}.alert{display:flex;gap:10px;align-items:flex-start;padding:12px 14px;border:1px solid #f0d7b4;border-radius:11px;background:var(--amber-soft);color:#77440f}.alert-mark{display:grid;place-items:center;flex:0 0 22px;height:22px;border-radius:50%;background:#d98627;color:#fff;font-size:12px;font-weight:900}
.kpi-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:18px}.kpi-card{position:relative;overflow:hidden;min-height:154px;padding:18px;background:#fff;border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow)}.kpi-card[data-state="partial"],.kpi-card[data-state="withheld"]{border-color:#e0b16d;background:#fffaf0}.kpi-card:after{content:"";position:absolute;right:-25px;bottom:-38px;width:105px;height:105px;border-radius:50%;background:var(--accent-soft)}.kpi-top{display:flex;justify-content:space-between;gap:8px;align-items:center}.kpi-label{color:var(--muted);font-size:12px;font-weight:800;letter-spacing:.06em;text-transform:uppercase}.kpi-value{position:relative;z-index:1;display:block;margin:13px 0 5px;font-size:34px;line-height:1;font-weight:850;letter-spacing:-.045em}.kpi-note{position:relative;z-index:1;margin:0;color:var(--muted);font-size:12px}.trend{padding:4px 7px;border-radius:999px;font-size:11px;font-weight:800}.trend.up{background:var(--green-soft);color:var(--green)}.trend.down{background:var(--red-soft);color:var(--red)}.trend.flat{background:#efefeb;color:#616762}.trend.partial{background:var(--amber-soft);color:var(--amber)}
.chart-panel{padding:20px;margin-bottom:18px}.chart-panel .panel-heading{margin-bottom:18px}.metric-description{max-width:650px}.chart-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.chart-card{min-width:0;padding:15px;border:1px solid #e6e5df;border-radius:13px;background:linear-gradient(180deg,#fff,#fbfaf7)}.chart-card-head{display:flex;justify-content:space-between;gap:12px;align-items:baseline;margin-bottom:10px}.chart-card h3{margin:0;font-size:14px}.chart-total{color:var(--muted);font-size:12px;font-weight:750}.chart-scroll{overflow-x:auto;padding:4px 0 0}.bar-grid{position:relative;display:grid;grid-auto-flow:column;grid-auto-columns:minmax(12px,1fr);align-items:end;gap:4px;height:205px;min-width:100%;border-bottom:1px solid #cfd1cc;background:repeating-linear-gradient(to top,transparent 0,transparent 50px,#ecece7 51px)}.bar-grid.density-mid{grid-auto-columns:minmax(9px,1fr)}.bar-grid.density-wide{grid-auto-columns:minmax(6px,1fr)}.bar-slot{height:100%;display:flex;align-items:end;justify-content:center}.bar{display:block;width:72%;min-height:2px;border-radius:5px 5px 2px 2px;background:var(--accent)}.bar.tone-1{background:#357a68}.bar.tone-2{background:#6772a8}.bar.tone-3{background:#ba8b32}.axis-labels{display:flex;justify-content:space-between;gap:12px;margin-top:7px;color:var(--muted);font-size:11px}.chart-data{margin-top:10px;color:var(--muted);font-size:12px}.chart-data summary{cursor:pointer;font-weight:700}.empty-state{padding:34px;border:1px dashed #cacbc5;border-radius:12px;text-align:center;color:var(--muted)}
.split-grid{display:grid;grid-template-columns:1.05fr .95fr;gap:18px;margin-bottom:18px}.split-grid>.section-panel:only-child{grid-column:1/-1}.section-panel{padding:20px}.health-grid,.pipeline-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.health-item,.pipeline-item{padding:13px;border:1px solid #e6e5df;border-radius:11px;background:#fbfaf7}.health-item b,.pipeline-item b{display:block;margin-bottom:3px;font-size:13px}.health-item span,.pipeline-item span{color:var(--muted);font-size:12px}.pipeline-value{display:block!important;margin:5px 0 1px;font-size:24px!important;line-height:1;font-weight:850;color:var(--ink)!important}.pipeline-note{margin:12px 0 0;color:var(--muted);font-size:12px}
.decision-panel{padding:20px;margin-bottom:18px}.decision-grid,.engagement-grid,.roadmap-grid{display:grid;gap:12px}.decision-grid{grid-template-columns:repeat(4,minmax(0,1fr))}.engagement-grid{grid-template-columns:repeat(5,minmax(0,1fr))}.roadmap-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.decision-card,.engagement-card,.roadmap-card{min-width:0;padding:15px;border:1px solid #e6e5df;border-radius:13px;background:#fbfaf7}.decision-card[data-state="withheld"],.engagement-card[data-state="withheld"],.decision-card[data-state="partial"]{border-color:#e0b16d;background:#fffaf0}.decision-card h3,.engagement-card h3,.roadmap-card h3{margin:0;font-size:13px}.decision-value{display:block;margin:12px 0 7px;font-size:30px;line-height:1;font-weight:850;letter-spacing:-.04em}.decision-meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:8px}.decision-note,.roadmap-card p{margin:0;color:var(--muted);font-size:12px}.decision-scope{display:block;margin-top:7px;color:var(--ink-2);font-size:11px;font-weight:750}.roadmap-card p+p{margin-top:8px}.roadmap-card strong{color:var(--ink-2)}.attention-list{display:grid;gap:9px;margin:0;padding:0;list-style:none}.attention-item{display:grid;grid-template-columns:auto minmax(0,1fr);gap:11px;padding:13px;border:1px solid #e6e5df;border-radius:12px;background:#fbfaf7}.attention-item[data-severity="immediate"]{border-color:#e9aaa3;background:var(--red-soft)}.attention-item[data-severity="review"]{border-color:#e0b16d;background:var(--amber-soft)}.attention-item[data-severity="clear"]{border-color:#a7d5c2;background:var(--green-soft)}.attention-rank{display:grid;place-items:center;width:28px;height:28px;border-radius:9px;background:var(--ink);color:#fff;font-size:11px;font-weight:850;text-transform:uppercase}.attention-severity{display:inline-block!important;margin:0 0 4px!important;color:var(--ink-2)!important;font-size:11px!important;font-weight:850;text-transform:uppercase;letter-spacing:.06em}.attention-copy h3{margin:0;font-size:14px}.attention-copy p{margin:3px 0 0;color:var(--muted);font-size:12px}.attention-copy strong{color:var(--ink-2)}.operations-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.operation-card{min-width:0;padding:13px;border:1px solid #e6e5df;border-radius:11px;background:#fbfaf7}.operation-card[data-state="failed"]{border-color:#e9aaa3;background:var(--red-soft)}.operation-card b{display:block;font-size:13px}.operation-card span{display:block;overflow-wrap:anywhere;color:var(--muted);font-size:12px}.capability-strip{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.capability-chip{max-width:100%;padding:7px 9px;border-radius:9px;background:#efefeb;color:var(--ink-2);font-size:11px;font-weight:700}.capability-chip[data-state="not_recorded"]{background:var(--amber-soft);color:var(--ink-2)}.pulse-table td[data-state="withheld"]{color:var(--amber);font-weight:750}.pulse-table td[data-state="not_configured"],.pulse-table td[data-state="unavailable"]{color:var(--muted)}
.operation-card[data-state="never_run"],.operation-card[data-state="running"]{border-color:#e0b16d;background:var(--amber-soft)}
.pulse-source{display:block;color:var(--muted);font-size:10px;font-weight:700}
.table-panel{overflow:hidden;margin-bottom:18px}.table-panel .panel-heading{padding:20px 20px 0}.table-scroll{overflow-x:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:11px 14px;border-bottom:1px solid #ecebe6;white-space:nowrap}th{color:var(--muted);font-size:11px;letter-spacing:.06em;text-transform:uppercase}td{font-size:13px}tbody tr:hover{background:#fbfaf7}.metric-name{font-weight:750}.source-chip{display:inline-block;padding:3px 7px;border-radius:999px;background:#efefeb;color:#505852;font-size:11px;font-weight:700}.positive{color:var(--green);font-weight:750}.negative{color:var(--red);font-weight:750}.muted{color:var(--muted)}.footer{display:flex;justify-content:space-between;gap:20px;color:var(--muted);font-size:12px}.sr-only{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}
.plot-form{grid-template-columns:repeat(4,minmax(135px,1fr))}.check-field{display:flex;min-height:42px;align-items:center;gap:9px;padding:9px 11px;border:1px solid #c8c9c3;border-radius:9px;background:#fff}.check-field input{width:17px;min-height:auto;height:17px;margin:0}.check-field span{font-size:13px}.chart-stage{position:relative;min-height:390px;border:1px solid #e3e4de;border-radius:14px;background:linear-gradient(180deg,#fff 0%,#fbfaf7 100%);overflow:hidden}.time-series-chart{display:block;width:100%;height:390px}.chart-status{position:absolute;left:18px;top:14px;z-index:2;max-width:calc(100% - 36px);padding:6px 9px;border:1px solid rgba(222,221,213,.9);border-radius:8px;background:rgba(255,255,255,.9);color:var(--muted);font-size:11px;pointer-events:none}.chart-legend{display:flex;flex-wrap:wrap;gap:10px 18px;margin:13px 0 0;padding:0;list-style:none;color:var(--ink-2);font-size:12px}.chart-legend li{display:flex;align-items:center;gap:7px}.legend-swatch{width:18px;height:3px;border-radius:4px;background:var(--accent)}.legend-tone-1{background:#277962}.legend-tone-2{background:#5869a6}.legend-tone-3{background:#b27b24}.legend-tone-4{background:#9b4d7c}.legend-tone-5{background:#2e7ea1}.chart-fallback{margin-top:16px}.chart-fallback>summary{cursor:pointer;color:var(--muted);font-size:12px;font-weight:750}.plot-note{display:flex;gap:9px;align-items:flex-start;margin:12px 0 0;color:var(--muted);font-size:12px}.plot-note b{color:var(--ink-2)}.plot-mode{border-color:#f1b195!important;background:var(--accent-soft)!important;color:#7d351a!important}
.graph-form{grid-template-columns:minmax(160px,1fr) minmax(190px,1.25fr) minmax(130px,.7fr) 2fr auto}.layer-picker{display:flex;min-width:0;flex-wrap:wrap;gap:7px 12px;min-height:42px;padding:8px 10px;border:1px solid #c8c9c3;border-radius:9px;background:#fff}.layer-picker label{display:flex;align-items:center;gap:5px;color:var(--ink-2);font-size:12px;font-weight:700}.layer-picker input{width:15px;height:15px;min-height:0;margin:0}.graph-stage{display:grid;grid-template-columns:minmax(0,1fr) minmax(235px,.38fr);gap:12px;align-items:start;overflow:hidden;padding:12px;border:1px solid #e3e4de;border-radius:14px;background:linear-gradient(180deg,#fff,#fbfaf7)}.site-graph-svg{display:block;width:100%;height:auto;min-height:300px}.graph-edge{stroke:#aab0ac;stroke-width:1.7;opacity:.58;cursor:pointer;transition:opacity .16s ease,stroke-width .16s ease;vector-effect:non-scaling-stroke}.graph-edge:hover,.graph-edge:focus,.graph-edge.is-active{opacity:1;stroke-width:4;outline:none}.graph-edge.is-related{opacity:.9;stroke-width:2.6}.graph-edge.is-dimmed{opacity:.08}.graph-edge.action{stroke:#e86d3d}.graph-edge.related{stroke:#5869a6}.graph-node-group{cursor:pointer}.graph-node-group:focus{outline:none}.graph-node{fill:#fff;stroke:#355f52;stroke-width:2;transition:stroke-width .16s ease,filter .16s ease,opacity .16s ease}.graph-node.goal{fill:var(--green-soft);stroke:var(--green)}.graph-node.unreachable{fill:var(--red-soft);stroke:var(--red)}.graph-node.selected{fill:var(--accent-soft);stroke:var(--accent);stroke-width:4}.graph-node-group:hover .graph-node,.graph-node-group:focus .graph-node,.graph-node-group.is-active .graph-node{stroke-width:4;filter:drop-shadow(0 4px 10px rgba(25,35,31,.18))}.graph-node-group.is-related .graph-node{stroke-width:3}.graph-node-group.is-dimmed .graph-node{opacity:.22}.graph-label{fill:var(--ink);font-size:11px;font-weight:750;text-anchor:middle;opacity:0;pointer-events:none;transition:opacity .16s ease}.graph-node-group:hover .graph-label,.graph-node-group:focus .graph-label,.graph-node-group.is-active .graph-label,.graph-node-group.is-related .graph-label{opacity:1}.graph-inspector{min-width:0;padding:12px;border:1px solid #e3e4de;border-radius:12px;background:rgba(255,255,255,.86);color:var(--muted);font-size:12px}.graph-inspector strong{display:block;color:var(--ink);font-size:13px}.graph-inspector p{margin:6px 0 0}.graph-inspector.is-pinned{border-color:#f1b195;background:var(--accent-soft)}.graph-caption{margin:10px 0 0;color:var(--muted);font-size:12px}.graph-disclosure{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:0 0 16px;padding:14px;border:1px solid #dfded7;border-radius:13px;background:#fbfaf7}.graph-disclosure p{min-width:0;margin:0;overflow-wrap:anywhere;color:var(--muted);font-size:12px}.graph-disclosure strong{display:block;color:var(--ink);font-size:13px}.graph-reasons{grid-column:1/-1;margin:0;padding-left:20px;color:var(--muted);font-size:12px}.graph-actions{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.graph-actions a{padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:#fff;font-size:12px;font-weight:750;text-decoration:none}.graph-view-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;margin-bottom:18px}.graph-view-grid .section-panel{margin-bottom:0}.view-note{margin:8px 0 0;color:var(--muted);font-size:12px}.matrix-scroll{overflow:auto;max-height:520px}.matrix-table th,.matrix-table td{text-align:center;padding:8px;min-width:36px}.matrix-table th:first-child,.matrix-table td:first-child{text-align:left;position:sticky;left:0;background:#fff;z-index:1}.matrix-hit{background:var(--accent-soft);color:#7d351a;font-weight:850}.edge-tools{display:grid;grid-template-columns:minmax(180px,1fr) minmax(130px,.45fr) minmax(110px,.35fr) auto;gap:10px;align-items:end;padding:0 20px 16px}.edge-table-panel{margin-bottom:18px}.edge-identity{white-space:normal;overflow-wrap:anywhere}.edge-evidence{max-width:360px;white-space:normal;overflow-wrap:anywhere}.pager{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:14px 20px;color:var(--muted);font-size:12px}.pager a{font-weight:750}.distance-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:8px}.distance-item{padding:12px 8px;border:1px solid #e6e5df;border-radius:10px;background:#fbfaf7;text-align:center}.distance-item b{display:block;font-size:22px}.distance-item span{color:var(--muted);font-size:11px}.graph-meta{display:flex;min-width:0;flex-wrap:wrap;gap:8px;margin:0 0 18px}.graph-meta span{max-width:100%;padding:5px 8px;overflow-wrap:anywhere;border-radius:999px;background:#efefeb;color:var(--ink-2);font-size:11px;font-weight:750}.graph-empty{padding:42px 20px;text-align:center}.graph-empty h2{margin:0 0 8px}.graph-empty p{max-width:620px;margin:auto;color:var(--muted)}
.graph-stage{background:radial-gradient(ellipse at 50% 42%,rgba(255,240,232,.95),rgba(255,255,255,.92) 42%,#fbfaf7 100%)}.graph-depth-plane{fill:#efe9df;opacity:.55}.graph-edge{fill:none;stroke-linecap:round;stroke-linejoin:round}.graph-edge.menu,.graph-edge.utility,.graph-edge.breadcrumb{opacity:.34}.graph-edge.menu{stroke:#8b8f8c}.graph-edge.utility{stroke:#9a855f}.graph-edge.breadcrumb{stroke:#87918f;stroke-dasharray:5 5}.graph-node-shadow{fill:#1f2925;opacity:.12;filter:blur(3px)}.graph-node{filter:url(#node-lift)}.graph-node.depth-front{stroke-width:3}.graph-label{paint-order:stroke;stroke:#fff7;stroke-width:3px}.graph-label .graph-label-title{font-weight:850}.graph-label .graph-label-route{fill:var(--muted);font-size:9px;font-weight:700}.graph-edge-glow{stroke:#fff;stroke-width:5;opacity:.35}.graph-layout-note{display:inline-block;margin-left:7px;color:var(--muted);font-size:11px;font-weight:750}
.graph-stage{grid-template-columns:minmax(0,1fr) minmax(255px,.34fr);gap:14px;padding:16px}.graph-map{position:relative;min-width:0}.graph-map-help{max-width:760px;margin:0 0 8px;color:var(--ink-2);font-size:12px;font-weight:750}.graph-canvas-toolbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 8px}.graph-canvas-toolbar button{min-height:32px;padding:5px 9px;border-color:#c8c9c3;background:rgba(255,255,255,.88);color:var(--ink-2);font-size:12px;font-weight:800}.graph-canvas-toolbar button:hover{background:#fff;color:var(--ink)}.graph-zoom-status{padding:4px 7px;border-radius:999px;background:#efefeb;color:var(--muted);font-size:11px;font-weight:800}.site-graph-svg{min-height:430px;cursor:grab;touch-action:none;user-select:none}.graph-map.is-panning .site-graph-svg{cursor:grabbing}.graph-viewport{transform-origin:0 0}.graph-depth-plane{opacity:.42}.graph-cluster-label{fill:var(--muted);font-size:12px;font-weight:850;letter-spacing:.08em;text-transform:uppercase;paint-order:stroke;stroke:#fff8;stroke-width:4px}.graph-node-group.is-key .graph-label,.graph-node-group.goal .graph-label,.graph-node-group.selected .graph-label{opacity:1}.graph-label .graph-label-route{display:none}.graph-node-group.is-dimmed .graph-label{opacity:.12}.graph-node-shadow{opacity:.1}.graph-edge{opacity:.48}.graph-edge:hover,.graph-edge:focus,.graph-edge.is-active{stroke-width:3}.graph-edge.is-related{stroke-width:2.2}.graph-edge.menu,.graph-edge.utility,.graph-edge.breadcrumb{opacity:.2}
@media(max-width:980px){.kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.filter-form,.plot-form{grid-template-columns:repeat(2,minmax(0,1fr))}.filter-form button{grid-column:span 2}.chart-grid,.split-grid{grid-template-columns:1fr}}
@media(max-width:980px){.decision-grid,.engagement-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.roadmap-grid,.operations-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:980px){.graph-form,.edge-tools{grid-template-columns:1fr 1fr}.graph-form button,.edge-tools button{grid-column:span 2}.graph-stage,.graph-view-grid{grid-template-columns:1fr}.graph-disclosure{grid-template-columns:1fr 1fr}.distance-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(max-width:650px){.topbar-inner,.shell{padding-left:16px;padding-right:16px}.topbar-inner{align-items:flex-start}.live-state{margin-top:9px}.hero{grid-template-columns:1fr}.coverage-badge{justify-self:start}.filter-form,.plot-form,.graph-form,.edge-tools{grid-template-columns:1fr}.filter-form button,.graph-form button,.edge-tools button{grid-column:auto}.tools-row,.footer,.pager{align-items:flex-start;flex-direction:column}.kpi-grid{grid-template-columns:1fr 1fr;gap:10px}.kpi-card{min-height:132px;padding:15px}.kpi-value{font-size:28px}.chart-panel,.section-panel{padding:16px}.health-grid,.pipeline-grid{grid-template-columns:1fr 1fr}.bar-grid{height:175px}.chart-stage{min-height:315px}.time-series-chart{height:315px}th,td{padding:10px 12px}.panel-heading{display:block}.quick-links{margin-top:10px}.graph-disclosure{grid-template-columns:1fr}.distance-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.site-graph-svg{min-height:240px}}
@media(max-width:650px){.decision-grid,.engagement-grid,.roadmap-grid,.operations-grid{grid-template-columns:1fr}.decision-panel{padding:16px}.attention-item{grid-template-columns:1fr}.attention-rank{width:auto;padding:0 9px;justify-self:start}.pulse-table{min-width:670px}}
@media(max-width:420px){.kpi-grid{grid-template-columns:1fr}.health-grid,.pipeline-grid{grid-template-columns:1fr}.topbar-inner{display:block}.brand{margin-bottom:10px}.live-state{max-width:100%;overflow-wrap:anywhere}}
"""

VISUAL_REFRESH_CSS = """
:root{--ink:#12201b;--ink-2:#263a32;--paper:#f1f4f2;--surface:#fff;--line:#dce4df;--muted:#5c6b64;--accent:#e96d3c;--accent-soft:#fff0e9;--green:#13795b;--green-soft:#e5f5ef;--amber:#9a5a10;--amber-soft:#fff5df;--red:#a64138;--red-soft:#fdecea;--shadow:0 18px 48px rgba(20,39,31,.07);--shadow-soft:0 8px 24px rgba(20,39,31,.055)}
body{background:radial-gradient(circle at 8% 0%,#fff 0,transparent 28%),linear-gradient(180deg,#f6f8f7 0,#eef2ef 100%);font-size:14px}.topbar{position:sticky;top:0;z-index:10;background:rgba(18,32,27,.96);border-bottom:1px solid rgba(255,255,255,.09);backdrop-filter:blur(16px)}.topbar-inner{max-width:1360px;padding-top:14px;padding-bottom:14px}.brand-mark{border:0;background:linear-gradient(135deg,#ff9b72,#e96d3c);color:#1b211e;box-shadow:inset 0 1px rgba(255,255,255,.5)}.live-state{padding:7px 10px;border:1px solid rgba(255,255,255,.13);border-radius:999px;background:rgba(255,255,255,.055)}
.shell{max-width:1360px;padding-top:26px}.report-nav{margin-bottom:14px}.report-nav a,.subnav a,.quick-links a{border-color:#d8e0db;background:rgba(255,255,255,.72);box-shadow:0 1px 2px rgba(20,39,31,.03);transition:transform .15s ease,border-color .15s ease,background .15s ease}.report-nav a:hover,.subnav a:hover,.quick-links a:hover{transform:translateY(-1px);border-color:#aebcb4}.report-nav a.active,.subnav a.active{background:#183129;border-color:#183129}.subnav{margin:14px 0 18px}
.hero{align-items:center;margin-bottom:18px;padding:30px 32px;border:1px solid rgba(255,255,255,.11);border-radius:26px;background:radial-gradient(circle at 82% 18%,rgba(233,109,60,.27),transparent 30%),linear-gradient(135deg,#142a22 0%,#1c3b31 58%,#244a3d 100%);box-shadow:0 24px 60px rgba(17,39,31,.18);color:#fff}.hero .eyebrow{color:#ffae8e}.hero h1{max-width:760px;font-size:clamp(32px,4.5vw,52px)}.hero-copy{color:#c7d5cf}.trust-card{min-width:205px;padding:16px 17px;border:1px solid rgba(255,255,255,.17);border-radius:18px;background:rgba(255,255,255,.09);box-shadow:inset 0 1px rgba(255,255,255,.08);backdrop-filter:blur(10px)}.trust-card[data-state="partial"]{background:rgba(255,191,105,.11)}.trust-label{display:block;color:#bfcec8;font-size:10px;font-weight:850;letter-spacing:.11em;text-transform:uppercase}.trust-value{display:flex;justify-content:space-between;gap:16px;align-items:baseline;margin:6px 0;color:#fff}.trust-value strong{font-size:25px;letter-spacing:-.04em}.trust-value span{color:#d8e3df;font-size:12px;font-weight:750}.coverage-track{height:6px;overflow:hidden;border-radius:999px;background:rgba(255,255,255,.15)}.coverage-fill{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,#5ad0a0,#8ce6bd)}.trust-card[data-state="partial"] .coverage-fill{background:linear-gradient(90deg,#eaa04f,#ffd08b)}
.panel{border-color:rgba(205,217,210,.92);border-radius:20px;box-shadow:var(--shadow-soft)}.kpi-grid{gap:12px}.kpi-card{min-height:142px;padding:17px 17px 16px;border-radius:18px;box-shadow:var(--shadow-soft)}.kpi-card:before{content:"";position:absolute;left:0;top:0;width:100%;height:4px;background:linear-gradient(90deg,#e96d3c,#f2a37f)}.kpi-card:nth-child(2):before{background:linear-gradient(90deg,#168264,#62c4a2)}.kpi-card:nth-child(3):before{background:linear-gradient(90deg,#586aa8,#8998ce)}.kpi-card:nth-child(4):before{background:linear-gradient(90deg,#a97924,#d4aa59)}.kpi-card:after{right:-38px;bottom:-50px;width:125px;height:125px;opacity:.72}.kpi-value{margin-top:18px;font-size:38px}.kpi-note{max-width:92%;line-height:1.42}
.dashboard-primary{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(310px,.72fr);gap:18px;align-items:stretch;margin-bottom:18px}.dashboard-primary>.chart-panel,.dashboard-primary>.attention-panel{margin-bottom:0}.chart-panel{padding:23px}.chart-stage{min-height:430px;border-color:#dfe7e2;border-radius:16px;background:linear-gradient(180deg,#fbfdfc 0%,#f6faf7 100%)}.time-series-chart{height:430px}.chart-status{left:20px;top:16px;border-color:#dfe7e2;border-radius:999px;background:rgba(255,255,255,.92);box-shadow:0 4px 14px rgba(20,39,31,.06)}.source-chip{background:#edf3ef;color:#385248}
.attention-panel{padding:21px;background:linear-gradient(180deg,#fff,#fbfdfc)}.attention-panel .panel-heading{margin-bottom:16px}.attention-list{gap:10px}.attention-item{grid-template-columns:auto minmax(0,1fr);padding:14px;border-color:#e4ebe7;border-radius:15px;background:#f7faf8}.attention-item[data-severity="review"]{border-color:#f0d8a8;background:#fffaf0}.attention-item[data-severity="immediate"]{border-color:#efc3be;background:#fff3f1}.attention-item[data-severity="clear"]{border-color:#bfe0d2;background:#edf9f4}.attention-rank{width:30px;height:30px;border-radius:50%;background:#23443a}.attention-copy h3{font-size:15px;line-height:1.3}.attention-copy p{line-height:1.45}.attention-severity{color:#6b7b73!important}
.decision-panel{padding:22px}.decision-card,.engagement-card,.roadmap-card{border-color:#e2e9e5;border-radius:15px;background:#f8faf9}.decision-value{font-size:32px}.decision-grid{gap:10px}.engagement-grid{gap:10px}.split-grid{gap:14px}.section-panel{padding:21px}.health-item,.pipeline-item,.operation-card{border-color:#e2e9e5;border-radius:13px;background:#f8faf9}
.control-panel{padding:0;overflow:hidden}.control-summary{margin:0;padding:17px 20px;cursor:pointer;list-style:none;align-items:center}.control-summary::-webkit-details-marker{display:none}.control-summary:after{content:"+";display:grid;place-items:center;flex:0 0 30px;height:30px;border-radius:50%;background:#edf3ef;color:#27453a;font-size:20px;font-weight:500}.control-panel[open] .control-summary:after{content:"−"}.control-content{padding:0 20px 19px;border-top:1px solid #e9eeeb}.control-content .filter-form{padding-top:18px}.control-content .tools-row{margin-bottom:0}.data-notices{margin:0 0 18px;border:1px solid #edd7af;border-radius:15px;background:#fffaf0}.data-notices>summary{cursor:pointer;padding:13px 16px;color:#744711;font-weight:800}.data-notices .alerts{margin:0;padding:0 12px 12px}.data-notices .alert{border:0;background:rgba(255,255,255,.62)}
.control-summary:focus,.data-notices>summary:focus,.evidence-panel>summary:focus{outline:3px solid rgba(233,109,60,.3);outline-offset:-3px}
.evidence-panel{padding:0;overflow:hidden}.evidence-panel>summary{cursor:pointer;list-style:none;padding:19px 21px}.evidence-panel>summary::-webkit-details-marker{display:none}.evidence-panel>summary:after{content:"View";padding:5px 9px;border-radius:999px;background:#edf3ef;color:#385248;font-size:11px;font-weight:800}.evidence-panel[open]>summary:after{content:"Hide"}.evidence-body{padding:0 21px 21px}.evidence-panel .operations-grid,.evidence-panel .roadmap-grid{margin-top:0}.table-panel{border-radius:20px}.table-scroll{scrollbar-color:#bdc9c2 transparent}.footer{padding:8px 2px 0}
@media(max-width:1080px){.dashboard-primary{grid-template-columns:1fr}.attention-panel .attention-list{grid-template-columns:repeat(2,minmax(0,1fr))}.attention-panel .attention-item:last-child:nth-child(odd){grid-column:1/-1}}
@media(max-width:760px){.hero{padding:24px}.trust-card{width:100%}.dashboard-primary{display:block}.dashboard-primary>.chart-panel{margin-bottom:14px}.attention-panel .attention-list{grid-template-columns:1fr}.attention-panel .attention-item:last-child:nth-child(odd){grid-column:auto}.chart-stage{min-height:340px}.time-series-chart{height:340px}.control-summary{display:flex}.filter-form,.plot-form{grid-template-columns:1fr}.filter-form button{grid-column:auto}.kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:480px){.shell{padding-top:18px}.hero{padding:22px 19px;border-radius:21px}.hero h1{font-size:34px}.kpi-grid{grid-template-columns:1fr}.kpi-card{min-height:130px}.chart-panel,.attention-panel,.decision-panel,.section-panel{padding:17px}.chart-stage{min-height:300px}.time-series-chart{height:300px}.live-state{border-radius:12px}}
"""

HEIGHT_CLASSES = "".join(f".h-{level}{{height:{level * 2}%}}" for level in range(51))
WIDTH_CLASSES = "".join(f".p-{level}{{width:{level}%}}" for level in range(101))
GEOGRAPHY_CSS = """
.geography-panel{padding:21px;margin-bottom:18px}.geography-controls{display:flex;align-items:end;gap:12px}.geography-controls .field{min-width:210px}.geography-grid{display:grid;grid-template-columns:1.18fr .82fr;gap:14px}.map-card{min-width:0;padding:14px;border:1px solid #e1e4dd;border-radius:15px;background:linear-gradient(180deg,#fbfdfb,#f5f3ed)}.map-card h3{margin:0;font-size:14px}.map-card>p{margin:3px 0 11px;color:var(--muted);font-size:12px}.geo-svg{display:block;width:100%;height:auto;min-height:280px;border-radius:12px;background:#e7f0ed}.geo-shape{stroke:#fff;stroke-width:.65;vector-effect:non-scaling-stroke;cursor:pointer;transition:filter .12s ease,opacity .12s ease}.geo-shape[data-interactive="false"]{cursor:default}.geo-shape:hover,.geo-shape:focus{filter:brightness(.88);outline:none;stroke:#17201d;stroke-width:1.4}.geo-shape.is-selected{stroke:#17201d;stroke-width:2}.county-shape{fill:rgba(255,255,255,.15);stroke:#6f7b76;stroke-width:.35;vector-effect:non-scaling-stroke;pointer-events:none}.geo-status{margin:12px 0 0;padding:10px 12px;border-radius:10px;background:#edf3ef;color:#33473f;font-size:12px}.geo-disclosure{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px;margin-top:12px}.geo-disclosure p{margin:0;padding:10px;border:1px solid #e2e4de;border-radius:10px;color:var(--muted);font-size:11px}.geo-disclosure strong{display:block;color:var(--ink);font-size:12px}.geography-fallback{margin-top:12px}.geography-fallback>summary{cursor:pointer;color:var(--muted);font-size:12px;font-weight:750}.geo-empty{padding:18px;color:var(--muted);text-align:center}.geo-suppressed{color:var(--amber);font-weight:750}
@media(max-width:900px){.geography-grid{grid-template-columns:1fr}.geo-svg{min-height:240px}.geo-disclosure{grid-template-columns:1fr}}
@media(max-width:560px){.geography-panel{padding:16px}.geography-controls{display:grid;grid-template-columns:1fr}.geography-controls .field{min-width:0}.geo-svg{min-height:210px}.map-card{padding:10px}}
"""
CSS = BASE_CSS + VISUAL_REFRESH_CSS + GEOGRAPHY_CSS + HEIGHT_CLASSES + WIDTH_CLASSES

JS = r"""
(() => {
  const colors = ["#e86d3d", "#277962", "#5869a6", "#b27b24", "#9b4d7c", "#2e7ea1"];
  const format = new Intl.NumberFormat(undefined, {maximumFractionDigits: 2});

  function updateMetricOptions() {
    const source = document.querySelector('select[name="source"]');
    const metric = document.querySelector('select[name="metric"]');
    if (!source || !metric) return;
    let first = null;
    for (const option of metric.options) {
      const visible = option.dataset.source === source.value;
      option.hidden = !visible;
      option.disabled = !visible;
      if (visible && first === null) first = option;
    }
    if (metric.selectedOptions[0]?.disabled && first) first.selected = true;
  }

  function updateSiteOptions() {
    const source = document.querySelector('select[name="source"]');
    const metric = document.querySelector('select[name="metric"]');
    const site = document.querySelector('select[name="site"]');
    const selectedSource = source?.value || metric?.selectedOptions[0]?.dataset.source || "";
    if (!selectedSource || !site) return;
    let first = null;
    for (const option of site.options) {
      const supported = option.value === "all" || (option.dataset.sources || "").split(",").includes(selectedSource);
      option.hidden = !supported;
      option.disabled = !supported;
      if (supported && first === null) first = option;
    }
    if (site.selectedOptions[0]?.disabled && first) first.selected = true;
  }

  function drawChart(canvas, payload) {
    const series = payload.series || [];
    const comparison = payload.comparison_series || [];
    const status = document.getElementById("chart-status");
    const legend = document.getElementById("chart-legend");
    if (!series.length) {
      status.textContent = "No stored daily values match this selection.";
      canvas.dataset.rendered = "empty";
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(320, Math.round(rect.width));
    const height = Math.max(280, Math.round(rect.height));
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const margin = {left: 58, right: 24, top: 52, bottom: 42};
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const allValues = [...series, ...comparison].flatMap(item => item.points.map(point => Number(point.value)));
    let min = Math.min(0, ...allValues);
    let max = Math.max(0, ...allValues);
    if (min === max) max = min + 1;
    const padding = (max - min) * .08;
    if (max > 0) max += padding;
    if (min < 0) min -= padding;
    const dateMs = value => Date.parse(value + "T00:00:00Z");
    const currentStart = dateMs(payload.window.start.slice(0, 10));
    const currentEnd = dateMs(payload.window.end.slice(0, 10));
    const comparisonStart = dateMs(payload.comparison_window.start.slice(0, 10));
    const dayCount = Math.max(1, Math.round((currentEnd - currentStart) / 86400000));
    const xIndex = index => margin.left + (dayCount === 1 ? plotWidth / 2 : index / (dayCount - 1) * plotWidth);
    const xPoint = (point, startMs) => xIndex(Math.round((dateMs(point.date) - startMs) / 86400000));
    const y = value => margin.top + (max - value) / (max - min) * plotHeight;

    ctx.font = "11px system-ui, sans-serif";
    ctx.textBaseline = "middle";
    ctx.strokeStyle = "#e4e4de";
    ctx.fillStyle = "#727974";
    ctx.lineWidth = 1;
    for (let step = 0; step <= 4; step++) {
      const value = max - (max - min) * step / 4;
      const py = margin.top + plotHeight * step / 4;
      ctx.beginPath(); ctx.moveTo(margin.left, py); ctx.lineTo(width - margin.right, py); ctx.stroke();
      ctx.textAlign = "right"; ctx.fillText(format.format(value), margin.left - 9, py);
    }
    const dateAt = index => new Date(currentStart + index * 86400000).toISOString().slice(0, 10);
    const tickIndexes = [...new Set([0, Math.floor((dayCount - 1) / 2), dayCount - 1])];
    ctx.textBaseline = "top";
    for (const index of tickIndexes) {
      ctx.textAlign = index === 0 ? "left" : index === dayCount - 1 ? "right" : "center";
      ctx.fillText(dateAt(index), xIndex(index), height - margin.bottom + 13);
    }

    function contiguousSegments(points) {
      const segments = [];
      for (const point of points) {
        const current = segments[segments.length - 1];
        const previous = current?.[current.length - 1];
        const gapDays = previous ? Math.round((dateMs(point.date) - dateMs(previous.date)) / 86400000) : 1;
        if (!current || gapDays !== 1) segments.push([point]); else current.push(point);
      }
      return segments;
    }

    function path(item, color, dashed, fill) {
      if (!item.points.length) return;
      const startMs = dashed ? comparisonStart : currentStart;
      for (const segment of contiguousSegments(item.points)) {
        ctx.beginPath();
        segment.forEach((point, index) => {
          const px = xPoint(point, startMs), py = y(Number(point.value));
          if (index === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
        });
        if (fill && segment.length > 1) {
          ctx.lineTo(xPoint(segment[segment.length - 1], startMs), y(min));
          ctx.lineTo(xPoint(segment[0], startMs), y(min)); ctx.closePath();
          const gradient = ctx.createLinearGradient(0, margin.top, 0, height - margin.bottom);
          gradient.addColorStop(0, color + "42"); gradient.addColorStop(1, color + "08");
          ctx.fillStyle = gradient; ctx.fill();
          ctx.beginPath();
          segment.forEach((point, index) => {
            const px = xPoint(point, startMs), py = y(Number(point.value));
            if (index === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
          });
        }
        ctx.strokeStyle = color; ctx.globalAlpha = dashed ? .55 : 1; ctx.lineWidth = dashed ? 1.5 : 2.5;
        ctx.setLineDash(dashed ? [6, 5] : []); ctx.lineJoin = "round"; ctx.lineCap = "round"; ctx.stroke();
      }
      ctx.setLineDash([]); ctx.globalAlpha = 1;
      if (!dashed && item.points.length <= 45) {
        ctx.fillStyle = color;
        item.points.forEach(point => { ctx.beginPath(); ctx.arc(xPoint(point,startMs), y(Number(point.value)), 3, 0, Math.PI * 2); ctx.fill(); });
      }
    }

    if (payload.style === "bar") {
      const groupWidth = Math.max(2, Math.min(18, plotWidth / dayCount * .7));
      const barWidth = Math.max(1.5, groupWidth / series.length);
      series.forEach((item, seriesIndex) => {
        ctx.fillStyle = colors[seriesIndex % colors.length];
        item.points.forEach(point => {
          const valueY = y(Number(point.value));
          const zeroY = y(0);
          ctx.fillRect(xPoint(point,currentStart) - groupWidth / 2 + seriesIndex * barWidth, Math.min(valueY, zeroY), barWidth - 1, Math.max(1, Math.abs(zeroY - valueY)));
        });
      });
      comparison.forEach((item, seriesIndex) => path(item, colors[seriesIndex % colors.length], true, false));
    } else {
      comparison.forEach((item, seriesIndex) => path(item, colors[seriesIndex % colors.length], true, false));
      series.forEach((item, seriesIndex) => path(item, colors[seriesIndex % colors.length], false, payload.style === "area"));
    }

    if (legend) {
      legend.replaceChildren();
      series.forEach((item, index) => {
        const li = document.createElement("li");
        const swatch = document.createElement("span"); swatch.className = `legend-swatch legend-tone-${index % colors.length}`;
        li.append(swatch, document.createTextNode(payload.site_names[item.site_id] || item.site_id)); legend.append(li);
      });
      if (comparison.length) {
        const li = document.createElement("li"); li.textContent = "Dashed = previous period"; legend.append(li);
      } else if (payload.compare && !payload.comparison_available) {
        const li = document.createElement("li"); li.textContent = "Previous-period comparison unavailable"; legend.append(li);
      }
    }
    const rangeText = `${dateAt(0)} through ${dateAt(dayCount - 1)}`;
    const comparisonText = payload.compare && !payload.comparison_available ? " - comparison unavailable" : "";
    status.textContent = `${payload.metric_label} - ${series.length} series - ${rangeText}${comparisonText}`;
    canvas.dataset.rendered = "true";
    canvas.onpointermove = event => {
      const bounds = canvas.getBoundingClientRect();
      const index = Math.max(0, Math.min(dayCount - 1, Math.round((event.clientX - bounds.left - margin.left) / plotWidth * (dayCount - 1))));
      const selectedDate = dateAt(index);
      const values = series.map(item => {
        const point = item.points.find(candidate => candidate.date === selectedDate);
        return `${payload.site_names[item.site_id] || item.site_id}: ${point ? format.format(Number(point.value)) : "no value"}`;
      });
      status.textContent = `${selectedDate} - ${values.join(" - ")}`;
    };
    canvas.onpointerleave = () => { status.textContent = `${payload.metric_label} - ${series.length} series - ${rangeText}${comparisonText}`; };
  }

  async function loadChart() {
    const canvas = document.getElementById("time-series-chart");
    if (!canvas) return;
    const status = document.getElementById("chart-status");
    try {
      const response = await fetch(canvas.dataset.seriesUrl, {headers: {Accept: "application/json"}, credentials: "same-origin"});
      if (!response.ok) throw new Error(`Series request failed (${response.status})`);
      const payload = await response.json();
      canvas._payload = payload;
      drawChart(canvas, payload);
      new ResizeObserver(() => drawChart(canvas, canvas._payload)).observe(canvas);
    } catch (error) {
      status.textContent = error.message;
      canvas.dataset.rendered = "error";
    }
  }

  const svgNamespace = "http://www.w3.org/2000/svg";

  function heatColor(value, maximum) {
    if (value === undefined || value === null) return "#dfe7e2";
    const intensity = Math.sqrt(Math.max(0, Number(value)) / Math.max(1, maximum));
    const lightness = 92 - intensity * 48;
    return `hsl(16 72% ${lightness}%)`;
  }

  function worldPath(geometry) {
    const polygons = geometry?.type === "Polygon" ? [geometry.coordinates] :
      geometry?.type === "MultiPolygon" ? geometry.coordinates : [];
    return polygons.map(polygon => polygon.map(ring => ring.map((point, index) => {
      const x = (Number(point[0]) + 180) / 360 * 960;
      const y = (90 - Number(point[1])) / 180 * 480;
      return `${index ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`;
    }).join("") + "Z").join("")).join("");
  }

  function decodeArc(topology, arcIndex) {
    const reversed = arcIndex < 0;
    const encoded = topology.arcs[reversed ? ~arcIndex : arcIndex] || [];
    const scale = topology.transform?.scale || [1, 1];
    const translate = topology.transform?.translate || [0, 0];
    let x = 0, y = 0;
    const points = encoded.map(point => {
      x += point[0]; y += point[1];
      return [x * scale[0] + translate[0], y * scale[1] + translate[1]];
    });
    return reversed ? points.reverse() : points;
  }

  function topoRing(topology, indexes) {
    const points = [];
    for (const index of indexes) {
      const arc = decodeArc(topology, index);
      points.push(...(points.length ? arc.slice(1) : arc));
    }
    return points.map((point, index) => `${index ? "L" : "M"}${point[0].toFixed(2)},${point[1].toFixed(2)}`).join("") + "Z";
  }

  function topoPath(topology, geometry) {
    const polygons = geometry.type === "Polygon" ? [geometry.arcs] : geometry.arcs;
    return polygons.map(polygon => polygon.map(ring => topoRing(topology, ring)).join("")).join("");
  }

  function updateGeographyTable(payload) {
    const body = document.getElementById("geography-table-body");
    if (!body) return;
    body.replaceChildren();
    if (!payload.countries.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td"); cell.colSpan = 3; cell.className = "geo-empty";
      cell.textContent = "No unsuppressed country rows are stored for this source and exact window.";
      row.append(cell); body.append(row); return;
    }
    for (const item of payload.countries) {
      const row = document.createElement("tr");
      for (const value of [item.code, format.format(Number(item.value)), payload.label]) {
        const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
      }
      row.firstElementChild.className = "metric-name"; body.append(row);
    }
  }

  function renderWorldMap(svg, world, payload, status) {
    svg.replaceChildren();
    const byCode = new Map(payload.countries.map(item => [item.code, Number(item.value)]));
    const maximum = Math.max(1, ...byCode.values());
    for (const feature of world.features || []) {
      const properties = feature.properties || {};
      const alpha2 = properties.ISO_A2_EH || properties.ISO_A2;
      const alpha3 = properties.ISO_A3_EH || properties.ADM0_A3;
      const matching = payload.countries.find(item => item.code === (item.code_system === "iso-alpha3" ? alpha3 : alpha2));
      const value = matching ? Number(matching.value) : undefined;
      const path = document.createElementNS(svgNamespace, "path");
      path.setAttribute("d", worldPath(feature.geometry));
      path.setAttribute("fill", heatColor(value, maximum));
      path.setAttribute("class", "geo-shape");
      const name = properties.NAME_EN || properties.NAME || properties.ADMIN || alpha3;
      const label = `${name}: ${value === undefined ? "no unsuppressed stored value" : format.format(value) + " " + payload.label}`;
      const interactive = value !== undefined || alpha2 === "US";
      path.dataset.interactive = String(interactive);
      if (!interactive) {
        path.setAttribute("aria-hidden", "true"); svg.append(path); continue;
      }
      path.setAttribute("tabindex", "0"); path.setAttribute("aria-label", label);
      const inspect = () => { status.textContent = label; };
      path.addEventListener("pointerenter", inspect); path.addEventListener("focus", inspect);
      const activate = () => {
        inspect();
        if (alpha2 === "US") document.getElementById("us-geography")?.scrollIntoView({behavior: "smooth", block: "center"});
      };
      path.addEventListener("click", activate);
      path.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activate(); } });
      svg.append(path);
    }
  }

  function renderUsMap(svg, topology, payload, status) {
    svg.replaceChildren();
    const stateRows = new Map(payload.us_states.map(item => [item.name, Number(item.value)]));
    const maximum = Math.max(1, ...stateRows.values());
    const countiesLayer = document.createElementNS(svgNamespace, "g");
    const statesLayer = document.createElementNS(svgNamespace, "g");
    svg.append(statesLayer, countiesLayer);
    let selected = null;

    const showCounties = geometry => {
      selected = geometry.id;
      countiesLayer.replaceChildren();
      for (const county of topology.objects.counties?.geometries || []) {
        if (!String(county.id).startsWith(String(geometry.id))) continue;
        const path = document.createElementNS(svgNamespace, "path");
        path.setAttribute("d", topoPath(topology, county)); path.setAttribute("class", "county-shape");
        countiesLayer.append(path);
      }
      for (const path of statesLayer.children) path.classList.toggle("is-selected", path.dataset.stateId === selected);
      const name = geometry.properties?.name || geometry.id;
      const value = stateRows.get(name);
      status.textContent = `${name}: ${value === undefined ? "no unsuppressed state value" : format.format(value) + " " + payload.label}. County boundaries are orientation only; county values are unavailable.`;
    };

    for (const geometry of topology.objects.states?.geometries || []) {
      const name = geometry.properties?.name || geometry.id;
      const value = stateRows.get(name);
      const path = document.createElementNS(svgNamespace, "path");
      path.setAttribute("d", topoPath(topology, geometry)); path.setAttribute("fill", heatColor(value, maximum));
      path.setAttribute("class", "geo-shape"); path.setAttribute("tabindex", "0"); path.dataset.stateId = geometry.id;
      const label = `${name}: ${value === undefined ? "no unsuppressed state value" : format.format(value) + " " + payload.label}`;
      path.setAttribute("aria-label", `${label}. Activate for county boundaries.`);
      path.addEventListener("pointerenter", () => { if (!selected) status.textContent = label; });
      path.addEventListener("focus", () => { if (!selected) status.textContent = label; });
      path.addEventListener("click", () => showCounties(geometry));
      path.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); showCounties(geometry); } });
      statesLayer.append(path);
    }
  }

  async function loadGeography() {
    const stage = document.getElementById("geography-map");
    if (!stage) return;
    const worldSvg = document.getElementById("world-geo-map");
    const usSvg = document.getElementById("us-geo-map");
    const status = document.getElementById("geography-status");
    const source = document.getElementById("geography-source");
    try {
      const [worldResponse, usResponse] = await Promise.all([
        fetch(stage.dataset.worldMap, {credentials: "same-origin"}),
        fetch(stage.dataset.usMap, {credentials: "same-origin"}),
      ]);
      if (!worldResponse.ok || !usResponse.ok) throw new Error("Local map boundary request failed.");
      const world = await worldResponse.json(); const us = await usResponse.json();
      const render = async () => {
        const url = new URL(stage.dataset.geographyApi, window.location.href);
        url.searchParams.set("source", source.value);
        status.textContent = `Loading ${source.selectedOptions[0].textContent} geography...`;
        const response = await fetch(url, {headers: {Accept: "application/json"}, credentials: "same-origin"});
        if (!response.ok) throw new Error(`Geography request failed (${response.status})`);
        const payload = await response.json();
        renderWorldMap(worldSvg, world, payload, status); renderUsMap(usSvg, us, payload, status); updateGeographyTable(payload);
        const withheld = payload.suppression.withheld_country_rows + payload.suppression.withheld_us_state_rows;
        status.textContent = `${payload.label}: ${payload.countries.length} countries and ${payload.us_states.length} US states displayed; ${withheld} low-volume rows withheld. ${payload.coverage.note}`;
        stage.dataset.rendered = "true";
      };
      source.addEventListener("change", () => render().catch(error => { status.textContent = error.message; stage.dataset.rendered = "error"; }));
      await render();
    } catch (error) {
      status.textContent = error.message; stage.dataset.rendered = "error";
    }
  }

  function initSiteGraph() {
    const stage = document.querySelector("[data-site-graph-stage]");
    if (!stage) return;
    const inspector = stage.querySelector("[data-graph-inspector]");
    const svg = stage.querySelector(".site-graph-svg");
    const map = stage.querySelector("[data-graph-map]");
    const viewport = stage.querySelector("[data-graph-viewport]");
    const zoomInButton = stage.querySelector("[data-graph-zoom-in]");
    const zoomOutButton = stage.querySelector("[data-graph-zoom-out]");
    const zoomResetButton = stage.querySelector("[data-graph-zoom-reset]");
    const zoomStatus = stage.querySelector("[data-graph-zoom-status]");
    const nodes = Array.from(stage.querySelectorAll("[data-graph-node]"));
    const edges = Array.from(stage.querySelectorAll("[data-graph-edge]"));
    if (!inspector || !svg || (!nodes.length && !edges.length)) return;

    function initCanvasView() {
      if (!viewport) return;
      const viewBox = svg.viewBox.baseVal;
      if (!viewBox || !viewBox.width || !viewBox.height) return;
      const transform = {x: 0, y: 0, scale: 1, homeX: 0, homeY: 0, homeScale: 1};
      let dragState = null;

      function clientToViewBox(clientX, clientY) {
        const screenMatrix = svg.getScreenCTM?.();
        if (screenMatrix) {
          const point = svg.createSVGPoint();
          point.x = clientX;
          point.y = clientY;
          const transformed = point.matrixTransform(screenMatrix.inverse());
          return {x: transformed.x, y: transformed.y};
        }
        const rect = svg.getBoundingClientRect();
        const width = rect.width || 1;
        const height = rect.height || 1;
        return {
          x: viewBox.x + ((clientX - rect.left) / width) * viewBox.width,
          y: viewBox.y + ((clientY - rect.top) / height) * viewBox.height,
        };
      }

      function clampScale(scale) {
        return Math.min(Math.max(scale, transform.homeScale * 0.45), transform.homeScale * 3.2);
      }

      function setTransform(x, y, scale) {
        transform.x = x;
        transform.y = y;
        transform.scale = clampScale(scale);
        viewport.setAttribute(
          "transform",
          `translate(${transform.x.toFixed(2)} ${transform.y.toFixed(2)}) scale(${transform.scale.toFixed(4)})`
        );
        if (zoomStatus) {
          zoomStatus.textContent = `${Math.round((transform.scale / transform.homeScale) * 100)}%`;
        }
      }

      function fitView() {
        let box;
        try {
          box = viewport.getBBox();
        } catch {
          box = {x: 0, y: 0, width: viewBox.width, height: viewBox.height};
        }
        const margin = Math.max(72, Math.min(viewBox.width, viewBox.height) * 0.06);
        const usableWidth = Math.max(1, viewBox.width - margin * 2);
        const usableHeight = Math.max(1, viewBox.height - margin * 2);
        const scale = clampScale(Math.min(usableWidth / Math.max(1, box.width), usableHeight / Math.max(1, box.height), 1.08) * 0.98);
        const x = viewBox.x + (viewBox.width - box.width * scale) / 2 - box.x * scale;
        const y = viewBox.y + (viewBox.height - box.height * scale) / 2 - box.y * scale;
        transform.homeX = x;
        transform.homeY = y;
        transform.homeScale = scale || 1;
        setTransform(x, y, transform.homeScale);
      }

      function zoomAt(factor, point) {
        const nextScale = clampScale(transform.scale * factor);
        const anchorX = (point.x - transform.x) / transform.scale;
        const anchorY = (point.y - transform.y) / transform.scale;
        setTransform(point.x - anchorX * nextScale, point.y - anchorY * nextScale, nextScale);
      }

      function zoomFromCenter(factor) {
        zoomAt(factor, {x: viewBox.x + viewBox.width / 2, y: viewBox.y + viewBox.height / 2});
      }

      zoomInButton?.addEventListener("click", () => zoomFromCenter(1.18));
      zoomOutButton?.addEventListener("click", () => zoomFromCenter(1 / 1.18));
      zoomResetButton?.addEventListener("click", fitView);
      svg.addEventListener("wheel", event => {
        event.preventDefault();
        const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
        zoomAt(factor, clientToViewBox(event.clientX, event.clientY));
      }, {passive: false});

      svg.addEventListener("pointerdown", event => {
        if (event.button !== 0 || event.pointerType === "mouse" && event.buttons !== 1) return;
        const start = clientToViewBox(event.clientX, event.clientY);
        dragState = {
          pointerId: event.pointerId,
          startClientX: event.clientX,
          startClientY: event.clientY,
          startViewX: start.x,
          startViewY: start.y,
          x: transform.x,
          y: transform.y,
          moved: false,
        };
      });

      svg.addEventListener("pointermove", event => {
        if (!dragState || dragState.pointerId !== event.pointerId) return;
        const current = clientToViewBox(event.clientX, event.clientY);
        const screenMove = Math.hypot(event.clientX - dragState.startClientX, event.clientY - dragState.startClientY);
        if (screenMove > 3) {
          if (!dragState.moved) {
            dragState.moved = true;
            svg.setPointerCapture?.(event.pointerId);
            map?.classList.add("is-panning");
          }
          stage.dataset.graphDragging = "true";
          setTransform(
            dragState.x + current.x - dragState.startViewX,
            dragState.y + current.y - dragState.startViewY,
            transform.scale
          );
        }
      });

      function finishDrag(event) {
        if (!dragState || dragState.pointerId !== event.pointerId) return;
        const pointerId = dragState.pointerId;
        const moved = dragState.moved;
        dragState = null;
        map?.classList.remove("is-panning");
        stage.dataset.graphDragging = "false";
        if (svg.hasPointerCapture?.(pointerId)) {
          svg.releasePointerCapture(pointerId);
        }
        if (moved) {
          stage.dataset.graphSuppressClick = "true";
          window.setTimeout(() => { stage.dataset.graphSuppressClick = "false"; }, 0);
        }
      }

      svg.addEventListener("pointerup", finishDrag);
      svg.addEventListener("pointercancel", finishDrag);
      svg.addEventListener("lostpointercapture", finishDrag);
      svg.addEventListener("pointerleave", event => {
        if (dragState && !dragState.moved) finishDrag(event);
      });
      fitView();
      window.addEventListener("resize", () => fitView(), {passive: true});
    }

    function isPinned() {
      return stage.dataset.graphPinned === "true";
    }

    function setInspector(title, lines, pinned) {
      inspector.replaceChildren();
      const strong = document.createElement("strong");
      strong.textContent = title;
      inspector.append(strong);
      for (const line of lines) {
        const p = document.createElement("p");
        p.textContent = line;
        inspector.append(p);
      }
      inspector.classList.toggle("is-pinned", Boolean(pinned));
    }

    function edgeTouchesRoute(edge, route) {
      return edge.dataset.source === route || edge.dataset.destination === route;
    }

    function edgeConnects(edge, routeA, routeB) {
      return (edge.dataset.source === routeA && edge.dataset.destination === routeB) ||
        (edge.dataset.source === routeB && edge.dataset.destination === routeA);
    }

    function clearGraph() {
      for (const node of nodes) node.classList.remove("is-active", "is-related", "is-dimmed");
      for (const edge of edges) edge.classList.remove("is-active", "is-related", "is-dimmed");
      stage.dataset.graphPinned = "false";
      setInspector("No graph selection", [
        "Focus or hover a node or edge to inspect it. Click to pin a selection; Escape or empty graph space clears it."
      ], false);
    }

    function focusNode(node, pinned) {
      const route = node.dataset.graphNodeRoute || "";
      const name = node.dataset.graphNodeName || route || "Page";
      for (const candidate of nodes) {
        const candidateRoute = candidate.dataset.graphNodeRoute || "";
        const active = candidate === node;
        const related = !active && edges.some(edge => edgeConnects(edge, route, candidateRoute));
        candidate.classList.toggle("is-active", active);
        candidate.classList.toggle("is-related", related);
        candidate.classList.toggle("is-dimmed", !active && !related);
      }
      for (const edge of edges) {
        const related = edgeTouchesRoute(edge, route);
        edge.classList.toggle("is-active", false);
        edge.classList.toggle("is-related", related);
        edge.classList.toggle("is-dimmed", !related);
      }
      stage.dataset.graphPinned = pinned ? "true" : "false";
      setInspector(name, [
        `Route: ${route || "unknown"}`,
        `Goal distance: ${node.dataset.goalDistance || "unknown"}`,
        `Authority: ${node.dataset.authority || "unknown"}`,
        `${pinned ? "Pinned" : "Focused"} page; connected edges and neighbor pages are highlighted.`
      ], pinned);
    }

    function focusEdge(edge, pinned) {
      const source = edge.dataset.source || "";
      const destination = edge.dataset.destination || "";
      const sourceName = edge.dataset.sourceName || source;
      const destinationName = edge.dataset.destinationName || destination;
      for (const node of nodes) {
        const route = node.dataset.graphNodeRoute || "";
        const related = route === source || route === destination;
        node.classList.toggle("is-active", false);
        node.classList.toggle("is-related", related);
        node.classList.toggle("is-dimmed", !related);
      }
      for (const candidate of edges) {
        const active = candidate === edge;
        const related = !active && (
          edgeTouchesRoute(candidate, source) ||
          edgeTouchesRoute(candidate, destination)
        );
        candidate.classList.toggle("is-active", active);
        candidate.classList.toggle("is-related", related);
        candidate.classList.toggle("is-dimmed", !active && !related);
      }
      stage.dataset.graphPinned = pinned ? "true" : "false";
      setInspector(`${sourceName} -> ${destinationName}`, [
        `Routes: ${source} -> ${destination}`,
        `Layer: ${edge.dataset.layer || "unknown"}`,
        `Occurrences: ${edge.dataset.occurrences || "1"}`,
        `Anchor sample: ${edge.dataset.anchor || "unlabeled"}`,
        `${pinned ? "Pinned" : "Focused"} edge; endpoints and adjacent edges are highlighted.`
      ], pinned);
    }

    function bindInteractiveElement(element, focusFn) {
      element.addEventListener("pointerenter", () => { if (!isPinned()) focusFn(element, false); });
      element.addEventListener("focus", () => { if (!isPinned()) focusFn(element, false); });
      element.addEventListener("click", event => {
        if (stage.dataset.graphSuppressClick === "true" || stage.dataset.graphDragging === "true") {
          event.preventDefault();
          event.stopPropagation();
          return;
        }
        event.preventDefault();
        event.stopPropagation();
        focusFn(element, true);
      });
      element.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          focusFn(element, true);
        } else if (event.key === "Escape") {
          event.preventDefault();
          clearGraph();
        }
      });
    }

    for (const node of nodes) bindInteractiveElement(node, focusNode);
    for (const edge of edges) bindInteractiveElement(edge, focusEdge);
    stage.addEventListener("pointerleave", () => { if (!isPinned()) clearGraph(); });
    stage.addEventListener("click", event => {
      if (stage.dataset.graphSuppressClick === "true") return;
      if (event.target === stage || event.target === svg || event.target === viewport || event.target.classList?.contains("graph-depth-plane")) clearGraph();
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && (isPinned() || stage.contains(document.activeElement))) clearGraph();
    });
    initCanvasView();
    clearGraph();
  }

  const source = document.querySelector('select[name="source"]');
  if (source) {
    updateMetricOptions();
    updateSiteOptions();
    source.addEventListener("change", () => { updateMetricOptions(); updateSiteOptions(); });
  }
  const metric = document.querySelector('select[name="metric"]');
  if (!source && metric) {
    updateSiteOptions();
    metric.addEventListener("change", updateSiteOptions);
  }
  loadChart();
  loadGeography();
  initSiteGraph();
})();
"""


def _window(query, timezone, default_days, default_end_lag_days=0):
    return report_window(
        timezone=timezone,
        default_days=default_days,
        default_end_lag_days=default_end_lag_days,
        start=query.get("start", [None])[0],
        end=query.get("end", [None])[0],
    )


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _compare_flag(query) -> bool:
    value = query.get("compare", [""])[0].casefold()
    if value in {"", "0", "false", "no"}:
        return False
    if value in {"1", "true", "yes"}:
        return True
    raise ValueError("invalid comparison flag")


def _metric_label(metric: str) -> str:
    return METRIC_LABELS.get(metric, metric.replace(".", " ").replace("-", " ").title())


def _source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source.replace("-", " ").title())


def _format_value(value: int | float | None, unit: str = "count") -> str:
    if value is None:
        return "—"
    number = float(value)
    if unit == "ratio":
        return f"{number * 100:.1f}%"
    if unit == "position":
        return f"{number:.1f}"
    if unit == "seconds":
        minutes = number / 60
        return f"{minutes:,.1f} min"
    if unit == "bytes":
        for suffix in ("B", "KB", "MB", "GB", "TB"):
            if abs(number) < 1024 or suffix == "TB":
                return f"{number:,.1f} {suffix}" if suffix != "B" else f"{number:,.0f} B"
            number /= 1024
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def _metric_total(result, metric):
    summary = result.get("summary_totals", {}).get(metric)
    if summary is None:
        summary = (
            result.get("decision_support", {}) or {}
        ).get("supporting_metrics", {}).get(metric)
    if summary is not None:
        return {
            "value": summary["value"],
            "previous": summary.get("previous_value"),
            "change": summary.get("change_percent"),
            "unit": summary["unit"],
            "metric": metric,
            "source": summary.get("source"),
            "coverage_status": summary.get("coverage_status", "unknown"),
            "covered_cells": summary.get("covered_cells", 0),
            "expected_cells": summary.get("expected_cells", 0),
            "observed": summary.get("observed", False),
        }
    rows = [row for row in result["rows"] if row["metric"] == metric]
    if not rows:
        return None
    if metric in METRICS and METRICS[metric].aggregation in {"weighted", "latest"}:
        return None
    value = sum(float(row["value"]) for row in rows)
    prior_values = [float(row["previous_value"]) for row in rows if row["previous_value"] is not None]
    previous = sum(prior_values) if prior_values else None
    change = None if previous in {None, 0} else round((value - previous) / previous * 100, 1)
    return {
        "value": value, "previous": previous, "change": change,
        "unit": rows[0]["unit"], "metric": metric, "source": rows[0]["source"],
        "coverage_status": "legacy",
        "covered_cells": 0,
        "expected_cells": 0,
        "observed": True,
    }


def _trend(change, *, lower_is_better=False, neutral=False):
    if change is None:
        return '<span class="trend flat">No prior</span>'
    favorable = change < 0 if lower_is_better else change > 0
    state = "flat" if neutral or change == 0 else "up" if favorable else "down"
    prefix = "+" if change > 0 else ""
    return f'<span class="trend {state}">{prefix}{change:.1f}%</span>'


def _summary_cards(result, expected_metrics):
    expected = set(expected_metrics)
    if expected and all(metric.startswith("forms.") for metric in expected):
        definitions = FORMS_SUMMARY
    elif expected and all(metric.startswith("search.") for metric in expected):
        definitions = SEARCH_SUMMARY
    elif result["subreport_id"]:
        definitions = TRAFFIC_SUMMARY
    else:
        definitions = PORTFOLIO_SUMMARY
    cards = []
    pipeline_values = {
        "forms.submissions": "submissions",
        "forms.inbox-deliveries": "inbox_deliveries",
        "forms.pending": "pending",
        "forms.failed": "failed",
    }
    for label, candidates, note in definitions:
        total = None
        selected_metric = candidates[0]
        for metric in candidates:
            total = _metric_total(result, metric)
            if total:
                selected_metric = metric
                break
            pipeline_key = pipeline_values.get(metric)
            if pipeline_key and result["forms_pipeline"] is not None:
                total = {
                    "value": result["forms_pipeline"].get(pipeline_key),
                    "previous": None,
                    "change": None,
                    "unit": "count",
                    "metric": metric,
                    "source": None,
                    "coverage_status": "unknown",
                    "covered_cells": 0,
                    "expected_cells": 0,
                    "observed": False,
                }
                selected_metric = metric
                break
        observed = total is not None and total["value"] is not None
        partial = bool(total and total.get("coverage_status") == "partial")
        source_conflict = bool(total and total.get("source") == "mixed")
        withheld = bool(
            total and total["value"] is None and (partial or source_conflict)
            and (
                source_conflict
                or total.get("observed", False)
                or any(row["metric"] == selected_metric for row in result["rows"])
            )
        )
        if observed:
            value = _format_value(total["value"], total["unit"])
            source_row = next((row for row in result["rows"] if row["metric"] == total["metric"]), None)
            source_id = total.get("source") or (source_row["source"] if source_row else None)
            source = _source_label(source_id) if source_id else "Current window"
            badge = (
                '<span class="trend partial">Partial data</span>'
                if partial
                else _trend(
                    total["change"],
                    lower_is_better=total["metric"] in {"search.position", "forms.pending", "forms.failed"},
                )
            )
            coverage_note = (
                f'; observed {total.get("covered_cells", 0)} of {total.get("expected_cells", 0)} configured cells'
                if partial else ""
            )
            detail = f"{note} - {source}{coverage_note}"
        elif withheld:
            value = "Withheld"
            badge = (
                '<span class="trend partial">Source conflict</span>'
                if source_conflict
                else '<span class="trend partial">Partial data</span>'
            )
            detail = (
                f'{note} - aggregate withheld because multiple actual provider sources are present'
                if source_conflict
                else f'{note} - aggregate withheld; observed '
                f'{total.get("covered_cells", 0)} of {total.get("expected_cells", 0)} configured cells'
            )
        else:
            value = "Unknown"
            badge = '<span class="trend flat">Not observed</span>'
            detail = f"{note} - no stored observation"
        state = "withheld" if withheld else "partial" if partial else "observed" if observed else "unknown"
        cards.append(
            f'<article class="kpi-card" data-metric="{_e(selected_metric)}" data-state="{state}"><div class="kpi-top"><span class="kpi-label">{_e(label)}</span>{badge}</div>'
            f'<strong class="kpi-value">{_e(value)}</strong><p class="kpi-note">{_e(detail)}</p></article>'
        )
    return '<section class="kpi-grid" aria-label="Portfolio summary">' + "".join(cards) + "</section>"


def _coverage_summary_html(result):
    coverage = result.get("coverage") or {}
    expected = int(coverage.get("expected_cells") or 0)
    covered = int(coverage.get("covered_cells") or 0)
    percent = round(covered / expected * 100) if expected else (100 if result.get("complete") else 0)
    percent = max(0, min(100, percent))
    state = "complete" if result.get("complete") else "partial"
    label = "Trusted window" if state == "complete" else "Coverage to review"
    return (
        f'<aside class="trust-card" data-state="{state}" aria-label="{_e(label)}: {percent}%">'
        '<span class="trust-label">Data confidence</span>'
        f'<span class="trust-value"><strong>{percent}%</strong><span>{_e(label)}</span></span>'
        f'<span class="coverage-track" aria-hidden="true"><span class="coverage-fill p-{percent}"></span></span>'
        '</aside>'
    )


def _decision_badge(item):
    state = item.get("state", "unavailable")
    if state == "observed":
        direction = {
            "durable_leads": "higher",
            "notification_sent_rate": "higher",
            "report_coverage": "higher",
            "umami_bounce_rate": "lower",
        }.get(item.get("id"))
        if item.get("change_state") == "new":
            css_class = (
                "up" if direction == "higher"
                else "down" if direction == "lower"
                else "flat"
            )
            return f'<span class="trend {css_class}">New</span>'
        return _trend(
            item.get("change_percent"),
            lower_is_better=direction == "lower",
            neutral=direction is None,
        )
    labels = {
        "complete": ("up", "Complete"),
        "partial": ("partial", "Partial"),
        "withheld": ("partial", "Withheld"),
        "not_measured": ("flat", "Not measured"),
        "not_configured": ("flat", "Not configured"),
        "unavailable": ("flat", "Unavailable"),
    }
    css_class, label = labels.get(state, ("flat", state.replace("_", " ").title()))
    return f'<span class="trend {_e(css_class)}">{_e(label)}</span>'


def _decision_card(item, *, card_class, site_names):
    state = str(item.get("state", "unavailable"))
    value = item.get("value")
    value_label = (
        _format_value(value, str(item.get("unit", "count")))
        if value is not None
        else "Withheld" if state == "withheld"
        else "Not measured" if state in {"not_measured", "not_configured"}
        else "Unavailable"
    )
    source = str(item.get("source") or "Current report")
    scope = [
        site_names.get(site_id, site_id)
        for site_id in item.get("scope_site_ids", [])
    ]
    scope_html = (
        f'<span class="decision-scope">Scope: {_e(", ".join(scope))}</span>'
        if scope else ""
    )
    return (
        f'<article class="{_e(card_class)}" data-state="{_e(state)}">'
        f'<div class="decision-meta"><h3>{_e(item["label"])}</h3>{_decision_badge(item)}</div>'
        f'<strong class="decision-value">{_e(value_label)}</strong>'
        f'<p class="decision-note">{_e(item["note"])} <b>{_e(source)}</b></p>'
        f'{scope_html}</article>'
    )


def _decision_summary_html(support, site_names):
    cards = "".join(
        _decision_card(item, card_class="decision-card", site_names=site_names)
        for item in support.get("outcomes", [])
    )
    return (
        '<section class="panel decision-panel" aria-labelledby="decision-summary-title">'
        '<div class="panel-heading"><div><h2 id="decision-summary-title">Decision summary</h2>'
        '<p>Four outcome and trust signals first; provider inventory stays supporting evidence.</p>'
        f'</div></div><div class="decision-grid">{cards}</div></section>'
    )


def _attention_html(support):
    rows = []
    for index, item in enumerate(support.get("attention_items", []), start=1):
        severity = str(item.get("severity", "review"))
        rank = "OK" if severity == "clear" else str(index)
        severity_label = {
            "immediate": "Immediate",
            "review": "Review",
            "clear": "Clear",
        }.get(severity, severity.replace("_", " ").title())
        rows.append(
            f'<li class="attention-item" data-severity="{_e(severity)}">'
            f'<span class="attention-rank">{_e(rank)}</span><div class="attention-copy">'
            f'<p class="attention-severity">{_e(severity_label)}</p>'
            f'<h3>{_e(item["title"])}</h3><p>{_e(item["evidence"])}</p>'
            f'<p><strong>Next action:</strong> {_e(item["action"])}</p></div></li>'
        )
    return (
        '<section class="panel attention-panel" aria-labelledby="attention-title">'
        '<div class="panel-heading"><div><h2 id="attention-title">What needs attention</h2>'
        '<p>Deterministic evidence and a next action; ordinary movement is not called an anomaly.</p>'
        f'</div></div><ol class="attention-list">{"".join(rows)}</ol></section>'
    )


def _engagement_html(support, site_names):
    cards = "".join(
        _decision_card(item, card_class="engagement-card", site_names=site_names)
        for item in support.get("engagement", [])
    )
    return (
        '<section class="panel decision-panel" aria-labelledby="engagement-title">'
        '<div class="panel-heading"><div><h2 id="engagement-title">Engagement and lead health</h2>'
        '<p>Interpretable ratios with visible provider definitions and complete-input guards.</p>'
        f'</div></div><div class="engagement-grid">{cards}</div></section>'
    )


def _pulse_cell(cell, unit="count", *, expected_source=None):
    state = str(cell.get("state", "unavailable"))
    value = cell.get("value")
    labels = {
        "withheld": "Withheld",
        "not_configured": "Not configured",
        "unavailable": "Unknown",
        "not_measured": "Not measured",
    }
    label = _format_value(value, unit) if value is not None else labels.get(state, "Unknown")
    source = cell.get("source")
    source_html = (
        f'<small class="pulse-source">{_e(_source_label(source))}</small>'
        if source and source != expected_source else ""
    )
    return f'<td data-state="{_e(state)}">{_e(label)}{source_html}</td>'


def _site_pulse_html(support, site_names):
    rows = []
    for item in support.get("site_pulse", []):
        metrics = item["metrics"]
        coverage = item["coverage"]
        coverage_label = (
            f'{coverage["covered_cells"]}/{coverage["expected_cells"]} '
            f'{coverage["status"]}'
        )
        rows.append(
            f'<tr><th scope="row" class="metric-name">{_e(site_names.get(item["site_id"], item["site_id"]))}</th>'
            + _pulse_cell(metrics["umami.visits"], expected_source="umami")
            + _pulse_cell(metrics["google.sessions"], expected_source="google-analytics")
            + _pulse_cell(metrics["search.clicks"], expected_source="search-console")
            + _pulse_cell(metrics["forms.submissions"], expected_source="cloudflare-forms")
            + f'<td data-state="{_e(coverage["status"])}">{_e(coverage_label)}</td></tr>'
        )
    return (
        '<section class="panel table-panel" aria-labelledby="site-pulse-title">'
        '<div class="panel-heading"><div><h2 id="site-pulse-title">Site pulse</h2>'
        '<p>Separate source facts by site; unavailable never becomes zero.</p></div></div>'
        '<div class="table-scroll"><table class="pulse-table"><caption class="sr-only">Per-site decision metrics and data coverage</caption>'
        '<thead><tr><th scope="col">Site</th>'
        '<th scope="col">Umami visits</th><th scope="col">GA sessions</th><th scope="col">Search clicks</th><th scope="col">Durable leads</th><th scope="col">Decision coverage</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>'
    )


def _operations_html(support, site_names):
    operation_cards = []
    for item in support.get("operations_health", []):
        status = str(item.get("status") or "unknown")
        result = item.get("result_kind") or "no result"
        points = int(item.get("points_written") or 0)
        through = item.get("data_through") or "not reported"
        finished = (
            item.get("finished_at") or
            ("not recorded" if status == "never_run" else "still running")
        )
        error = (
            f'; category {item["error_category"]}'
            if item.get("error_category") else ""
        )
        operation_cards.append(
            f'<article class="operation-card" data-state="{_e(status)}">'
            f'<b>{_e(site_names.get(item["site_id"], item["site_id"]))} - {_e(_source_label(item["source"]))}</b>'
            f'<span>{_e(status)} / {_e(result)} / {_e(points)} points{_e(error)}</span>'
            f'<span>Data through {_e(through)}</span><span>Finished {_e(finished)}</span></article>'
        )
    if not operation_cards:
        operation_cards.append(
            '<div class="empty-state">No current binding has a recorded sync attempt yet.</div>'
        )
    capability_chips = []
    for item in support.get("capabilities", []):
        state = str(item.get("state") or "not_recorded")
        if state == "not_recorded":
            capability_chips.append(
                f'<span class="capability-chip" data-state="not_recorded">'
                f'{_e(_source_label(item["provider"]))} - no capability snapshot</span>'
            )
            continue
        lookback = (
            f'{item["max_lookback_days"]}d verified lookback'
            if item.get("max_lookback_days") is not None else "lookback not reported"
        )
        warning_count = int(item.get("warning_count") or 0)
        probed_at = item.get("probed_at") or "time not reported"
        capability_chips.append(
            f'<span class="capability-chip" data-state="recorded">{_e(_source_label(item["provider"]))} - '
            f'snapshot {_e(probed_at)}; {_e(lookback)}'
            f'{f"; {warning_count} recorded warning(s)" if warning_count else ""}</span>'
        )
    return (
        '<details class="panel decision-panel evidence-panel" aria-labelledby="operations-title">'
        '<summary class="panel-heading"><div><h2 id="operations-title">Data operations</h2>'
        '<p>Sync status and capability evidence for every current binding.</p>'
        f'</div></summary><div class="evidence-body"><div class="operations-grid">{"".join(operation_cards)}</div>'
        f'<div class="capability-strip">{"".join(capability_chips)}</div></div></details>'
    )


def _roadmap_html(support):
    cards = []
    for item in support.get("measurement_gaps", []):
        cards.append(
            f'<article class="roadmap-card"><h3>{_e(item["label"])}</h3>'
            f'<p>{_e(item["question"])}</p><p><strong>Needs:</strong> {_e(item["requires"])}</p></article>'
        )
    return (
        '<details class="panel decision-panel evidence-panel" aria-labelledby="roadmap-title">'
        '<summary class="panel-heading"><div><h2 id="roadmap-title">Measurement roadmap</h2>'
        '<p>High-value questions the current facts cannot answer yet.</p>'
        f'</div></summary><div class="evidence-body"><div class="roadmap-grid">{"".join(cards)}</div></div></details>'
    )


def _decision_overview_html(support):
    if not support:
        return ""
    return _attention_html(support)


def _decision_support_html(support, site_names):
    if not support:
        return ""
    return (
        _decision_summary_html(support, site_names)
        + _engagement_html(support, site_names)
        + _site_pulse_html(support, site_names)
        + _operations_html(support, site_names)
        + _roadmap_html(support)
    )


def _chart_html(result, metric, site_names):
    matching = [series for series in result["series"] if series["metric"] == metric]
    if not matching:
        return '<div class="empty-state">No daily series is available for this metric and window.</div>'
    cards = []
    for index, series in enumerate(matching):
        points = series["points"]
        maximum = max((abs(float(point["value"])) for point in points), default=0) or 1
        density = "density-wide" if len(points) > 60 else "density-mid" if len(points) > 35 else ""
        bars = []
        data_rows = []
        for point in points:
            value = float(point["value"])
            level = 0 if value == 0 else max(1, min(50, round(abs(value) / maximum * 50)))
            readable = _format_value(point["value"], series["unit"])
            title = f'{point["date"]}: {readable}'
            bars.append(
                f'<span class="bar-slot"><span aria-hidden="true" class="bar tone-{index % 4} h-{level}" title="{_e(title)}"></span></span>'
            )
            data_rows.append(f'<tr><td>{_e(point["date"])}</td><td>{_e(readable)}</td></tr>')
        aggregate_row = next(
            (
                row for row in result.get("rows", [])
                if row["metric"] == metric
                and row["site_id"] == series["site_id"]
                and row["source"] == series["source"]
            ),
            None,
        )
        aggregate = (
            aggregate_row["value"]
            if aggregate_row is not None
            else sum(float(point["value"]) for point in points)
        )
        aggregation = METRICS[metric].aggregation if metric in METRICS else "sum"
        coverage_status = (
            aggregate_row.get("coverage_status", "unknown")
            if aggregate_row is not None else "unknown"
        )
        aggregate_label = (
            "window aggregate"
            if aggregation in {"weighted", "latest"}
            else "observed total (partial)"
            if coverage_status == "partial"
            else "window total"
        )
        first = points[0]["date"] if points else ""
        last = points[-1]["date"] if points else ""
        cards.append(
            f'<article class="chart-card" data-chart="{_e(metric)}"><div class="chart-card-head">'
            f'<h3>{_e(site_names.get(series["site_id"], series["site_id"]))}</h3>'
            f'<span class="chart-total">{_e(_format_value(aggregate, series["unit"]))} {_e(aggregate_label)}</span></div>'
            f'<div class="chart-scroll"><div class="bar-grid {density}" role="img" aria-label="{_e(_metric_label(metric))} by day for {site_names.get(series["site_id"], series["site_id"])}">'
            + "".join(bars)
            + f'</div><div class="axis-labels"><span>{_e(first)}</span><span>{_e(last)}</span></div></div>'
            f'<details class="chart-data"><summary>View daily values</summary><table><thead><tr><th>Date</th><th>Value</th></tr></thead><tbody>{"".join(data_rows)}</tbody></table></details></article>'
        )
    return '<div class="chart-grid">' + "".join(cards) + "</div>"


def _forms_html(pipeline):
    if not pipeline:
        return ""
    labels = {
        "submissions": "Stored submissions",
        "inbox_deliveries": "Inbox deliveries",
        "delivery_gap": "Delivery gap",
        "pending": "Pending",
        "failed": "Failed",
    }
    items = "".join(
        f'<div class="pipeline-item" data-state="{"observed" if value is not None else "unknown"}"><b>{_e(labels[key])}</b>'
        f'<span class="pipeline-value">{_e(_format_value(value))}</span>'
        f'<span>{"Current window" if key != "delivery_gap" else "Stored minus delivered"}</span></div>'
        for key, value in pipeline.items() if key in labels
    )
    gap = pipeline.get("delivery_gap")
    comparable = bool(pipeline.get("delivery_comparable"))
    note = (
        "Storage and inbox evidence agree for the complete selected scope." if comparable and gap == 0
        else "Counts differ across complete comparable coverage; inspect notification state." if comparable and gap is not None
        else "Coverage is incomplete or differs between storage and inbox evidence, so no delivery gap is asserted."
    )
    return f'<section class="panel section-panel"><div class="panel-heading"><div><h2>Forms delivery</h2><p>Independent storage and mailbox evidence.</p></div></div><div class="pipeline-grid">{items}</div><p class="pipeline-note">{_e(note)}</p></section>'


def _health_html(result, expected_metrics):
    report_coverage = result.get("coverage")
    if report_coverage:
        coverage = (
            f"{report_coverage['covered_cells']} of {report_coverage['expected_cells']} expected cells; "
            f"status {report_coverage['status']}"
        )
    else:
        present = {row["metric"] for row in result["rows"]}
        coverage = f"{len(present)} of {len(expected_metrics)} metrics"
    freshness = []
    for item in result.get("source_health", []):
        through = item.get("data_through") or "no data"
        ingested = item.get("ingested_at") or "never"
        label = f"{item['site_id']} - {_source_label(item['source'])}"
        semantics = f"{item['time_basis']}; {item['sampling']}; {item['data_state']}"
        freshness.append(
            f'<div class="health-item" data-state="{_e(item["status"])}"><b>{_e(label)}</b>'
            f'<span>Data through {_e(through)}; ingested {_e(ingested)}</span><span>{_e(semantics)}</span></div>'
        )
    if not freshness:
        for source, observed in result.get("freshness", {}).items():
            timestamp = datetime.fromisoformat(observed).astimezone(UTC).strftime("%b %d, %H:%M UTC")
            freshness.append(
                f'<div class="health-item"><b>{_e(_source_label(source))}</b><span>Ingested {_e(timestamp)}; data date unavailable</span></div>'
            )
    if not freshness:
        freshness.append('<div class="health-item"><b>No source data</b><span>Try a wider date window.</span></div>')
    return (
        '<section class="panel section-panel"><div class="panel-heading"><div><h2>Data health</h2>'
        f'<p>{_e(coverage)} represented in this view.</p></div></div><div class="health-grid">'
        + "".join(freshness)
        + "</div></section>"
    )


def _warnings_html(warnings):
    if not warnings:
        return ""
    count = len(warnings)
    return f'<details class="data-notices"><summary>{count} data note{"s" if count != 1 else ""} for this window</summary><section class="alerts" aria-label="Report warnings">' + "".join(
        f'<div class="alert"><span class="alert-mark">!</span><span>{_e(item)}</span></div>' for item in warnings
    ) + "</section></details>"


def _metrics_table(result, site_names):
    rows = []
    for row in result["rows"]:
        change = row["change_percent"]
        lower_is_better = row["metric"] in {"search.position", "forms.pending", "forms.failed"}
        favorable = change is not None and (change < 0 if lower_is_better else change > 0)
        change_class = "muted" if change in {None, 0} else "positive" if favorable else "negative"
        change_text = "—" if change is None else f'{"+" if change > 0 else ""}{change}%'
        rows.append(
            f'<tr><td class="metric-name">{_e(_metric_label(row["metric"]))}</td>'
            f'<td>{_e(site_names.get(row["site_id"], row["site_id"]))}</td>'
            f'<td><span class="source-chip">{_e(_source_label(row["source"]))}</span></td>'
            f'<td>{_e(_format_value(row["value"], row["unit"]))}</td>'
            f'<td>{_e(_format_value(row["previous_value"], row["unit"]))}</td>'
            f'<td>{_e(row.get("coverage_status", "unknown").replace("_", " ").title())}</td>'
            f'<td class="{change_class}">{_e(change_text)}</td></tr>'
        )
    if not rows:
        rows.append('<tr><td colspan="7">No data in this window.</td></tr>')
    return "".join(rows)


def _provider_comparisons_html(result, site_names):
    comparisons = result.get("provider_comparisons", [])
    if not comparisons:
        return ""

    rows = []
    for comparison in comparisons:
        google = comparison["providers"]["google-analytics"]
        umami = comparison["providers"]["umami"]
        paired = comparison["paired_dates"]["count"]
        paired_label = (
            f"{paired} paired date" if paired == 1
            else f"{paired} paired dates" if paired
            else "No paired dates"
        )
        state = comparison["evidence_state"].replace("_", " ").capitalize()
        totals = comparison["totals"]
        if totals["google_pageviews"] is None:
            totals_label = "Withheld"
        else:
            ratio = totals["google_to_umami_ratio"]
            ratio_label = "undefined" if ratio is None else f"{ratio:g}×"
            totals_label = (
                f"GA4 {totals['google_pageviews']:,}; "
                f"Umami {totals['umami_pageviews']:,}; "
                f"absolute difference {totals['absolute_difference']:,}; "
                f"GA4-to-Umami ratio {ratio_label}"
            )
        low_volume = (
            '<span class="source-chip">Low volume</span>'
            if comparison["low_volume_warning"] else ""
        )

        def range_span(summary):
            return "; ".join(
                f"{item['start']} to {item['end']}"
                for item in summary.get("ranges", [])
            ) or "none"

        def date_span(summary):
            if not summary["count"]:
                return "none"
            return f"{summary['count']} ({range_span(summary)})"

        def provider_dates(provider):
            complete = provider["complete_dates"]
            return (
                f"{date_span(complete)} complete dates; "
                f"first available {provider['first_available_date'] or 'unknown'}; "
                f"data through {provider['data_through'] or 'unknown'}"
            )

        def route_status(provider):
            reconciliation = provider["route_reconciliation"]
            label = reconciliation["status"].replace("_", " ").capitalize()
            if reconciliation["reason"]:
                label += f" ({reconciliation['reason'].replace('_', ' ')})"
            return label

        rows.append(
            "<tr>"
            f'<th scope="row" class="metric-name">{_e(site_names.get(comparison["site_id"], comparison["site_id"]))}</th>'
            f"<td><b>{_e(state)}</b>{low_volume}<br>"
            f"GA4-only {date_span(comparison['google_only_dates'])}; "
            f"Umami-only {date_span(comparison['umami_only_dates'])}</td>"
            f"<td>{_e(provider_dates(google))}</td>"
            f"<td>{_e(provider_dates(umami))}</td>"
            f"<td>{_e(paired_label)}<br>"
            f"{_e(range_span(comparison['paired_dates']))}</td>"
            f"<td>{_e(totals_label)}</td>"
            f"<td>GA4: {_e(route_status(google))}<br>"
            f"Umami: {_e(route_status(umami))}</td>"
            "</tr>"
        )

    semantics = next(
        (
            comparison.get("semantics", [])
            for comparison in comparisons
            if comparison.get("semantics")
        ),
        [],
    )
    provider_semantics = []
    first_providers = comparisons[0]["providers"]
    for label, key in (
        ("GA4", "google-analytics"),
        ("Umami", "umami"),
    ):
        semantics_record = first_providers[key]["semantics"]
        provider_semantics.append(
            f"{label}: {semantics_record['pageview_definition']} "
            f"{semantics_record['time_basis']}; {semantics_record['sampling']}; "
            f"{semantics_record['data_state']}."
        )
    limits = next(
        (
            comparison.get("coverage_limits", [])
            for comparison in comparisons
            if comparison.get("coverage_limits")
        ),
        [],
    )
    disclosure_items = "".join(
        f"<li>{_e(item)}</li>" for item in (*semantics, *provider_semantics, *limits)
    )
    return (
        '<section class="panel table-panel" '
        'aria-label="GA4 and Umami pageview comparability">'
        '<div class="panel-heading"><div><h2>Provider pageview comparison</h2>'
        '<p>Mature complete overlapping dates only. Providers remain separate '
        'and neither is declared correct.</p></div></div>'
        '<div class="table-scroll"><table>'
        '<caption class="sr-only">GA4 and Umami pageview comparison by site</caption>'
        '<thead><tr><th scope="col">Site</th><th scope="col">Evidence</th>'
        '<th scope="col">GA4 coverage</th><th scope="col">Umami coverage</th>'
        '<th scope="col">Overlap</th><th scope="col">Paired totals</th>'
        '<th scope="col">Route reconciliation</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
        + (
            "<details><summary>Semantics and coverage limits</summary><ul>"
            + disclosure_items + "</ul></details>"
            if disclosure_items else ""
        )
        + "</section>"
    )


def _available_sites_by_source(config, report) -> dict[str, set[str]]:
    connection_sources = {item.id: item.provider for item in config.connections}
    report_sources = {METRICS[metric].source for metric in report.metric_ids}
    available = {source: set() for source in report_sources}
    for binding in config.bindings:
        if binding.site_id not in report.site_ids:
            continue
        provider = connection_sources[binding.connection_id]
        if provider == "fixture":
            for source in report_sources:
                available[source].add(binding.site_id)
        elif provider in available:
            available[provider].add(binding.site_id)
    return available


def _scoped_coverage(coverage, *, source: str, metric: str, site_id: str | None):
    buckets = []
    for item in coverage.get("by_site_source", []):
        if item["source"] != source or not item.get("configured"):
            continue
        if site_id is not None and item["site_id"] != site_id:
            continue
        cells = item.get("metric_coverage", {}).get(
            metric, {"status": "unavailable", "expected": 0, "covered": 0}
        )
        missing_ranges = [
            entry for entry in item.get("missing_ranges", [])
            if entry["metric"] == metric
        ]
        buckets.append(
            {
                "site_id": item["site_id"],
                "source": source,
                "status": cells["status"],
                "configured": True,
                "expected_cells": cells["expected"],
                "covered_cells": cells["covered"],
                "missing_cells_count": sum(int(entry["cells"]) for entry in missing_ranges),
                "missing_ranges": missing_ranges,
                "metric_status": {metric: cells["status"]},
                "metric_coverage": {metric: cells},
            }
        )
    expected = sum(int(item["expected_cells"]) for item in buckets)
    covered = sum(int(item["covered_cells"]) for item in buckets)
    status = (
        "not_configured" if not buckets
        else "complete" if expected and expected == covered
        else "unavailable" if covered == 0
        else "partial"
    )
    return {
        "status": status,
        "expected_cells": expected,
        "covered_cells": covered,
        "by_metric": {metric: status},
        "by_metric_cells": {metric: {"expected": expected, "covered": covered}},
        "by_site_source": buckets,
    }


def _fill_query_proven_zero_series(series, coverage, health, *, metric: str, window):
    """Materialize zeroes only where successful acquisition coverage proves them."""

    if metric not in METRICS or METRICS[metric].aggregation != "sum":
        return series
    start = datetime.fromisoformat(window["start"]).date()
    end = datetime.fromisoformat(window["end"]).date()
    all_dates = []
    day = start
    while day < end:
        all_dates.append(day.isoformat())
        day += timedelta(days=1)
    output = [
        {**item, "points": [dict(point) for point in item["points"]]}
        for item in series
    ]
    for bucket in coverage.get("by_site_source", []):
        if not bucket.get("configured"):
            continue
        missing_dates = set()
        for item in bucket.get("missing_ranges", []):
            if item["metric"] != metric or item["start"] is None:
                continue
            missing_day = datetime.fromisoformat(item["start"]).date()
            missing_end = datetime.fromisoformat(item["end"]).date()
            while missing_day <= missing_end:
                missing_dates.add(missing_day.isoformat())
                missing_day += timedelta(days=1)
        matching = next(
            (item for item in output if item["site_id"] == bucket["site_id"]),
            None,
        )
        if matching is None:
            health_sources = {
                item["source"] for item in health
                if item["site_id"] == bucket["site_id"]
                and item.get("metric_source") == METRICS[metric].source
            }
            actual_source = (
                next(iter(health_sources))
                if len(health_sources) == 1 else METRICS[metric].source
            )
            matching = {
                "metric": metric,
                "site_id": bucket["site_id"],
                "source": actual_source,
                "unit": METRICS[metric].unit,
                "points": [],
            }
            output.append(matching)
        values = {point["date"]: point["value"] for point in matching["points"]}
        for date_label in all_dates:
            if date_label not in missing_dates:
                values.setdefault(date_label, 0)
        matching["points"] = [
            {"date": date_label, "value": value}
            for date_label, value in sorted(values.items())
        ]
    return sorted(output, key=lambda item: (item["site_id"], item["source"], item["metric"]))


def _site_graph_display_name(route: str) -> str:
    if route == "/":
        return "Home"
    leaf = route.rstrip("/").rsplit("/", 1)[-1]
    words = leaf.replace("-", " ").replace("_", " ").strip().split()
    replacements = {
        "seo": "SEO",
        "b2b": "B2B",
        "ai": "AI",
        "api": "API",
        "ga4": "GA4",
        "d1": "D1",
    }
    return " ".join(replacements.get(word.casefold(), word.title()) for word in words) or route


def _site_graph_stable_unit(value: str, salt: str) -> float:
    digest = hashlib.blake2s(f"{salt}:{value}".encode("utf-8"), digest_size=4).hexdigest()
    return int(digest, 16) / 0xFFFFFFFF


def _site_graph_label_width(route: str, node: dict[str, object]) -> float:
    label = str(node.get("pretty_name") or _site_graph_display_name(route))
    return float(min(230, max(88, len(label) * 7.2 + 42)))


def _site_graph_topology_label(node: dict[str, object], route: str) -> str:
    distance = int(node.get("goal_distance", -1))
    if node.get("selected") or distance == 0:
        return "Focus / goal"
    if route == "/":
        return "Entry page"
    if distance == 1:
        return "One click away"
    if distance == 2:
        return "Two-click support"
    if distance > 2:
        return "Longer path"
    return "Disconnected here"


def _site_graph_components(routes: list[str], adjacency: dict[str, dict[str, float]]) -> list[list[str]]:
    unseen = set(routes)
    components: list[list[str]] = []
    while unseen:
        start = min(unseen)
        stack = [start]
        unseen.remove(start)
        component: list[str] = []
        while stack:
            route = stack.pop()
            component.append(route)
            for neighbor in adjacency.get(route, {}):
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    stack.append(neighbor)
        components.append(sorted(component))
    return components


def _site_graph_positions(nodes, edges):
    node_by_route = {str(node["route"]): node for node in nodes}
    routes = sorted(node_by_route)
    node_count = max(1, len(routes))
    layout_columns = max(4, math.ceil(math.sqrt(node_count) * 1.28))
    layout_rows = max(1, math.ceil(node_count / layout_columns))
    width = int(min(2600, max(1500, layout_columns * 280)))
    height = int(min(1900, max(920, layout_rows * 245 + 360)))
    center_x = width / 2
    center_y = height * 0.48
    authority_values = [node.get("authority", 0) for node in nodes]
    authority_min = min(authority_values, default=0)
    authority_span = max(authority_values, default=0) - authority_min or 1

    layer_weight = {
        "contextual": 2.8,
        "action": 2.6,
        "related": 2.3,
        "breadcrumb": 1.7,
        "menu": 1.1,
        "utility": 0.8,
    }
    adjacency: dict[str, dict[str, float]] = {route: {} for route in routes}
    edge_forces: list[tuple[str, str, float, str]] = []
    for edge in edges:
        source = str(edge.get("source", ""))
        destination = str(edge.get("destination", ""))
        if source not in node_by_route or destination not in node_by_route or source == destination:
            continue
        layer = str(edge.get("layer", "contextual"))
        occurrences = int(edge.get("occurrence_count", 1) or 1)
        weight = layer_weight.get(layer, 1.0) + min(occurrences, 8) * 0.08
        adjacency[source][destination] = adjacency[source].get(destination, 0.0) + weight
        adjacency[destination][source] = adjacency[destination].get(source, 0.0) + weight * 0.9
        edge_forces.append((source, destination, weight, layer))

    components = _site_graph_components(routes, adjacency)
    focus_routes = {
        route for route, node in node_by_route.items()
        if node.get("selected") or int(node.get("goal_distance", -1)) == 0
    }
    if not focus_routes and "/" in node_by_route:
        focus_routes = {"/"}
    focus_components = {
        index for index, component in enumerate(components)
        if any(route in focus_routes for route in component)
    }
    component_order = sorted(
        range(len(components)),
        key=lambda index: (0 if index in focus_components else 1, -len(components[index]), components[index][0]),
    )
    component_slot = {component_index: slot for slot, component_index in enumerate(component_order)}
    component_rank: dict[str, int] = {}
    component_size: dict[str, int] = {}
    for index, component in enumerate(components):
        for rank, route in enumerate(component):
            component_rank[route] = rank
            component_size[route] = len(component)

    positioned: dict[str, dict[str, float | int | bool]] = {}
    total_components = max(1, len(component_order))
    component_arc = (math.tau / total_components) if total_components > 1 else math.tau
    for route in routes:
        node = node_by_route[route]
        distance = int(node.get("goal_distance", -1))
        ring = 0 if node.get("selected") or distance == 0 else 5 if distance < 0 else min(distance, 4)
        authority_norm = (float(node.get("authority", 0)) - authority_min) / authority_span
        route_depth = max(0, len([part for part in route.strip("/").split("/") if part]) - 1)
        component_index = next(index for index, component in enumerate(components) if route in component)
        slot = component_slot[component_index]
        rank = component_rank.get(route, 0)
        count = max(1, component_size.get(route, 1))
        rank_offset = ((rank + 0.5) / count - 0.5) * min(component_arc * 0.72, 1.9)
        jitter_angle = (_site_graph_stable_unit(route, "angle") - 0.5) * 0.48
        if total_components == 1:
            angle = -math.pi / 2 + ((rank + 0.5) / count) * math.tau + jitter_angle
        else:
            angle = -math.pi / 2 + slot * component_arc + component_arc / 2 + rank_offset + jitter_angle
        base_radius = min(width, height) * 0.16
        ring_gap = min(width, height) * 0.105
        orbit = base_radius + ring * ring_gap + route_depth * 24
        if ring == 0:
            x = center_x + (_site_graph_stable_unit(route, "x") - 0.5) * 40
            y = center_y + (_site_graph_stable_unit(route, "y") - 0.5) * 28
            anchor_x, anchor_y = center_x, center_y
            pinned = True
        elif route == "/":
            x = center_x - orbit * 0.74
            y = center_y + (_site_graph_stable_unit(route, "entry-y") - 0.5) * 70
            anchor_x, anchor_y = x, y
            pinned = False
        elif ring == 5:
            disconnected_angle = math.pi * (0.23 + _site_graph_stable_unit(route, "disconnected") * 0.54)
            orbit = min(width, height) * (0.39 + _site_graph_stable_unit(route, "outer") * 0.08)
            x = center_x + math.cos(disconnected_angle) * orbit
            y = center_y + abs(math.sin(disconnected_angle)) * orbit * 0.86
            anchor_x, anchor_y = x, y
            pinned = False
        else:
            x = center_x + math.cos(angle) * orbit
            y = center_y + math.sin(angle) * orbit * 0.82
            anchor_x, anchor_y = x, y
            pinned = False
        node_radius = 24 + authority_norm * 10 + (5 if ring == 0 else 0)
        positioned[route] = {
            "x": x,
            "y": y,
            "r": int(round(node_radius)),
            "ring": ring,
            "anchor_x": anchor_x,
            "anchor_y": anchor_y,
            "pinned": pinned,
            "label_width": _site_graph_label_width(route, node),
            "group_label": _site_graph_topology_label(node, route),
        }
    margin = 54
    for iteration in range(180):
        displacement = {route: [0.0, 0.0] for route in positioned}
        for route, item in positioned.items():
            anchor_strength = 0.052 if item["pinned"] else 0.018 if int(item["ring"]) == 5 else 0.012
            displacement[route][0] += (float(item["anchor_x"]) - float(item["x"])) * anchor_strength
            displacement[route][1] += (float(item["anchor_y"]) - float(item["y"])) * anchor_strength
        for source, destination, weight, layer in edge_forces:
            source_item = positioned[source]
            destination_item = positioned[destination]
            dx = float(destination_item["x"]) - float(source_item["x"])
            dy = float(destination_item["y"]) - float(source_item["y"])
            distance = math.hypot(dx, dy)
            if distance < 0.1:
                angle = _site_graph_stable_unit(f"{source}->{destination}", "edge") * math.tau
                dx, dy, distance = math.cos(angle), math.sin(angle), 1.0
            desired = 185 + abs(int(source_item["ring"]) - int(destination_item["ring"])) * 24
            if layer in {"menu", "utility"}:
                desired += 48
            force = (distance - desired) * min(0.038, 0.009 * weight)
            unit_x, unit_y = dx / distance, dy / distance
            displacement[source][0] += unit_x * force
            displacement[source][1] += unit_y * force
            displacement[destination][0] -= unit_x * force
            displacement[destination][1] -= unit_y * force
        route_items = list(positioned.items())
        for index, (left_route, left) in enumerate(route_items):
            for right_route, right in route_items[index + 1 :]:
                dx = float(right["x"]) - float(left["x"])
                dy = float(right["y"]) - float(left["y"])
                distance = math.hypot(dx, dy)
                if distance < 0.1:
                    angle = _site_graph_stable_unit(f"{left_route}:{right_route}", "repel") * math.tau
                    dx, dy, distance = math.cos(angle), math.sin(angle), 1.0
                label_clearance = (float(left["label_width"]) + float(right["label_width"])) * 0.24
                minimum = int(left["r"]) + int(right["r"]) + max(74, label_clearance)
                force = 0.0
                if distance < minimum:
                    force = (minimum - distance) * 0.46
                elif distance < 310:
                    force = (310 - distance) * 0.006
                if not force:
                    continue
                unit_x, unit_y = dx / distance, dy / distance
                left_factor = 0.28 if left["pinned"] else 1.0
                right_factor = 0.28 if right["pinned"] else 1.0
                displacement[left_route][0] -= unit_x * force * left_factor
                displacement[left_route][1] -= unit_y * force * left_factor
                displacement[right_route][0] += unit_x * force * right_factor
                displacement[right_route][1] += unit_y * force * right_factor
        max_step = 42 - min(32, iteration * 0.16)
        for route, item in positioned.items():
            dx, dy = displacement[route]
            length = math.hypot(dx, dy)
            if length > max_step:
                dx *= max_step / length
                dy *= max_step / length
            item["x"] = min(width - margin, max(margin, float(item["x"]) + dx))
            item["y"] = min(height - margin, max(margin, float(item["y"]) + dy))
    for _ in range(100):
        moved = False
        route_items = list(positioned.items())
        for index, (left_route, left) in enumerate(route_items):
            for right_route, right in route_items[index + 1 :]:
                dx = float(right["x"]) - float(left["x"])
                dy = float(right["y"]) - float(left["y"])
                distance = math.hypot(dx, dy)
                if distance < 0.1:
                    angle = _site_graph_stable_unit(f"{left_route}:{right_route}", "final") * math.tau
                    dx, dy, distance = math.cos(angle), math.sin(angle), 1.0
                label_clearance = (float(left["label_width"]) + float(right["label_width"])) * 0.24
                minimum = int(left["r"]) + int(right["r"]) + max(82, label_clearance)
                if distance >= minimum:
                    continue
                moved = True
                push = (minimum - distance) * 0.52
                unit_x, unit_y = dx / distance, dy / distance
                left_factor = 0.18 if left["pinned"] else 1.0
                right_factor = 0.18 if right["pinned"] else 1.0
                left["x"] = float(left["x"]) - unit_x * push * left_factor
                left["y"] = float(left["y"]) - unit_y * push * left_factor
                right["x"] = float(right["x"]) + unit_x * push * right_factor
                right["y"] = float(right["y"]) + unit_y * push * right_factor
        for item in positioned.values():
            item["x"] = min(width - margin, max(margin, float(item["x"])))
            item["y"] = min(height - margin, max(margin, float(item["y"])))
        if not moved:
            break
    positions = {}
    for route, item in positioned.items():
        positions[route] = {
            "x": int(round(float(item["x"]))),
            "y": int(round(float(item["y"]))),
            "r": int(item["r"]),
            "depth": (float(item["y"]) - center_y) / max(1, height / 2),
            "ring": int(item["ring"]),
        }
    label_groups: dict[str, list[str]] = {}
    for route, item in positioned.items():
        label_groups.setdefault(str(item["group_label"]), []).append(route)
    label_order = {
        "Focus / goal": 0,
        "Entry page": 1,
        "One click away": 2,
        "Two-click support": 3,
        "Longer path": 4,
        "Disconnected here": 5,
    }
    cluster_labels = []
    for label, label_routes in sorted(label_groups.items(), key=lambda item: (label_order.get(item[0], 99), item[0])):
        group_positions = [positions[route] for route in label_routes]
        x = sum(item["x"] for item in group_positions) / len(group_positions)
        y = min(item["y"] for item in group_positions) - 58
        cluster_labels.append(
            {
                "label": label,
                "count": len(label_routes),
                "x": int(round(min(width - 80, max(80, x)))),
                "y": int(round(min(height - 42, max(42, y)))),
            }
        )
    return width, height, positions, cluster_labels


def _site_graph_edge_path(source, destination, source_radius: int) -> str:
    sx, sy = source["x"], source["y"]
    dx, dy = destination["x"], destination["y"]
    if sx == dx and sy == dy:
        loop = source_radius + 28
        return (
            f"M {sx + source_radius} {sy - 3} "
            f"C {sx + loop} {sy - loop} {sx + loop} {sy + loop} {sx + source_radius} {sy + 3}"
        )
    mid_x = (sx + dx) / 2
    mid_y = (sy + dy) / 2
    delta_x = dx - sx
    delta_y = dy - sy
    distance = max(1, math.hypot(delta_x, delta_y))
    curve = min(70, max(24, distance * 0.12))
    sign = -1 if (sx + sy + dx + dy) % 2 else 1
    control_x = mid_x - sign * delta_y / distance * curve
    control_y = mid_y + sign * delta_x / distance * curve * 0.62
    return f"M {sx} {sy} Q {int(round(control_x))} {int(round(control_y))} {dx} {dy}"


def _site_graph_svg(payload):
    nodes = payload["visualization"]["nodes"]
    edges = payload["visualization"]["edges"]
    if not nodes:
        return '<div class="empty-state">No pages match this bounded view.</div>'
    width, height, positions, cluster_labels = _site_graph_positions(nodes, edges)
    edge_html = []
    edge_priority = {"menu": 0, "utility": 1, "breadcrumb": 2, "contextual": 3, "related": 4, "action": 5}
    for edge in sorted(edges, key=lambda item: edge_priority.get(item["layer"], 9)):
        source_route = edge["source"]
        destination_route = edge["destination"]
        layer = edge["layer"]
        source = positions.get(source_route)
        destination = positions.get(destination_route)
        if not source or not destination:
            continue
        source_name = edge.get("source_name") or _site_graph_display_name(source_route)
        destination_name = edge.get("destination_name") or _site_graph_display_name(destination_route)
        anchor = edge["anchor"] or "unlabeled"
        occurrence_count = edge.get("occurrence_count", 1)
        path = _site_graph_edge_path(source, destination, source["r"])
        edge_label = (
            f"Edge from {source_name} to {destination_name}; "
            f"{layer} layer; {occurrence_count} occurrence(s); anchor {anchor}"
        )
        edge_html.append(
            f'<path class="graph-edge {_e(layer)}" d="{_e(path)}" marker-end="url(#arrow)" tabindex="0" role="button" '
            f'aria-label="{_e(edge_label)}" data-graph-edge data-source="{_e(source_route)}" '
            f'data-source-name="{_e(source_name)}" data-destination-name="{_e(destination_name)}" '
            f'data-destination="{_e(destination_route)}" data-layer="{_e(layer)}" '
            f'data-occurrences="{occurrence_count}" data-anchor="{_e(anchor)}"><title>'
            f'{_e(source_name)} to {_e(destination_name)}; {_e(layer)}; '
            f'{occurrence_count} occurrence(s); anchor {_e(anchor)}'
            f'</title></path>'
        )
    key_routes = {
        str(node["route"])
        for node in sorted(nodes, key=lambda item: float(item.get("authority", 0)), reverse=True)[:8]
    }
    node_html = []
    for node in sorted(nodes, key=lambda item: positions[str(item["route"])]["depth"]):
        route = node["route"]
        x, y, radius = positions[route]["x"], positions[route]["y"], positions[route]["r"]
        distance = node["goal_distance"]
        state = "selected" if node["selected"] else "goal" if distance == 0 else "unreachable" if distance < 0 else ""
        pretty_name = node.get("pretty_name") or _site_graph_display_name(route)
        label = pretty_name if len(pretty_name) <= 28 else pretty_name[:25] + "..."
        distance_label = "goal" if distance == 0 else "unreachable" if distance < 0 else f"{distance} hop"
        node_label = f'Page {pretty_name}; route {route}; {distance_label}; authority {node["authority"]:.4f}'
        depth_class = "depth-front" if positions[route]["depth"] > 0 else "depth-back"
        key_class = "is-key" if route in key_routes or distance in (0, 1) or node["selected"] else ""
        node_html.append(
            f'<g class="graph-node-group {state} {key_class}" tabindex="0" role="button" aria-label="{_e(node_label)}" '
            f'data-graph-node data-graph-node-route="{_e(route)}" '
            f'data-graph-node-name="{_e(pretty_name)}" '
            f'data-goal-distance="{_e(distance_label)}" data-authority="{node["authority"]:.4f}">'
            f'<ellipse class="graph-node-shadow" cx="{x}" cy="{y + radius + 7}" rx="{radius + 9}" ry="8"></ellipse>'
            f'<circle class="graph-node {state} {depth_class}" cx="{x}" cy="{y}" r="{radius}"><title>'
            f'{_e(pretty_name)}; route {_e(route)}; {distance_label}; authority {node["authority"]:.4f}'
            f'</title></circle><text class="graph-label" x="{x}" y="{y + radius + 19}">'
            f'<tspan class="graph-label-title" x="{x}">{_e(label)}</tspan></text></g>'
        )
    plane = (
        f'<ellipse class="graph-depth-plane" cx="{width // 2}" cy="{int(height * .52)}" '
        f'rx="{int(width * .42)}" ry="{int(height * .28)}"></ellipse>'
    )
    cluster_html = "".join(
        f'<text class="graph-cluster-label" x="{label["x"]}" y="{label["y"]}">'
        f'{_e(label["label"])} · {label["count"]}</text>'
        for label in cluster_labels
    )
    return (
        '<div class="graph-stage" data-site-graph-stage><div class="graph-map" data-graph-map>'
        '<p class="graph-map-help">Read this map as pages and pathways: circles are pages, arrows are internal links, and placement follows link relationships plus click distance from the focus page. Drag the map, scroll to zoom, or use the controls.</p>'
        '<div class="graph-canvas-toolbar" role="group" aria-label="Graph canvas controls">'
        '<button type="button" data-graph-zoom-out aria-label="Zoom out">Zoom out</button>'
        '<button type="button" data-graph-zoom-in aria-label="Zoom in">Zoom in</button>'
        '<button type="button" data-graph-zoom-reset>Reset view</button>'
        '<span class="graph-zoom-status" data-graph-zoom-status aria-live="polite">100%</span></div>'
        f'<svg class="site-graph-svg" viewBox="0 0 {width} {height}" role="img" '
        'aria-labelledby="graph-title graph-description"><title id="graph-title">Site Graph structural overview</title>'
        f'<desc id="graph-description">An organic topology map of pages and '
        f'aggregated internal-link relationships in the selected layers. An equivalent table follows the graphic.</desc>'
        '<defs><marker id="arrow" markerUnits="strokeWidth" markerWidth="4.8" markerHeight="4.8" '
        'refX="4.25" refY="2.4" orient="auto"><path d="M0,0 L4.8,2.4 L0,4.8 z" fill="#78817c"></path></marker>'
        '<filter id="node-lift" x="-35%" y="-35%" width="170%" height="170%"><feDropShadow dx="0" dy="4" stdDeviation="4" flood-color="#17201d" flood-opacity=".18"></feDropShadow></filter></defs>'
        '<g class="graph-viewport" data-graph-viewport>'
        + plane + cluster_html + "".join(edge_html) + "".join(node_html) + '</g></svg></div>'
        '<aside class="graph-inspector" data-graph-inspector aria-live="polite"></aside></div>'
    )


def _site_graph_table(payload):
    rows = "".join(
        f'<tr><td class="edge-identity"><a href="{_e("/site-graph?" + urlencode({"site": payload["site"]["key"], "page": node["route"]}))}">{_e(node.get("pretty_name") or _site_graph_display_name(node["route"]))}</a><br>{_e(node["route"])}</td>'
        f'<td>{"Goal" if node["goal_distance"] == 0 else "Unreachable" if node["goal_distance"] < 0 else str(node["goal_distance"])}</td>'
        f'<td>{node["authority"]:.4f}</td><td>{"Selected" if node["selected"] else ""}</td></tr>'
        for node in payload["visualization"]["nodes"]
    )
    edge_rows = "".join(
        f'<tr><td>{_e(edge["source"])}</td><td>{_e(edge["destination"])}</td><td>{_e(edge["layer"])}</td>'
        f'<td>{edge.get("occurrence_count", 1)}</td><td>{_e(edge["anchor"] or "Unlabeled")}</td></tr>'
        for edge in payload["visualization"]["edges"]
    )
    return (
        '<details class="chart-fallback" open><summary>Graph nodes and edges</summary>'
        '<div class="table-scroll"><table><caption class="sr-only">Bounded graph nodes</caption><thead><tr><th>Page</th><th>Goal distance</th><th>Authority</th><th>State</th></tr></thead><tbody>'
        + (rows or '<tr><td colspan="4">No nodes.</td></tr>') + '</tbody></table></div>'
        '<div class="table-scroll"><table><caption class="sr-only">Displayed graph edges</caption><thead><tr><th>Source</th><th>Destination</th><th>Layer</th><th>Occurrences</th><th>Anchor sample</th></tr></thead><tbody>'
        + (edge_rows or '<tr><td colspan="5">No edges in this view.</td></tr>') + '</tbody></table></div></details>'
    )


def _site_graph_url(payload, **changes):
    params = {
        "site": payload["site"]["key"],
        "layer": payload["selected_layers"],
        "graph": payload["display"]["requested_graph_mode"],
        "page": payload["neighborhood"]["selected_page"],
        "edge_query": payload["edge_table"]["query"],
        "edge_sort": payload["edge_table"]["sort"],
        "edge_order": payload["edge_table"]["order"],
        "edge_page": payload["edge_table"]["page"],
    }
    params.update(changes)
    params = {key: value for key, value in params.items() if value is not None and value != ""}
    return "/site-graph?" + urlencode(params, doseq=True)


def _site_graph_disclosure(payload):
    display = payload["display"]
    unresolved = display["unresolved_relationships"]
    selected_page = display["filters"]["selected_page"] or "none"
    edge_query = display["filters"]["edge_query"] or "none"
    reasons = "".join(f"<li>{_e(reason)}</li>" for reason in display["truncation_reasons"])
    layer_accounting = ", ".join(
        f"{layer} {payload['layer_counts'].get(layer, 0)}" for layer in SITE_GRAPH_LAYERS
    )
    actions = []
    if display["full_graph_available"] and display["graph_mode"] != "full":
        actions.append(f'<a href="{_e(_site_graph_url(payload, graph="full"))}">Show safe full graph</a>')
    if display["graph_mode"] == "full":
        actions.append(f'<a href="{_e(_site_graph_url(payload, graph="bounded"))}">Use bounded graph</a>')
    if tuple(payload["selected_layers"]) != SITE_GRAPH_LAYERS:
        actions.append(
            f'<a href="{_e(_site_graph_url(payload, layer=SITE_GRAPH_LAYERS, edge_page=1))}">Account for all internal layers</a>'
        )
    csv_params = {
        "site": payload["site"]["key"],
        "layer": payload["selected_layers"],
        "edge_query": payload["edge_table"]["query"],
        "edge_sort": payload["edge_table"]["sort"],
        "edge_order": payload["edge_table"]["order"],
    }
    actions.append(
        f'<a href="{_e("/api/v1/site-graph.csv?" + urlencode(csv_params, doseq=True))}">Export complete edge CSV</a>'
    )
    return (
        '<div class="graph-disclosure" aria-label="Graph completeness disclosure">'
        f'<p><strong>{display["displayed_nodes"]} displayed of {display["total_nodes"]} total nodes</strong>'
        f'Node threshold: {display["thresholds"]["nodes"]}</p>'
        f'<p><strong>{display["displayed_unique_edges"]} displayed of {display["total_unique_edges"]} total unique edges</strong>'
        f'{display["represented_occurrences"]} represented of {display["total_occurrences"]} total link occurrences</p>'
        f'<p><strong>{unresolved} unresolved relationship{"s" if unresolved != 1 else ""}</strong>'
        'Not drawn as resolved page-to-page edges</p>'
        f'<p><strong>Truncation occurred:</strong> {"Yes" if display["truncated"] else "No"}<br>'
        f'Mode: {_e(display["graph_mode"])}; aggregation: source-destination-layer</p>'
        f'<p><strong>Active projection and filters</strong>Projection: {_e(display["projection"])}; '
        f'layers: {_e(", ".join(display["layers"]))}; neighborhood: {_e(selected_page)}; '
        f'edge-table query: {_e(edge_query)}</p>'
        '<p><strong>Analytical basis stays compiled contextual</strong>'
        'Selected layers change edge counts, tables, and the drawing only; goal distance, components, resilience, and findings remain contextual.</p>'
        f'<p><strong>{payload["coverage"]["link_occurrences"]} stored internal link occurrences across all layers</strong>'
        f'Current selected layers account for {display["total_occurrences"]}; layer totals: '
        f'{_e(layer_accounting)}</p>'
        + (f'<ul class="graph-reasons">{reasons}</ul>' if reasons else '')
        + '</div><div class="graph-actions">' + ''.join(actions) + '</div>'
    )


def _complete_site_graph_edge_table(payload):
    edge_table = payload["edge_table"]
    rows = []
    for row in edge_table["rows"]:
        evidence = row["evidence"]
        evidence_label = "; ".join(
            item for item in (
                evidence["anchor_sample"] and f'anchor: {evidence["anchor_sample"]}',
                evidence["landmark_sample"] and f'landmark: {evidence["landmark_sample"]}',
                evidence["source"] and f'source: {evidence["source"]}',
                evidence["classification"] and f'classification: {evidence["classification"]}',
            ) if item
        ) or "No descriptive sample"
        rows.append(
            f'<tr><td class="edge-identity"><strong>{_e(row["source"]["pretty_name"])}</strong><br>{_e(row["source"]["route"])}</td>'
            f'<td class="edge-identity"><strong>{_e(row["destination"]["pretty_name"])}</strong><br>{_e(row["destination"]["route"])}</td>'
            f'<td><span class="source-chip">{_e(row["layer"])}</span></td><td>{row["occurrence_count"]}</td>'
            f'<td class="edge-evidence">{_e(evidence_label)}; confidence {evidence["confidence_min"]}-{evidence["confidence_max"]}</td></tr>'
        )
    hidden = (
        f'<input type="hidden" name="site" value="{_e(payload["site"]["key"])}">'
        + ''.join(f'<input type="hidden" name="layer" value="{_e(layer)}">' for layer in payload["selected_layers"])
        + (f'<input type="hidden" name="page" value="{_e(payload["neighborhood"]["selected_page"])}">' if payload["neighborhood"]["selected_page"] else '')
        + f'<input type="hidden" name="graph" value="{_e(payload["display"]["requested_graph_mode"])}">'
    )
    sort_options = "".join(
        f'<option value="{value}"{" selected" if edge_table["sort"] == value else ""}>{label}</option>'
        for value, label in (("source", "Source"), ("destination", "Destination"), ("layer", "Layer"), ("occurrences", "Occurrences"))
    )
    order_options = "".join(
        f'<option value="{value}"{" selected" if edge_table["order"] == value else ""}>{label}</option>'
        for value, label in (("asc", "Ascending"), ("desc", "Descending"))
    )
    previous_link = (
        f'<a href="{_e(_site_graph_url(payload, edge_page=edge_table["page"] - 1))}">Previous page</a>'
        if edge_table["page"] > 1 else '<span>Previous page</span>'
    )
    next_link = (
        f'<a href="{_e(_site_graph_url(payload, edge_page=edge_table["page"] + 1))}">Next page</a>'
        if edge_table["page"] < edge_table["page_count"] else '<span>Next page</span>'
    )
    source_sort_order = "desc" if edge_table["sort"] == "source" and edge_table["order"] == "asc" else "asc"
    return (
        '<section class="panel table-panel edge-table-panel">'
        '<div class="panel-heading"><div><h2>Complete selected-projection edge table</h2>'
        f'<p>{edge_table["displayed_rows"]} of {edge_table["filtered_unique_edges"]} unique edges on this page; '
        f'{edge_table["total_unique_edges"]} before table filtering. '
        f'<a href="{_e(_site_graph_url(payload, edge_sort="source", edge_order=source_sort_order, edge_page=1))}">Sort by source</a>. '
        'Pretty names are derived from canonical routes because the current page-fact contract does not store page titles.</p></div></div>'
        '<form class="edge-tools" method="get" action="/site-graph">' + hidden
        + f'<label class="field"><span>Filter complete edge table</span><input name="edge_query" value="{_e(edge_table["query"])}" placeholder="Route or layer"></label>'
        f'<label class="field"><span>Sort</span><select name="edge_sort">{sort_options}</select></label>'
        f'<label class="field"><span>Order</span><select name="edge_order">{order_options}</select></label>'
        '<button type="submit">Apply table filters</button></form>'
        '<div class="table-scroll"><table><thead><tr><th>Source page</th><th>Destination page</th><th>Layer</th><th>Occurrences</th><th>Sanitized evidence</th></tr></thead><tbody>'
        + (''.join(rows) or '<tr><td colspan="5">No resolved unique edges match this table filter.</td></tr>')
        + f'</tbody></table></div><div class="pager">{previous_link}<span>Page {edge_table["page"]} of {edge_table["page_count"]}</span>{next_link}</div></section>'
    )


def _site_graph_distance_label(distance):
    if distance == 0:
        return "Goal"
    if distance < 0:
        return "Unreachable"
    return str(distance)


def _site_graph_page_table(payload):
    nodes = sorted(payload["visualization"]["nodes"], key=lambda item: item["route"])
    inbound = {}
    outbound = {}
    for row in payload["edge_table"]["rows"]:
        source = row["source"]["route"]
        destination = row["destination"]["route"]
        count = row["occurrence_count"]
        outbound[source] = outbound.get(source, 0) + count
        inbound[destination] = inbound.get(destination, 0) + count
    display = payload["display"]
    edge_table = payload["edge_table"]
    table_title = "Complete page table" if len(nodes) == display["total_nodes"] else "Displayed page table"
    rows = "".join(
        f'<tr><td class="edge-identity"><a href="{_e(_site_graph_url(payload, page=node["route"], edge_page=1))}">{_e(node.get("pretty_name") or _site_graph_display_name(node["route"]))}</a><br>{_e(node["route"])}</td>'
        f'<td>{_e(_site_graph_distance_label(node["goal_distance"]))}</td><td>{node["authority"]:.4f}</td>'
        f'<td>{outbound.get(node["route"], 0)}</td><td>{inbound.get(node["route"], 0)}</td>'
        f'<td>{"Selected" if node["selected"] else ""}</td></tr>'
        for node in nodes
    )
    return (
        '<section id="site-graph-pages" class="panel table-panel">'
        f'<div class="panel-heading"><div><h2>{table_title}</h2>'
        f'<p>{len(nodes)} of {display["total_nodes"]} pages represented. Inbound/outbound counts below are computed from '
        f'{edge_table["displayed_rows"]} of {edge_table["filtered_unique_edges"]} selected-projection table rows loaded in this response.</p></div></div>'
        '<div class="table-scroll"><table><thead><tr><th>Page</th><th>Goal distance</th><th>Authority</th><th>Outgoing occurrences</th><th>Incoming occurrences</th><th>State</th></tr></thead><tbody>'
        + (rows or '<tr><td colspan="6">No pages match this view.</td></tr>')
        + '</tbody></table></div></section>'
    )


def _site_graph_matrix_panel(payload):
    routes = sorted({node["route"] for node in payload["visualization"]["nodes"]})
    route_limit = 32
    matrix_routes = routes[:route_limit]
    route_index = {route: index + 1 for index, route in enumerate(matrix_routes)}
    counts = {}
    for row in payload["edge_table"]["rows"]:
        source = row["source"]["route"]
        destination = row["destination"]["route"]
        if source in route_index and destination in route_index:
            counts[(source, destination)] = counts.get((source, destination), 0) + row["occurrence_count"]
    header = "".join(f'<th><abbr title="{_e(route)}">{route_index[route]}</abbr></th>' for route in matrix_routes)
    body_rows = []
    for source in matrix_routes:
        cells = []
        for destination in matrix_routes:
            value = counts.get((source, destination), 0)
            cells.append(f'<td class="matrix-hit">{value}</td>' if value else '<td>0</td>')
        body_rows.append(
            f'<tr><th scope="row"><abbr title="{_e(source)}">{route_index[source]}</abbr></th>{"".join(cells)}</tr>'
        )
    legend = "".join(f'<span><strong>{route_index[route]}</strong> {_e(route)}</span>' for route in matrix_routes)
    truncation_note = (
        f" Matrix route list is capped at {route_limit} displayed routes for readability."
        if len(routes) > route_limit else ""
    )
    return (
        '<section id="site-graph-matrix" class="panel section-panel"><div class="panel-heading"><div><h2>Adjacency matrix</h2>'
        f'<p>Matrix uses {len(matrix_routes)} displayed routes and {payload["edge_table"]["displayed_rows"]} loaded edge-table rows.'
        f'{_e(truncation_note)}</p></div></div><div class="matrix-scroll"><table class="matrix-table"><thead><tr><th>From / to</th>{header}</tr></thead><tbody>'
        + ("".join(body_rows) or '<tr><td>No matrix entries.</td></tr>')
        + f'</tbody></table></div><div class="graph-meta">{legend}</div></section>'
    )


def _site_graph_evidence_panel(payload):
    classification_counts = {}
    source_counts = {}
    selected_layer_counts = {}
    for row in payload["edge_table"]["rows"]:
        evidence = row["evidence"]
        classification = evidence["classification"] or "unknown"
        evidence_source = evidence["source"] or "unknown"
        classification_counts[classification] = classification_counts.get(classification, 0) + row["occurrence_count"]
        source_counts[evidence_source] = source_counts.get(evidence_source, 0) + row["occurrence_count"]
        selected_layer_counts[row["layer"]] = selected_layer_counts.get(row["layer"], 0) + row["occurrence_count"]

    def rows_for(counts):
        return "".join(
            f'<tr><td>{_e(label)}</td><td>{count}</td></tr>'
            for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ) or '<tr><td colspan="2">No evidence rows in this selected projection.</td></tr>'

    return (
        '<section id="site-graph-evidence" class="panel table-panel"><div class="panel-heading"><div><h2>Evidence rollup</h2>'
        f'<p>{payload["edge_table"]["displayed_rows"]} loaded selected-projection rows; sanitized anchors, landmarks, sources, confidence bands, and classifications are in the edge table.</p></div></div>'
        '<div class="table-scroll"><table><thead><tr><th>Selected layer</th><th>Occurrences</th></tr></thead><tbody>'
        + rows_for(selected_layer_counts)
        + '</tbody></table></div><div class="table-scroll"><table><thead><tr><th>Evidence classification</th><th>Occurrences</th></tr></thead><tbody>'
        + rows_for(classification_counts)
        + '</tbody></table></div><div class="table-scroll"><table><thead><tr><th>Evidence source</th><th>Occurrences</th></tr></thead><tbody>'
        + rows_for(source_counts)
        + '</tbody></table></div></section>'
    )


def _site_graph_resilience_panel(payload):
    overview = payload["overview"]
    structural = payload.get("evidence_core21", {}).get("structural_metrics", {})
    cards = (
        ("Components", overview["components"], "Strongly connected structural groups"),
        (
            "True orphans",
            structural.get("true_orphans", overview["orphans"]),
            "Pages without inbound evidence in the complete topology",
        ),
        (
            "Contextual orphans",
            structural.get("contextual_orphans", 0),
            "Pages with inbound evidence only outside the selected layers",
        ),
        (
            "Contextual dead ends",
            structural.get("contextual_dead_ends", overview["contextual_dead_ends"]),
            "Selected-projection pages with no continuation",
        ),
        (
            "Menu-dependent",
            structural.get("menu_dependent", overview["menu_dependent_pages"]),
            "Pages reachable from home only through menu relationships",
        ),
        (
            "Global-shell-dependent",
            structural.get("global_shell_dependent", 0),
            "Pages whose inbound evidence is limited to menu or utility relationships",
        ),
    )
    card_html = "".join(
        f'<article class="health-item"><b>{_e(label)}</b><span class="pipeline-value">{value}</span><span>{_e(note)}</span></article>'
        for label, value, note in cards
    )
    return (
        '<section id="site-graph-resilience" class="panel section-panel"><div class="panel-heading"><div><h2>Resilience view</h2>'
        '<p>Structural failure-mode indicators from the compiled graph snapshot.</p></div></div>'
        f'<div class="health-grid">{card_html}</div></section>'
    )


def _site_graph_core21_panel(payload):
    evidence = payload.get("evidence_core21", {})
    if not evidence.get("available"):
        return (
            '<section id="site-graph-core21" class="panel section-panel">'
            '<div class="panel-heading"><div><h2>Evidence coverage</h2>'
            '<p>Core 2.1 evidence is unavailable for this legacy snapshot.</p></div></div></section>'
        )
    coverage = evidence["coverage"]
    structural = evidence["structural_metrics"]
    state_rows = "".join(
        f"<tr><td>{_e(state.replace('-', ' ').title())}</td><td>{count}</td></tr>"
        for state, count in coverage["state_counts"].items()
    ) or '<tr><td colspan="2">No route candidates.</td></tr>'
    structural_rows = (
        "".join(
            f"<tr><td>{_e(label)}</td><td>{structural[key]}</td></tr>"
            for key, label in (
                ("true_orphans", "True orphans"),
                ("contextual_orphans", "Contextual orphans"),
                ("contextual_dead_ends", "Contextual dead ends"),
                ("menu_dependent", "Menu-dependent pages"),
                ("homepage_dependent", "Homepage-dependent pages"),
                ("global_shell_dependent", "Global-shell-dependent pages"),
            )
        )
        if structural.get("available")
        else (
            '<tr><td colspan="2">Structural metrics withheld: '
            f'{_e(structural["reason"])}</td></tr>'
        )
    )
    return (
        '<section id="site-graph-core21" class="panel table-panel">'
        '<div class="panel-heading"><div><h2>Graph Evidence Core 2.1 coverage</h2>'
        '<p>Complete reconciliation totals are independent of the bounded SVG. '
        f'Freshness uses {_e(evidence["freshness_basis"])} evidence for revision '
        f'{_e(evidence["repository_revision"][:12])}.</p></div></div>'
        '<div class="graph-meta">'
        f'<span>{coverage["candidates"]} candidates</span>'
        f'<span>{coverage["entities"]} resolved entities</span>'
        f'<span>{coverage["relationships"]} relationships</span>'
        f'<span>{coverage["unresolved"]} unresolved</span>'
        f'<span>{coverage["contradictions"]} contradictions</span></div>'
        '<div class="split-grid"><div class="table-scroll"><table>'
        '<caption class="sr-only">Complete route-resolution coverage by state</caption>'
        '<thead><tr><th>Resolution state</th><th>Routes</th></tr></thead>'
        f'<tbody>{state_rows}</tbody></table></div>'
        '<div class="table-scroll"><table>'
        '<caption class="sr-only">Corrected structural findings for the selected projection</caption>'
        '<thead><tr><th>Corrected finding</th><th>Pages</th></tr></thead>'
        f'<tbody>{structural_rows}</tbody></table></div></div>'
        '<p class="graph-caption">Full-topology goal reachability is withheld because the '
        'compatible display model carries only the compiled contextual projection.</p>'
        '</section>'
    )


def _site_graph_entry_goal_panel(payload):
    nodes = sorted(
        payload["visualization"]["nodes"],
        key=lambda node: (999 if node["goal_distance"] < 0 else node["goal_distance"], -node["authority"], node["route"]),
    )
    rows = "".join(
        f'<tr><td class="edge-identity"><strong>{_e(node.get("pretty_name") or _site_graph_display_name(node["route"]))}</strong><br>{_e(node["route"])}</td><td>{_e(_site_graph_distance_label(node["goal_distance"]))}</td>'
        f'<td>{node["authority"]:.4f}</td><td>{"Selected neighborhood" if node["selected"] else "Structural route"}</td></tr>'
        for node in nodes
    )
    return (
        '<section id="site-graph-entry-goal" class="panel table-panel"><div class="panel-heading"><div><h2>Entry-to-goal structural view</h2>'
        '<p>Goal distance uses structural link paths to configured goal pages. This does not claim visitor-entry behavior.</p></div></div>'
        '<div class="distance-grid">'
        + "".join(
            f'<div class="distance-item"><b>{payload["goal_distance_buckets"][key]}</b><span>{_e(key.title())}</span></div>'
            for key in ("goal", "1", "2", "3", "4+", "menu-only", "unreachable")
        )
        + '</div><div class="table-scroll"><table><thead><tr><th>Page</th><th>Goal distance</th><th>Authority</th><th>Mode</th></tr></thead><tbody>'
        + (rows or '<tr><td colspan="4">No pages available.</td></tr>')
        + '</tbody></table></div></section>'
    )


def _site_graph_snapshot_panel(payload):
    snapshot = payload["snapshot"]
    display = payload["display"]
    diff = payload.get("snapshot_diff") or {}
    rows = (
        ("Current snapshot", snapshot["id"]),
        ("Captured", snapshot["captured_at"]),
        ("Repository state", "clean" if snapshot["clean"] else "dirty override"),
        ("Stored contextual snapshots", snapshot["count"]),
        ("Analyzed revision", payload["revision"]),
        ("Manifest hash", payload["manifest_hash"]),
        ("Displayed nodes", f'{display["displayed_nodes"]} of {display["total_nodes"]}'),
        ("Displayed unique edges", f'{display["displayed_unique_edges"]} of {display["total_unique_edges"]}'),
        ("Represented occurrences", f'{display["represented_occurrences"]} of {display["total_occurrences"]}'),
    )
    body = "".join(f'<tr><th>{_e(label)}</th><td>{_e(value)}</td></tr>' for label, value in rows)
    if diff.get("available"):
        current = diff["current"]
        previous = diff["previous"]
        pages = diff["pages"]
        edges = diff["edges"]
        sample_rows = []
        for label, sample in (("Added edge", edges["added_sample"]), ("Removed edge", edges["removed_sample"])):
            for edge in sample:
                sample_rows.append(
                    f'<tr><td>{_e(label)}</td><td>{_e(edge["source"])}</td><td>{_e(edge["destination"])}</td><td>{_e(edge["layer"])}</td></tr>'
                )
        for label, sample in (("Added page", pages["added_sample"]), ("Removed page", pages["removed_sample"])):
            for route in sample:
                sample_rows.append(f'<tr><td>{_e(label)}</td><td colspan="3">{_e(route)}</td></tr>')
        diff_body = (
            f'<p class="view-note">Comparing current revision {_e(current["revision"])} to previous distinct revision '
            f'{_e(previous["revision"])}. Edge diff is capped at {diff["limit"]} edges; '
            f'limited: {"yes" if diff.get("limited") else "no"}.</p>'
            '<div class="distance-grid">'
            f'<div class="distance-item"><b>{pages["added"]}</b><span>Pages added</span></div>'
            f'<div class="distance-item"><b>{pages["removed"]}</b><span>Pages removed</span></div>'
            f'<div class="distance-item"><b>{pages["unchanged"]}</b><span>Pages unchanged</span></div>'
            f'<div class="distance-item"><b>{edges["added"]}</b><span>Edges added</span></div>'
            f'<div class="distance-item"><b>{edges["removed"]}</b><span>Edges removed</span></div>'
            f'<div class="distance-item"><b>{edges["unchanged"]}</b><span>Edges unchanged</span></div>'
            '</div><div class="table-scroll"><table><thead><tr><th>Change</th><th>Source/page</th><th>Destination</th><th>Layer</th></tr></thead><tbody>'
            + ("".join(sample_rows) or '<tr><td colspan="4">No page or edge identity changes in the displayed samples.</td></tr>')
            + '</tbody></table></div>'
        )
    else:
        diff_body = f'<p class="view-note">Snapshot diff unavailable: {_e(diff.get("reason", "No previous snapshot comparison is available."))}</p>'
    return (
        '<section id="site-graph-snapshot" class="panel table-panel"><div class="panel-heading"><div><h2>Snapshot diff</h2>'
        '<p>Current snapshot, display counts, and previous-snapshot identity changes when a distinct prior snapshot exists.</p></div></div>'
        f'<div class="table-scroll"><table><tbody>{body}</tbody></table></div>{diff_body}</section>'
    )


def _site_graph_analysis_panels(payload):
    return (
        _site_graph_core21_panel(payload)
        + _site_graph_page_table(payload)
        + '<div class="graph-view-grid">'
        + _site_graph_matrix_panel(payload)
        + _site_graph_resilience_panel(payload)
        + _site_graph_entry_goal_panel(payload)
        + _site_graph_snapshot_panel(payload)
        + '</div>'
        + _site_graph_evidence_panel(payload)
    )


def _geography_panel(payload, api_url):
    source_options = "".join(
        f'<option value="{_e(source)}"{" selected" if source == payload["source"] else ""}>{_e(spec["label"])}</option>'
        for source, spec in SOURCE_CONFIG.items()
    )
    rows = "".join(
        f'<tr><td class="metric-name">{_e(row["code"])}</td><td>{_format_value(row["value"])}</td><td>{_e(payload["label"])}</td></tr>'
        for row in payload["countries"]
    ) or '<tr><td colspan="3" class="geo-empty">No unsuppressed country rows are stored for this source and exact window.</td></tr>'
    suppression = payload["suppression"]
    return (
        '<section class="panel geography-panel" id="geography-map" '
        f'data-geography-api="{_e(api_url)}" data-world-map="/assets/maps/world-countries.geojson" '
        'data-us-map="/assets/maps/us-counties.json" aria-labelledby="geography-title">'
        '<div class="panel-heading"><div><p class="eyebrow">Geographic demand</p>'
        '<h2 id="geography-title">World visitor geography</h2>'
        '<p>Provider-labeled choropleths from stored aggregate dimensions. Darker areas indicate more activity within the selected source; sources are never blended.</p></div>'
        '<div class="geography-controls"><label class="field"><span>Map source</span>'
        f'<select id="geography-source">{source_options}</select></label></div></div>'
        '<div class="geography-grid"><article class="map-card"><h3>Countries</h3>'
        '<p>Select the United States to move into state and county-boundary detail.</p>'
        '<svg class="geo-svg" id="world-geo-map" viewBox="0 0 960 480" role="img" aria-label="World country choropleth"></svg></article>'
        '<article class="map-card" id="us-geography"><h3>United States</h3>'
        '<p>State values where the provider supports region data; select a state to reveal county boundaries.</p>'
        '<svg class="geo-svg" id="us-geo-map" viewBox="0 0 975 610" role="img" aria-label="United States state choropleth and county boundary drilldown"></svg></article></div>'
        '<div class="geo-status" id="geography-status" role="status">Loading local geography boundaries and stored aggregates...</div>'
        '<div class="geo-disclosure"><p><strong>Privacy floor</strong>'
        f'Buckets below {_e(suppression["threshold"])} are withheld; {suppression["withheld_country_rows"]} country and {suppression["withheld_us_state_rows"]} state rows are currently hidden.</p>'
        '<p><strong>County boundaries are orientation only</strong>Current providers do not expose trustworthy county aggregates. No county values are inferred from city names or IP data.</p>'
        f'<p><strong>Method</strong>{_e(payload["methodology"])}</p></div>'
        '<details class="geography-fallback"><summary>Accessible ranked country values and no-JavaScript fallback</summary>'
        '<div class="table-scroll"><table><caption class="sr-only">Geographic activity by country</caption>'
        f'<thead><tr><th scope="col">Country code</th><th scope="col">Value</th><th scope="col">Provider metric</th></tr></thead><tbody id="geography-table-body">{rows}</tbody></table></div></details></section>'
    )


def _route_observation_csv(payload):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow((
        "site_id", "source", "metric", "unit", "window_start", "window_end",
        "route", "dimensions", "value", "coverage", "freshness", "data_state",
        "provider_time_basis", "provider_limitation",
    ))
    for row in payload["rows"]:
        writer.writerow((
            row["site_id"], row["source"], row["metric"], row["unit"],
            row["window"]["start"], row["window"]["end"], row["route"] or "",
            json.dumps(row["dimensions"], sort_keys=True, separators=(",", ":")),
            row["value"], row["coverage"], row["freshness"], row["data_state"],
            row["provider_time_basis"], row["provider_limitation"],
        ))
    return output.getvalue()


def _route_observation_html(payload):
    filters = payload["filters"]
    site_options = "".join(
        f'<option value="{_e(site)}"{" selected" if site == filters["site"] else ""}>{_e(site)}</option>'
        for site in payload["available_sites"]
    )
    source_options = '<option value="">All configured providers</option>' + "".join(
        f'<option value="{_e(source)}"{" selected" if source == filters["source"] else ""}>{_e(source)}</option>'
        for source in payload["available_sources"]
    )
    metric_options = '<option value="">All accepted route metrics</option>' + "".join(
        f'<option value="{_e(metric)}"{" selected" if metric == filters["metric"] else ""}>{_e(metric)}</option>'
        for metric in payload["available_metrics"]
    )
    rows = "".join(
        "<tr>"
        f'<td>{_e(row["site_id"])}</td><td>{_e(row["source"])}</td>'
        f'<td>{_e(row["metric"])}</td><td>{_e(row["route"] or "Provider dimension")}</td>'
        f'<td>{_e(", ".join(f"{key}={value}" for key, value in row["dimensions"].items()))}</td>'
        f'<td>{_e(row["value"])} {_e(row["unit"])}</td><td>{_e(row["coverage"])}</td>'
        f'<td>{_e(row["data_state"])}</td><td>{_e(row["freshness"])}</td>'
        f'<td>{_e(row["provider_limitation"])}</td></tr>'
        for row in payload["rows"]
    ) or '<tr><td colspan="10">No accepted route observations match this bounded window.</td></tr>'
    query = {
        key: value for key, value in (
            ("report", filters["report"]),
            ("start", payload["window"]["start"][:10]),
            ("end", payload["window"]["end"][:10]),
            ("site", filters["site"]),
            ("source", filters["source"]),
            ("metric", filters["metric"]),
            ("route", filters["route"]),
        ) if value
    }
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Route observations - Boho Analytics</title><link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/assets/app.css"></head><body><a class="skip-link" href="#main">Skip to route observations</a>
<header class="topbar"><div class="topbar-inner"><div class="brand"><span class="brand-mark">BA</span><div><strong>Boho Analytics</strong><span>Private portfolio command center</span></div></div><div class="live-state">Read-only route observations</div></div></header>
<main class="shell" id="main"><div class="report-nav" aria-label="Dashboard areas"><a href="/">Analytics</a><a href="/site-graph">Site Graph</a><a class="active" href="/route-observations">Route observations</a></div>
<section class="hero"><div><p class="eyebrow">{_e(payload["window"]["start"][:10])} to {_e(payload["window"]["end"][:10])} - end exclusive</p><h1>Route observations</h1><p class="hero-copy">Provider-separated, privacy-bounded route and acquisition facts. Search clicks are not sessions; GA4 sessions are not Umami visits.</p></div><span class="coverage-badge{" partial" if payload["truncated"] else ""}">{payload["displayed_rows"]} of {payload["total_rows"]} rows</span></section>
<section class="panel control-panel"><div class="panel-heading"><div><h2>Bounded filters</h2><p>Filters never trigger provider collection or alter stored facts.</p></div></div>
<form class="filter-form" method="get" action="/route-observations"><input type="hidden" name="report" value="{_e(filters["report"])}">
<label class="field"><span>Start</span><input type="date" name="start" value="{_e(payload["window"]["start"][:10])}"></label>
<label class="field"><span>End (exclusive)</span><input type="date" name="end" value="{_e(payload["window"]["end"][:10])}"></label>
<label class="field"><span>Site</span><select name="site">{site_options}</select></label>
<label class="field"><span>Provider</span><select name="source">{source_options}</select></label>
<label class="field"><span>Metric</span><select name="metric">{metric_options}</select></label>
<label class="field"><span>Exact route</span><input name="route" value="{_e(filters["route"])}" placeholder="/services/"></label>
<button type="submit">Apply filters</button></form></section>
<aside class="alerts" aria-label="Interpretation notice"><div class="alert"><span class="alert-mark">i</span><div><strong>Provider semantics remain separate</strong><br>No visitor or session identifiers, raw queries, or full external referrer URLs are exposed. Search Console completeness remains provider-limited and its provider-date basis is disclosed per row.</div></div></aside>
<section class="panel table-panel"><div class="panel-heading"><div><h2>Accepted observations</h2><p>Complete matching-row total: {payload["total_rows"]}. Display is bounded to {payload["limit"]}; export uses the same bounded, sanitized rows.</p></div><a href="{_e("/api/v1/route-observations.csv?" + urlencode(query))}">Download CSV</a></div>
<div class="table-scroll"><table><caption class="sr-only">Provider-separated route observations with coverage, freshness, and limitations</caption>
<thead><tr><th>Site</th><th>Source</th><th>Metric</th><th>Route</th><th>Dimensions</th><th>Value</th><th>Coverage</th><th>State</th><th>Freshness</th><th>Provider limitation</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<footer class="footer"><span>Read-only compatibility view</span><span>No provider sync, raw query, identifier, or full external referrer data</span></footer></main></body></html>"""


def handler_factory(config, store, credentials=None):
    reports = ReportService(config, store)
    geography = GeographyService(config, store)
    graph_reports = SiteGraphDisplayReportService(SiteGraphStore(store.path))
    credential_provider = credentials or ReferenceCredentialProvider()
    password = None
    if config.web.auth_mode == "basic":
        with credential_provider.acquire(config.web.auth_credential_ref) as lease:
            password = require_text(lease, "password", "value")

    class Handler(BaseHTTPRequestHandler):
        server_version = "BohoAnalytics"
        sys_version = ""

        def log_message(self, format, *args):
            # Paths may contain report IDs only; query strings are deliberately omitted.
            print(f"web {self.command} {urlsplit(self.path).path} {args[1] if len(args) > 1 else '-'}")

        def end_headers(self):
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            super().end_headers()

        def _allowed(self):
            host = self.headers.get("Host", "").split(":", 1)[0].casefold()
            if host not in {item.casefold() for item in config.web.allowed_hosts}:
                self.send_error(400, "Invalid Host")
                return False
            if password is not None:
                auth = self.headers.get("Authorization", "")
                expected = "Basic " + base64.b64encode(f"{config.web.username}:{password}".encode()).decode()
                if not hmac.compare_digest(auth, expected):
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="analytics"')
                    self.end_headers()
                    return False
            return True

        def _send(self, status, content_type, body, extra_headers=None):
            raw = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def _send_chunks(self, status, content_type, first_chunk, chunks, extra_headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(first_chunk.encode("utf-8"))
            for chunk in chunks:
                self.wfile.write(chunk.encode("utf-8"))

        def do_GET(self):
            if not self._allowed():
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                return self._send(
                    200,
                    "application/json",
                    json.dumps({"ok": True, **build_identity()}, sort_keys=True, separators=(",", ":")),
                )
            if parsed.path == "/assets/app.css":
                return self._send(200, "text/css; charset=utf-8", CSS)
            if parsed.path == "/assets/app.js":
                return self._send(200, "text/javascript; charset=utf-8", JS)
            if parsed.path == "/assets/maps/world-countries.geojson":
                body = files("boho_analytics_platform").joinpath(
                    "static/natural-earth-countries-110m.geojson").read_text(encoding="utf-8")
                return self._send(200, "application/geo+json; charset=utf-8", body)
            if parsed.path == "/assets/maps/us-counties.json":
                body = files("boho_analytics_platform").joinpath(
                    "static/us-counties-albers-10m.json").read_text(encoding="utf-8")
                return self._send(200, "application/json; charset=utf-8", body)
            if parsed.path == "/favicon.svg":
                return self._send(200, "image/svg+xml", FAVICON_SVG)
            try:
                query = parse_qs(parsed.query, keep_blank_values=True)
                analytics_fields = {
                    "report", "subreport", "start", "end", "site", "metric",
                    "source", "style", "compare", "view",
                }
                report_fields = {"report", "subreport", "start", "end", "site"}
                geography_fields = {"report", "start", "end", "site", "source"}
                graph_fields = {
                    "site", "page", "graph", "layer", "edge_query", "edge_sort",
                    "edge_order", "edge_page",
                }
                route_observation_fields = {
                    "report", "start", "end", "site", "source", "metric", "route",
                }
                if parsed.path == "/":
                    allowed = analytics_fields
                    repeatable = set()
                elif parsed.path in {"/api/v1/report", "/api/v1/report.csv"}:
                    allowed = report_fields
                    repeatable = set()
                elif parsed.path in {"/api/v1/series", "/api/v1/series.csv"}:
                    allowed = analytics_fields
                    repeatable = set()
                elif parsed.path == "/api/v1/geography":
                    allowed = geography_fields
                    repeatable = set()
                elif parsed.path in {
                    "/site-graph", "/api/v1/site-graph", "/api/v1/site-graph.csv"
                }:
                    allowed = graph_fields
                    repeatable = {"layer"}
                elif parsed.path in {
                    "/route-observations", "/api/v1/route-observations",
                    "/api/v1/route-observations.csv",
                }:
                    allowed = route_observation_fields
                    repeatable = set()
                else:
                    allowed = set()
                    repeatable = set()
                if set(query) - allowed:
                    raise ValueError("unknown query field")
                if any(len(values) != 1 for key, values in query.items() if key not in repeatable):
                    raise ValueError("duplicate query field")
                nonblank_fields = (
                    ("start", "end", "site")
                    if parsed.path in {
                        "/route-observations", "/api/v1/route-observations",
                        "/api/v1/route-observations.csv",
                    }
                    else ("start", "end", "site", "metric", "source")
                )
                if any(
                    value == ""
                    for field in nonblank_fields
                    for value in query.get(field, ())
                ):
                    raise ValueError("blank query value")
                if parsed.path == "/":
                    return self._dashboard(query)
                if parsed.path == "/site-graph":
                    return self._site_graph(query)
                if parsed.path == "/api/v1/site-graph":
                    return self._site_graph_api(query)
                if parsed.path == "/api/v1/site-graph.csv":
                    return self._site_graph_csv(query)
                if parsed.path == "/route-observations":
                    return self._route_observations(query)
                if parsed.path == "/api/v1/route-observations":
                    return self._route_observations_api(query)
                if parsed.path == "/api/v1/route-observations.csv":
                    return self._route_observations_csv(query)
                if parsed.path == "/api/v1/geography":
                    return self._geography_api(query)
                if parsed.path in {
                    "/api/v1/report", "/api/v1/report.csv", "/api/v1/series", "/api/v1/series.csv"
                }:
                    return self._api(parsed.path, query)
                self.send_error(404)
            except (ValueError, KeyError):
                message = "invalid site graph request" if parsed.path in {
                    "/site-graph", "/api/v1/site-graph", "/api/v1/site-graph.csv"
                } else "invalid route observation request" if parsed.path in {
                    "/route-observations", "/api/v1/route-observations",
                    "/api/v1/route-observations.csv",
                } else "invalid report request"
                self._send(400, "application/json", json.dumps({"error": message}, sort_keys=True))

        def _route_observation_payload(self, query):
            report_id = query.get("report", [config.reports[0].id])[0]
            report = next((item for item in config.reports if item.id == report_id), None)
            if report is None:
                raise ValueError("unknown report")
            site_id = query.get("site", [report.site_ids[0]])[0]
            if site_id not in report.site_ids:
                raise ValueError("unknown route observation site")
            source = query.get("source", [""])[0]
            metric = query.get("metric", [""])[0]
            route = query.get("route", [""])[0].strip()
            available_sources = sorted({
                METRICS[item].source for item in ROUTE_OBSERVATION_METRICS
            })
            if source and source not in available_sources:
                raise ValueError("unknown route observation source")
            if metric and metric not in ROUTE_OBSERVATION_METRICS:
                raise ValueError("unknown route observation metric")
            if metric and source and METRICS[metric].source != source:
                raise ValueError("metric does not belong to selected source")
            if len(route) > 2_000 or "\n" in route or "\r" in route:
                raise ValueError("invalid route observation route")
            window = _window(
                query,
                config.platform.default_timezone,
                report.default_window_days,
                report.default_end_lag_days,
            )
            if (window.end - window.start).days > ROUTE_OBSERVATION_MAX_DAYS:
                raise ValueError("route observation window exceeds the bounded limit")
            metric_ids = tuple(
                item for item in ROUTE_OBSERVATION_METRICS
                if (not source or METRICS[item].source == source)
                and (not metric or item == metric)
            )
            points = store.query(
                client_id=report.client_id,
                site_ids=(site_id,),
                metric_ids=metric_ids,
                window=window,
            )
            rows = []
            for point in points:
                dimension_names = {key for key, _value in point.dimensions}
                if dimension_names & {"query", "query_cluster"}:
                    continue
                dimensions = {
                    key: value for key, value in point.dimensions
                    if key in ROUTE_OBSERVATION_DIMENSIONS
                }
                point_route = dimensions.get("route") or dimensions.get("referrer_route")
                if route and point_route != route:
                    continue
                semantics = SOURCE_SEMANTICS.get(point.source)
                data_state = dimensions.get(
                    "data_state",
                    semantics.data_state if semantics else point.completeness.value,
                )
                limitation = {
                    "search-console": (
                        "Provider-limited Search Console rows; newest dates can be provisional "
                        "and the provider-date basis differs from the site day."
                    ),
                    "google-analytics": (
                        "GA4 provider-reported aggregates; sessions and engagement remain GA4-only."
                    ),
                    "umami": (
                        "Umami provider-reported aggregates; visits remain separate from GA4 sessions."
                    ),
                }.get(point.source, "Provider-reported aggregate.")
                rows.append({
                    "site_id": point.site_id,
                    "source": point.source,
                    "metric": point.metric,
                    "unit": point.unit,
                    "value": str(point.value),
                    "route": point_route,
                    "dimensions": dict(sorted(dimensions.items())),
                    "window": {
                        "start": point.start.isoformat(),
                        "end": point.end.isoformat(),
                    },
                    "coverage": point.completeness.value,
                    "freshness": point.observed_at.isoformat(),
                    "data_state": data_state,
                    "provider_time_basis": semantics.time_basis if semantics else "unknown",
                    "provider_limitation": limitation,
                })
            rows.sort(key=lambda item: (
                item["source"], item["metric"], item["route"] or "",
                tuple(item["dimensions"].items()), item["window"]["start"],
            ))
            total_rows = len(rows)
            return {
                "schema_version": 1,
                "read_only": True,
                "provider_aggregation": "separate",
                "window": {
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                    "timezone": window.timezone,
                },
                "filters": {
                    "report": report_id,
                    "site": site_id,
                    "source": source,
                    "metric": metric,
                    "route": route,
                },
                "available_sites": list(report.site_ids),
                "available_sources": available_sources,
                "available_metrics": list(ROUTE_OBSERVATION_METRICS),
                "total_rows": total_rows,
                "displayed_rows": min(total_rows, ROUTE_OBSERVATION_LIMIT),
                "limit": ROUTE_OBSERVATION_LIMIT,
                "truncated": total_rows > ROUTE_OBSERVATION_LIMIT,
                "complete_totals": True,
                "rows": rows[:ROUTE_OBSERVATION_LIMIT],
                "privacy": {
                    "visitor_or_session_identifiers": False,
                    "raw_queries": False,
                    "full_external_referrer_urls": False,
                },
            }

        def _route_observations(self, query):
            return self._send(
                200, "text/html; charset=utf-8",
                _route_observation_html(self._route_observation_payload(query)),
            )

        def _route_observations_api(self, query):
            return self._send(
                200, "application/json",
                json.dumps(self._route_observation_payload(query), sort_keys=True),
            )

        def _route_observations_csv(self, query):
            return self._send(
                200, "text/csv; charset=utf-8",
                _route_observation_csv(self._route_observation_payload(query)),
                {"Content-Disposition": 'attachment; filename="route-observations.csv"'},
            )

        @staticmethod
        def _graph_layers(query):
            layers = tuple(query.get("layer", []))
            if "all" in layers:
                return SITE_GRAPH_LAYERS
            return layers or ("contextual", "related", "action")

        def _site_graph_payload(self, query):
            site_key = query.get("site", [None])[0]
            known_sites = {item["key"] for item in graph_reports.sites()}
            if site_key is not None and site_key not in known_sites:
                raise ValueError("unknown site graph site")
            selected_page = query.get("page", [None])[0]
            try:
                edge_page = int(query.get("edge_page", ["1"])[0])
            except (TypeError, ValueError) as error:
                raise ValueError("invalid edge page") from error
            payload = graph_reports.summary(
                site_key=site_key,
                selected_page=selected_page,
                layers=self._graph_layers(query),
                graph_mode=query.get("graph", ["auto"])[0],
                edge_query=query.get("edge_query", [""])[0],
                edge_sort=query.get("edge_sort", ["source"])[0],
                edge_order=query.get("edge_order", ["asc"])[0],
                edge_page=edge_page,
            )
            payload["evidence_core21"] = site_graph_core21_projection(
                graph_reports.store, payload, tuple(payload.get("selected_layers", ()))
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
            return payload

        def _site_graph_api(self, query):
            return self._send(200, "application/json", json.dumps(self._site_graph_payload(query), sort_keys=True))

        def _site_graph_csv(self, query):
            chunks = graph_reports.iter_edge_csv(
                site_key=query.get("site", [None])[0],
                layers=self._graph_layers(query),
                edge_query=query.get("edge_query", [""])[0],
                edge_sort=query.get("edge_sort", ["source"])[0],
                edge_order=query.get("edge_order", ["asc"])[0],
            )
            first_chunk = next(chunks)
            return self._send_chunks(
                200,
                "text/csv; charset=utf-8",
                first_chunk,
                chunks,
                {"Content-Disposition": 'attachment; filename="site-graph-edges.csv"'},
            )

        def _site_graph(self, query):
            payload = self._site_graph_payload(query)
            if payload["empty"]:
                site_links = "".join(
                    f'<a href="{_e("/site-graph?" + urlencode({"site": site["key"]}))}">{_e(site["key"])}</a>'
                    for site in payload["sites"]
                )
                page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Site Graph - Boho Analytics</title><link rel="icon" href="/favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="/assets/app.css"></head><body><a class="skip-link" href="#main">Skip to graph dashboard</a>
<header class="topbar"><div class="topbar-inner"><div class="brand"><span class="brand-mark">BA</span><div><strong>Boho Analytics</strong><span>Private portfolio command center</span></div></div><div class="live-state">Read-only structural evidence</div></div></header>
<main class="shell" id="main"><div class="report-nav" aria-label="Dashboard areas"><a href="/">Analytics</a><a class="active" href="/site-graph">Site Graph</a><a href="/route-observations">Route observations</a>{site_links}</div>
<section class="panel graph-empty"><h1>Site Graph</h1><h2>No compiled snapshot yet</h2><p>{_e(payload["notice"])} Compile an authorized repository snapshot from the command line; browser requests cannot ingest, build, or compile sites.</p><p>Active projection: contextual. Selected layers: {_e(", ".join(payload["display"]["layers"]))}. Total nodes: 0; total unique edges: 0; total link occurrences: 0.</p></section></main></body></html>"""
                return self._send(200, "text/html; charset=utf-8", page)

            site_options = "".join(
                f'<option value="{_e(site["key"])}"{" selected" if site["key"] == payload["site"]["key"] else ""}>{_e(site["key"])}</option>'
                for site in payload["sites"]
            )
            selected_page = payload["neighborhood"]["selected_page"] or ""
            selected_layers = set(payload["selected_layers"])
            requested_graph_mode = payload["display"]["requested_graph_mode"]
            layer_controls = "".join(
                f'<label><input type="checkbox" name="layer" value="{layer}"{" checked" if layer in selected_layers else ""}>{layer.title()}</label>'
                for layer in SITE_GRAPH_LAYERS
            )
            overview = payload["overview"]
            cards = (
                ("Pages", payload["display"]["total_nodes"], "Complete repository page facts"),
                ("Unique edges", payload["display"]["total_unique_edges"], "Aggregated by source, destination, and layer"),
                ("Link occurrences", payload["display"]["total_occurrences"], "Selected internal crawlable relationship facts"),
                ("Unresolved", payload["display"]["unresolved_relationships"], "Unique selected relationships without a known destination page"),
            )
            cards_html = "".join(
                f'<article class="kpi-card"><div class="kpi-top"><span class="kpi-label">{_e(label)}</span></div><strong class="kpi-value">{value}</strong><p class="kpi-note">{_e(note)}</p></article>'
                for label, value, note in cards
            )
            bucket_html = "".join(
                f'<div class="distance-item"><b>{payload["goal_distance_buckets"][key]}</b><span>{_e(key.title())}</span></div>'
                for key in ("goal", "1", "2", "3", "4+", "menu-only", "unreachable")
            )
            component_rows = "".join(
                f'<tr><td>{_e(component["key"])}</td><td>{len(component["nodes"])}</td><td>{component["internal_edges"]}</td><td>{_e(", ".join(component["nodes"][:8]))}</td></tr>'
                for component in payload["components"]
            ) or '<tr><td colspan="4">No components available.</td></tr>'
            finding_rows = "".join(
                f'<tr><td>{_e(item["type"].replace("_", " ").title())}</td><td>{_e(item["severity"].title())}</td><td>{_e(", ".join(item["nodes"]))}</td></tr>'
                for item in payload["finding_details"]
            ) or '<tr><td colspan="3">No structural findings in this projection.</td></tr>'
            revision = payload["revision"][:12]
            graph_table = _site_graph_table(payload)
            graph_disclosure = _site_graph_disclosure(payload)
            complete_edge_table = _complete_site_graph_edge_table(payload)
            analysis_panels = _site_graph_analysis_panels(payload)
            view_links = (
                '<nav class="quick-links" aria-label="Site Graph analysis views">'
                '<a href="#site-graph-pages">Pages</a><a href="#site-graph-matrix">Matrix</a>'
                '<a href="#site-graph-resilience">Resilience</a><a href="#site-graph-entry-goal">Entry-to-goal</a>'
                '<a href="#site-graph-snapshot">Snapshot</a><a href="#site-graph-evidence">Evidence</a></nav>'
            )
            graph_mode_options = "".join(
                f'<option value="{value}"{" selected" if requested_graph_mode == value else ""}>{label}</option>'
                for value, label in (("auto", "Auto: full when safe"), ("full", "Request full graph"), ("bounded", "Bounded graph"))
            )
            page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Site Graph - Boho Analytics</title><link rel="icon" href="/favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="/assets/app.css"><script src="/assets/app.js" defer></script></head>
<body><a class="skip-link" href="#main">Skip to graph dashboard</a>
<header class="topbar"><div class="topbar-inner"><div class="brand"><span class="brand-mark">BA</span><div><strong>Boho Analytics</strong><span>Private portfolio command center</span></div></div><div class="live-state"><span class="live-dot"></span>Read-only structural evidence</div></div></header>
<main class="shell" id="main"><div class="report-nav" aria-label="Dashboard areas"><a href="/">Analytics</a><a class="active" href="/site-graph">Site Graph</a><a href="/route-observations">Route observations</a></div>
<section class="hero"><div><p class="eyebrow">Revision {_e(revision)} - Snapshot {_e(payload["snapshot"]["captured_at"][:10])}</p><h1>Site Graph</h1><p class="hero-copy">Inspect internal-link structure with exact completeness disclosures, a safe full-graph mode for small sites, bounded rendering for larger sites, and a complete paginated evidence table.</p></div><span class="coverage-badge">{payload["coverage"]["pages"]} pages covered</span></section>
<div class="graph-meta"><span>Site {_e(payload["site"]["display_name"])}</span><span>Manifest {_e(payload["manifest_hash"][:12])}</span><span>{payload["snapshot"]["count"]} contextual snapshot(s)</span><span>{"Clean repository" if payload["snapshot"]["clean"] else "Dirty override snapshot"}</span></div>
{view_links}
<section class="panel control-panel"><div class="panel-heading"><div><h2>Graph controls</h2><p>Contextual, related, and action links are shown by default. Browser controls are read-only.</p></div></div>
<form class="filter-form graph-form" method="get" action="/site-graph"><label class="field"><span>Site</span><select name="site">{site_options}</select></label><label class="field"><span>Neighborhood page route</span><input name="page" value="{_e(selected_page)}" placeholder="Structural overview"></label><label class="field"><span>Graph mode</span><select name="graph">{graph_mode_options}</select></label><fieldset class="field"><legend>Link layers</legend><div class="layer-picker">{layer_controls}</div></fieldset><button type="submit">Update graph</button></form></section>
<aside class="alerts" aria-label="Interpretation notice"><div class="alert"><span class="alert-mark">i</span><div><strong>Structural evidence</strong><br>{_e(payload["structural_evidence_notice"])}</div></div></aside>
<section class="kpi-grid" aria-label="Graph overview">{cards_html}</section>
<section class="panel chart-panel"><div class="panel-heading"><div><h2>{'Two-hop page neighborhood' if payload["neighborhood"]["selected_page"] else 'Full structural overview' if payload["display"]["graph_mode"] == 'full' else 'Bounded structural overview'}</h2><p>Active projection: contextual; layers: {_e(", ".join(payload["selected_layers"]))}; unique edges aggregate matching occurrences by source, destination, and layer.</p></div><span class="source-chip">{_e(payload["display"]["graph_mode"])} mode</span></div>{graph_disclosure}{_site_graph_svg(payload)}<p class="graph-caption">Arrows represent aggregated, stored, crawlable internal relationships in the selected layers. Node color marks goals, unreachable pages, and the selected page.</p>{graph_table}</section>
{analysis_panels}
{complete_edge_table}
<section class="panel section-panel"><div class="panel-heading"><div><h2>Goal distance</h2><p>Shortest structural path to a configured goal in the compiled contextual projection.</p></div></div><div class="distance-grid">{bucket_html}</div></section>
<div class="split-grid"><section class="panel table-panel"><div class="panel-heading"><div><h2>Strongly connected components</h2><p>Deterministic Kosaraju components; these are structural groups, not audience segments.</p></div></div><div class="table-scroll"><table><thead><tr><th>Component</th><th>Pages</th><th>Internal edges</th><th>Members</th></tr></thead><tbody>{component_rows}</tbody></table></div></section>
<section class="panel table-panel"><div class="panel-heading"><div><h2>Findings</h2><p>Evidence-linked structural review items for this snapshot.</p></div></div><div class="table-scroll"><table><thead><tr><th>Finding</th><th>Severity</th><th>Pages</th></tr></thead><tbody>{finding_rows}</tbody></table></div></section></div>
<footer class="footer"><span>Captured {_e(payload["snapshot"]["captured_at"])}</span><span>Read-only - loopback-first - no browser ingest, build, compile, or provider sync</span></footer></main></body></html>"""
            return self._send(200, "text/html; charset=utf-8", page)

        def _request(
            self, query, *, force_overview=False,
            include_decision_support=True,
            include_provider_comparisons=True,
        ):
            report_id = query.get("report", [config.reports[0].id])[0]
            report = next((item for item in config.reports if item.id == report_id), None)
            if report is None:
                raise ValueError("unknown report")
            subreport_id = None if force_overview else query.get("subreport", [None])[0]
            default_days = report.default_window_days
            default_end_lag_days = report.default_end_lag_days
            if subreport_id:
                subreport = next((item for item in report.subreports if item.id == subreport_id), None)
                if subreport is None:
                    raise ValueError("unknown subreport")
                default_days = subreport.default_window_days
                default_end_lag_days = subreport.default_end_lag_days
            site_id = query.get("site", [None])[0]
            if site_id == "all":
                site_id = None
            window = _window(
                query,
                config.platform.default_timezone,
                default_days,
                default_end_lag_days,
            )
            return reports.render(
                report_id, window, subreport_id, site_id,
                include_decision_support=include_decision_support,
                include_provider_comparisons=include_provider_comparisons,
            ), report

        def _geography_payload(self, query):
            report_id = query.get("report", [config.reports[0].id])[0]
            report = next((item for item in config.reports if item.id == report_id), None)
            if report is None:
                raise ValueError("unknown report")
            source = query.get("source", ["umami"])[0]
            site_id = query.get("site", [None])[0]
            if site_id == "all":
                site_id = None
            window = _window(
                query, config.platform.default_timezone,
                report.default_window_days, report.default_end_lag_days,
            )
            return geography.render(report_id, window, source, site_id=site_id)

        def _geography_api(self, query):
            return self._send(
                200, "application/json",
                json.dumps(self._geography_payload(query), sort_keys=True, separators=(",", ":")),
            )

        def _series_payload(self, query, *, rendered=None):
            view = query.get("view", [""])[0]
            if view not in {"", "plot"}:
                raise ValueError("invalid series view")
            is_plot = view == "plot"
            report, definition = rendered or self._request(
                query,
                force_overview=is_plot,
                include_decision_support=False,
                include_provider_comparisons=False,
            )
            candidates = tuple(
                metric for metric in (definition.metric_ids if is_plot else self._active_metrics(definition, report))
                if metric in METRICS and METRICS[metric].aggregation != "window"
            )
            requested_metric = query.get("metric", [None])[0]
            if requested_metric is not None and requested_metric not in candidates:
                raise ValueError("invalid series metric")
            metric = requested_metric if requested_metric in candidates else next(
                (item for item in CHART_PRIORITY if item in candidates), candidates[0] if candidates else ""
            )
            requested_source = query.get("source", [None])[0]
            source = requested_source or (METRICS[metric].source if metric else "")
            if source not in SOURCE_LABELS or (metric and METRICS[metric].source != source):
                raise ValueError("invalid series source")
            available_sites = _available_sites_by_source(config, definition)
            supported_sites = available_sites.get(source, set())
            if report["site_id"] is not None and report["site_id"] not in supported_sites:
                raise ValueError("selected site is not configured for this series source")
            style = query.get("style", ["line" if is_plot else "area"])[0]
            if style not in {"line", "area", "bar"}:
                raise ValueError("invalid chart style")
            compare = _compare_flag(query)

            def selected(items):
                return [
                    item for item in items
                    if item["metric"] == metric and item["source"] in {source, "fixture"}
                ]

            raw_current = selected(report["series"])
            raw_previous = selected(report["comparison_series"]) if compare else []
            current_coverage = _scoped_coverage(
                report.get("coverage", {}),
                source=source,
                metric=metric,
                site_id=report["site_id"],
            )
            prior_coverage = _scoped_coverage(
                report.get("comparison", {}).get("coverage", {}),
                source=source,
                metric=metric,
                site_id=report["site_id"],
            )
            source_health = [
                item for item in report.get("source_health", [])
                if item.get("metric_source") == source
                and (report["site_id"] is None or item["site_id"] == report["site_id"])
            ]
            prior_source_health = [
                item for item in report.get("comparison_source_health", [])
                if item.get("metric_source") == source
                and (report["site_id"] is None or item["site_id"] == report["site_id"])
            ]
            current = _fill_query_proven_zero_series(
                raw_current,
                current_coverage,
                source_health,
                metric=metric,
                window=report["window"],
            )
            candidate_previous = _fill_query_proven_zero_series(
                raw_previous,
                prior_coverage,
                prior_source_health,
                metric=metric,
                window=report["comparison_window"],
            ) if compare else []
            comparison_available = bool(
                compare
                and current
                and candidate_previous
                and current_coverage["status"] == "complete"
                and prior_coverage["status"] == "complete"
            )
            previous = candidate_previous if comparison_available else []
            comparison_status = (
                "not_requested" if not compare
                else "available" if comparison_available
                else "unavailable"
            )
            warnings = []
            if not raw_current and current and current_coverage["status"] == "complete":
                warnings.append(
                    "The provider query completed for this selection; displayed zeroes are query-proven quiet dates."
                )
            elif not current:
                warnings.append("No stored daily values match this plot selection.")
            elif current_coverage["status"] != "complete":
                warnings.append(
                    "This plotted metric has partial stored coverage; displayed values are observed only."
                )
            if compare and not comparison_available:
                warnings.append(
                    "The requested comparison is unavailable because the prior period lacks complete stored coverage.",
                )
            return {
                "schema_version": 2,
                "report_id": report["report_id"],
                "subreport_id": report["subreport_id"],
                "site_id": report["site_id"],
                "source": source,
                "source_label": _source_label(source),
                "metric": metric,
                "metric_label": _metric_label(metric) if metric else "Daily series",
                "style": style,
                "compare": compare,
                "comparison_available": comparison_available,
                "comparison_status": comparison_status,
                "window": report["window"],
                "comparison_window": report["comparison_window"],
                "series": current,
                "comparison_series": previous,
                "generated_at": report.get("generated_at"),
                "coverage": current_coverage,
                "comparison": {
                    "status": comparison_status,
                    "available": comparison_available,
                    "coverage": prior_coverage,
                },
                "source_health": source_health,
                "comparison_source_health": prior_source_health,
                "summary_totals": {
                    metric: report.get("summary_totals", {}).get(metric, {})
                },
                "site_names": {
                    site.id: site.name for site in config.sites
                    if site.id in supported_sites
                    and (report["site_id"] is None or site.id == report["site_id"])
                },
                "warnings": warnings,
                "complete": current_coverage["status"] == "complete",
            }

        @staticmethod
        def _active_metrics(definition, report):
            if not report["subreport_id"]:
                return definition.metric_ids
            subreport = next(item for item in definition.subreports if item.id == report["subreport_id"])
            return subreport.metric_ids

        def _api(self, path, query):
            if path in {"/api/v1/series", "/api/v1/series.csv"}:
                payload = self._series_payload(query)
                if path.endswith(".csv"):
                    scope = payload.get("site_id") or "all-sites"
                    section = payload.get("subreport_id") or "overview"
                    comparison = "-comparison" if payload["compare"] else ""
                    filename = f'{payload["report_id"]}-{section}-{scope}-{payload["metric"]}{comparison}-{payload["window"]["start"][:10]}-{payload["window"]["end"][:10]}.csv'
                    return self._send(
                        200,
                        "text/csv; charset=utf-8",
                        to_series_csv(payload, include_comparison=payload["compare"]),
                        {"Content-Disposition": f'attachment; filename="{filename}"'},
                    )
                return self._send(200, "application/json", json.dumps(payload, sort_keys=True))
            report, _definition = self._request(query)
            if path.endswith(".csv"):
                scope = report.get("site_id") or "all-sites"
                section = report.get("subreport_id") or "overview"
                filename = f'{report["report_id"]}-{section}-{scope}-{report["window"]["start"][:10]}-{report["window"]["end"][:10]}.csv'
                return self._send(
                    200,
                    "text/csv; charset=utf-8",
                    to_csv(report),
                    {"Content-Disposition": f'attachment; filename="{filename}"'},
                )
            return self._send(200, "application/json", json.dumps(report, sort_keys=True))

        def _dashboard(self, query):
            view = query.get("view", ["dashboard"])[0]
            if view not in {"dashboard", "plot"}:
                raise ValueError("invalid view")
            is_plot = view == "plot"
            result, report = self._request(
                query,
                force_overview=is_plot,
                include_decision_support=not is_plot,
                include_provider_comparisons=not is_plot,
            )
            start = result["window"]["start"][:10]
            end = result["window"]["end"][:10]
            site_names = {site.id: site.name for site in config.sites}
            active_definition = next(
                (item for item in report.subreports if item.id == result["subreport_id"]),
                None,
            )
            expected_metrics = report.metric_ids if is_plot else (
                active_definition.metric_ids if active_definition else report.metric_ids
            )
            available_metrics = tuple(
                metric for metric in expected_metrics
                if metric in METRICS and METRICS[metric].aggregation != "window"
            )
            requested_metric = query.get("metric", [None])[0]
            available_sites = _available_sites_by_source(config, report)
            represented_sources = tuple(
                source for source in SOURCE_LABELS
                if available_sites.get(source)
                and any(METRICS[metric].source == source for metric in available_metrics)
            )
            requested_source = query.get("source", [None])[0]
            if requested_metric is not None and requested_metric not in available_metrics:
                raise ValueError("invalid dashboard metric")
            if requested_source is not None and requested_source not in represented_sources:
                raise ValueError("invalid dashboard source")
            if (
                requested_metric is not None
                and requested_source is not None
                and METRICS[requested_metric].source != requested_source
            ):
                raise ValueError("dashboard metric does not belong to source")
            inferred_source = METRICS[requested_metric].source if requested_metric in available_metrics else None
            selected_source = requested_source if requested_source in represented_sources else (
                inferred_source or (represented_sources[0] if represented_sources else "")
            )
            source_metrics = tuple(metric for metric in available_metrics if METRICS[metric].source == selected_source)
            selected_metric = requested_metric if requested_metric in source_metrics else next(
                (metric for metric in CHART_PRIORITY if metric in source_metrics),
                source_metrics[0] if source_metrics else "",
            )
            style = query.get("style", ["line" if is_plot else "area"])[0]
            if style not in {"line", "area", "bar"}:
                raise ValueError("invalid chart style")
            compare = _compare_flag(query)
            relevant_sources = {METRICS[metric].source for metric in expected_metrics}
            site_option_sources = set(represented_sources) if is_plot else relevant_sources
            form_site_ids = tuple(
                site_id for site_id in report.site_ids
                if any(site_id in available_sites.get(source, set()) for source in site_option_sources)
            )
            selected_site_supported = result["site_id"] is None or result["site_id"] in (
                available_sites.get(selected_source, set())
                if selected_source else set(form_site_ids)
            )
            if not selected_site_supported:
                raise ValueError("selected site is not configured for this dashboard metric source")

            if is_plot:
                effective_query = {key: list(values) for key, values in query.items()}
                effective_query["view"] = ["plot"]
                effective_query["source"] = [selected_source]
                effective_query["metric"] = [selected_metric]
                effective_query["style"] = [style]
                plot_payload = self._series_payload(
                    effective_query, rendered=(result, report)
                )
                result = {
                    **result,
                    "coverage": plot_payload["coverage"],
                    "comparison": plot_payload["comparison"],
                    "source_health": plot_payload["source_health"],
                    "comparison_source_health": plot_payload["comparison_source_health"],
                    "summary_totals": plot_payload["summary_totals"],
                    "warnings": plot_payload["warnings"],
                    "complete": plot_payload["complete"],
                }

            def route(path="/", **overrides):
                params = {"report": report.id, "start": start, "end": end}
                if is_plot:
                    params["view"] = "plot"
                    params["source"] = selected_source
                    params["style"] = style
                    if compare:
                        params["compare"] = "1"
                elif result["subreport_id"]:
                    params["subreport"] = result["subreport_id"]
                if result["site_id"]:
                    params["site"] = result["site_id"]
                if selected_metric:
                    params["metric"] = selected_metric
                for key, value in overrides.items():
                    if value in {None, ""}:
                        params.pop(key, None)
                    else:
                        params[key] = value
                return path + "?" + urlencode(params)

            report_nav = "".join(
                f'<a class="{"active" if not is_plot and item.id == report.id else ""}" href="{_e("/?" + urlencode({"report": item.id, "start": start, "end": end}))}">{_e(item.title)}</a>'
                for item in config.reports
            )
            plot_url = "/?" + urlencode({"report": report.id, "view": "plot", "start": start, "end": end})
            report_nav += f'<a class="plot-mode {"active" if is_plot else ""}" href="{_e(plot_url)}">Plot Builder</a>'
            report_nav += '<a href="/site-graph">Site Graph</a>'
            report_nav += '<a href="/route-observations">Route observations</a>'
            if is_plot:
                source_defaults = {
                    source: next(
                        (metric for metric in CHART_PRIORITY if metric in available_metrics and METRICS[metric].source == source),
                        next(metric for metric in available_metrics if METRICS[metric].source == source),
                    )
                    for source in represented_sources
                }
                subnav = "".join(
                    f'<a class="{"active" if source == selected_source else ""}" href="{_e(route(source=source, metric=source_defaults[source]))}">{_e(_source_label(source))}</a>'
                    for source in represented_sources
                )
            else:
                overview_metric = next(
                    (metric for metric in CHART_PRIORITY if metric in report.metric_ids and METRICS[metric].aggregation != "window"),
                    next((metric for metric in report.metric_ids if METRICS[metric].aggregation != "window"), None),
                )
                subnav = f'<a class="{"active" if not result["subreport_id"] else ""}" href="{_e(route(subreport=None, metric=overview_metric))}">Overview</a>'
                subnav += "".join(
                    f'<a class="{"active" if item.id == result["subreport_id"] else ""}" href="{_e(route(subreport=item.id, metric=next((metric for metric in CHART_PRIORITY if metric in item.metric_ids and METRICS[metric].aggregation != "window"), next((metric for metric in item.metric_ids if METRICS[metric].aggregation != "window"), None))))}">{_e(item.title)}</a>'
                    for item in report.subreports
                )
            all_selected = " selected" if result["site_id"] is None else ""
            site_options = f'<option value="all"{all_selected}>All sites</option>' + "".join(
                (
                    f'<option value="{_e(site_id)}" data-sources="{_e(",".join(sorted(source for source in represented_sources if site_id in available_sites.get(source, set()))))}"'
                    f'{" selected" if site_id == result["site_id"] else ""}'
                    f'{"" if not selected_source or site_id in available_sites.get(selected_source, set()) else " hidden disabled"}>'
                    f'{_e(site_names.get(site_id, site_id))}</option>'
                )
                for site_id in form_site_ids
            )
            metric_order = [metric for metric in CHART_PRIORITY if metric in available_metrics]
            metric_order += sorted(set(available_metrics) - set(metric_order))
            metric_options = "".join(
                f'<option value="{_e(metric)}" data-source="{_e(METRICS[metric].source)}"{" selected" if metric == selected_metric else ""}>{_e(_metric_label(metric))}</option>'
                for metric in metric_order
            ) or '<option value="">No daily series</option>'
            source_options = "".join(
                f'<option value="{_e(source)}"{" selected" if source == selected_source else ""}>{_e(_source_label(source))}</option>'
                for source in represented_sources
            )
            hidden_subreport = (
                f'<input type="hidden" name="subreport" value="{_e(result["subreport_id"])}">'
                if result["subreport_id"] else ""
            )

            end_date = datetime.fromisoformat(end).date()
            preset_links = []
            for days in (7, 30, 90, 365):
                try:
                    candidate = end_date - timedelta(days=days)
                except OverflowError:
                    continue
                if days >= candidate.toordinal():
                    continue
                preset_links.append(
                    f'<a href="{_e(route(start=candidate.isoformat()))}">{days} days</a>'
                )
            presets = "".join(preset_links)
            export_params = {"report": report.id, "start": start, "end": end}
            if is_plot:
                export_params.update({"view": "plot", "source": selected_source, "metric": selected_metric, "style": style})
                if compare:
                    export_params["compare"] = "1"
            elif result["subreport_id"]:
                export_params["subreport"] = result["subreport_id"]
            if result["site_id"]:
                export_params["site"] = result["site_id"]
            endpoint = "/api/v1/series" if is_plot else "/api/v1/report"
            csv_url = endpoint + ".csv?" + urlencode(export_params)
            json_url = endpoint + "?" + urlencode(export_params)
            export_name = "series" if is_plot else "report"
            quick_links = presets + f'<a href="{_e(csv_url)}">Download {export_name} CSV</a><a href="{_e(json_url)}">Load {export_name} JSON</a>'
            if is_plot:
                full_report_url = "/api/v1/report?" + urlencode({"report": report.id, "start": start, "end": end})
                quick_links += f'<a href="{_e(full_report_url)}">Full report JSON</a>'
            window_end = end_date - timedelta(days=1)
            window_label = f'{datetime.fromisoformat(start).strftime("%b %d")}–{window_end.strftime("%b %d, %Y")}'
            description = METRICS[selected_metric].description if selected_metric else "No daily series is available."
            freshness_count = len(result.get("source_health", []))
            page_title = "Time-series Plot Builder" if is_plot else result["title"]
            hero_copy = (
                "Select a source, metric, site, exact date window, and chart style. Every plot is built from stored local snapshots."
                if is_plot else
                "A read-only view of traffic, search visibility, and form-delivery evidence across the portfolio."
            )
            form_class = "filter-form plot-form" if is_plot else "filter-form"
            view_input = '<input type="hidden" name="view" value="plot">' if is_plot else ""
            source_field = (
                f'<label class="field"><span>Data source</span><select name="source">{source_options}</select></label>'
                if is_plot else ""
            )
            style_field = ""
            if is_plot:
                style_options = "".join(
                    f'<option value="{item}"{" selected" if item == style else ""}>{item.title()}</option>'
                    for item in ("line", "area", "bar")
                )
                checked = " checked" if compare else ""
                style_field = (
                    f'<label class="field"><span>Chart style</span><select name="style">{style_options}</select></label>'
                    f'<label class="field"><span>Comparison</span><span class="check-field"><input type="checkbox" name="compare" value="1"{checked}><span>Previous period</span></span></label>'
                )
            series_params = dict(export_params)
            if not is_plot and result["subreport_id"]:
                series_params["subreport"] = result["subreport_id"]
            series_params.update({"metric": selected_metric, "source": selected_source, "style": style})
            if compare:
                series_params["compare"] = "1"
            series_url = "/api/v1/series?" + urlencode(series_params)
            summary_html = "" if is_plot else _summary_cards(result, expected_metrics)
            provider_comparison_html = (
                "" if is_plot else _provider_comparisons_html(result, site_names)
            )
            attention_html = (
                "" if is_plot
                else _decision_overview_html(result.get("decision_support"))
            )
            decision_html = (
                "" if is_plot
                else _decision_support_html(result.get("decision_support"), site_names)
            )
            if is_plot:
                supporting_html = _health_html(result, expected_metrics)
            else:
                supporting_html = (
                    f'<div class="split-grid">{_forms_html(result["forms_pipeline"])}{_health_html(result, expected_metrics)}</div>'
                    f'<section class="panel table-panel"><div class="panel-heading"><div><h2>Metric detail</h2><p>Provider-labeled totals with the immediately preceding period for comparison.</p></div></div><div class="table-scroll"><table><thead><tr><th>Metric</th><th>Site</th><th>Source</th><th>Current</th><th>Previous</th><th>Coverage</th><th>Change</th></tr></thead><tbody>{_metrics_table(result, site_names)}</tbody></table></div></section>'
                )
            controls_open = " open" if is_plot else ""
            coverage_summary = _coverage_summary_html(result)
            chart_panel = (
                '<section class="panel chart-panel"><div class="panel-heading"><div>'
                f'<p class="eyebrow">Primary trend</p><h2>{_e(_metric_label(selected_metric)) if selected_metric else "Daily trend"}</h2>'
                f'<p class="metric-description">{_e(description)} Dates omitted by a provider are not imputed.</p></div>'
                f'<span class="source-chip">{_e(_source_label(selected_source)) if selected_source else "Daily series"}</span></div>'
                f'<div class="chart-stage"><div class="chart-status" id="chart-status" role="status">Loading stored series...</div><canvas class="time-series-chart" id="time-series-chart" data-series-url="{_e(series_url)}" role="img" aria-label="{_e(_metric_label(selected_metric)) if selected_metric else "Daily time series"}"></canvas></div>'
                '<ul class="chart-legend" id="chart-legend" aria-label="Chart legend"></ul>'
                '<p class="plot-note"><b>Local and read-only.</b> Missing dates remain missing; the dashboard never fills them with invented zeroes.</p>'
                f'<details class="chart-fallback"><summary>Accessible daily values and no-JavaScript fallback</summary>{_chart_html(result, selected_metric, site_names)}</details></section>'
            )
            primary_content = (
                chart_panel if is_plot
                else f'<div class="dashboard-primary">{chart_panel}{attention_html}</div>'
            )
            geography_html = ""
            if not is_plot:
                geography_params = {
                    "report": report.id, "start": start, "end": end, "source": "umami",
                }
                if result["site_id"]:
                    geography_params["site"] = result["site_id"]
                geography_query = {key: [value] for key, value in geography_params.items()}
                geography_html = _geography_panel(
                    self._geography_payload(geography_query),
                    "/api/v1/geography?" + urlencode(geography_params),
                )

            page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(page_title)} - Boho Analytics</title><link rel="icon" href="/favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="/assets/app.css"><script src="/assets/app.js" defer></script></head>
<body><a class="skip-link" href="#main">Skip to dashboard</a>
<header class="topbar"><div class="topbar-inner"><div class="brand"><span class="brand-mark">BA</span><div><strong>Boho Analytics</strong><span>Private portfolio command center</span></div></div><div class="live-state"><span class="live-dot"></span>Local snapshot - {freshness_count} sources reporting</div></div></header>
<main class="shell" id="main"><div class="report-nav" aria-label="Saved reports">{report_nav}</div>
<section class="hero"><div><p class="eyebrow">{_e(window_label)} - End date exclusive</p><h1>{_e(page_title)}</h1><p class="hero-copy">{_e(hero_copy)}</p></div>{coverage_summary}</section>
<nav class="subnav" aria-label="Report sections">{subnav}</nav>
<details class="panel control-panel"{controls_open}><summary class="panel-heading control-summary"><div><h2>{'Build a custom plot' if is_plot else 'Report tools'}</h2><p>{'Choose source, metric, scope, comparison, and chart style.' if is_plot else 'Change the window or site scope, or export the underlying evidence.'}</p></div></summary><div class="control-content">
<form class="{form_class}" method="get" action="/"><input type="hidden" name="report" value="{_e(report.id)}">{view_input}{hidden_subreport}
<label class="field"><span>Start date</span><input type="date" name="start" value="{_e(start)}" required></label>
<label class="field"><span>End date</span><input type="date" name="end" value="{_e(end)}" required></label>
{source_field}<label class="field"><span>Metric</span><select name="metric">{metric_options}</select></label>
<label class="field"><span>Site scope</span><select name="site">{site_options}</select></label>{style_field}<button type="submit">{'Plot selected data' if is_plot else 'Update dashboard'}</button></form>
<div class="tools-row"><span class="tools-label">Quick tools</span><div class="quick-links">{quick_links}</div></div></div></details>
{_warnings_html(result['warnings'])}{summary_html}{primary_content}{provider_comparison_html}{geography_html}{decision_html}{supporting_html}
<footer class="footer"><span>Generated {_e(result['generated_at'])}</span><span>Read-only - loopback-first - no browser credentials</span></footer></main></body></html>"""
            self._send(200, "text/html; charset=utf-8", page)

    return Handler


def serve(config, store) -> None:
    server = ThreadingHTTPServer((config.web.bind_host, config.web.port), handler_factory(config, store))
    print(f"Boho Analytics listening on http://{config.web.bind_host}:{config.web.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
