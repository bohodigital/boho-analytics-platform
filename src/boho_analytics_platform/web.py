"""Loopback-first, server-rendered dashboard and read-only report API."""

from __future__ import annotations

import base64
import hmac
import html
import json
from datetime import UTC, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from .catalog import METRICS
from .credentials import ReferenceCredentialProvider, require_text
from .models import QueryWindow
from .reporting import ReportService, to_csv


SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}


METRIC_LABELS = {
    "umami.pageviews": "Page views",
    "umami.sessions": "Sessions",
    "umami.visitors": "Visitors",
    "umami.visits": "Visits",
    "umami.bounces": "Bounces",
    "umami.total-time": "Visit time",
    "cloudflare.requests": "Edge requests",
    "cloudflare.visits": "Edge visits",
    "cloudflare.bytes": "Response bytes",
    "google.active-users": "Active users",
    "google.sessions": "GA sessions",
    "google.pageviews": "GA page views",
    "google.events": "GA events",
    "google.key-events": "Key events",
    "search.clicks": "Search clicks",
    "search.impressions": "Search impressions",
    "search.ctr": "Search CTR",
    "search.position": "Average position",
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
    "search-console": "Search Console",
    "cloudflare-forms": "Forms database",
    "forms-inbox": "Forms inbox",
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
    ("Page views", ("umami.pageviews", "google.pageviews"), "Audience attention"),
    ("Sessions", ("umami.sessions", "google.sessions"), "Engaged visits"),
    ("Search impressions", ("search.impressions",), "Organic visibility"),
    ("Inbox deliveries", ("forms.inbox-deliveries",), "Form notification evidence"),
)

TRAFFIC_SUMMARY = (
    ("Page views", ("umami.pageviews", "google.pageviews"), "Audience attention"),
    ("Sessions", ("umami.sessions", "google.sessions"), "Engaged visits"),
    ("Visitors", ("umami.visitors", "google.active-users"), "Measured audience"),
    ("Edge requests", ("cloudflare.requests",), "Cloudflare delivery volume"),
)

SEARCH_SUMMARY = (
    ("Impressions", ("search.impressions",), "Search result visibility"),
    ("Clicks", ("search.clicks",), "Organic visits from Google"),
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
:root{--ink:#17201d;--ink-2:#26312d;--paper:#f4f2ec;--surface:#fff;--line:#deddd5;--muted:#6d746f;--accent:#e86d3d;--accent-soft:#fff0e8;--green:#1f7a5a;--green-soft:#e7f5ef;--amber:#a55c12;--amber-soft:#fff4dc;--red:#a43f35;--red-soft:#fdebe8;--shadow:0 12px 35px rgba(25,35,31,.07)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:inherit}button,input,select{font:inherit}.skip-link{position:fixed;left:12px;top:-80px;z-index:20;background:#fff;padding:10px 14px;border-radius:8px}.skip-link:focus{top:12px}
.topbar{background:var(--ink);color:#fff}.topbar-inner{max-width:1240px;margin:auto;padding:18px 28px;display:flex;justify-content:space-between;gap:24px;align-items:center}.brand{display:flex;align-items:center;gap:12px}.brand-mark{display:grid;place-items:center;width:38px;height:38px;border:1px solid rgba(255,255,255,.28);border-radius:11px;color:#ffd4c2;font-weight:800;letter-spacing:-.04em}.brand strong{display:block;font-size:15px}.brand span{display:block;color:#aeb9b4;font-size:12px}.live-state{display:flex;align-items:center;gap:8px;color:#dfe9e5;font-size:13px}.live-dot{width:8px;height:8px;border-radius:50%;background:#4fd49c;box-shadow:0 0 0 4px rgba(79,212,156,.12)}
.shell{max-width:1240px;margin:auto;padding:34px 28px 48px}.hero{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:24px;align-items:end;margin-bottom:22px}.eyebrow{margin:0 0 7px;color:var(--accent);font-size:12px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.hero h1{margin:0;font-size:clamp(29px,4vw,46px);line-height:1.08;letter-spacing:-.045em}.hero-copy{max-width:720px;margin:11px 0 0;color:var(--muted);font-size:16px}.coverage-badge{align-self:start;display:inline-flex;align-items:center;gap:8px;padding:9px 12px;border-radius:999px;background:var(--green-soft);color:var(--green);font-size:13px;font-weight:750}.coverage-badge.partial{background:var(--amber-soft);color:var(--amber)}
.report-nav,.subnav,.quick-links{display:flex;flex-wrap:wrap;gap:8px}.report-nav{margin:0 0 12px}.report-nav a,.subnav a,.quick-links a{padding:8px 11px;border:1px solid var(--line);border-radius:9px;background:rgba(255,255,255,.6);color:var(--ink-2);font-size:13px;font-weight:700;text-decoration:none}.report-nav a:hover,.subnav a:hover,.quick-links a:hover{background:#fff;border-color:#b9bab4}.report-nav a.active,.subnav a.active{background:var(--ink);border-color:var(--ink);color:#fff}.subnav{margin-bottom:16px}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:17px;box-shadow:var(--shadow)}.control-panel{padding:18px;margin-bottom:18px}.panel-heading{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:14px}.panel-heading h2{margin:0;font-size:18px;letter-spacing:-.02em}.panel-heading p{margin:3px 0 0;color:var(--muted);font-size:13px}.filter-form{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr)) auto;gap:12px;align-items:end}.field{display:grid;gap:6px}.field span{font-size:12px;font-weight:750;color:var(--ink-2)}input,select{width:100%;min-height:42px;padding:9px 11px;border:1px solid #c8c9c3;border-radius:9px;background:#fff;color:var(--ink)}input:focus,select:focus,button:focus,a:focus{outline:3px solid rgba(232,109,61,.28);outline-offset:2px;border-color:var(--accent)}button{min-height:42px;padding:9px 16px;border:1px solid var(--ink);border-radius:9px;background:var(--ink);color:#fff;font-weight:800;cursor:pointer}button:hover{background:#283530}.tools-row{display:flex;justify-content:space-between;gap:14px;align-items:center;margin-top:14px;padding-top:14px;border-top:1px solid #ecebe5}.tools-label{color:var(--muted);font-size:12px;font-weight:750;text-transform:uppercase;letter-spacing:.08em}
.alerts{display:grid;gap:9px;margin:0 0 18px}.alert{display:flex;gap:10px;align-items:flex-start;padding:12px 14px;border:1px solid #f0d7b4;border-radius:11px;background:var(--amber-soft);color:#77440f}.alert-mark{display:grid;place-items:center;flex:0 0 22px;height:22px;border-radius:50%;background:#d98627;color:#fff;font-size:12px;font-weight:900}
.kpi-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:18px}.kpi-card{position:relative;overflow:hidden;min-height:154px;padding:18px;background:#fff;border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow)}.kpi-card:after{content:"";position:absolute;right:-25px;bottom:-38px;width:105px;height:105px;border-radius:50%;background:var(--accent-soft)}.kpi-top{display:flex;justify-content:space-between;gap:8px;align-items:center}.kpi-label{color:var(--muted);font-size:12px;font-weight:800;letter-spacing:.06em;text-transform:uppercase}.kpi-value{position:relative;z-index:1;display:block;margin:13px 0 5px;font-size:34px;line-height:1;font-weight:850;letter-spacing:-.045em}.kpi-note{position:relative;z-index:1;margin:0;color:var(--muted);font-size:12px}.trend{padding:4px 7px;border-radius:999px;font-size:11px;font-weight:800}.trend.up{background:var(--green-soft);color:var(--green)}.trend.down{background:var(--red-soft);color:var(--red)}.trend.flat{background:#efefeb;color:#616762}
.chart-panel{padding:20px;margin-bottom:18px}.chart-panel .panel-heading{margin-bottom:18px}.metric-description{max-width:650px}.chart-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.chart-card{min-width:0;padding:15px;border:1px solid #e6e5df;border-radius:13px;background:linear-gradient(180deg,#fff,#fbfaf7)}.chart-card-head{display:flex;justify-content:space-between;gap:12px;align-items:baseline;margin-bottom:10px}.chart-card h3{margin:0;font-size:14px}.chart-total{color:var(--muted);font-size:12px;font-weight:750}.chart-scroll{overflow-x:auto;padding:4px 0 0}.bar-grid{position:relative;display:grid;grid-auto-flow:column;grid-auto-columns:minmax(12px,1fr);align-items:end;gap:4px;height:205px;min-width:100%;border-bottom:1px solid #cfd1cc;background:repeating-linear-gradient(to top,transparent 0,transparent 50px,#ecece7 51px)}.bar-grid.density-mid{grid-auto-columns:minmax(9px,1fr)}.bar-grid.density-wide{grid-auto-columns:minmax(6px,1fr)}.bar-slot{height:100%;display:flex;align-items:end;justify-content:center}.bar{display:block;width:72%;min-height:2px;border-radius:5px 5px 2px 2px;background:var(--accent)}.bar.tone-1{background:#357a68}.bar.tone-2{background:#6772a8}.bar.tone-3{background:#ba8b32}.axis-labels{display:flex;justify-content:space-between;gap:12px;margin-top:7px;color:var(--muted);font-size:11px}.chart-data{margin-top:10px;color:var(--muted);font-size:12px}.chart-data summary{cursor:pointer;font-weight:700}.empty-state{padding:34px;border:1px dashed #cacbc5;border-radius:12px;text-align:center;color:var(--muted)}
.split-grid{display:grid;grid-template-columns:1.05fr .95fr;gap:18px;margin-bottom:18px}.split-grid>.section-panel:only-child{grid-column:1/-1}.section-panel{padding:20px}.health-grid,.pipeline-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.health-item,.pipeline-item{padding:13px;border:1px solid #e6e5df;border-radius:11px;background:#fbfaf7}.health-item b,.pipeline-item b{display:block;margin-bottom:3px;font-size:13px}.health-item span,.pipeline-item span{color:var(--muted);font-size:12px}.pipeline-value{display:block!important;margin:5px 0 1px;font-size:24px!important;line-height:1;font-weight:850;color:var(--ink)!important}.pipeline-note{margin:12px 0 0;color:var(--muted);font-size:12px}
.table-panel{overflow:hidden;margin-bottom:18px}.table-panel .panel-heading{padding:20px 20px 0}.table-scroll{overflow-x:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:11px 14px;border-bottom:1px solid #ecebe6;white-space:nowrap}th{color:var(--muted);font-size:11px;letter-spacing:.06em;text-transform:uppercase}td{font-size:13px}tbody tr:hover{background:#fbfaf7}.metric-name{font-weight:750}.source-chip{display:inline-block;padding:3px 7px;border-radius:999px;background:#efefeb;color:#505852;font-size:11px;font-weight:700}.positive{color:var(--green);font-weight:750}.negative{color:var(--red);font-weight:750}.muted{color:var(--muted)}.footer{display:flex;justify-content:space-between;gap:20px;color:var(--muted);font-size:12px}.sr-only{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}
@media(max-width:980px){.kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.filter-form{grid-template-columns:repeat(2,minmax(0,1fr))}.filter-form button{grid-column:span 2}.chart-grid,.split-grid{grid-template-columns:1fr}}
@media(max-width:650px){.topbar-inner,.shell{padding-left:16px;padding-right:16px}.topbar-inner{align-items:flex-start}.live-state{margin-top:9px}.hero{grid-template-columns:1fr}.coverage-badge{justify-self:start}.filter-form{grid-template-columns:1fr}.filter-form button{grid-column:auto}.tools-row,.footer{align-items:flex-start;flex-direction:column}.kpi-grid{grid-template-columns:1fr 1fr;gap:10px}.kpi-card{min-height:132px;padding:15px}.kpi-value{font-size:28px}.chart-panel,.section-panel{padding:16px}.health-grid,.pipeline-grid{grid-template-columns:1fr 1fr}.bar-grid{height:175px}th,td{padding:10px 12px}.panel-heading{display:block}.quick-links{margin-top:10px}}
@media(max-width:420px){.kpi-grid{grid-template-columns:1fr}.health-grid,.pipeline-grid{grid-template-columns:1fr}.topbar-inner{display:block}.brand{margin-bottom:10px}}
"""

HEIGHT_CLASSES = "".join(f".h-{level}{{height:{level * 2}%}}" for level in range(51))
CSS = BASE_CSS + HEIGHT_CLASSES


def _window(query, timezone, default_days):
    zone = UTC if timezone == "UTC" else ZoneInfo(timezone)
    today = datetime.now(zone).date()
    end_date = datetime.fromisoformat(query.get("end", [today.isoformat()])[0]).date()
    start_date = datetime.fromisoformat(
        query.get("start", [(end_date - timedelta(days=default_days)).isoformat()])[0]
    ).date()
    return QueryWindow(
        datetime.combine(start_date, time.min, zone),
        datetime.combine(end_date, time.min, zone),
        timezone,
    )


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


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
    rows = [row for row in result["rows"] if row["metric"] == metric]
    if not rows:
        return None
    value = sum(float(row["value"]) for row in rows)
    prior_values = [float(row["previous_value"]) for row in rows if row["previous_value"] is not None]
    previous = sum(prior_values) if prior_values else None
    change = None if previous in {None, 0} else round((value - previous) / previous * 100, 1)
    return {"value": value, "previous": previous, "change": change, "unit": rows[0]["unit"], "metric": metric}


def _trend(change, *, lower_is_better=False):
    if change is None:
        return '<span class="trend flat">No prior</span>'
    favorable = change < 0 if lower_is_better else change > 0
    state = "flat" if change == 0 else "up" if favorable else "down"
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
        for metric in candidates:
            total = _metric_total(result, metric)
            if total:
                break
            pipeline_key = pipeline_values.get(metric)
            if pipeline_key and result["forms_pipeline"] is not None:
                total = {
                    "value": result["forms_pipeline"].get(pipeline_key, 0),
                    "previous": None,
                    "change": None,
                    "unit": "count",
                    "metric": metric,
                }
                break
        if total:
            value = _format_value(total["value"], total["unit"])
            source_row = next((row for row in result["rows"] if row["metric"] == total["metric"]), None)
            source = _source_label(source_row["source"]) if source_row else "Current window"
            badge = _trend(
                total["change"],
                lower_is_better=total["metric"] in {"search.position", "forms.pending", "forms.failed"},
            )
            detail = f"{note} · {source}"
        else:
            value = "—"
            badge = '<span class="trend flat">No data</span>'
            detail = note
        cards.append(
            f'<article class="kpi-card"><div class="kpi-top"><span class="kpi-label">{_e(label)}</span>{badge}</div>'
            f'<strong class="kpi-value">{_e(value)}</strong><p class="kpi-note">{_e(detail)}</p></article>'
        )
    return '<section class="kpi-grid" aria-label="Portfolio summary">' + "".join(cards) + "</section>"


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
        total = sum(float(point["value"]) for point in points)
        first = points[0]["date"] if points else ""
        last = points[-1]["date"] if points else ""
        cards.append(
            f'<article class="chart-card" data-chart="{_e(metric)}"><div class="chart-card-head">'
            f'<h3>{_e(site_names.get(series["site_id"], series["site_id"]))}</h3>'
            f'<span class="chart-total">{_e(_format_value(total, series["unit"]))} total</span></div>'
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
        f'<div class="pipeline-item"><b>{_e(labels[key])}</b><span class="pipeline-value">{_e(_format_value(value))}</span>'
        f'<span>{"Current window" if key != "delivery_gap" else "Stored minus delivered"}</span></div>'
        for key, value in pipeline.items()
    )
    gap = pipeline.get("delivery_gap", 0)
    note = "Storage and inbox evidence agree." if gap == 0 else "Counts differ; use the forms report to inspect notification state."
    return f'<section class="panel section-panel"><div class="panel-heading"><div><h2>Forms delivery</h2><p>Independent storage and mailbox evidence.</p></div></div><div class="pipeline-grid">{items}</div><p class="pipeline-note">{_e(note)}</p></section>'


def _health_html(result, expected_metrics):
    present = {row["metric"] for row in result["rows"]}
    coverage = f"{len(present)} of {len(expected_metrics)} metrics"
    freshness = []
    for source, observed in result["freshness"].items():
        timestamp = datetime.fromisoformat(observed).astimezone(UTC).strftime("%b %d, %H:%M UTC")
        freshness.append(
            f'<div class="health-item"><b>{_e(_source_label(source))}</b><span>Observed {_e(timestamp)}</span></div>'
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
    return '<section class="alerts" aria-label="Report warnings">' + "".join(
        f'<div class="alert"><span class="alert-mark">!</span><span>{_e(item)}</span></div>' for item in warnings
    ) + "</section>"


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
            f'<td class="{change_class}">{_e(change_text)}</td></tr>'
        )
    if not rows:
        rows.append('<tr><td colspan="6">No data in this window.</td></tr>')
    return "".join(rows)


def handler_factory(config, store, credentials=None):
    reports = ReportService(config, store)
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

        def do_GET(self):
            if not self._allowed():
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                return self._send(200, "application/json", '{"ok":true}')
            if parsed.path == "/assets/app.css":
                return self._send(200, "text/css; charset=utf-8", CSS)
            try:
                if parsed.path == "/":
                    return self._dashboard(parse_qs(parsed.query))
                if parsed.path in {"/api/v1/report", "/api/v1/report.csv"}:
                    return self._api(parsed.path, parse_qs(parsed.query))
                self.send_error(404)
            except (ValueError, KeyError):
                self._send(400, "application/json", '{"error":"invalid report request"}')

        def _request(self, query):
            report_id = query.get("report", [config.reports[0].id])[0]
            report = next((item for item in config.reports if item.id == report_id), None)
            if report is None:
                raise ValueError("unknown report")
            subreport_id = query.get("subreport", [None])[0]
            default_days = report.default_window_days
            if subreport_id:
                subreport = next((item for item in report.subreports if item.id == subreport_id), None)
                if subreport is None:
                    raise ValueError("unknown subreport")
                default_days = subreport.default_window_days
            site_id = query.get("site", [None])[0]
            window = _window(query, config.platform.default_timezone, default_days)
            return reports.render(report_id, window, subreport_id, site_id), report

        def _api(self, path, query):
            report, _definition = self._request(query)
            if path.endswith(".csv"):
                filename = f'{report["report_id"]}-{report["window"]["start"][:10]}-{report["window"]["end"][:10]}.csv'
                return self._send(
                    200,
                    "text/csv; charset=utf-8",
                    to_csv(report),
                    {"Content-Disposition": f'attachment; filename="{filename}"'},
                )
            return self._send(200, "application/json", json.dumps(report, sort_keys=True))

        def _dashboard(self, query):
            result, report = self._request(query)
            start = result["window"]["start"][:10]
            end = result["window"]["end"][:10]
            site_names = {site.id: site.name for site in config.sites}
            available_metrics = {series["metric"] for series in result["series"]}
            requested_metric = query.get("metric", [None])[0]
            selected_metric = requested_metric if requested_metric in available_metrics else next(
                (metric for metric in CHART_PRIORITY if metric in available_metrics),
                sorted(available_metrics)[0] if available_metrics else "",
            )

            def route(path="/", **overrides):
                params = {"report": report.id, "start": start, "end": end}
                if result["subreport_id"]:
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
                f'<a class="{"active" if item.id == report.id else ""}" href="{_e("/?" + urlencode({"report": item.id, "start": start, "end": end}))}">{_e(item.title)}</a>'
                for item in config.reports
            )
            subnav = f'<a class="{"active" if not result["subreport_id"] else ""}" href="{_e(route(subreport=None))}">Overview</a>'
            subnav += "".join(
                f'<a class="{"active" if item.id == result["subreport_id"] else ""}" href="{_e(route(subreport=item.id))}">{_e(item.title)}</a>'
                for item in report.subreports
            )
            site_options = '<option value="">All sites</option>' + "".join(
                f'<option value="{_e(site_id)}"{" selected" if site_id == result["site_id"] else ""}>{_e(site_names.get(site_id, site_id))}</option>'
                for site_id in report.site_ids
            )
            metric_order = [metric for metric in CHART_PRIORITY if metric in available_metrics]
            metric_order += sorted(available_metrics - set(metric_order))
            metric_options = "".join(
                f'<option value="{_e(metric)}"{" selected" if metric == selected_metric else ""}>{_e(_metric_label(metric))}</option>'
                for metric in metric_order
            ) or '<option value="">No daily series</option>'
            hidden_subreport = (
                f'<input type="hidden" name="subreport" value="{_e(result["subreport_id"])}">'
                if result["subreport_id"] else ""
            )

            end_date = datetime.fromisoformat(end).date()
            presets = "".join(
                f'<a href="{_e(route(start=(end_date - timedelta(days=days)).isoformat()))}">{days} days</a>'
                for days in (7, 30, 90)
            )
            export_params = {"report": report.id, "start": start, "end": end}
            if result["subreport_id"]:
                export_params["subreport"] = result["subreport_id"]
            if result["site_id"]:
                export_params["site"] = result["site_id"]
            csv_url = "/api/v1/report.csv?" + urlencode(export_params)
            json_url = "/api/v1/report?" + urlencode(export_params)
            quick_links = presets + f'<a href="{_e(csv_url)}">Download CSV</a><a href="{_e(json_url)}">JSON API</a>'

            active_definition = next(
                (item for item in report.subreports if item.id == result["subreport_id"]),
                None,
            )
            expected_metrics = active_definition.metric_ids if active_definition else report.metric_ids
            status_class = "" if result["complete"] else " partial"
            status_text = "Complete coverage" if result["complete"] else "Partial coverage"
            window_end = end_date - timedelta(days=1)
            window_label = f'{datetime.fromisoformat(start).strftime("%b %d")}–{window_end.strftime("%b %d, %Y")}'
            description = METRICS[selected_metric].description if selected_metric else "No daily series is available."
            freshness_count = len(result["freshness"])

            page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(result['title'])} · Boho Analytics</title><link rel="stylesheet" href="/assets/app.css"></head>
<body><a class="skip-link" href="#main">Skip to dashboard</a>
<header class="topbar"><div class="topbar-inner"><div class="brand"><span class="brand-mark">BA</span><div><strong>Boho Analytics</strong><span>Private portfolio command center</span></div></div><div class="live-state"><span class="live-dot"></span>Local snapshot · {freshness_count} sources reporting</div></div></header>
<main class="shell" id="main"><div class="report-nav" aria-label="Saved reports">{report_nav}</div>
<section class="hero"><div><p class="eyebrow">{_e(window_label)} · End date exclusive</p><h1>{_e(result['title'])}</h1><p class="hero-copy">A read-only view of traffic, search visibility, and form-delivery evidence across the portfolio.</p></div><span class="coverage-badge{status_class}">{_e(status_text)}</span></section>
<nav class="subnav" aria-label="Report sections">{subnav}</nav>
<section class="panel control-panel"><div class="panel-heading"><div><h2>Report tools</h2><p>Choose a site, metric, or exact reporting window. Browser requests never trigger provider syncs.</p></div></div>
<form class="filter-form" method="get" action="/"><input type="hidden" name="report" value="{_e(report.id)}">{hidden_subreport}
<label class="field"><span>Start date</span><input type="date" name="start" value="{_e(start)}" required></label>
<label class="field"><span>End date</span><input type="date" name="end" value="{_e(end)}" required></label>
<label class="field"><span>Site scope</span><select name="site">{site_options}</select></label>
<label class="field"><span>Graph metric</span><select name="metric">{metric_options}</select></label><button type="submit">Update dashboard</button></form>
<div class="tools-row"><span class="tools-label">Quick tools</span><div class="quick-links">{quick_links}</div></div></section>
{_warnings_html(result['warnings'])}{_summary_cards(result, expected_metrics)}
<section class="panel chart-panel"><div class="panel-heading"><div><h2>{_e(_metric_label(selected_metric)) if selected_metric else 'Daily trend'}</h2><p class="metric-description">{_e(description)} Dates omitted by a provider are not imputed.</p></div><span class="source-chip">Daily series</span></div>{_chart_html(result, selected_metric, site_names)}</section>
<div class="split-grid">{_forms_html(result['forms_pipeline'])}{_health_html(result, expected_metrics)}</div>
<section class="panel table-panel"><div class="panel-heading"><div><h2>Metric detail</h2><p>Provider-labeled totals with the immediately preceding period for comparison.</p></div></div><div class="table-scroll"><table><thead><tr><th>Metric</th><th>Site</th><th>Source</th><th>Current</th><th>Previous</th><th>Change</th></tr></thead><tbody>{_metrics_table(result, site_names)}</tbody></table></div></section>
<footer class="footer"><span>Generated {_e(result['generated_at'])}</span><span>Read-only · loopback-first · no browser credentials</span></footer></main></body></html>"""
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
