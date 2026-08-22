"""Privacy-bounded page catalog, daily materialization, and evidence queries.

This module is the single calculation layer for the dashboard, evidence API,
CLI, and MCP adapter. It intentionally stores internal routes only. Search
query text, public hostnames, provider payloads, and credentials are outside
the contract.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import math
import re
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from .storage import CURRENT_IDENTITY_VERSIONS, SQLiteMetricStore


PAGE_METRICS = {
    "search.route-clicks": ("search-console", "clicks"),
    "search.route-impressions": ("search-console", "impressions"),
    "search.route-position": ("search-console", "position"),
    "umami.route-pageviews": ("umami", "pageviews"),
    "umami.route-visits": ("umami", "visits"),
    "google.page-path-views": ("google-analytics", "pageviews"),
    "google.landing-page-sessions": ("google-analytics", "sessions"),
    "google.route-engaged-sessions": ("google-analytics", "engaged_sessions"),
    "google.route-engagement-seconds": (
        "google-analytics",
        "engagement_seconds",
    ),
    "google.route-key-events": ("google-analytics", "key_events"),
}

METRIC_DEFINITIONS = {
    "umami_pageviews": "Umami pageview events attributed to normalized internal routes.",
    "umami_visits": "Umami visits attributed to normalized internal routes; not GA4 sessions.",
    "ga4_pageviews": "GA4 screenPageViews attributed to normalized page paths.",
    "ga4_sessions": "GA4 sessions attributed to normalized landing pages.",
    "gsc_clicks": "Google Search Console clicks for ordinary page-scope rows.",
    "gsc_impressions": "Google Search Console impressions for ordinary page-scope rows.",
    "gsc_ctr": "GSC clicks divided by GSC impressions; recomputed from summed counts.",
    "gsc_position": "GSC average position weighted by GSC impressions; lower is better.",
    "published_pages": "Current public sitemap URL fingerprints, not all discovered routes.",
    "indexed_pages": "Current sitemap URL fingerprints whose latest inspection is indexed.",
}

_SCHEME_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_CLUSTER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COMPLETENESS_RANK = {"unknown": 0, "realtime": 1, "provisional": 2, "final": 3}
_COUNT_FIELDS = {
    "clicks",
    "impressions",
    "pageviews",
    "visits",
    "sessions",
    "engaged_sessions",
}
_SORTS = {
    "impressions": "gsc_impressions",
    "clicks": "gsc_clicks",
    "pageviews": "umami_pageviews",
    "visits": "umami_visits",
    "ga4_pageviews": "ga4_pageviews",
    "sessions": "ga4_sessions",
    "route": "route",
}
_SEARCH_TYPES = {"web", "image", "video", "news", "discover", "googleNews"}


class SchemeValidationError(ValueError):
    """A clustering scheme violates the bounded declarative contract."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _page_id(site_id: str, route: str) -> str:
    return _sha(f"page-v1\0{site_id}\0{route}")


def normalize_internal_route(value: str) -> str:
    """Validate a previously normalized internal route without public URL text."""

    if not isinstance(value, str):
        raise ValueError("route must be text")
    route = value.strip()
    if (
        not route.startswith("/")
        or len(route.encode("utf-8")) > 2_048
        or "://" in route
        or "\x00" in route
        or "\r" in route
        or "\n" in route
    ):
        raise ValueError("route is not a bounded internal route")
    return route


def _route_from_public_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("sitemap entry is not a public URL")
    # Sitemap query strings are deliberately not retained. Provider feeds may
    # keep explicitly allowlisted internal query keys under their own contract.
    return normalize_internal_route(parsed.path or "/")


def _decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("page metric value is not numeric") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("page metric value must be finite and non-negative")
    return parsed


def _count(value: Decimal) -> int:
    integral = value.to_integral_value()
    if value != integral:
        raise ValueError("count metric is not an integer")
    return int(integral)


def _safe_date(value: str) -> str:
    if not _DATE.fullmatch(value):
        raise ValueError("provider date must use YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("provider date is not canonical")
    return value


def _bounded_window(start: str, end: str, *, max_days: int = 366) -> tuple[str, str]:
    start = _safe_date(start)
    end = _safe_date(end)
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    if not start_date < end_date:
        raise ValueError("start must precede the exclusive end date")
    if (end_date - start_date).days > max_days:
        raise ValueError(f"page intelligence window cannot exceed {max_days} days")
    return start, end


def _search_type(value: str) -> str:
    if value not in _SEARCH_TYPES:
        raise ValueError("unsupported Search Console surface")
    return value


def _cursor(offset: int) -> str | None:
    if offset <= 0:
        return None
    raw = f"page-intelligence-v1:{offset}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _cursor_offset(value: str | None) -> int:
    if not value:
        return 0
    if len(value) > 128:
        raise ValueError("cursor is invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
        prefix, number = raw.split(":", 1)
        offset = int(number)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("cursor is invalid") from exc
    if prefix != "page-intelligence-v1" or not 0 <= offset <= 1_000_000:
        raise ValueError("cursor is invalid")
    return offset


def _site_ids(config, selected: Iterable[str] | None) -> tuple[str, ...]:
    configured = tuple(item.id for item in config.sites)
    requested = set(selected or ())
    unknown = requested - set(configured)
    if unknown:
        raise ValueError(f"unknown site id(s): {', '.join(sorted(unknown))}")
    return tuple(item for item in configured if not requested or item in requested)


def default_scheme_definition() -> dict[str, object]:
    return {
        "schema_version": 1,
        "scheme_id": "path-sections",
        "name": "Path sections",
        "mode": "exclusive",
        "site_ids": [],
        "strategy": {"operator": "path-section", "depth": 1},
        "fallback": {"cluster_id": "root", "label": "Root"},
    }


def _bounded_label(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise SchemeValidationError(f"{field} must be text")
    value = value.strip()
    if not 1 <= len(value) <= 128 or any(ch in value for ch in "\r\n\x00"):
        raise SchemeValidationError(f"{field} must contain 1-128 safe characters")
    return value


def validate_scheme_definition(value: object) -> dict[str, object]:
    """Return a canonicalized, executable-code-free scheme definition."""

    if not isinstance(value, Mapping):
        raise SchemeValidationError("scheme must be a JSON object")
    allowed = {
        "schema_version",
        "scheme_id",
        "name",
        "mode",
        "site_ids",
        "rules",
        "strategy",
        "fallback",
    }
    unknown = set(value) - allowed
    if unknown:
        raise SchemeValidationError(f"unknown scheme field(s): {', '.join(sorted(unknown))}")
    if value.get("schema_version") != 1:
        raise SchemeValidationError("scheme schema_version must be 1")
    scheme_id = value.get("scheme_id")
    if not isinstance(scheme_id, str) or not _SCHEME_ID.fullmatch(scheme_id):
        raise SchemeValidationError("scheme_id must be a lowercase kebab identifier")
    name = _bounded_label(value.get("name"), "name")
    mode = value.get("mode")
    if mode not in {"exclusive", "multilabel"}:
        raise SchemeValidationError("mode must be exclusive or multilabel")
    raw_sites = value.get("site_ids", [])
    if not isinstance(raw_sites, list) or len(raw_sites) > 100:
        raise SchemeValidationError("site_ids must be a list of at most 100 identifiers")
    site_ids = []
    for item in raw_sites:
        if not isinstance(item, str) or not _SCHEME_ID.fullmatch(item):
            raise SchemeValidationError("site_ids contains an invalid identifier")
        if item not in site_ids:
            site_ids.append(item)
    fallback = value.get("fallback")
    if not isinstance(fallback, Mapping) or set(fallback) != {"cluster_id", "label"}:
        raise SchemeValidationError("fallback must contain cluster_id and label")
    fallback_id = fallback.get("cluster_id")
    if not isinstance(fallback_id, str) or not _CLUSTER_ID.fullmatch(fallback_id):
        raise SchemeValidationError("fallback cluster_id is invalid")
    clean_fallback = {
        "cluster_id": fallback_id,
        "label": _bounded_label(fallback.get("label"), "fallback label"),
    }
    has_rules = "rules" in value
    has_strategy = "strategy" in value
    if has_rules == has_strategy:
        raise SchemeValidationError("scheme must contain exactly one of rules or strategy")
    output: dict[str, object] = {
        "schema_version": 1,
        "scheme_id": scheme_id,
        "name": name,
        "mode": mode,
        "site_ids": site_ids,
        "fallback": clean_fallback,
    }
    if has_strategy:
        strategy = value.get("strategy")
        if not isinstance(strategy, Mapping) or set(strategy) != {"operator", "depth"}:
            raise SchemeValidationError("strategy must contain operator and depth")
        if strategy.get("operator") != "path-section":
            raise SchemeValidationError("unsupported strategy operator")
        depth = strategy.get("depth")
        if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 5:
            raise SchemeValidationError("path-section depth must be from 1 to 5")
        if mode != "exclusive":
            raise SchemeValidationError("path-section strategy must be exclusive")
        output["strategy"] = {"operator": "path-section", "depth": depth}
        return output

    raw_rules = value.get("rules")
    if not isinstance(raw_rules, list) or not 1 <= len(raw_rules) <= 200:
        raise SchemeValidationError("rules must contain 1-200 entries")
    cluster_ids = {fallback_id}
    clean_rules = []
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, Mapping):
            raise SchemeValidationError(f"rule {index + 1} must be an object")
        if set(raw_rule) != {"cluster_id", "label", "priority", "match"}:
            raise SchemeValidationError(
                f"rule {index + 1} must contain cluster_id, label, priority, and match"
            )
        cluster_id = raw_rule.get("cluster_id")
        if not isinstance(cluster_id, str) or not _CLUSTER_ID.fullmatch(cluster_id):
            raise SchemeValidationError(f"rule {index + 1} cluster_id is invalid")
        if cluster_id in cluster_ids:
            raise SchemeValidationError(f"duplicate cluster_id: {cluster_id}")
        cluster_ids.add(cluster_id)
        priority = raw_rule.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int) or not -10_000 <= priority <= 10_000:
            raise SchemeValidationError(f"rule {index + 1} priority is invalid")
        match = raw_rule.get("match")
        if not isinstance(match, Mapping) or not match or set(match) - {"path_prefixes", "path_globs"}:
            raise SchemeValidationError(
                f"rule {index + 1} match supports only path_prefixes and path_globs"
            )
        clean_match: dict[str, list[str]] = {}
        for kind in ("path_prefixes", "path_globs"):
            if kind not in match:
                continue
            patterns = match[kind]
            if not isinstance(patterns, list) or not 1 <= len(patterns) <= 50:
                raise SchemeValidationError(f"rule {index + 1} {kind} is invalid")
            clean_patterns = []
            for pattern in patterns:
                if not isinstance(pattern, str) or not pattern.startswith("/"):
                    raise SchemeValidationError(f"rule {index + 1} pattern must start with /")
                if (
                    len(pattern.encode("utf-8")) > 512
                    or "://" in pattern
                    or any(ch in pattern for ch in "\r\n\x00[]")
                    or (kind == "path_prefixes" and any(ch in pattern for ch in "*?"))
                ):
                    raise SchemeValidationError(f"rule {index + 1} contains an unsafe pattern")
                clean_patterns.append(pattern)
            clean_match[kind] = clean_patterns
        clean_rules.append({
            "cluster_id": cluster_id,
            "label": _bounded_label(raw_rule.get("label"), f"rule {index + 1} label"),
            "priority": priority,
            "match": clean_match,
            "_order": index,
        })
    clean_rules.sort(key=lambda item: (-int(item["priority"]), int(item["_order"])))
    for item in clean_rules:
        item.pop("_order")
    output["rules"] = clean_rules
    return output


def load_scheme(path: str | Path) -> dict[str, object]:
    raw = Path(path).read_bytes()
    if len(raw) > 65_536:
        raise SchemeValidationError("scheme file exceeds 64 KiB")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemeValidationError("scheme file must be valid UTF-8 JSON") from exc
    return validate_scheme_definition(value)


def _rule_matches(route: str, match: Mapping[str, object]) -> bool:
    return any(
        route.startswith(prefix)
        for prefix in match.get("path_prefixes", [])
    ) or any(
        fnmatch.fnmatchcase(route, pattern)
        for pattern in match.get("path_globs", [])
    )


def _slug_segment(segment: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", segment.casefold()).strip("-")
    return (slug or "other")[:64].rstrip("-") or "other"


def assign_route(definition: Mapping[str, object], site_id: str, route: str) -> list[tuple[str, str]]:
    if definition.get("site_ids") and site_id not in definition["site_ids"]:
        return []
    fallback = definition["fallback"]
    strategy = definition.get("strategy")
    if isinstance(strategy, Mapping):
        segments = [item for item in route.split("/") if item]
        depth = int(strategy["depth"])
        if len(segments) < depth:
            return [(str(fallback["cluster_id"]), str(fallback["label"]))]
        selected = segments[depth - 1]
        return [(_slug_segment(selected), selected.replace("-", " ").replace("_", " ").title())]
    matches = [
        (str(rule["cluster_id"]), str(rule["label"]))
        for rule in definition.get("rules", [])
        if _rule_matches(route, rule["match"])
    ]
    if definition["mode"] == "exclusive":
        return matches[:1] or [(str(fallback["cluster_id"]), str(fallback["label"]))]
    return matches or [(str(fallback["cluster_id"]), str(fallback["label"]))]


class PageIntelligenceService:
    def __init__(self, config, store: SQLiteMetricStore, *, now=None) -> None:
        self.config = config
        self.store = store
        self.now = now or _now
        self.sites = {item.id: item for item in config.sites}

    def _ensure_default_scheme(self, db: sqlite3.Connection, instant: datetime) -> str:
        definition = validate_scheme_definition(default_scheme_definition())
        return self._store_scheme(db, definition, instant, "built-in default")

    @staticmethod
    def _active_version_row(db: sqlite3.Connection, scheme_id: str):
        return db.execute(
            """SELECT v.version_id,v.definition_json,v.version_number
                 FROM page_scheme_activations AS a
                 JOIN page_scheme_versions AS v ON v.version_id=a.version_id
                WHERE a.scheme_id=?
                ORDER BY a.activated_at DESC,a.id DESC LIMIT 1""",
            (scheme_id,),
        ).fetchone()

    def _store_scheme(
        self,
        db: sqlite3.Connection,
        definition: Mapping[str, object],
        instant: datetime,
        reason: str,
    ) -> str:
        clean = validate_scheme_definition(definition)
        canonical = _canonical_json(clean)
        definition_hash = _sha(canonical)
        scheme_id = str(clean["scheme_id"])
        row = db.execute(
            """SELECT version_id FROM page_scheme_versions
                WHERE scheme_id=? AND definition_hash=?""",
            (scheme_id, definition_hash),
        ).fetchone()
        db.execute(
            """INSERT INTO page_schemes(scheme_id,name,mode,created_at)
                 VALUES (?,?,?,?)
                 ON CONFLICT(scheme_id) DO UPDATE SET
                   name=excluded.name,mode=excluded.mode""",
            (scheme_id, clean["name"], clean["mode"], _iso(instant)),
        )
        if row:
            version_id = str(row["version_id"])
        else:
            version_number = int(db.execute(
                "SELECT COALESCE(MAX(version_number),0)+1 FROM page_scheme_versions WHERE scheme_id=?",
                (scheme_id,),
            ).fetchone()[0])
            version_id = _sha(f"scheme-v1\0{scheme_id}\0{definition_hash}")
            db.execute(
                """INSERT INTO page_scheme_versions(
                     version_id,scheme_id,version_number,definition_json,
                     definition_hash,created_at
                   ) VALUES (?,?,?,?,?,?)""",
                (
                    version_id,
                    scheme_id,
                    version_number,
                    canonical,
                    definition_hash,
                    _iso(instant),
                ),
            )
        active = self._active_version_row(db, scheme_id)
        if not active or active["version_id"] != version_id:
            activation_id = _sha(
                f"activation-v1\0{scheme_id}\0{version_id}\0{_iso(instant)}\0{reason}"
            )
            db.execute(
                """INSERT INTO page_scheme_activations(
                     id,scheme_id,version_id,activated_at,reason
                   ) VALUES (?,?,?,?,?)""",
                (activation_id, scheme_id, version_id, _iso(instant), reason[:256]),
            )
        return version_id

    def apply_scheme(self, definition: Mapping[str, object], *, reason: str = "operator apply") -> dict[str, object]:
        clean = validate_scheme_definition(definition)
        instant = self.now()
        with self.store.connect() as db:
            version_id = self._store_scheme(db, clean, instant, reason)
            assigned = self._assign_version(db, version_id, clean, instant)
            version = db.execute(
                "SELECT version_number,definition_hash FROM page_scheme_versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
        return {
            "scheme_id": clean["scheme_id"],
            "version_id": version_id,
            "version_number": version["version_number"],
            "definition_hash": version["definition_hash"],
            "assignments": assigned,
            "activated_at": _iso(instant),
        }

    def activate_scheme_version(self, scheme_id: str, version_number: int, *, reason: str) -> dict[str, object]:
        if not _SCHEME_ID.fullmatch(scheme_id):
            raise ValueError("invalid scheme id")
        instant = self.now()
        with self.store.connect() as db:
            row = db.execute(
                """SELECT version_id,definition_json FROM page_scheme_versions
                    WHERE scheme_id=? AND version_number=?""",
                (scheme_id, version_number),
            ).fetchone()
            if not row:
                raise ValueError("scheme version does not exist")
            activation_id = _sha(
                f"activation-v1\0{scheme_id}\0{row['version_id']}\0{_iso(instant)}\0{reason}"
            )
            db.execute(
                """INSERT INTO page_scheme_activations(
                     id,scheme_id,version_id,activated_at,reason
                   ) VALUES (?,?,?,?,?)""",
                (activation_id, scheme_id, row["version_id"], _iso(instant), reason[:256]),
            )
            assigned = self._assign_version(
                db, row["version_id"], json.loads(row["definition_json"]), instant
            )
        return {
            "scheme_id": scheme_id,
            "version_id": row["version_id"],
            "version_number": version_number,
            "assignments": assigned,
            "activated_at": _iso(instant),
        }

    @staticmethod
    def _assign_version(
        db: sqlite3.Connection,
        version_id: str,
        definition: Mapping[str, object],
        instant: datetime,
    ) -> int:
        rows = db.execute("SELECT page_id,site_id,route FROM page_catalog ORDER BY page_id").fetchall()
        assignments = []
        for row in rows:
            for cluster_id, label in assign_route(definition, row["site_id"], row["route"]):
                assignments.append((version_id, row["page_id"], cluster_id, label, _iso(instant)))
        db.execute("DELETE FROM page_scheme_assignments WHERE version_id=?", (version_id,))
        db.executemany(
            """INSERT INTO page_scheme_assignments(
                 version_id,page_id,cluster_id,cluster_label,assigned_at
               ) VALUES (?,?,?,?,?)""",
            assignments,
        )
        return len(assignments)

    def preview_scheme(
        self,
        definition: Mapping[str, object],
        *,
        site_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        clean = validate_scheme_definition(definition)
        if site_id and site_id not in self.sites:
            raise ValueError("unknown site id")
        if not 1 <= limit <= 500:
            raise ValueError("preview limit must be from 1 to 500")
        where = "WHERE site_id=?" if site_id else ""
        params = (site_id,) if site_id else ()
        with self.store.connect(readonly=True) as db:
            rows = db.execute(
                f"SELECT site_id,route FROM page_catalog {where} ORDER BY site_id,route LIMIT ?",
                (*params, limit),
            ).fetchall()
        clusters: dict[str, dict[str, object]] = {}
        samples = []
        for row in rows:
            assigned = assign_route(clean, row["site_id"], row["route"])
            samples.append({"site_id": row["site_id"], "route": row["route"], "clusters": [item[0] for item in assigned]})
            for cluster_id, label in assigned:
                item = clusters.setdefault(cluster_id, {"cluster_id": cluster_id, "label": label, "pages": 0})
                item["pages"] = int(item["pages"]) + 1
        return {
            "definition": clean,
            "pages_examined": len(rows),
            "clusters": sorted(clusters.values(), key=lambda item: (-int(item["pages"]), str(item["cluster_id"]))),
            "samples": samples,
            "preview_only": True,
        }

    def record_sitemap_inventory(
        self,
        site_id: str,
        urls: Sequence[str],
        inventory_hash: str,
        observed_at: datetime,
    ) -> int:
        if site_id not in self.sites:
            raise ValueError("unknown site id")
        site = self.sites[site_id]
        records = [(_route_from_public_url(url), _sha(url)) for url in urls]
        instant = _iso(observed_at)
        with self.store.connect() as db:
            db.execute(
                """UPDATE page_catalog_sources SET current_member=0,last_seen_at=?
                    WHERE source='sitemap' AND page_id IN (
                      SELECT page_id FROM page_catalog WHERE site_id=?
                    )""",
                (instant, site_id),
            )
            db.execute("DELETE FROM page_catalog_index_links WHERE site_id=?", (site_id,))
            for route, url_hash in records:
                page_id = _page_id(site_id, route)
                db.execute(
                    """INSERT INTO page_catalog(
                         page_id,client_id,site_id,route,first_seen_at,last_seen_at
                       ) VALUES (?,?,?,?,?,?)
                       ON CONFLICT(site_id,route) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                    (page_id, site.client_id, site_id, route, instant, instant),
                )
                db.execute(
                    """INSERT INTO page_catalog_sources(
                         page_id,source,first_seen_at,last_seen_at,current_member
                       ) VALUES (?,'sitemap',?,?,1)
                       ON CONFLICT(page_id,source) DO UPDATE SET
                         last_seen_at=excluded.last_seen_at,current_member=1""",
                    (page_id, instant, instant),
                )
                db.execute(
                    """INSERT INTO page_catalog_index_links(
                         site_id,page_id,url_hash,inventory_hash,last_seen_at
                       ) VALUES (?,?,?,?,?)""",
                    (site_id, page_id, url_hash, inventory_hash, instant),
                )
            for scheme_id_row in db.execute(
                "SELECT scheme_id FROM page_schemes ORDER BY scheme_id"
            ).fetchall():
                scheme = self._active_version_row(db, scheme_id_row["scheme_id"])
                if scheme:
                    self._assign_version(
                        db, scheme["version_id"],
                        json.loads(scheme["definition_json"]), observed_at,
                    )
        return len(records)

    def materialize(self, selected_sites: Iterable[str] | None = None) -> dict[str, object]:
        site_ids = _site_ids(self.config, selected_sites)
        instant = self.now()
        run_id = uuid.uuid4().hex
        scope = _canonical_json(list(site_ids))
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO page_materialization_runs(
                     id,site_scope,started_at,status
                   ) VALUES (?,?,?,'running')""",
                (run_id, scope, _iso(instant)),
            )
        try:
            result = self._materialize(site_ids, instant)
        except Exception as exc:
            with self.store.connect() as db:
                db.execute(
                    """UPDATE page_materialization_runs
                          SET finished_at=?,status='failed',error_category=? WHERE id=?""",
                    (_iso(self.now()), type(exc).__name__[:60], run_id),
                )
            raise
        with self.store.connect() as db:
            db.execute(
                """UPDATE page_materialization_runs SET
                     finished_at=?,status='complete',source_facts=?,pages_seen=?,
                     daily_cells=?,source_facts_hash=? WHERE id=?""",
                (
                    _iso(self.now()),
                    result["source_facts"],
                    result["pages_seen"],
                    result["daily_cells"],
                    result["source_facts_hash"],
                    run_id,
                ),
            )
        return {"run_id": run_id, "status": "complete", **result}

    def _materialize(self, site_ids: Sequence[str], instant: datetime) -> dict[str, object]:
        placeholders = ",".join("?" for _ in site_ids)
        metric_placeholders = ",".join("?" for _ in PAGE_METRICS)
        params = [*site_ids, *PAGE_METRICS]
        with self.store.connect(readonly=True) as db:
            rows = db.execute(
                f"""SELECT point_key,client_id,site_id,source,metric,value,start_at,
                            dimensions_json,completeness,observed_at,identity_version
                       FROM metric_facts
                      WHERE site_id IN ({placeholders})
                        AND metric IN ({metric_placeholders})
                   ORDER BY point_key""",
                params,
            ).fetchall()
        groups: dict[tuple[str, str, str, str, str], dict[str, object]] = {}
        pages: dict[tuple[str, str], tuple[str, str, str]] = {}
        accepted_keys = []
        for row in rows:
            expected_identity = CURRENT_IDENTITY_VERSIONS.get(row["source"], 1)
            if int(row["identity_version"]) != expected_identity:
                continue
            dimensions = json.loads(row["dimensions_json"])
            route_value = dimensions.get("route")
            if not isinstance(route_value, str):
                continue
            route = normalize_internal_route(route_value)
            mapped_source, field = PAGE_METRICS[row["metric"]]
            if row["source"] != mapped_source:
                raise ValueError("page metric source does not match the metric catalog")
            if mapped_source == "search-console":
                if dimensions.get("observation_scope") != "page":
                    continue
                search_type = str(dimensions.get("search_type") or "web")
                date_label = _safe_date(str(dimensions.get("provider_date") or row["start_at"][:10]))
                timezone = str(dimensions.get("provider_timezone") or "America/Los_Angeles")
            else:
                search_type = ""
                date_label = _safe_date(row["start_at"][:10])
                timezone = self.sites[row["site_id"]].timezone
            if mapped_source == "search-console" and (
                len(search_type) > 32
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", search_type)
            ):
                raise ValueError("search type is invalid")
            value = _decimal(row["value"])
            if field in _COUNT_FIELDS:
                numeric: Decimal | int = _count(value)
            else:
                numeric = value
            key = (row["site_id"], route, date_label, mapped_source, search_type)
            cell = groups.setdefault(key, {
                "values": defaultdict(Decimal),
                "position_count": 0,
                "completeness": [],
                "states": set(),
                "observed_at": row["observed_at"],
                "timezone": timezone,
                "facts": [],
            })
            cell["values"][field] += Decimal(numeric)
            if field == "position":
                cell["position_count"] = int(cell["position_count"]) + 1
            cell["completeness"].append(str(row["completeness"]))
            cell["states"].add(str(dimensions.get("data_state") or "stored"))
            cell["observed_at"] = max(str(cell["observed_at"]), str(row["observed_at"]))
            cell["facts"].append(str(row["point_key"]))
            pages[(row["site_id"], route)] = (
                _page_id(row["site_id"], route), row["client_id"], mapped_source
            )
            accepted_keys.append(str(row["point_key"]))
        with self.store.connect() as db:
            for site_id in site_ids:
                db.execute(
                    "DELETE FROM page_daily WHERE page_id IN (SELECT page_id FROM page_catalog WHERE site_id=?)",
                    (site_id,),
                )
            for (site_id, route), (page_id, client_id, source) in pages.items():
                observed = max(
                    str(cell["observed_at"])
                    for key, cell in groups.items()
                    if key[0] == site_id and key[1] == route
                )
                db.execute(
                    """INSERT INTO page_catalog(
                         page_id,client_id,site_id,route,first_seen_at,last_seen_at
                       ) VALUES (?,?,?,?,?,?)
                       ON CONFLICT(site_id,route) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                    (page_id, client_id, site_id, route, observed, observed),
                )
                sources = {key[3] for key in groups if key[0] == site_id and key[1] == route}
                for page_source in sources:
                    db.execute(
                        """INSERT INTO page_catalog_sources(
                             page_id,source,first_seen_at,last_seen_at,current_member
                           ) VALUES (?,?,?,?,1)
                           ON CONFLICT(page_id,source) DO UPDATE SET
                             last_seen_at=excluded.last_seen_at,current_member=1""",
                        (page_id, page_source, observed, observed),
                    )
            for key, cell in groups.items():
                site_id, route, date_label, source, search_type = key
                page_id = _page_id(site_id, route)
                values = cell["values"]
                completeness = min(
                    cell["completeness"], key=lambda item: _COMPLETENESS_RANK.get(item, -1)
                )
                if completeness not in _COMPLETENESS_RANK:
                    completeness = "unknown"
                states = sorted(cell["states"])
                data_state = states[0] if len(states) == 1 else "mixed"
                impressions = _count(values["impressions"]) if "impressions" in values else None
                position_weight = None
                if "position" in values and impressions is not None:
                    average_position = values["position"] / int(cell["position_count"])
                    position_weight = float(average_position * impressions)
                facts_hash = _sha("\n".join(sorted(cell["facts"])))
                record = {
                    "pageviews": _count(values["pageviews"]) if "pageviews" in values else None,
                    "visits": _count(values["visits"]) if "visits" in values else None,
                    "sessions": _count(values["sessions"]) if "sessions" in values else None,
                    "engaged_sessions": _count(values["engaged_sessions"]) if "engaged_sessions" in values else None,
                    "engagement_seconds": float(values["engagement_seconds"]) if "engagement_seconds" in values else None,
                    "key_events": float(values["key_events"]) if "key_events" in values else None,
                    "clicks": _count(values["clicks"]) if "clicks" in values else None,
                    "impressions": impressions,
                }
                if source == "search-console" and (
                    record["clicks"] is None or record["impressions"] is None
                ):
                    raise ValueError(
                        "Search Console page cell lacks explicit clicks or impressions"
                    )
                db.execute(
                    """INSERT INTO page_daily(
                         page_id,date_label,source,search_type,pageviews,visits,sessions,
                         engaged_sessions,engagement_seconds,key_events,clicks,impressions,
                         position_weight,completeness,data_state,provider_timezone,
                         observed_at,source_facts_hash,materialized_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        page_id, date_label, source, search_type,
                        record["pageviews"], record["visits"], record["sessions"],
                        record["engaged_sessions"], record["engagement_seconds"],
                        record["key_events"], record["clicks"], record["impressions"],
                        position_weight, completeness, data_state, cell["timezone"],
                        cell["observed_at"], facts_hash, _iso(instant),
                    ),
                )
            self._ensure_default_scheme(db, instant)
            for scheme_id_row in db.execute(
                "SELECT scheme_id FROM page_schemes ORDER BY scheme_id"
            ).fetchall():
                version = self._active_version_row(db, scheme_id_row["scheme_id"])
                if version:
                    self._assign_version(
                        db, version["version_id"],
                        json.loads(version["definition_json"]), instant,
                    )
        return {
            "sites": list(site_ids),
            "source_facts": len(accepted_keys),
            "pages_seen": len(pages),
            "daily_cells": len(groups),
            "source_facts_hash": _sha("\n".join(sorted(accepted_keys))),
            "materialized_at": _iso(instant),
        }

    def _evidence(
        self,
        *,
        endpoint: str,
        start: str | None,
        end: str | None,
        site_ids: Sequence[str],
        search_type: str,
        scheme: Mapping[str, object] | None,
        data: object,
        pagination: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        with self.store.connect(readonly=True) as db:
            latest = db.execute(
                """SELECT finished_at,source_facts_hash,status
                     FROM page_materialization_runs
                    ORDER BY started_at DESC LIMIT 1"""
            ).fetchone()
            freshness = db.execute(
                """SELECT d.source,MAX(d.date_label) AS data_through,
                          MAX(d.observed_at) AS observed_at,
                          COUNT(*) AS cells
                     FROM page_daily AS d
                     JOIN page_catalog AS p ON p.page_id=d.page_id
                    WHERE p.site_id IN ({})
                    GROUP BY d.source ORDER BY d.source""".format(
                    ",".join("?" for _ in site_ids)
                ),
                list(site_ids),
            ).fetchall()
        return {
            "schema_version": 1,
            "endpoint": endpoint,
            "generated_at": _iso(self.now()),
            "filters": {
                "start": start,
                "end_exclusive": end,
                "site_ids": list(site_ids),
                "search_type": search_type,
                "scheme_id": scheme.get("scheme_id") if scheme else None,
                "scheme_version": scheme.get("version_number") if scheme else None,
            },
            "metric_definitions": METRIC_DEFINITIONS,
            "materialization": {
                "status": latest["status"] if latest else "never-run",
                "finished_at": latest["finished_at"] if latest else None,
                "source_facts_hash": latest["source_facts_hash"] if latest else None,
            },
            "freshness": [dict(row) for row in freshness],
            "completeness_notes": [
                "Search Console page rows are top-row exports and may be incomplete even when dates are present.",
                "Missing provider/date/page cells remain missing; they are not manufactured as zero.",
                "Umami visits, GA4 sessions, and provider pageview metrics are not interchangeable.",
                "The exclusive end date is not included in totals.",
            ],
            "pagination": dict(pagination or {}),
            "data": data,
        }

    def _scheme(self, scheme_id: str) -> dict[str, object]:
        if not _SCHEME_ID.fullmatch(scheme_id):
            raise ValueError("invalid scheme id")
        with self.store.connect(readonly=True) as db:
            row = self._active_version_row(db, scheme_id)
        if not row:
            raise ValueError("scheme is not active")
        definition = json.loads(row["definition_json"])
        return {
            "scheme_id": scheme_id,
            "version_id": row["version_id"],
            "version_number": row["version_number"],
            "name": definition["name"],
            "mode": definition["mode"],
            "definition": definition,
        }

    @staticmethod
    def _aggregate_select(alias: str = "d") -> str:
        return f"""
          SUM(CASE WHEN {alias}.source='umami' THEN {alias}.pageviews END) AS umami_pageviews,
          SUM(CASE WHEN {alias}.source='umami' THEN {alias}.visits END) AS umami_visits,
          SUM(CASE WHEN {alias}.source='google-analytics' THEN {alias}.pageviews END) AS ga4_pageviews,
          SUM(CASE WHEN {alias}.source='google-analytics' THEN {alias}.sessions END) AS ga4_sessions,
          SUM(CASE WHEN {alias}.source='google-analytics' THEN {alias}.engaged_sessions END) AS ga4_engaged_sessions,
          SUM(CASE WHEN {alias}.source='google-analytics' THEN {alias}.engagement_seconds END) AS ga4_engagement_seconds,
          SUM(CASE WHEN {alias}.source='google-analytics' THEN {alias}.key_events END) AS ga4_key_events,
          SUM(CASE WHEN {alias}.source='search-console' THEN {alias}.clicks END) AS gsc_clicks,
          SUM(CASE WHEN {alias}.source='search-console' THEN {alias}.impressions END) AS gsc_impressions,
          SUM(CASE WHEN {alias}.source='search-console' THEN {alias}.position_weight END) AS gsc_position_weight
        """

    @staticmethod
    def _metric_row(row: Mapping[str, object]) -> dict[str, object]:
        output = {
            key: row[key]
            for key in (
                "umami_pageviews", "umami_visits", "ga4_pageviews", "ga4_sessions",
                "ga4_engaged_sessions", "ga4_engagement_seconds", "ga4_key_events",
                "gsc_clicks", "gsc_impressions",
            )
            if key in row.keys()
        }
        impressions = output.get("gsc_impressions")
        clicks = output.get("gsc_clicks")
        weight = row["gsc_position_weight"] if "gsc_position_weight" in row.keys() else None
        output["gsc_ctr"] = (
            float(clicks) / float(impressions)
            if clicks is not None and impressions not in {None, 0}
            else None
        )
        output["gsc_position"] = (
            float(weight) / float(impressions)
            if weight is not None and impressions not in {None, 0}
            else None
        )
        return output

    def properties(
        self,
        start: str,
        end: str,
        *,
        site_ids: Iterable[str] | None = None,
        search_type: str = "web",
    ) -> dict[str, object]:
        start, end = _bounded_window(start, end)
        search_type = _search_type(search_type)
        selected = _site_ids(self.config, site_ids)
        placeholders = ",".join("?" for _ in selected)
        with self.store.connect(readonly=True) as db:
            rows = db.execute(
                f"""SELECT p.site_id,{self._aggregate_select()},
                           COUNT(DISTINCT p.page_id) AS discovered_routes
                      FROM page_catalog AS p
                      LEFT JOIN page_daily AS d ON d.page_id=p.page_id
                       AND d.date_label>=? AND d.date_label<?
                       AND (d.source!='search-console' OR d.search_type=?)
                     WHERE p.site_id IN ({placeholders})
                     GROUP BY p.site_id ORDER BY p.site_id""",
                (start, end, search_type, *selected),
            ).fetchall()
            coverage = {
                row["site_id"]: dict(row)
                for row in db.execute(
                    f"""SELECT i.site_id,i.published_pages,
                               SUM(CASE WHEN s.indexed=1 THEN 1 ELSE 0 END) AS indexed_observed_pages,
                               SUM(CASE WHEN s.inspected_at IS NOT NULL THEN 1 ELSE 0 END) AS inspected_pages,
                               MAX(s.inspected_at) AS last_inspected_at
                          FROM index_coverage_inventories AS i
                          LEFT JOIN index_coverage_url_status AS s
                            ON s.site_id=i.site_id AND s.inventory_hash=i.inventory_hash
                         WHERE i.site_id IN ({placeholders}) GROUP BY i.site_id""",
                    selected,
                ).fetchall()
            }
        output = []
        by_site = {row["site_id"]: row for row in rows}
        for site_id in selected:
            row = by_site.get(site_id)
            metrics = self._metric_row(row) if row else {key: None for key in (
                "umami_pageviews", "umami_visits", "ga4_pageviews", "ga4_sessions",
                "ga4_engaged_sessions", "ga4_engagement_seconds", "ga4_key_events",
                "gsc_clicks", "gsc_impressions", "gsc_ctr", "gsc_position",
            )}
            item = {
                "site_id": site_id,
                "site_name": self.sites[site_id].name,
                "discovered_routes": row["discovered_routes"] if row else 0,
                **metrics,
            }
            indexed = coverage.get(site_id)
            coverage_complete = bool(
                indexed
                and indexed["published_pages"] > 0
                and indexed["published_pages"] == indexed["inspected_pages"]
            )
            item.update({
                "published_pages": indexed["published_pages"] if indexed else None,
                "indexed_pages": (
                    indexed["indexed_observed_pages"]
                    if coverage_complete else None
                ),
                "indexed_observed_pages": (
                    indexed["indexed_observed_pages"] if indexed else None
                ),
                "inspected_pages": indexed["inspected_pages"] if indexed else None,
                "indexed_percentage": (
                    float(indexed["indexed_observed_pages"]) / float(indexed["published_pages"])
                    if coverage_complete and indexed["published_pages"]
                    else None
                ),
                "index_coverage_status": (
                    "complete" if coverage_complete else "partial" if indexed else "not-run"
                ),
                "last_inspected_at": indexed["last_inspected_at"] if indexed else None,
            })
            output.append(item)
        return self._evidence(
            endpoint="properties", start=start, end=end, site_ids=selected,
            search_type=search_type, scheme=None, data={"properties": output}
        )

    def pages(
        self,
        start: str,
        end: str,
        *,
        site_ids: Iterable[str] | None = None,
        search_type: str = "web",
        scheme_id: str = "path-sections",
        limit: int = 100,
        cursor: str | None = None,
        sort: str = "impressions",
        minimum_impressions: int = 0,
    ) -> dict[str, object]:
        start, end = _bounded_window(start, end)
        search_type = _search_type(search_type)
        selected = _site_ids(self.config, site_ids)
        scheme = self._scheme(scheme_id)
        if not 1 <= limit <= 500:
            raise ValueError("limit must be from 1 to 500")
        if sort not in _SORTS:
            raise ValueError("unsupported page sort")
        if not 0 <= minimum_impressions <= 1_000_000_000:
            raise ValueError("minimum_impressions is out of range")
        offset = _cursor_offset(cursor)
        placeholders = ",".join("?" for _ in selected)
        sort_column = _SORTS[sort]
        direction = "ASC" if sort == "route" else "DESC"
        with self.store.connect(readonly=True) as db:
            rows = db.execute(
                f"""WITH page_metrics AS (
                      SELECT p.page_id,p.site_id,p.route,{self._aggregate_select()}
                        FROM page_catalog AS p
                        LEFT JOIN page_daily AS d ON d.page_id=p.page_id
                         AND d.date_label>=? AND d.date_label<?
                         AND (d.source!='search-console' OR d.search_type=?)
                       WHERE p.site_id IN ({placeholders})
                       GROUP BY p.page_id,p.site_id,p.route
                    )
                    SELECT m.*,
                           COUNT(DISTINCT l.url_hash) AS published_urls,
                           MAX(s.indexed) AS indexed
                      FROM page_metrics AS m
                      LEFT JOIN page_catalog_index_links AS l ON l.page_id=m.page_id
                      LEFT JOIN index_coverage_url_status AS s
                        ON s.site_id=l.site_id AND s.url_hash=l.url_hash
                     WHERE COALESCE(m.gsc_impressions,0)>=?
                     GROUP BY m.page_id
                     ORDER BY {sort_column} {direction} NULLS LAST,m.route ASC
                     LIMIT ? OFFSET ?""",
                (
                    start, end, search_type, *selected, minimum_impressions,
                    limit + 1, offset,
                ),
            ).fetchall()
            visible_page_ids = [row["page_id"] for row in rows[:limit]]
            assignments: dict[str, list[dict[str, str]]] = defaultdict(list)
            if visible_page_ids:
                assignment_placeholders = ",".join("?" for _ in visible_page_ids)
                for assignment in db.execute(
                    f"""SELECT page_id,cluster_id,cluster_label
                           FROM page_scheme_assignments
                          WHERE version_id=? AND page_id IN ({assignment_placeholders})
                       ORDER BY page_id,cluster_id""",
                    (scheme["version_id"], *visible_page_ids),
                ).fetchall():
                    assignments[assignment["page_id"]].append({
                        "cluster_id": assignment["cluster_id"],
                        "label": assignment["cluster_label"],
                    })
        has_more = len(rows) > limit
        rows = rows[:limit]
        output = []
        for row in rows:
            output.append({
                "page_id": row["page_id"], "site_id": row["site_id"],
                "route": row["route"],
                "clusters": assignments.get(row["page_id"], []),
                "published_urls": row["published_urls"],
                "indexed": bool(row["indexed"]) if row["indexed"] is not None else None,
                **self._metric_row(row),
            })
        next_cursor = _cursor(offset + limit) if has_more else None
        return self._evidence(
            endpoint="pages", start=start, end=end, site_ids=selected,
            search_type=search_type, scheme=scheme,
            pagination={"limit": limit, "next_cursor": next_cursor, "sort": sort},
            data={"pages": output},
        )

    def clusters(
        self,
        start: str,
        end: str,
        *,
        site_ids: Iterable[str] | None = None,
        search_type: str = "web",
        scheme_id: str = "path-sections",
    ) -> dict[str, object]:
        start, end = _bounded_window(start, end)
        search_type = _search_type(search_type)
        selected = _site_ids(self.config, site_ids)
        scheme = self._scheme(scheme_id)
        placeholders = ",".join("?" for _ in selected)
        with self.store.connect(readonly=True) as db:
            rows = db.execute(
                f"""WITH page_metrics AS (
                      SELECT p.page_id,{self._aggregate_select()}
                        FROM page_catalog AS p
                        LEFT JOIN page_daily AS d ON d.page_id=p.page_id
                         AND d.date_label>=? AND d.date_label<?
                         AND (d.source!='search-console' OR d.search_type=?)
                       WHERE p.site_id IN ({placeholders})
                       GROUP BY p.page_id
                    )
                    SELECT a.cluster_id,a.cluster_label,
                           SUM(m.umami_pageviews) AS umami_pageviews,
                           SUM(m.umami_visits) AS umami_visits,
                           SUM(m.ga4_pageviews) AS ga4_pageviews,
                           SUM(m.ga4_sessions) AS ga4_sessions,
                           SUM(m.ga4_engaged_sessions) AS ga4_engaged_sessions,
                           SUM(m.ga4_engagement_seconds) AS ga4_engagement_seconds,
                           SUM(m.ga4_key_events) AS ga4_key_events,
                           SUM(m.gsc_clicks) AS gsc_clicks,
                           SUM(m.gsc_impressions) AS gsc_impressions,
                           SUM(m.gsc_position_weight) AS gsc_position_weight,
                           COUNT(DISTINCT a.page_id) AS pages
                      FROM page_scheme_assignments AS a
                      JOIN page_metrics AS m ON m.page_id=a.page_id
                     WHERE a.version_id=?
                     GROUP BY a.cluster_id,a.cluster_label
                     ORDER BY gsc_impressions DESC NULLS LAST,a.cluster_id""",
                (start, end, search_type, *selected, scheme["version_id"]),
            ).fetchall()
            coverage_rows = db.execute(
                f"""SELECT a.cluster_id,
                           COUNT(DISTINCT l.url_hash) AS published_pages,
                           COUNT(DISTINCT CASE WHEN s.inspected_at IS NOT NULL THEN l.url_hash END) AS inspected_pages,
                           COUNT(DISTINCT CASE WHEN s.indexed=1 THEN l.url_hash END) AS indexed_pages
                      FROM page_scheme_assignments AS a
                      JOIN page_catalog AS p ON p.page_id=a.page_id
                      LEFT JOIN page_catalog_index_links AS l ON l.page_id=p.page_id
                      LEFT JOIN index_coverage_url_status AS s
                        ON s.site_id=l.site_id AND s.url_hash=l.url_hash
                     WHERE a.version_id=? AND p.site_id IN ({placeholders})
                     GROUP BY a.cluster_id""",
                (scheme["version_id"], *selected),
            ).fetchall()
        coverage_by_cluster = {row["cluster_id"]: row for row in coverage_rows}
        output = []
        control_impressions = 0
        for row in rows:
            metrics = self._metric_row(row)
            coverage = coverage_by_cluster.get(row["cluster_id"])
            published_pages = coverage["published_pages"] if coverage else 0
            inspected_pages = coverage["inspected_pages"] if coverage else 0
            indexed_observed_pages = coverage["indexed_pages"] if coverage else 0
            coverage_complete = published_pages > 0 and published_pages == inspected_pages
            indexed_pages = indexed_observed_pages if coverage_complete else None
            impressions = metrics.get("gsc_impressions")
            if impressions is not None:
                control_impressions += int(impressions)
            output.append({
                "cluster_id": row["cluster_id"], "label": row["cluster_label"],
                "pages": row["pages"], "published_pages": published_pages,
                "inspected_pages": inspected_pages,
                "indexed_pages": indexed_pages,
                "indexed_observed_pages": indexed_observed_pages,
                "index_coverage_status": (
                    "complete" if coverage_complete else "partial" if published_pages else "not-mapped"
                ),
                "indexed_percentage": (
                    float(indexed_pages) / float(published_pages)
                    if coverage_complete and published_pages else None
                ),
                **metrics,
            })
        overlap = scheme["mode"] == "multilabel"
        for item in output:
            item["impression_share"] = (
                float(item["gsc_impressions"]) / control_impressions
                if not overlap and control_impressions and item["gsc_impressions"] is not None
                else None
            )
        return self._evidence(
            endpoint="clusters", start=start, end=end, site_ids=selected,
            search_type=search_type, scheme=scheme,
            data={
                "scheme_mode": scheme["mode"],
                "overlap_disclosed": overlap,
                "shares_available": not overlap,
                "clusters": output,
            },
        )

    def opportunities(
        self,
        start: str,
        end: str,
        *,
        site_ids: Iterable[str] | None = None,
        search_type: str = "web",
        scheme_id: str = "path-sections",
        minimum_impressions: int = 25,
        maximum_ctr: float = 0.05,
        limit: int = 100,
    ) -> dict[str, object]:
        if not 0 <= maximum_ctr <= 1:
            raise ValueError("maximum_ctr must be from 0 to 1")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be from 1 to 500")
        payload = self.pages(
            start, end, site_ids=site_ids, search_type=search_type,
            scheme_id=scheme_id, limit=500, sort="impressions",
            minimum_impressions=minimum_impressions,
        )
        rows = [
            item for item in payload["data"]["pages"]
            if item["gsc_ctr"] is not None and float(item["gsc_ctr"]) <= maximum_ctr
        ][:limit]
        candidate_pool_truncated = payload["pagination"].get("next_cursor") is not None
        payload["endpoint"] = "opportunities"
        payload["filters"].update({
            "minimum_impressions": minimum_impressions,
            "maximum_ctr": maximum_ctr,
        })
        payload["data"] = {
            "interpretation": "Diagnostic candidate set only; ranking does not forecast uplift or establish causality.",
            "candidate_pool_cap": 500,
            "candidate_pool_truncated": candidate_pool_truncated,
            "opportunities": rows,
        }
        payload["pagination"] = {
            "limit": limit,
            "returned": len(rows),
            "candidate_pool_cap": 500,
            "candidate_pool_truncated": candidate_pool_truncated,
        }
        return payload

    def schemes(self) -> dict[str, object]:
        with self.store.connect(readonly=True) as db:
            rows = db.execute(
                """SELECT s.scheme_id,s.name,s.mode,v.version_id,v.version_number,
                          v.definition_hash,a.activated_at,a.reason,
                          COUNT(sa.page_id) AS assignments
                     FROM page_schemes AS s
                     JOIN page_scheme_activations AS a ON a.scheme_id=s.scheme_id
                     JOIN page_scheme_versions AS v ON v.version_id=a.version_id
                     LEFT JOIN page_scheme_assignments AS sa ON sa.version_id=v.version_id
                    WHERE a.id IN (
                      SELECT a2.id FROM page_scheme_activations AS a2
                       WHERE a2.scheme_id=a.scheme_id
                       ORDER BY a2.activated_at DESC,a2.id DESC LIMIT 1
                    )
                    GROUP BY s.scheme_id ORDER BY s.name,s.scheme_id"""
            ).fetchall()
        selected = tuple(self.sites)
        return self._evidence(
            endpoint="schemes", start=None, end=None, site_ids=selected,
            search_type="web", scheme=None,
            data={"schemes": [dict(row) for row in rows]},
        )


def previous_window(days: int = 30) -> tuple[str, str]:
    if not 1 <= days <= 366:
        raise ValueError("days must be from 1 to 366")
    end = _now().date()
    return (end - timedelta(days=days)).isoformat(), end.isoformat()
