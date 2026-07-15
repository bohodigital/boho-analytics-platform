"""Loopback-first, server-rendered dashboard and read-only report API."""

from __future__ import annotations

import base64
import hmac
import html
import json
from datetime import UTC, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from .credentials import ReferenceCredentialProvider, require_text
from .models import QueryWindow
from .reporting import ReportService, to_csv


SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-origin",
}


CSS = """*{box-sizing:border-box}body{font:15px system-ui,sans-serif;margin:0;background:#f5f3ef;color:#191816}header,main{max-width:1120px;margin:auto;padding:24px}header{display:flex;justify-content:space-between;align-items:center}.card{background:white;border:1px solid #ded9d0;border-radius:12px;padding:18px;margin:14px 0;max-width:100%;overflow-x:auto}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid #eee}input,select,button{padding:9px;border:1px solid #bbb;border-radius:6px;max-width:100%}button{background:#25231f;color:white}.warn{color:#8b3d11}.muted{color:#68635b}nav a{margin-right:12px}@media(max-width:650px){header{display:block}header,main{padding:16px}nav a{display:inline-block;margin:4px 10px 4px 0}table{font-size:12px;min-width:560px}form label{display:block;margin:8px 0}}"""


def _window(query, timezone, default_days):
    zone = UTC if timezone == "UTC" else ZoneInfo(timezone); today = datetime.now(zone).date()
    end_date = datetime.fromisoformat(query.get("end", [today.isoformat()])[0]).date()
    start_date = datetime.fromisoformat(query.get("start", [(end_date - timedelta(days=default_days)).isoformat()])[0]).date()
    return QueryWindow(datetime.combine(start_date, time.min, zone), datetime.combine(end_date, time.min, zone), timezone)


def handler_factory(config, store, credentials=None):
    reports = ReportService(config, store); credential_provider = credentials or ReferenceCredentialProvider()
    password = None
    if config.web.auth_mode == "basic":
        with credential_provider.acquire(config.web.auth_credential_ref) as lease: password = require_text(lease, "password", "value")

    class Handler(BaseHTTPRequestHandler):
        server_version = "BohoAnalytics"
        sys_version = ""

        def log_message(self, format, *args):
            # Paths may contain report IDs only; query strings are deliberately omitted.
            print(f"web {self.command} {urlsplit(self.path).path} {args[1] if len(args)>1 else '-'}")

        def end_headers(self):
            for key, value in SECURITY_HEADERS.items(): self.send_header(key, value)
            super().end_headers()

        def _allowed(self):
            host = self.headers.get("Host", "").split(":", 1)[0].casefold()
            if host not in {item.casefold() for item in config.web.allowed_hosts}: self.send_error(400, "Invalid Host"); return False
            if password is not None:
                auth = self.headers.get("Authorization", "")
                expected = "Basic " + base64.b64encode(f"{config.web.username}:{password}".encode()).decode()
                if not hmac.compare_digest(auth, expected):
                    self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="analytics"'); self.end_headers(); return False
            return True

        def _send(self, status, content_type, body):
            raw = body.encode("utf-8"); self.send_response(status); self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def do_GET(self):
            if not self._allowed(): return
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz": return self._send(200, "application/json", '{"ok":true}')
            if parsed.path == "/assets/app.css": return self._send(200, "text/css; charset=utf-8", CSS)
            try:
                if parsed.path == "/": return self._dashboard(parse_qs(parsed.query))
                if parsed.path in {"/api/v1/report", "/api/v1/report.csv"}: return self._api(parsed.path, parse_qs(parsed.query))
                self.send_error(404)
            except (ValueError, KeyError): self._send(400, "application/json", '{"error":"invalid report request"}')

        def _request(self, query):
            report_id = query.get("report", [config.reports[0].id])[0]; sub = query.get("subreport", [None])[0]
            definition = next(item for item in config.reports if item.id == report_id)
            window = _window(query, config.platform.default_timezone, definition.default_window_days)
            return reports.render(report_id, window, sub)

        def _api(self, path, query):
            report = self._request(query)
            if path.endswith(".csv"): return self._send(200, "text/csv; charset=utf-8", to_csv(report))
            return self._send(200, "application/json", json.dumps(report, sort_keys=True))

        def _dashboard(self, query):
            result = self._request(query); report = next(item for item in config.reports if item.id == result["report_id"])
            rows = "".join(f"<tr><td>{html.escape(r['metric'])}</td><td>{html.escape(r['site_id'])}</td><td>{html.escape(r['source'])}</td><td>{r['value']}</td><td>{r['previous_value'] if r['previous_value'] is not None else '-'} </td><td>{str(r['change_percent'])+'%' if r['change_percent'] is not None else '-'}</td></tr>" for r in result["rows"])
            warnings = "".join(f"<p class=warn>{html.escape(item)}</p>" for item in result["warnings"])
            start = result["window"]["start"][:10]; end = result["window"]["end"][:10]
            nav = "".join(f'<a href="/?report={html.escape(report.id)}&amp;subreport={html.escape(sub.id)}&amp;start={start}&amp;end={end}">{html.escape(sub.title)}</a>' for sub in report.subreports)
            hidden_subreport = f'<input type=hidden name=subreport value="{html.escape(result["subreport_id"])}">' if result["subreport_id"] else ""
            forms = ""
            if result["forms_pipeline"]:
                forms = '<section class="card"><h2>Forms pipeline</h2><div class="grid">' + "".join(f"<div><b>{html.escape(k.replace('_',' ').title())}</b><p>{v}</p></div>" for k,v in result["forms_pipeline"].items()) + "</div></section>"
            page = f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>{html.escape(result['title'])}</title><link rel=stylesheet href=/assets/app.css></head><body><header><div><h1>{html.escape(result['title'])}</h1><p class=muted>Read-only analytics dashboard</p></div><nav>{nav}</nav></header><main><form class=card method=get><input type=hidden name=report value="{html.escape(report.id)}">{hidden_subreport}<label>Start <input type=date name=start value={start}></label> <label>End <input type=date name=end value={end}></label> <button>Apply window</button></form>{warnings}{forms}<section class=card><h2>Metrics</h2><table><thead><tr><th>Metric</th><th>Site</th><th>Source</th><th>Value</th><th>Previous</th><th>Change</th></tr></thead><tbody>{rows or '<tr><td colspan=6>No data in this window.</td></tr>'}</tbody></table></section><p class=muted>Generated {html.escape(result['generated_at'])}. Provider freshness is exposed by the JSON API.</p></main></body></html>"""
            self._send(200, "text/html; charset=utf-8", page)
    return Handler


def serve(config, store) -> None:
    server = ThreadingHTTPServer((config.web.bind_host, config.web.port), handler_factory(config, store))
    print(f"Boho Analytics listening on http://{config.web.bind_host}:{config.web.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
