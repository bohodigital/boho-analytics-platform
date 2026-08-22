"""Sitemap inventory and Google URL Inspection census orchestration."""

from __future__ import annotations

import gzip
import hashlib
import io
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

from .connectors.google import _access_token
from .credentials import ReferenceCredentialProvider
from .http import JsonHttpClient, ProviderError
from .storage import SQLiteMetricStore


URL_INSPECTION_ENDPOINT = (
    "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
)
_VERDICTS = {"PASS", "FAIL", "NEUTRAL", "UNKNOWN", "VERDICT_UNSPECIFIED"}


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _safe_category(exc: Exception) -> str:
    if isinstance(exc, ProviderError):
        return exc.category[:60]
    return re.sub(r"(?<!^)(?=[A-Z])", "-", type(exc).__name__).casefold()[:60]


def _normalize_public_url(value: str, expected_host: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("sitemap contains a non-public URL")
    if parsed.hostname.casefold() != expected_host.casefold():
        raise ValueError("sitemap contains an URL outside the configured site host")
    if parsed.username or parsed.password:
        raise ValueError("sitemap URL contains credentials")
    host = parsed.hostname.casefold()
    if parsed.port:
        host = f"{host}:{parsed.port}"
    path = parsed.path or "/"
    return urllib.parse.urlunsplit(
        (parsed.scheme.casefold(), host, path, parsed.query, "")
    )


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, host: str) -> None:
        super().__init__()
        self.host = host.casefold()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.hostname.casefold() != self.host
        ):
            raise urllib.error.HTTPError(
                req.full_url, code, "cross-host sitemap redirect rejected", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SitemapInventoryClient:
    """Fetch a bounded same-host sitemap tree and return canonical page URLs."""

    def __init__(
        self,
        *,
        timeout: int = 30,
        max_document_bytes: int = 12_000_000,
        max_documents: int = 500,
        max_urls: int = 100_000,
        max_depth: int = 4,
    ) -> None:
        self.timeout = timeout
        self.max_document_bytes = max_document_bytes
        self.max_documents = max_documents
        self.max_urls = max_urls
        self.max_depth = max_depth

    def _read(self, url: str, host: str) -> bytes:
        opener = urllib.request.build_opener(_SameHostRedirect(host))
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/xml,text/xml,application/gzip;q=0.8",
                "User-Agent": "boho-analytics-platform/0.2 sitemap-census",
            },
        )
        with opener.open(request, timeout=self.timeout) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if not final.hostname or final.hostname.casefold() != host.casefold():
                raise ValueError("sitemap response left the configured site host")
            raw = response.read(self.max_document_bytes + 1)
            if len(raw) > self.max_document_bytes:
                raise ValueError("sitemap document exceeded the configured size limit")
            compressed = (
                response.headers.get("Content-Encoding", "").casefold() == "gzip"
                or final.path.casefold().endswith(".gz")
            )
        if compressed:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                raw = stream.read(self.max_document_bytes + 1)
            if len(raw) > self.max_document_bytes:
                raise ValueError("expanded sitemap exceeded the configured size limit")
        return raw

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].casefold()

    def fetch(self, canonical_url: str) -> tuple[str, ...]:
        canonical = urllib.parse.urlsplit(canonical_url)
        if not canonical.hostname:
            raise ValueError("configured canonical URL has no host")
        host = canonical.hostname.casefold()
        root = urllib.parse.urljoin(canonical_url.rstrip("/") + "/", "sitemap.xml")
        queue: list[tuple[str, int]] = [(root, 0)]
        seen_documents: set[str] = set()
        urls: set[str] = set()
        while queue:
            document_url, depth = queue.pop(0)
            document_url = _normalize_public_url(document_url, host)
            if document_url in seen_documents:
                continue
            if len(seen_documents) >= self.max_documents:
                raise ValueError("sitemap tree exceeded the configured document limit")
            seen_documents.add(document_url)
            try:
                raw = self._read(document_url, host)
                if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                    raise ValueError("sitemap document declarations are not allowed")
                root_element = ET.fromstring(raw)
            except ET.ParseError as exc:
                raise ValueError("sitemap document is not valid XML") from exc
            kind = self._local_name(root_element.tag)
            locations = [
                (item.text or "").strip()
                for item in root_element.iter()
                if self._local_name(item.tag) == "loc" and (item.text or "").strip()
            ]
            if kind == "sitemapindex":
                if depth >= self.max_depth:
                    raise ValueError("sitemap tree exceeded the configured depth limit")
                queue.extend((location, depth + 1) for location in locations)
            elif kind == "urlset":
                for location in locations:
                    urls.add(_normalize_public_url(location, host))
                    if len(urls) > self.max_urls:
                        raise ValueError("sitemap inventory exceeded the configured URL limit")
            else:
                raise ValueError("sitemap root must be urlset or sitemapindex")
        return tuple(sorted(urls))


@dataclass(frozen=True, slots=True)
class IndexCoverageResult:
    site_id: str
    status: str
    published_pages: int | None
    inspected_this_run: int
    inspected_current: int
    indexed_pages: int | None
    indexed_percentage: float | None
    error_category: str | None = None

    def json_value(self) -> dict[str, object]:
        return {
            "site_id": self.site_id,
            "status": self.status,
            "published_pages": self.published_pages,
            "inspected_this_run": self.inspected_this_run,
            "inspected_current": self.inspected_current,
            "indexed_pages": self.indexed_pages,
            "indexed_percentage": self.indexed_percentage,
            "error_category": self.error_category,
        }


class IndexCoverageEngine:
    """Run a quota-bounded per-property index census with durable progress."""

    def __init__(
        self,
        config,
        store: SQLiteMetricStore,
        *,
        credential_provider=None,
        http=None,
        sitemaps=None,
        sleeper=None,
        now=None,
    ) -> None:
        self.config = config
        self.store = store
        self.credentials = credential_provider or ReferenceCredentialProvider()
        self.http = http or JsonHttpClient(
            timeout=config.platform.http_timeout_seconds,
            max_bytes=config.platform.max_response_bytes,
        )
        self.sitemaps = sitemaps or SitemapInventoryClient(
            timeout=config.platform.http_timeout_seconds,
            max_document_bytes=config.platform.max_response_bytes,
        )
        self.sleep = sleeper or time.sleep
        self.now = now or (lambda: datetime.now(UTC))

    def _bindings(self, selected_sites: set[str] | None):
        known_sites = {site.id for site in self.config.sites}
        if selected_sites:
            unknown = selected_sites - known_sites
            if unknown:
                raise ValueError(f"unknown site id(s): {', '.join(sorted(unknown))}")
        connections = {item.id: item for item in self.config.connections}
        bindings = []
        seen_sites = set()
        for binding in self.config.bindings:
            connection = connections[binding.connection_id]
            if connection.provider != "search-console":
                continue
            if selected_sites and binding.site_id not in selected_sites:
                continue
            if binding.site_id in seen_sites:
                raise ValueError(
                    f"site has multiple Search Console bindings: {binding.site_id}"
                )
            seen_sites.add(binding.site_id)
            bindings.append((binding, connection))
        if selected_sites and seen_sites != selected_sites:
            missing = ", ".join(sorted(selected_sites - seen_sites))
            raise ValueError(f"site has no Search Console binding: {missing}")
        if not bindings:
            raise ValueError("no Search Console bindings are configured")
        return bindings

    def _inspect(self, token: str, property_id: str, url: str) -> str:
        result = self.http.request(
            "POST",
            URL_INSPECTION_ENDPOINT,
            headers={"Authorization": f"Bearer {token}"},
            body={
                "inspectionUrl": url,
                "siteUrl": property_id,
                "languageCode": "en-US",
            },
        )
        if not isinstance(result, dict):
            raise ValueError("URL Inspection returned an invalid response")
        inspection = result.get("inspectionResult")
        index_status = (
            inspection.get("indexStatusResult") if isinstance(inspection, dict) else None
        )
        verdict = index_status.get("verdict") if isinstance(index_status, dict) else None
        if verdict not in _VERDICTS:
            raise ValueError("URL Inspection response omitted a valid index verdict")
        return verdict

    def sync(
        self,
        selected_sites: set[str] | None = None,
        *,
        per_property_limit: int = 1_900,
        pause_seconds: float = 0.12,
        workers: int = 16,
        refresh_days: int = 21,
        freshness_days: int = 30,
    ) -> list[IndexCoverageResult]:
        if not 1 <= per_property_limit <= 2_000:
            raise ValueError("per-property limit must be from 1 to 2000")
        if not 0 <= pause_seconds <= 10:
            raise ValueError("pause seconds must be from 0 to 10")
        if not 1 <= workers <= 32:
            raise ValueError("workers must be from 1 to 32")
        if not 1 <= refresh_days < freshness_days <= 365:
            raise ValueError("refresh days must be positive and less than freshness days")
        bindings = self._bindings(selected_sites)
        sites = {item.id: item for item in self.config.sites}
        owner = hashlib.sha256(f"{self.now().isoformat()}:{id(self)}".encode()).hexdigest()
        self.store.acquire_lock("index-coverage-sync", owner, lease_seconds=300)
        self.store.mark_abandoned_index_coverage_runs(self.now())
        next_lease_renewal = time.monotonic() + 60
        results = []
        try:
            for binding, connection in bindings:
                site = sites[binding.site_id]
                run_id = self.store.start_index_coverage_run(site.id, connection.id)
                inspected_this_run = 0
                published: int | None = None
                try:
                    urls = self.sitemaps.fetch(site.canonical_url)
                    published = len(urls)
                    by_hash = {_url_hash(url): url for url in urls}
                    inventory_hash = hashlib.sha256(
                        "\n".join(sorted(by_hash)).encode("ascii")
                    ).hexdigest()
                    observed_at = self.now()
                    self.store.begin_index_coverage_inventory(
                        site.id, inventory_hash, tuple(by_hash), observed_at
                    )
                    # Join sitemap membership to normalized internal routes only.
                    # Full public URL text remains in memory for the provider call.
                    from .page_intelligence import PageIntelligenceService

                    PageIntelligenceService(self.config, self.store).record_sitemap_inventory(
                        site.id, urls, inventory_hash, observed_at
                    )
                    pending = self.store.pending_index_coverage_hashes(
                        site.id,
                        inventory_hash,
                        refresh_before=observed_at - timedelta(days=refresh_days),
                        limit=per_property_limit,
                    )
                    with self.credentials.acquire(connection.credential_ref) as credential:
                        token = _access_token(credential)
                        pace_lock = Lock()
                        next_start = [time.monotonic()]

                        def inspect_one(url_hash: str) -> tuple[str, str]:
                            delay = 0.0
                            if pause_seconds:
                                with pace_lock:
                                    current = time.monotonic()
                                    scheduled = max(current, next_start[0])
                                    next_start[0] = scheduled + pause_seconds
                                    delay = scheduled - current
                            if delay:
                                self.sleep(delay)
                            return url_hash, self._inspect(
                                token, binding.resource_id, by_hash[url_hash]
                            )

                        with ThreadPoolExecutor(
                            max_workers=min(workers, len(pending) or 1),
                            thread_name_prefix="gsc-index",
                        ) as executor:
                            futures = {
                                executor.submit(inspect_one, url_hash): url_hash
                                for url_hash in pending
                            }
                            try:
                                for future in as_completed(futures):
                                    url_hash, verdict = future.result()
                                    self.store.record_index_coverage_inspection(
                                        site.id,
                                        inventory_hash,
                                        url_hash,
                                        verdict,
                                        self.now(),
                                    )
                                    inspected_this_run += 1
                                    if time.monotonic() >= next_lease_renewal:
                                        self.store.renew_lock(
                                            "index-coverage-sync",
                                            owner,
                                            lease_seconds=300,
                                        )
                                        next_lease_renewal = time.monotonic() + 60
                            except Exception:
                                for future in futures:
                                    future.cancel()
                                raise
                    summary = self.store.query_index_coverage(
                        [site.id],
                        fresh_after=self.now() - timedelta(days=freshness_days),
                    )[0]
                    status = "complete" if summary["status"] == "complete" else "partial"
                    self.store.finish_index_coverage_run(
                        run_id,
                        status,
                        published_pages=published,
                        inspected_this_run=inspected_this_run,
                    )
                    results.append(IndexCoverageResult(
                        site.id,
                        status,
                        published,
                        inspected_this_run,
                        int(summary["inspection_progress"]["inspected"]),
                        summary["indexed_pages"],
                        summary["indexed_percentage"],
                    ))
                except Exception as exc:
                    category = _safe_category(exc)
                    status = "partial" if inspected_this_run else "failed"
                    self.store.finish_index_coverage_run(
                        run_id,
                        status,
                        published_pages=published,
                        inspected_this_run=inspected_this_run,
                        error_category=category,
                    )
                    summary = self.store.query_index_coverage([site.id])[0]
                    results.append(IndexCoverageResult(
                        site.id,
                        status,
                        published,
                        inspected_this_run,
                        int(summary["inspection_progress"]["inspected"]),
                        summary["indexed_pages"],
                        summary["indexed_percentage"],
                        category,
                    ))
        finally:
            self.store.release_lock("index-coverage-sync", owner)
        return results
