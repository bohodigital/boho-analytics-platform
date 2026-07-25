"""Owner-authorized, read-only rendered evidence capture.

This module deliberately does not select or install a browser implementation.
Callers inject a disposable ``BrowserFactory`` that must apply ``RequestPolicy``
before every request.  The narrow boundary keeps browser execution optional and
lets reconciliation map these lane-local records to the shared schema.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import asdict, dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit


ADAPTER_NAME = "rendered-crawl"
ADAPTER_VERSION = "core21-r1"
MAX_ROUTES = 10_000
MAX_LINKS_PER_PAGE = 20_000
MAX_DIAGNOSTICS = 1_000
MAX_TEXT = 2_000
MAX_DOM_BYTES = 8 * 1024 * 1024
MAX_STATIC_ORIGINS = 16
CAPTURE_STATES = frozenset({"complete", "partial", "timeout", "failed", "blocked", "unchecked"})
RESOLUTION_STATES = frozenset({
    "confirmed-page", "redirect", "missing", "source-only", "artifact-only",
    "rendered-only", "dynamic-unknown", "contradicted", "unresolved", "excluded",
    "unchecked", "action", "fragment", "external",
})
STATIC_RESOURCE_TYPES = frozenset({"stylesheet", "script", "image", "font"})
SIDE_EFFECT_TERMS = re.compile(
    r"(?:^|[._~!$&'()*+,;=:@/?#-])"
    r"(?:account|admin|auth|book|booking|buy|cart|checkout|consent|delete|form|"
    r"login|logout|mail|order|pay|purchase|register|remove|reserve|save|submit|"
    r"signin|signout|signup|subscribe|unsubscribe)"
    r"(?:$|[._~!$&'()*+,;=:@/?#-])",
    re.IGNORECASE,
)
TRACKER_HOST_TERMS = re.compile(
    r"(?:^|[.-])(?:advert|analytics|beacon|clarity|doubleclick|facebook|gtag|"
    r"hotjar|matomo|pixel|segment|telemetry|tracker|tracking|ads?)(?:[.-]|$)",
    re.IGNORECASE,
)
TRACKER_PATH_TERMS = re.compile(
    r"(?:^|/)(?:advert|analytics|beacon|clarity|doubleclick|facebook|gtag|"
    r"hotjar|matomo|pixel|segment|telemetry|tracker|tracking|ads?)"
    r"(?:[./_-]|$)",
    re.IGNORECASE,
)
TRACKER_QUERY_TERMS = re.compile(
    r"(?:advert|analytics|beacon|clarity|doubleclick|facebook|gtag|hotjar|"
    r"matomo|pixel|segment|telemetry|tracker|tracking|(?:^|[&;])ads?(?:[=&;]|$))",
    re.IGNORECASE,
)


class RenderedCrawlError(ValueError):
    """Rendered-crawl authorization or input is unsafe."""


def _text(value: Any, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    value = "".join(character for character in value if ord(character) >= 32 or character in "\t\n\r")
    return value.strip()[:maximum]


def _origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise RenderedCrawlError("origins must use http or https and include a host")
        if parsed.username is not None or parsed.password is not None:
            raise RenderedCrawlError("origins must not contain credentials")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise RenderedCrawlError("origins must not include a path, query, or fragment")
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (TypeError, UnicodeError, ValueError) as exc:
        raise RenderedCrawlError("origin is malformed") from exc
    default_port = (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)
    host_text = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host_text if port is None or default_port else f"{host_text}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def _url_origin(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    default_port = (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)
    host_text = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host_text if port is None or default_port else f"{host_text}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def _is_tracker_target(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return False
    return bool(
        TRACKER_HOST_TERMS.search(parsed.hostname or "")
        or TRACKER_PATH_TERMS.search(parsed.path[:4096])
        or TRACKER_QUERY_TERMS.search(parsed.query[:4096])
    )


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _public_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 8192:
        return None
    if any(ord(character) < 32 for character in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            return None
    except ValueError:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _sanitized_target(value: str) -> str:
    value = _text(value, maximum=4096)
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[invalid-target]"
    if parsed.scheme in {"mailto", "tel"}:
        return f"{parsed.scheme}:[redacted]"
    if parsed.username is not None or parsed.password is not None:
        return "[invalid-target]"
    if parsed.query:
        value = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", parsed.fragment))
    return value


@dataclass(frozen=True)
class CrawlAuthorization:
    target_origin: str
    expected_revision: str
    observed_revision: str
    allowed_static_origins: tuple[str, ...] = ()
    expected_canonical_origin: str | None = None

    def __post_init__(self) -> None:
        canonical_target = _origin(self.target_origin)
        canonical_expected = (
            canonical_target
            if self.expected_canonical_origin is None
            else _origin(self.expected_canonical_origin)
        )
        canonical_static = tuple(sorted({_origin(item) for item in self.allowed_static_origins}))
        if canonical_target in canonical_static:
            raise RenderedCrawlError("target origin must not be repeated as a static origin")
        if len(canonical_static) > MAX_STATIC_ORIGINS:
            raise RenderedCrawlError(f"at most {MAX_STATIC_ORIGINS} static origins may be authorized")
        revision_pattern = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
        expected = self.expected_revision.lower()
        observed = self.observed_revision.lower()
        if not revision_pattern.fullmatch(expected) or not revision_pattern.fullmatch(observed):
            raise RenderedCrawlError("revisions must be exact 40- or 64-character hexadecimal object IDs")
        object.__setattr__(self, "target_origin", canonical_target)
        object.__setattr__(self, "expected_canonical_origin", canonical_expected)
        object.__setattr__(self, "allowed_static_origins", canonical_static)
        object.__setattr__(self, "expected_revision", expected)
        object.__setattr__(self, "observed_revision", observed)

    @property
    def revision_state(self) -> str:
        return "matched" if self.expected_revision == self.observed_revision else "mismatch"


@dataclass(frozen=True)
class Viewport:
    name: str
    width: int
    height: int
    mobile: bool


VIEWPORTS = (
    Viewport("desktop", 1440, 900, False),
    Viewport("mobile", 390, 844, True),
)


@dataclass(frozen=True)
class NetworkDecision:
    allowed: bool
    category: str


class RequestPolicy:
    """Fail-closed request policy supplied to every injected browser context."""

    def __init__(self, authorization: CrawlAuthorization) -> None:
        self.authorization = authorization

    def decide(
        self, url: str, *, resource_type: str, method: str = "GET", is_navigation: bool = False
    ) -> NetworkDecision:
        method = method.upper()
        if method not in {"GET", "HEAD"}:
            return NetworkDecision(False, "side-effect-method")
        try:
            parsed = urlsplit(url)
        except (TypeError, ValueError):
            return NetworkDecision(False, "invalid-url")
        if parsed.scheme in {"mailto", "tel"}:
            return NetworkDecision(False, "side-effect-scheme")
        origin = _url_origin(url)
        if origin is None:
            return NetworkDecision(False, "invalid-url")
        searchable = f"{parsed.hostname or ''}{parsed.path}?{parsed.query}"
        if _is_tracker_target(url):
            return NetworkDecision(False, "tracker")
        if SIDE_EFFECT_TERMS.search(searchable):
            return NetworkDecision(False, "side-effect-target")
        if origin == self.authorization.target_origin:
            return NetworkDecision(True, "same-origin")
        if (
            not is_navigation
            and origin in self.authorization.allowed_static_origins
            and resource_type in STATIC_RESOURCE_TYPES
        ):
            return NetworkDecision(True, "approved-static-origin")
        return NetworkDecision(False, "origin-not-authorized")


@dataclass(frozen=True)
class RawAnchor:
    href: str
    text: str = ""
    accessible_name: str = ""
    landmark: str = ""
    visible: bool = False
    in_viewport: bool = False
    rel: tuple[str, ...] = ()


@dataclass(frozen=True)
class RawForm:
    action: str
    text: str = ""
    accessible_name: str = ""
    landmark: str = ""
    visible: bool = False
    in_viewport: bool = False


@dataclass(frozen=True)
class BrowserCapture:
    requested_url: str
    final_url: str
    http_status: int | None
    canonical_url: str | None = None
    robots: str | None = None
    title: str = ""
    h1: tuple[str, ...] = ()
    schema_types: tuple[str, ...] = ()
    dom_html: str = ""
    anchors: tuple[RawAnchor, ...] = ()
    forms: tuple[RawForm, ...] = ()
    console_failures: tuple[str, ...] = ()
    network_failures: tuple[str, ...] = ()
    blocked_resources: tuple[str, ...] = ()
    hydration_complete: bool = True


class BrowserSession(Protocol):
    def navigate(self, url: str, *, timeout_ms: int) -> BrowserCapture:
        """Navigate without interaction and return a bounded capture."""

    def clear_state(self) -> None:
        """Clear cookies, local/session storage, caches, and service workers."""

    def close(self) -> None:
        """Terminate the context and all child processes."""


class BrowserFactory(Protocol):
    def open_context(
        self, *, profile_directory: Path, viewport: Viewport, request_policy: RequestPolicy
    ) -> BrowserSession:
        """Return a fresh context using only the supplied temporary profile."""


@dataclass(frozen=True)
class BoundedDiagnostic:
    category: str
    route: str
    viewport: str
    detail: str


@dataclass(frozen=True)
class LinkOccurrence:
    source_url: str
    target: str
    kind: str
    text: str
    accessible_name: str
    landmark: str
    visible: bool
    in_viewport: bool
    rel: tuple[str, ...]
    nofollow: bool
    resolution_state: str
    viewport: str
    provenance_hash: str


@dataclass(frozen=True)
class PageCandidate:
    requested_url: str
    final_url: str | None
    viewport: str
    capture_state: str
    resolution_state: str
    http_status: int | None
    canonical_url: str | None
    robots: str | None
    title: str
    h1: tuple[str, ...]
    schema_types: tuple[str, ...]
    dom_hash: str | None
    console_failure_classes: tuple[str, ...]
    network_failure_classes: tuple[str, ...]
    blocked_resource_classes: tuple[str, ...]
    provenance_hash: str


@dataclass(frozen=True)
class PageEntity:
    canonical_url: str
    resolution_state: str
    candidate_hashes: tuple[str, ...]
    stable_hash: str


@dataclass(frozen=True)
class CoverageSummary:
    requested_routes: int
    attempted_captures: int
    complete: int
    partial: int
    timeout: int
    failed: int
    blocked: int
    unchecked: int
    blocked_resources: int
    revision_state: str


@dataclass(frozen=True)
class EvidenceBatch:
    adapter: str
    adapter_version: str
    target_origin: str
    expected_canonical_origin: str
    expected_revision: str
    observed_revision: str
    revision_state: str
    page_candidates: tuple[PageCandidate, ...]
    page_entities: tuple[PageEntity, ...]
    link_occurrences: tuple[LinkOccurrence, ...]
    coverage: CoverageSummary
    diagnostics: tuple[BoundedDiagnostic, ...]
    stable_hash: str


@dataclass(frozen=True)
class AdapterResult:
    state: str
    evidence_batch: EvidenceBatch
    stable_hash: str


def _route_url(origin: str, route: str) -> str | None:
    if not isinstance(route, str) or not route or len(route) > MAX_TEXT:
        return None
    if any(ord(character) < 32 for character in route) or "\\" in route:
        return None
    value = urljoin(f"{origin}/", route)
    parsed = urlsplit(value)
    if _url_origin(value) != origin or parsed.fragment or parsed.query:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def _failure_classes(values: Iterable[str]) -> tuple[str, ...]:
    allowed = {
        "certificate", "connection", "content-security-policy", "dns", "http",
        "javascript", "mixed-content", "request-blocked", "resource", "timeout",
    }
    return tuple(sorted({value for value in values if value in allowed}))


def _resolution_for_target(source: str, target: str, kind: str, origin: str) -> str:
    try:
        parsed = urlsplit(target)
    except ValueError:
        return "unresolved"
    if kind == "form":
        return "action"
    if parsed.scheme in {"mailto", "tel"} or SIDE_EFFECT_TERMS.search(target):
        return "action"
    if target.startswith("#") or (parsed.fragment and urljoin(source, target).split("#", 1)[0] == source.split("#", 1)[0]):
        return "fragment"
    absolute = urljoin(source, target)
    if _url_origin(absolute) != origin:
        return "external"
    return "rendered-only"


def _link_occurrences(capture: BrowserCapture, viewport: Viewport, origin: str) -> tuple[LinkOccurrence, ...]:
    raw: list[tuple[str, RawAnchor | RawForm]] = [
        ("anchor", item) for item in capture.anchors[:MAX_LINKS_PER_PAGE]
    ]
    remaining = MAX_LINKS_PER_PAGE - len(raw)
    raw.extend(("form", item) for item in capture.forms[:remaining])
    occurrences: list[LinkOccurrence] = []
    for kind, item in raw:
        raw_target = item.href if isinstance(item, RawAnchor) else item.action
        target = _sanitized_target(raw_target)
        rel = tuple(sorted({_text(value, maximum=100).lower() for value in getattr(item, "rel", ()) if value}))
        resolution = _resolution_for_target(capture.final_url, raw_target, kind, origin)
        payload = {
            "source_url": capture.final_url,
            "target": target,
            "kind": kind,
            "text": _text(item.text),
            "accessible_name": _text(item.accessible_name),
            "landmark": _text(item.landmark, maximum=100),
            "visible": bool(item.visible),
            "in_viewport": bool(item.in_viewport),
            "rel": rel,
            "resolution_state": resolution,
            "viewport": viewport.name,
        }
        occurrences.append(LinkOccurrence(
            **payload,
            nofollow="nofollow" in rel,
            provenance_hash=_stable_hash(payload),
        ))
    return tuple(occurrences)


def _candidate(
    requested_url: str,
    viewport: Viewport,
    *,
    capture: BrowserCapture | None,
    capture_state: str,
    resolution_state: str,
) -> PageCandidate:
    if capture is None:
        payload = {
            "requested_url": requested_url,
            "final_url": None,
            "viewport": viewport.name,
            "capture_state": capture_state,
            "resolution_state": resolution_state,
            "http_status": None,
            "canonical_url": None,
            "robots": None,
            "title": "",
            "h1": (),
            "schema_types": (),
            "dom_hash": None,
            "console_failure_classes": (),
            "network_failure_classes": (),
            "blocked_resource_classes": (),
        }
    else:
        dom = capture.dom_html.encode("utf-8", "replace")
        final_url = _public_url(capture.final_url)
        canonical_url = _public_url(capture.canonical_url) if capture.canonical_url else None
        payload = {
            "requested_url": requested_url,
            "final_url": final_url,
            "viewport": viewport.name,
            "capture_state": capture_state,
            "resolution_state": resolution_state,
            "http_status": capture.http_status,
            "canonical_url": canonical_url,
            "robots": _text(capture.robots, maximum=500) or None,
            "title": _text(capture.title),
            "h1": tuple(_text(value) for value in capture.h1[:100]),
            "schema_types": tuple(sorted({_text(value, maximum=200) for value in capture.schema_types[:200] if value})),
            "dom_hash": hashlib.sha256(dom).hexdigest() if len(dom) <= MAX_DOM_BYTES else None,
            "console_failure_classes": _failure_classes(capture.console_failures),
            "network_failure_classes": _failure_classes(capture.network_failures),
            "blocked_resource_classes": tuple(sorted({_text(value, maximum=100) for value in capture.blocked_resources[:1000]})),
        }
        if len(dom) > MAX_DOM_BYTES:
            payload["capture_state"] = "partial"
    return PageCandidate(**payload, provenance_hash=_stable_hash(payload))


def _page_resolution(
    capture: BrowserCapture,
    requested_url: str,
    origin: str,
    expected_canonical_origin: str,
) -> tuple[str, str]:
    if capture.requested_url != requested_url:
        return "failed", "unresolved"
    if _public_url(capture.final_url) is None:
        return "blocked", "unchecked"
    if _url_origin(capture.final_url) != origin:
        return "blocked", "unchecked"
    if SIDE_EFFECT_TERMS.search(capture.final_url) or _is_tracker_target(capture.final_url):
        return "blocked", "unchecked"
    if (
        capture.canonical_url
        and _url_origin(capture.canonical_url) != expected_canonical_origin
    ):
        return "partial", "contradicted"
    if capture.http_status is None:
        return "partial", "dynamic-unknown"
    if capture.http_status == 404 or capture.http_status == 410:
        return "complete", "missing"
    if not 200 <= capture.http_status < 400:
        return "failed", "unresolved"
    if 300 <= capture.http_status < 400:
        return "complete", "redirect"
    if capture.final_url != requested_url:
        return "complete", "redirect"
    if not capture.hydration_complete:
        return "partial", "dynamic-unknown"
    return "complete", "confirmed-page"


def crawl_rendered_evidence(
    authorization: CrawlAuthorization,
    routes: Iterable[str],
    browser_factory: BrowserFactory,
    *,
    timeout_ms: int = 15_000,
    viewports: tuple[Viewport, ...] = VIEWPORTS,
) -> AdapterResult:
    """Capture deterministic read-only evidence with cleanup on every exit path."""
    if not 100 <= timeout_ms <= 120_000:
        raise RenderedCrawlError("timeout_ms must be from 100 to 120000")
    route_list = list(islice(routes, MAX_ROUTES + 1))
    if len(route_list) > MAX_ROUTES:
        raise RenderedCrawlError(f"at most {MAX_ROUTES} routes may be captured")
    if not viewports or len({item.name for item in viewports}) != len(viewports):
        raise RenderedCrawlError("viewports must be non-empty and have unique names")

    diagnostics: list[BoundedDiagnostic] = []
    candidates: list[PageCandidate] = []
    occurrences: list[LinkOccurrence] = []
    valid_urls: list[str] = []
    for route in route_list:
        url = _route_url(authorization.target_origin, route)
        if url is None:
            for viewport in viewports:
                candidates.append(_candidate(
                    str(route)[:MAX_TEXT], viewport, capture=None,
                    capture_state="unchecked", resolution_state="unchecked",
                ))
            if len(diagnostics) < MAX_DIAGNOSTICS:
                diagnostics.append(BoundedDiagnostic("invalid-route", str(route)[:MAX_TEXT], "*", "route is outside the authorized origin or malformed"))
        else:
            valid_urls.append(url)

    if authorization.revision_state != "matched":
        for url in valid_urls:
            for viewport in viewports:
                candidates.append(_candidate(
                    url, viewport, capture=None, capture_state="unchecked",
                    resolution_state="contradicted",
                ))
        diagnostics.append(BoundedDiagnostic(
            "revision-mismatch", "*", "*",
            f"expected {authorization.expected_revision}; observed {authorization.observed_revision}",
        ))
        return _result(authorization, candidates, occurrences, diagnostics)
    if not valid_urls:
        return _result(authorization, candidates, occurrences, diagnostics)

    policy = RequestPolicy(authorization)
    for viewport in viewports:
        with tempfile.TemporaryDirectory(prefix="boho-rendered-crawl-") as temporary:
            session: BrowserSession | None = None
            try:
                session = browser_factory.open_context(
                    profile_directory=Path(temporary), viewport=viewport, request_policy=policy
                )
                for url in valid_urls:
                    decision = policy.decide(url, resource_type="document", is_navigation=True)
                    if not decision.allowed:
                        candidates.append(_candidate(
                            url, viewport, capture=None, capture_state="blocked",
                            resolution_state="unchecked",
                        ))
                        continue
                    try:
                        capture = session.navigate(url, timeout_ms=timeout_ms)
                    except TimeoutError:
                        candidates.append(_candidate(
                            url, viewport, capture=None, capture_state="timeout",
                            resolution_state="unchecked",
                        ))
                        if len(diagnostics) < MAX_DIAGNOSTICS:
                            diagnostics.append(BoundedDiagnostic("timeout", url, viewport.name, "navigation timed out"))
                        continue
                    except Exception as exc:
                        candidates.append(_candidate(
                            url, viewport, capture=None, capture_state="failed",
                            resolution_state="unchecked",
                        ))
                        if len(diagnostics) < MAX_DIAGNOSTICS:
                            diagnostics.append(BoundedDiagnostic("navigation-failed", url, viewport.name, type(exc).__name__))
                        continue
                    state, resolution = _page_resolution(
                        capture,
                        url,
                        authorization.target_origin,
                        authorization.expected_canonical_origin,
                    )
                    candidate = _candidate(
                        url, viewport, capture=capture, capture_state=state,
                        resolution_state=resolution,
                    )
                    candidates.append(candidate)
                    if state != "blocked":
                        occurrences.extend(_link_occurrences(capture, viewport, authorization.target_origin))
                    if capture.blocked_resources and len(diagnostics) < MAX_DIAGNOSTICS:
                        diagnostics.append(BoundedDiagnostic(
                            "blocked-resource", url, viewport.name,
                            ",".join(sorted(set(capture.blocked_resources)))[:MAX_TEXT],
                        ))
            finally:
                if session is not None:
                    try:
                        session.clear_state()
                    finally:
                        session.close()

    return _result(authorization, candidates, occurrences, diagnostics)


def _result(
    authorization: CrawlAuthorization,
    candidates: list[PageCandidate],
    occurrences: list[LinkOccurrence],
    diagnostics: list[BoundedDiagnostic],
) -> AdapterResult:
    candidates.sort(key=lambda item: (item.requested_url, item.viewport, item.provenance_hash))
    occurrences.sort(key=lambda item: (item.source_url, item.target, item.viewport, item.provenance_hash))
    diagnostics.sort(key=lambda item: (item.category, item.route, item.viewport, item.detail))
    entity_groups: dict[str, list[str]] = {}
    entity_states: dict[str, set[str]] = {}
    for candidate in candidates:
        if candidate.final_url:
            canonical = candidate.canonical_url or candidate.final_url
            entity_groups.setdefault(canonical, []).append(candidate.provenance_hash)
            entity_states.setdefault(canonical, set()).add(candidate.resolution_state)
    entities: list[PageEntity] = []
    for canonical, hashes in sorted(entity_groups.items()):
        states = entity_states[canonical]
        resolution = next(iter(states)) if len(states) == 1 else "contradicted"
        payload = {
            "canonical_url": canonical,
            "resolution_state": resolution,
            "candidate_hashes": tuple(sorted(hashes)),
        }
        entities.append(PageEntity(**payload, stable_hash=_stable_hash(payload)))
    counts = {state: 0 for state in CAPTURE_STATES}
    for candidate in candidates:
        counts[candidate.capture_state] += 1
    coverage = CoverageSummary(
        requested_routes=len({item.requested_url for item in candidates}),
        attempted_captures=sum(counts[state] for state in ("complete", "partial", "timeout", "failed")),
        complete=counts["complete"],
        partial=counts["partial"],
        timeout=counts["timeout"],
        failed=counts["failed"],
        blocked=counts["blocked"],
        unchecked=counts["unchecked"],
        blocked_resources=sum(len(item.blocked_resource_classes) for item in candidates),
        revision_state=authorization.revision_state,
    )
    body = {
        "adapter": ADAPTER_NAME,
        "adapter_version": ADAPTER_VERSION,
        "target_origin": authorization.target_origin,
        "expected_canonical_origin": authorization.expected_canonical_origin,
        "expected_revision": authorization.expected_revision,
        "observed_revision": authorization.observed_revision,
        "revision_state": authorization.revision_state,
        "page_candidates": [asdict(item) for item in candidates],
        "page_entities": [asdict(item) for item in entities],
        "link_occurrences": [asdict(item) for item in occurrences],
        "coverage": asdict(coverage),
        "diagnostics": [asdict(item) for item in diagnostics[:MAX_DIAGNOSTICS]],
    }
    batch = EvidenceBatch(
        adapter=ADAPTER_NAME,
        adapter_version=ADAPTER_VERSION,
        target_origin=authorization.target_origin,
        expected_canonical_origin=authorization.expected_canonical_origin,
        expected_revision=authorization.expected_revision,
        observed_revision=authorization.observed_revision,
        revision_state=authorization.revision_state,
        page_candidates=tuple(candidates),
        page_entities=tuple(entities),
        link_occurrences=tuple(occurrences),
        coverage=coverage,
        diagnostics=tuple(diagnostics[:MAX_DIAGNOSTICS]),
        stable_hash=_stable_hash(body),
    )
    state = "accepted"
    if authorization.revision_state != "matched" or any(
        counts[item] for item in ("partial", "timeout", "failed", "blocked", "unchecked")
    ):
        state = "partial"
    if candidates and counts["unchecked"] == len(candidates):
        state = "unchecked"
    result_body = {"state": state, "evidence_batch_hash": batch.stable_hash}
    return AdapterResult(state, batch, _stable_hash(result_body))


def result_to_json(result: AdapterResult) -> str:
    """Serialize an adapter result with deterministic ordering."""
    return json.dumps(asdict(result), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
