"""Read-only aggregate delivery-evidence adapter for comms-platform SQLite."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..models import CapabilitySnapshot
from .common import binding_site, connection_bindings, daily_point, timestamp_day


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
            resources = tuple(sorted({str(binding.options.get("mailbox_key", binding.resource_id))
                for binding in connection_bindings(self.config, connection.id)}))
            for mailbox_key in resources:
                if db.execute("SELECT 1 FROM mailboxes WHERE mailbox_key = ? LIMIT 1", (mailbox_key,)).fetchone() is None:
                    raise ValueError(f"configured forms inbox mailbox is unavailable: {mailbox_key}")
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, resources,
            ("forms.inbox-deliveries", "forms.inbox-unread"))

    @staticmethod
    def _requested_days(window, timezone):
        zone = UTC if timezone == "UTC" else ZoneInfo(timezone)
        day = window.start.astimezone(zone).date()
        end = window.end.astimezone(zone).date()
        while day < end:
            yield day
            day += timedelta(days=1)

    def collect(self, connection, _credential, request):
        # Only aggregate counts and dates leave SQLite. Content/address columns are never selected.
        options = request.binding.options; mailbox_key = str(options.get("mailbox_key", request.binding.resource_id))
        clauses = ["mb.mailbox_key = ?", "julianday(m.received_at) >= julianday(?)",
            "julianday(m.received_at) < julianday(?)", "m.direction = 'inbound'"]
        params: list[object] = [mailbox_key, request.window.start.astimezone(UTC).isoformat(),
            request.window.end.astimezone(UTC).isoformat()]
        sender = options.get("sender_contains"); subject = options.get("subject_contains")
        if sender:
            clauses.append("instr(lower(m.from_address), lower(?)) > 0"); params.append(str(sender))
        if subject:
            clauses.append("instr(lower(m.subject), lower(?)) > 0"); params.append(str(subject))
        sql = f"""SELECT m.received_at, COUNT(*) AS aggregate_count,
          SUM(CASE WHEN instr(lower(ml.flags_json), '\\seen') = 0 THEN 1 ELSE 0 END) AS unread_count
          FROM messages m JOIN mailbox_locations ml ON ml.message_id=m.id JOIN mailboxes mb ON mb.id=ml.mailbox_id
          WHERE {' AND '.join(clauses)} GROUP BY m.received_at ORDER BY m.received_at"""
        with self._connect(self._path(connection)) as db:
            rows = db.execute(sql, params).fetchall()
        site = binding_site(self.config, request.binding.site_id)
        daily: dict[object, list[int]] = {}
        for row in rows:
            day = timestamp_day(row["received_at"], site.timezone)
            aggregate = daily.setdefault(day, [0, 0])
            aggregate[0] += int(row["aggregate_count"])
            aggregate[1] += int(row["unread_count"] or 0)
        for day in self._requested_days(request.window, site.timezone):
            deliveries, unread = daily.get(day, [0, 0])
            yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                metric="forms.inbox-deliveries", unit="count", day=day,
                value=deliveries, timezone=site.timezone)
            yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                metric="forms.inbox-unread", unit="count", day=day,
                value=unread, timezone=site.timezone)
