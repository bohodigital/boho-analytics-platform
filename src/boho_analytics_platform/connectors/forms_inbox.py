"""Read-only aggregate delivery-evidence adapter for a SQLite mail index."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..models import CapabilitySnapshot
from .common import binding_site, connection_bindings, daily_point, timestamp_day


class FormsInboxConnector:
    provider = "forms-inbox"

    _MAX_SUBJECT_EXCLUDES = 16
    _MAX_SUBJECT_EXCLUDE_LENGTH = 128

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

    @classmethod
    def _subject_excludes(cls, options) -> tuple[str, ...]:
        raw = options.get("subject_excludes")
        if raw is None:
            return ()
        if not isinstance(raw, list) or not raw or len(raw) > cls._MAX_SUBJECT_EXCLUDES:
            raise ValueError(
                "forms-inbox subject_excludes must be a non-empty array of at most "
                f"{cls._MAX_SUBJECT_EXCLUDES} strings"
            )
        markers: list[str] = []
        normalized: set[str] = set()
        for value in raw:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("forms-inbox subject_excludes entries must be non-empty strings")
            marker = value.strip()
            if len(marker) > cls._MAX_SUBJECT_EXCLUDE_LENGTH:
                raise ValueError(
                    "forms-inbox subject_excludes entries must be at most "
                    f"{cls._MAX_SUBJECT_EXCLUDE_LENGTH} characters"
                )
            if any(ord(character) < 32 or ord(character) == 127 for character in marker):
                raise ValueError("forms-inbox subject_excludes entries must not contain control characters")
            folded = marker.casefold()
            if folded in normalized:
                raise ValueError("forms-inbox subject_excludes entries must be unique")
            normalized.add(folded)
            markers.append(marker)
        return tuple(markers)

    @staticmethod
    def _observation_start(options) -> date | None:
        raw = options.get("observation_start")
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError("forms-inbox observation_start must be an ISO date string")
        try:
            value = date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("forms-inbox observation_start must be an ISO date string") from exc
        if value.isoformat() != raw:
            raise ValueError("forms-inbox observation_start must use YYYY-MM-DD")
        return value

    def probe(self, connection, _credential):
        required = {"messages", "mailbox_locations", "mailboxes"}
        with self._connect(self._path(connection)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not required.issubset(tables): raise ValueError("forms inbox database does not match the supported schema")
            bindings = connection_bindings(self.config, connection.id)
            for binding in bindings:
                self._subject_excludes(binding.options)
                self._observation_start(binding.options)
            resources = tuple(sorted({str(binding.options.get("mailbox_key", binding.resource_id))
                for binding in bindings}))
            for mailbox_key in resources:
                if db.execute("SELECT 1 FROM mailboxes WHERE mailbox_key = ? LIMIT 1", (mailbox_key,)).fetchone() is None:
                    raise ValueError(f"configured forms inbox mailbox is unavailable: {mailbox_key}")
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, resources,
            ("forms.inbox-deliveries", "forms.inbox-unread"))

    @staticmethod
    def _requested_days(window, timezone):
        zone = UTC if timezone == "UTC" else ZoneInfo(timezone)
        day = window.start.astimezone(zone).date()
        while True:
            day_start = datetime.combine(day, time.min, zone)
            if day_start >= window.end:
                break
            day_end = datetime.combine(day + timedelta(days=1), time.min, zone)
            if day_start >= window.start and day_end <= window.end:
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
        for marker in self._subject_excludes(options):
            clauses.append("instr(lower(coalesce(m.subject, '')), lower(?)) = 0")
            params.append(marker)
        sql = f"""SELECT m.received_at, COUNT(DISTINCT m.id) AS aggregate_count,
          COUNT(DISTINCT CASE WHEN instr(lower(coalesce(ml.flags_json, '')), '\\seen') = 0
            THEN m.id END) AS unread_count
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
        observation_start = self._observation_start(options)
        for day in self._requested_days(request.window, site.timezone):
            if day not in daily and (observation_start is None or day < observation_start):
                continue
            deliveries, unread = daily.get(day, [0, 0])
            yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                metric="forms.inbox-deliveries", unit="count", day=day,
                value=deliveries, timezone=site.timezone)
            yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                metric="forms.inbox-unread", unit="count", day=day,
                value=unread, timezone=site.timezone)
