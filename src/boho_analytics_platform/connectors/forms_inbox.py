"""Read-only aggregate delivery-evidence adapter for comms-platform SQLite."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..models import CapabilitySnapshot
from .common import binding_site, daily_point


class FormsInboxConnector:
    provider = "forms-inbox"

    def __init__(self, config, _http) -> None: self.config = config

    @staticmethod
    @contextmanager
    def _connect(path: Path):
        if not path.is_file(): raise ValueError("forms inbox database is unavailable")
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        try:
            yield connection
        finally:
            connection.close()

    def _path(self, connection) -> Path:
        value = connection.options.get("database_path")
        if not isinstance(value, str) or not value: raise ValueError("forms-inbox connection requires database_path")
        return self.config.resolve_path(value)

    def probe(self, connection, _credential):
        required = {"messages", "mailbox_locations", "mailboxes"}
        with self._connect(self._path(connection)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required.issubset(tables): raise ValueError("forms inbox database does not match the supported schema")
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, (),
            ("forms.inbox-deliveries", "forms.inbox-unread"))

    def collect(self, connection, _credential, request):
        # Only aggregate counts and dates leave SQLite. Content/address columns are never selected.
        options = request.binding.options; mailbox_key = str(options.get("mailbox_key", request.binding.resource_id))
        clauses = ["mb.mailbox_key = ?", "m.received_at >= ?", "m.received_at < ?", "m.direction = 'inbound'"]
        params: list[object] = [mailbox_key, request.window.start.isoformat(), request.window.end.isoformat()]
        sender = options.get("sender_contains"); subject = options.get("subject_contains")
        if sender:
            clauses.append("instr(lower(m.from_address), lower(?)) > 0"); params.append(str(sender))
        if subject:
            clauses.append("instr(lower(m.subject), lower(?)) > 0"); params.append(str(subject))
        sql = f"""SELECT substr(m.received_at,1,10) AS metric_day, COUNT(*) AS aggregate_count,
          SUM(CASE WHEN instr(lower(ml.flags_json), '\\seen') = 0 THEN 1 ELSE 0 END) AS unread_count
          FROM messages m JOIN mailbox_locations ml ON ml.message_id=m.id JOIN mailboxes mb ON mb.id=ml.mailbox_id
          WHERE {' AND '.join(clauses)} GROUP BY metric_day ORDER BY metric_day"""
        with self._connect(self._path(connection)) as db:
            rows = db.execute(sql, params).fetchall()
        site = binding_site(self.config, request.binding.site_id)
        for row in rows:
            yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                metric="forms.inbox-deliveries", unit="count", day=row["metric_day"],
                value=row["aggregate_count"], timezone=site.timezone)
            yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                metric="forms.inbox-unread", unit="count", day=row["metric_day"],
                value=row["unread_count"] or 0, timezone=site.timezone)
