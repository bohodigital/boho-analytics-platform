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
        self.assertEqual(status, 200); self.assertIn("Forms pipeline", body); self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers); self.assertEqual(headers["Cache-Control"], "no-store")

    def test_invalid_host_is_rejected(self): self.assertEqual(self.request("/healthz", "attacker.invalid")[0], 400)

    def test_json_and_csv_share_report_rows(self):
        self.assertIn('"umami.pageviews"', self.request("/api/v1/report?report=summary&start=2026-07-01&end=2026-07-02")[2])
        self.assertIn("umami.pageviews", self.request("/api/v1/report.csv?report=summary&start=2026-07-01&end=2026-07-02")[2])

    def test_subreport_navigation_and_form_preserve_scope_and_window(self):
        body = self.request("/?report=summary&subreport=forms&start=2026-07-01&end=2026-07-02")[2]
        self.assertIn('name=subreport value="forms"', body)
        self.assertIn("subreport=forms&amp;start=2026-07-01&amp;end=2026-07-02", body)


if __name__ == "__main__": unittest.main()
