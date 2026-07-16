from __future__ import annotations

import http.client
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

from boho_analytics_platform.config import load_config
from boho_analytics_platform.engine import SyncEngine
from boho_analytics_platform.models import QueryWindow
from boho_analytics_platform.storage import SQLiteMetricStore
from boho_analytics_platform.web import handler_factory
from support import config_text, write_fixture


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup); root = Path(self.temporary.name)
        fixture = root / "fixture.json"; write_fixture(fixture); path = root / "platform.toml"; path.write_text(config_text(root / "state.db", fixture), encoding="utf-8")
        self.config = load_config(path); self.store = SQLiteMetricStore(root / "state.db"); self.store.initialize()
        SyncEngine(self.config, self.store).sync(QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 2, tzinfo=UTC), "UTC"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(self.config, self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown); self.addCleanup(self.thread.join, 2)

    def request(self, path, host="127.0.0.1"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.putrequest("GET", path, skip_host=True); connection.putheader("Host", host); connection.endheaders()
        response = connection.getresponse(); body = response.read().decode(); headers = dict(response.getheaders()); connection.close()
        return response.status, headers, body

    def test_dashboard_is_server_rendered_and_has_security_headers(self):
        status, headers, body = self.request("/?report=summary&start=2026-07-01&end=2026-07-02")
        self.assertEqual(status, 200); self.assertIn("Forms delivery", body); self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn('data-chart="umami.pageviews"', body); self.assertIn("Report tools", body); self.assertIn('src="/assets/app.js"', body)
        self.assertIn('id="time-series-chart"', body); self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers); self.assertEqual(headers["Cache-Control"], "no-store")

    def test_invalid_host_is_rejected(self): self.assertEqual(self.request("/healthz", "attacker.invalid")[0], 400)

    def test_json_and_csv_share_report_rows(self):
        json_body = self.request("/api/v1/report?report=summary&start=2026-07-01&end=2026-07-02")[2]
        self.assertIn('"umami.pageviews"', json_body); self.assertIn('"series"', json_body)
        status, headers, csv_body = self.request("/api/v1/report.csv?report=summary&start=2026-07-01&end=2026-07-02")
        self.assertEqual(status, 200); self.assertIn("umami.pageviews", csv_body); self.assertIn("attachment", headers["Content-Disposition"])

    def test_plot_builder_filters_series_and_exports_portable_csv(self):
        path = "/?report=summary&view=plot&source=umami&metric=umami.pageviews&style=area&compare=1&start=2026-07-01&end=2026-07-02"
        status, _headers, body = self.request(path)
        self.assertEqual(status, 200); self.assertIn("Time-series Plot Builder", body)
        self.assertIn('name="source"', body); self.assertIn('name="style"', body); self.assertIn('name="compare"', body)
        self.assertIn("Load series JSON", body); self.assertIn("Download series CSV", body)

        api = "/api/v1/series?report=summary&view=plot&source=umami&metric=umami.pageviews&style=area&start=2026-07-01&end=2026-07-02"
        status, _headers, json_body = self.request(api)
        self.assertEqual(status, 200); self.assertIn('"metric": "umami.pageviews"', json_body)
        self.assertIn('"style": "area"', json_body); self.assertIn('"value": 12', json_body)

        status, headers, csv_body = self.request(api.replace("/series?", "/series.csv?"))
        self.assertEqual(status, 200); self.assertIn("period,date,metric,site_id,source,unit,value", csv_body)
        self.assertIn("current,2026-07-01,umami.pageviews", csv_body)
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_script_asset_is_same_origin_and_invalid_plot_source_is_rejected(self):
        status, _headers, body = self.request("/assets/app.js")
        self.assertEqual(status, 200); self.assertIn("ResizeObserver", body); self.assertIn("fetch(canvas.dataset.seriesUrl", body)
        invalid = "/api/v1/series?report=summary&source=search-console&metric=umami.pageviews&start=2026-07-01&end=2026-07-02"
        self.assertEqual(self.request(invalid)[0], 400)

    def test_subreport_navigation_and_form_preserve_scope_and_window(self):
        body = self.request("/?report=summary&subreport=forms&start=2026-07-01&end=2026-07-02")[2]
        self.assertIn('name="subreport" value="forms"', body)
        self.assertIn("subreport=forms", body); self.assertIn("start=2026-07-01", body); self.assertIn("end=2026-07-02", body)

    def test_site_scope_is_validated_and_preserved(self):
        path = "/?report=summary&site=example-site&start=2026-07-01&end=2026-07-02"
        status, _headers, body = self.request(path)
        self.assertEqual(status, 200); self.assertIn('<option value="example-site" selected>', body)
        self.assertIn("site=example-site", body)
        self.assertEqual(self.request("/?report=summary&site=unknown&start=2026-07-01&end=2026-07-02")[0], 400)

    def test_css_charts_need_no_inline_style_permission(self):
        status, _headers, body = self.request("/assets/app.css")
        self.assertEqual(status, 200); self.assertIn(".h-50{height:100%}", body)
        page_headers = self.request("/?report=summary&start=2026-07-01&end=2026-07-02")[1]
        self.assertNotIn("unsafe-inline", page_headers["Content-Security-Policy"])


if __name__ == "__main__": unittest.main()
