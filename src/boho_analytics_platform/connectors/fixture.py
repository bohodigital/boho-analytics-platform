"""Deterministic, explicitly sanitized fixture connector."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ..models import CapabilitySnapshot
from .common import binding_site, daily_point


class FixtureConnector:
    provider = "fixture"

    def __init__(self, config, _http) -> None:
        self.config = config

    def _load(self, connection):
        path = self.config.resolve_path(str(connection.options.get("path", "")))
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("points"), list):
            raise ValueError("fixture must contain a points array")
        forbidden = {"payload_json", "body", "body_preview", "body_index", "email", "address", "token", "ip", "user_agent"}
        def keys(value):
            if isinstance(value, dict):
                for key, child in value.items(): yield str(key).casefold(); yield from keys(child)
            elif isinstance(value, list):
                for child in value: yield from keys(child)
        if forbidden.intersection(keys(data)):
            raise ValueError("fixture contains a forbidden data field")
        return data

    def probe(self, connection, _credential):
        data = self._load(connection)
        metrics = sorted({str(item["metric"]) for item in data["points"]})
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True,
            tuple(sorted({str(item.get("resource_id", "demo")) for item in data["points"]})), tuple(metrics))

    def collect(self, connection, _credential, request):
        data = self._load(connection); site = binding_site(self.config, request.binding.site_id)
        for item in data["points"]:
            if str(item.get("resource_id", request.binding.resource_id)) != request.binding.resource_id: continue
            day = datetime.fromisoformat(str(item["date"])).date()
            if not request.window.start.date() <= day < request.window.end.date(): continue
            yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider,
                metric=str(item["metric"]), unit=str(item.get("unit", "count")), day=day,
                value=item["value"], timezone=site.timezone,
                dimensions={str(k): str(v) for k, v in item.get("dimensions", {}).items()})
