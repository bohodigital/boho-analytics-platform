from __future__ import annotations

import sqlite3
from contextlib import closing
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from boho_analytics_platform.config import load_config
from boho_analytics_platform.connectors.cloudflare import CloudflareFormsConnector
from boho_analytics_platform.connectors.forms_inbox import FormsInboxConnector
from boho_analytics_platform.contracts import SyncRequest
from boho_analytics_platform.credentials import MemoryCredentialLease
from boho_analytics_platform.models import QueryWindow
from boho_analytics_platform.reporting import ReportService
from boho_analytics_platform.storage import SQLiteMetricStore
from support import config_text, write_fixture


class FakeHttp:
    def __init__(self, rows=None): self.body = None; self.rows = rows
    def request(self, method, url, *, headers=None, body=None):
        self.body = body
        rows = self.rows if self.rows is not None else [
            {"received_at": "2026-07-01T10:00:00Z", "form_id": "contact", "notification_status": "sent", "aggregate_count": 2}]
        return {"result": [{"results": rows}]}


class FormsConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup); self.root = Path(self.temporary.name)
        self.fixture = self.root / "fixture.json"; write_fixture(self.fixture)

    def _window(self): return QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 2, tzinfo=UTC), "UTC")

    @staticmethod
    def _chicago_window():
        zone = ZoneInfo("America/Chicago")
        return QueryWindow(datetime(2026, 7, 1, tzinfo=zone), datetime(2026, 7, 2, tzinfo=zone), "America/Chicago")

    @staticmethod
    def _with_timezone(text, timezone):
        return text.replace('timezone = "UTC"', f'timezone = "{timezone}"')

    def test_d1_connector_aggregates_without_selecting_payload(self):
        text = config_text(self.root / "state.db", self.fixture, provider="cloudflare-forms", credential_ref="none:test",
            options='account_id = "account"\ndatabase_id = "database"')
        path = self.root / "config.toml"; path.write_text(text, encoding="utf-8"); config = load_config(path); http = FakeHttp()
        points = list(CloudflareFormsConnector(config, http).collect(config.connections[0], MemoryCredentialLease({"api_token": b"token"}), SyncRequest(config.bindings[0], self._window(), ())))
        self.assertNotIn("payload_json", http.body["sql"].casefold()); self.assertNotIn("select *", http.body["sql"].casefold())
        self.assertEqual({point.metric for point in points}, {
            "forms.failed", "forms.pending", "forms.sent", "forms.submissions",
        })
        self.assertEqual({point.metric: point.value for point in points}, {
            "forms.failed": 0, "forms.pending": 0, "forms.sent": 2, "forms.submissions": 2,
        })

    def test_d1_connector_zeros_configured_and_observed_form_ids_for_every_requested_day(self):
        text = config_text(self.root / "state.db", self.fixture, provider="cloudflare-forms", credential_ref="none:test",
            options='account_id = "account"\ndatabase_id = "database"')
        text += 'filters = { form_id = "contact" }\n'
        path = self.root / "config.toml"; path.write_text(text, encoding="utf-8"); config = load_config(path)
        window = QueryWindow(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 3, tzinfo=UTC), "UTC")
        http = FakeHttp([{"received_at": "2026-07-01T10:00:00Z", "form_id": "quote",
            "notification_status": "sent", "aggregate_count": 1}])
        points = list(CloudflareFormsConnector(config, http).collect(
            config.connections[0], MemoryCredentialLease({"api_token": b"token"}),
            SyncRequest(config.bindings[0], window, ())))
        self.assertEqual({dict(point.dimensions)["form_id"] for point in points}, {"contact", "quote"})
        self.assertEqual(len(points), 16)
        values = {
            (point.start.date().isoformat(), dict(point.dimensions)["form_id"], point.metric): point.value
            for point in points
        }
        expected_metrics = {"forms.failed", "forms.pending", "forms.sent", "forms.submissions"}
        for day in ("2026-07-01", "2026-07-02"):
            for form_id in ("contact", "quote"):
                self.assertEqual({metric for point_day, point_form, metric in values
                    if point_day == day and point_form == form_id}, expected_metrics)
        self.assertEqual(values[("2026-07-01", "quote", "forms.sent")], 1)
        self.assertEqual(values[("2026-07-01", "quote", "forms.pending")], 0)
        self.assertEqual(values[("2026-07-02", "quote", "forms.submissions")], 0)
        self.assertEqual(values[("2026-07-01", "contact", "forms.submissions")], 0)

    def test_d1_connector_pending_to_sent_sync_overwrites_pending_with_zero(self):
        text = config_text(self.root / "state.db", self.fixture, provider="cloudflare-forms", credential_ref="none:test",
            options='account_id = "account"\ndatabase_id = "database"')
        text += 'filters = { form_id = "contact" }\n'
        path = self.root / "config.toml"; path.write_text(text, encoding="utf-8"); config = load_config(path)
        connector = CloudflareFormsConnector(config, FakeHttp([{
            "received_at": "2026-07-01T10:00:00Z", "form_id": "contact",
            "notification_status": "pending", "aggregate_count": 1,
        }]))
        request = SyncRequest(config.bindings[0], self._window(), ())
        credential = MemoryCredentialLease({"api_token": b"token"})
        store = SQLiteMetricStore(self.root / "facts.db"); store.initialize()
        store.upsert(connector.collect(config.connections[0], credential, request))
        connector.http = FakeHttp([{
            "received_at": "2026-07-01T10:00:00Z", "form_id": "contact",
            "notification_status": "sent", "aggregate_count": 1,
        }])
        store.upsert(connector.collect(config.connections[0], credential, request))
        points = store.query(client_id="example-client", site_ids=["example-site"], metric_ids=[
            "forms.failed", "forms.pending", "forms.sent", "forms.submissions",
        ], window=self._window())
        self.assertEqual({point.metric: point.value for point in points}, {
            "forms.failed": 0, "forms.pending": 0, "forms.sent": 1, "forms.submissions": 1,
        })

    def test_d1_connector_filters_by_instant_and_groups_in_site_timezone(self):
        text = config_text(self.root / "state.db", self.fixture, provider="cloudflare-forms", credential_ref="none:test",
            options='account_id = "account"\ndatabase_id = "database"')
        text = self._with_timezone(text, "America/Chicago")
        path = self.root / "config.toml"; path.write_text(text, encoding="utf-8"); config = load_config(path)
        http = FakeHttp([{"received_at": "2026-07-02T00:30:00Z", "form_id": "contact",
            "notification_status": "sent", "aggregate_count": 1}])
        points = list(CloudflareFormsConnector(config, http).collect(
            config.connections[0], MemoryCredentialLease({"api_token": b"token"}),
            SyncRequest(config.bindings[0], self._chicago_window(), ())))
        self.assertNotIn("substr(received_at", http.body["sql"].casefold())
        self.assertIn("julianday(received_at)", http.body["sql"].casefold())
        self.assertEqual(http.body["params"][1:], ["2026-07-01T05:00:00+00:00", "2026-07-02T05:00:00+00:00"])
        self.assertTrue(all(point.start.date().isoformat() == "2026-07-01" for point in points))

    def test_d1_connector_sums_same_day_status_rows_before_upsert(self):
        text = config_text(self.root / "state.db", self.fixture, provider="cloudflare-forms", credential_ref="none:test",
            options='account_id = "account"\ndatabase_id = "database"')
        path = self.root / "config.toml"; path.write_text(text, encoding="utf-8"); config = load_config(path)
        http = FakeHttp([
            {"received_at": "2026-07-01T10:00:00Z", "form_id": "contact",
             "notification_status": "sent", "aggregate_count": 2},
            {"received_at": "2026-07-01T11:00:00Z", "form_id": "contact",
             "notification_status": "sent", "aggregate_count": 3},
        ])
        points = list(CloudflareFormsConnector(config, http).collect(
            config.connections[0], MemoryCredentialLease({"api_token": b"token"}),
            SyncRequest(config.bindings[0], self._window(), ())))
        values = {point.metric: point.value for point in points}
        self.assertEqual(values["forms.sent"], 5)
        self.assertEqual(values["forms.submissions"], 5)
        self.assertEqual(len([point for point in points if point.metric == "forms.sent"]), 1)

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

    def test_inbox_connector_filters_by_instant_and_groups_in_site_timezone(self):
        database = self.root / "mail.db"
        with closing(sqlite3.connect(database)) as db:
            db.executescript("""CREATE TABLE mailboxes(id INTEGER PRIMARY KEY,mailbox_key TEXT);
              CREATE TABLE messages(id INTEGER PRIMARY KEY,received_at TEXT,from_address TEXT,subject TEXT,direction TEXT);
              CREATE TABLE mailbox_locations(mailbox_id INTEGER,message_id INTEGER,flags_json TEXT);
              INSERT INTO mailboxes VALUES(1,'forms');
              INSERT INTO messages VALUES(1,'2026-07-02T00:30:00Z','sender@example.invalid','Form submission','inbound');
              INSERT INTO mailbox_locations VALUES(1,1,'[]');""")
            db.commit()
        text = config_text(self.root / "state.db", self.fixture, provider="forms-inbox", options=f'database_path = "{database.as_posix()}"')
        text = self._with_timezone(text, "America/Chicago")
        text = text.replace('resource_id = "demo"', 'resource_id = "forms"\n[bindings.options]\nmailbox_key = "forms"')
        path = self.root / "config.toml"; path.write_text(text, encoding="utf-8"); config = load_config(path)
        points = list(FormsInboxConnector(config, None).collect(
            config.connections[0], MemoryCredentialLease({}),
            SyncRequest(config.bindings[0], self._chicago_window(), ())))
        self.assertEqual([point.start.date().isoformat() for point in points], ["2026-07-01", "2026-07-01"])

    def test_inbox_connector_emits_zero_facts_for_each_missing_requested_day(self):
        database = self.root / "mail.db"
        with closing(sqlite3.connect(database)) as db:
            db.executescript("""CREATE TABLE mailboxes(id INTEGER PRIMARY KEY,mailbox_key TEXT);
              CREATE TABLE messages(id INTEGER PRIMARY KEY,received_at TEXT,from_address TEXT,subject TEXT,direction TEXT);
              CREATE TABLE mailbox_locations(mailbox_id INTEGER,message_id INTEGER,flags_json TEXT);
              INSERT INTO mailboxes VALUES(1,'forms');
              INSERT INTO messages VALUES(1,'2026-07-01T10:00:00Z','sender@example.invalid','Form submission','inbound');
              INSERT INTO mailbox_locations VALUES(1,1,'[]');""")
            db.commit()
        text = config_text(self.root / "state.db", self.fixture, provider="forms-inbox", options=f'database_path = "{database.as_posix()}"')
        text = text.replace('resource_id = "demo"', 'resource_id = "forms"\n[bindings.options]\nmailbox_key = "forms"')
        path = self.root / "config.toml"; path.write_text(text, encoding="utf-8"); config = load_config(path)
        window = QueryWindow(self._window().start, self._window().end + timedelta(days=1), "UTC")
        points = list(FormsInboxConnector(config, None).collect(
            config.connections[0], MemoryCredentialLease({}),
            SyncRequest(config.bindings[0], window, ())))
        values = {
            (point.start.date().isoformat(), point.metric): point.value
            for point in points
        }
        self.assertEqual(values, {
            ("2026-07-01", "forms.inbox-deliveries"): 1,
            ("2026-07-01", "forms.inbox-unread"): 1,
            ("2026-07-02", "forms.inbox-deliveries"): 0,
            ("2026-07-02", "forms.inbox-unread"): 0,
        })

    def test_inbox_report_sums_daily_unread_messages_across_the_window(self):
        database = self.root / "mail.db"
        with closing(sqlite3.connect(database)) as db:
            db.executescript("""CREATE TABLE mailboxes(id INTEGER PRIMARY KEY,mailbox_key TEXT);
              CREATE TABLE messages(id INTEGER PRIMARY KEY,received_at TEXT,from_address TEXT,subject TEXT,direction TEXT);
              CREATE TABLE mailbox_locations(mailbox_id INTEGER,message_id INTEGER,flags_json TEXT);
              INSERT INTO mailboxes VALUES(1,'forms');
              INSERT INTO messages VALUES(1,'2026-07-01T10:00:00Z','one@example.invalid','Form submission','inbound');
              INSERT INTO messages VALUES(2,'2026-07-02T10:00:00Z','two@example.invalid','Form submission','inbound');
              INSERT INTO mailbox_locations VALUES(1,1,'[]');
              INSERT INTO mailbox_locations VALUES(1,2,'["\\Seen"]');""")
            db.commit()
        text = config_text(self.root / "state.db", self.fixture, provider="forms-inbox", options=f'database_path = "{database.as_posix()}"')
        text = text.replace('resource_id = "demo"', 'resource_id = "forms"\n[bindings.options]\nmailbox_key = "forms"')
        text = text.replace(
            'metric_ids = ["umami.pageviews", "forms.submissions", "forms.inbox-deliveries"]',
            'metric_ids = ["forms.inbox-deliveries", "forms.inbox-unread"]', 1)
        path = self.root / "config.toml"; path.write_text(text, encoding="utf-8"); config = load_config(path)
        window = QueryWindow(self._window().start, self._window().end + timedelta(days=1), "UTC")
        points = list(FormsInboxConnector(config, None).collect(
            config.connections[0], MemoryCredentialLease({}),
            SyncRequest(config.bindings[0], window, ())))
        unread = [point.value for point in points if point.metric == "forms.inbox-unread"]
        self.assertEqual(unread, [1, 0])
        store = SQLiteMetricStore(self.root / "facts.db"); store.initialize(); store.upsert(points)
        rows = ReportService(config, store).render("summary", window)["rows"]
        values = {row["metric"]: row["value"] for row in rows}
        self.assertEqual(values["forms.inbox-deliveries"], 2)
        self.assertEqual(values["forms.inbox-unread"], 1)

    def test_d1_probe_queries_each_configured_site_resource(self):
        text = config_text(self.root / "state.db", self.fixture, provider="cloudflare-forms", credential_ref="none:test",
            options='account_id = "account"\ndatabase_id = "database"')
        path = self.root / "config.toml"; path.write_text(text, encoding="utf-8"); config = load_config(path); http = FakeHttp([])
        snapshot = CloudflareFormsConnector(config, http).probe(
            config.connections[0], MemoryCredentialLease({"api_token": b"token"}))
        self.assertEqual(snapshot.resources, ("demo",))
        self.assertIn("WHERE site_id = ?", http.body["sql"])
        self.assertEqual(http.body["params"], ["demo"])


if __name__ == "__main__": unittest.main()
