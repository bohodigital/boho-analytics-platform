from __future__ import annotations

import sqlite3
from contextlib import closing
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from boho_analytics_platform.config import load_config
from boho_analytics_platform.connectors.cloudflare import CloudflareFormsConnector
from boho_analytics_platform.connectors.forms_inbox import FormsInboxConnector
from boho_analytics_platform.contracts import SyncRequest
from boho_analytics_platform.credentials import MemoryCredentialLease
from boho_analytics_platform.models import QueryWindow
from support import config_text, write_fixture


class FakeHttp:
    def __init__(self): self.body = None
    def request(self, method, url, *, headers=None, body=None):
        self.body = body
        return {"result": [{"results": [{"metric_day": "2026-07-01", "form_id": "contact", "notification_status": "sent", "aggregate_count": 2}]}]}


class FormsConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup); self.root = Path(self.temporary.name)
        self.fixture = self.root / "fixture.json"; write_fixture(self.fixture)

    def _window(self): return QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 2, tzinfo=UTC), "UTC")

    def test_d1_connector_aggregates_without_selecting_payload(self):
        text = config_text(self.root / "state.db", self.fixture, provider="cloudflare-forms", credential_ref="none:test",
            options='account_id = "account"\ndatabase_id = "database"')
        path = self.root / "config.toml"; path.write_text(text, encoding="utf-8"); config = load_config(path); http = FakeHttp()
        points = list(CloudflareFormsConnector(config, http).collect(config.connections[0], MemoryCredentialLease({"api_token": b"token"}), SyncRequest(config.bindings[0], self._window(), ())))
        self.assertNotIn("payload_json", http.body["sql"].casefold()); self.assertNotIn("select *", http.body["sql"].casefold())
        self.assertEqual({point.metric for point in points}, {"forms.sent", "forms.submissions"})

    def test_inbox_connector_is_query_only_and_returns_counts(self):
        database = self.root / "mail.db"
        with closing(sqlite3.connect(database)) as db:
            db.executescript("""CREATE TABLE mailboxes(id INTEGER PRIMARY KEY,mailbox_key TEXT); CREATE TABLE messages(id INTEGER PRIMARY KEY,received_at TEXT,from_address TEXT,subject TEXT,direction TEXT); CREATE TABLE mailbox_locations(mailbox_id INTEGER,message_id INTEGER,flags_json TEXT); INSERT INTO mailboxes VALUES(1,'forms'); INSERT INTO messages VALUES(1,'2026-07-01T10:00:00+00:00','sender@example.invalid','Form submission','inbound'); INSERT INTO mailbox_locations VALUES(1,1,'[]');""")
            db.commit()
        text = config_text(self.root / "state.db", self.fixture, provider="forms-inbox", options=f'database_path = "{database.as_posix()}"')
        text = text.replace('resource_id = "demo"', 'resource_id = "forms"\n[bindings.options]\nmailbox_key = "forms"\nsubject_contains = "Form submission"')
        path = self.root / "config.toml"; path.write_text(text, encoding="utf-8"); config = load_config(path)
        points = list(FormsInboxConnector(config, None).collect(config.connections[0], MemoryCredentialLease({}), SyncRequest(config.bindings[0], self._window(), ())))
        self.assertEqual([point.metric for point in points], ["forms.inbox-deliveries", "forms.inbox-unread"]); self.assertEqual(points[0].value, 1)


if __name__ == "__main__": unittest.main()
