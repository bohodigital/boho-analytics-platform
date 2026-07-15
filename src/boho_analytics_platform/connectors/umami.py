"""Umami v3 read-only API adapter."""

from __future__ import annotations

from datetime import UTC, date, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from ..credentials import require_text
from ..models import CapabilitySnapshot
from .common import binding_site, daily_point, total_point


class UmamiConnector:
    provider = "umami"

    def __init__(self, config, http) -> None:
        self.config = config; self.http = http

    def _headers(self, connection, credential):
        api_key = credential.read("api_key")
        if api_key: return {"x-umami-api-key": api_key.decode("utf-8")}
        token = credential.read("token") or credential.read("value")
        if token: return {"Authorization": f"Bearer {token.decode('utf-8')}"}
        base = str(connection.options["base_url"]).rstrip("/")
        result = self.http.request("POST", f"{base}/api/auth/login", body={
            "username": require_text(credential, "username"), "password": require_text(credential, "password")})
        if not isinstance(result, dict) or not isinstance(result.get("token"), str): raise ValueError("Umami authentication response did not contain a token")
        return {"Authorization": f"Bearer {result['token']}"}

    def probe(self, connection, credential):
        base = str(connection.options["base_url"]).rstrip("/")
        result = self.http.request("GET", f"{base}/api/websites", headers=self._headers(connection, credential))
        data = result.get("data", result) if isinstance(result, dict) else result
        resources = tuple(sorted(str(item["id"]) for item in data if isinstance(item, dict) and "id" in item))
        return CapabilitySnapshot(connection.id, self.provider, datetime.now(UTC), True, resources,
            ("umami.pageviews", "umami.sessions", "umami.summary"))

    def collect(self, connection, credential, request):
        base = str(connection.options["base_url"]).rstrip("/"); headers = self._headers(connection, credential)
        query = urlencode({"startAt": int(request.window.start.timestamp() * 1000), "endAt": int(request.window.end.timestamp() * 1000), "unit": "day", "timezone": request.window.timezone})
        root = f"{base}/api/websites/{request.binding.resource_id}"
        pageviews = self.http.request("GET", f"{root}/pageviews?{query}", headers=headers)
        site = binding_site(self.config, request.binding.site_id)
        for metric, series in (("umami.pageviews", pageviews.get("pageviews", [])), ("umami.sessions", pageviews.get("sessions", []))):
            for item in series:
                day = _series_day(item["x"], request.window.timezone)
                yield daily_point(client_id=site.client_id, site_id=site.id, source=self.provider, metric=metric,
                    unit="count", day=day, value=item["y"], timezone=site.timezone)
        stats = self.http.request("GET", f"{root}/stats?{query}", headers=headers)
        for key, metric, unit in (("visitors", "umami.visitors", "count"), ("visits", "umami.visits", "count"),
                                  ("bounces", "umami.bounces", "count"), ("totaltime", "umami.total-time", "seconds")):
            value = stats.get(key)
            if isinstance(value, dict): value = value.get("value")
            if value is not None:
                yield total_point(client_id=site.client_id, site_id=site.id, source=self.provider, metric=metric,
                    unit=unit, start=request.window.start, end=request.window.end, value=value)


def _series_day(value, timezone: str) -> date:
    zone = UTC if timezone == "UTC" else ZoneInfo(timezone)
    if isinstance(value, str):
        stripped = value.strip()
        try:
            return date.fromisoformat(stripped)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            try:
                value = float(stripped)
            except ValueError as exc:
                raise ValueError("Umami returned an unsupported series timestamp") from exc
        else:
            return parsed.astimezone(zone).date() if parsed.tzinfo else parsed.date()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000, UTC).astimezone(zone).date()
    raise ValueError("Umami returned an unsupported series timestamp")
