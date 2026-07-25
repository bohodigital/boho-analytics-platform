"""Deterministic reconciliation for Graph Evidence Core 2.1.

The lane adapters deliberately emit independent evidence. This module converts
their bounded outputs to the frozen contract, reconciles only exact-revision
evidence, and preserves disagreement rather than inventing pages or edges.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from itertools import islice
from pathlib import PurePosixPath
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from .contracts import (
    AdapterResult,
    CoverageSummary,
    EvidenceBatch,
    LinkOccurrence,
    PageCandidate,
    PageEntity,
)
from .models import LINK_LAYERS, NON_TOPOLOGY_STATES, require_text, stable_hash
from .naming import assign_page_names


ADAPTER_VERSION = "2.1.0"
MAX_ADAPTER_RESULTS = 16
MAX_APPROVED_QUERY_KEYS = 32
MAX_RECONCILED_CANDIDATES = 100_000
MAX_RECONCILED_LINKS = 2_000_000
ENTITY_STATES = frozenset(
    {"confirmed-page", "source-only", "artifact-only", "rendered-only", "contradicted"}
)
_REVISION = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_SAFE_ADAPTER = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")


@dataclass(frozen=True, slots=True)
class LaneCoverage:
    adapter: str
    status: str
    revision_relation: str
    candidates: int
    pages: int
    relationships: int
    diagnostics: int


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    batch: EvidenceBatch
    lanes: tuple[LaneCoverage, ...]
    contradictions: tuple[str, ...]
    exclusions: tuple[str, ...]
    content_hash: str


def _origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ValueError("canonical origin must be an exact public HTTP(S) origin") from exc
    default = (parsed.scheme == "http" and port == 80) or (
        parsed.scheme == "https" and port == 443
    )
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host if port is None or default else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def _safe_adapter(value: str) -> str:
    normalized = value.casefold().replace("_", "-")
    if not _SAFE_ADAPTER.fullmatch(normalized):
        raise ValueError("adapter name is unsafe")
    return normalized


def _safe_relative(value: str, *, fallback: str) -> str:
    text = value.replace("\\", "/") if isinstance(value, str) else ""
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        return fallback
    return path.as_posix()[:1000]


def canonical_route(
    value: str,
    *,
    canonical_origin: str,
    approved_query_keys: frozenset[str] = frozenset(),
) -> tuple[str, str]:
    """Return ``(route, classification)`` without retaining query values."""

    if not isinstance(value, str) or not value or len(value) > 8192:
        return "", "unresolved"
    if "\\" in value or any(ord(character) < 32 for character in value):
        return "", "unresolved"
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            return "", "unresolved"
    except ValueError:
        return "", "unresolved"
    scheme = parsed.scheme.casefold()
    if scheme in {"mailto", "tel", "javascript", "data"}:
        return "", "action"
    if not scheme and not parsed.netloc and not parsed.path and parsed.fragment:
        return "", "fragment"
    if scheme or parsed.netloc:
        candidate_origin = _origin(
            urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        )
        if candidate_origin != canonical_origin:
            return "", "external"
    if parsed.query:
        keys = {key for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
        if not keys or not keys.issubset(approved_query_keys):
            return "", "unresolved"
    try:
        decoded = unquote(parsed.path or "/")
    except (UnicodeError, ValueError):
        return "", "unresolved"
    if (
        not decoded.startswith("/")
        or decoded.startswith("//")
        or "%" in decoded
        or "\\" in decoded
        or any(part in {".", ".."} for part in PurePosixPath(decoded).parts)
    ):
        return "", "unresolved"
    route = re.sub(r"/{2,}", "/", decoded)
    if route != "/" and not route.endswith("/") and "." not in PurePosixPath(route).name:
        route += "/"
    return route, "topology"


def _canonical_url(origin: str, route: str) -> str:
    return f"{origin}{route}" if route != "/" else f"{origin}/"


def _safe_raw_destination(value: str, state: str, canonical: str = "") -> str:
    if canonical:
        return canonical
    try:
        parsed = urlsplit(value if isinstance(value, str) else "")
    except ValueError:
        return f"[{state}]"
    if state == "action":
        candidate = f"{parsed.scheme.casefold()}:" if parsed.scheme else "[action]"
    elif state == "fragment":
        candidate = f"#{parsed.fragment[:500]}" if parsed.fragment else "[fragment]"
    elif state == "external" and parsed.scheme in {"http", "https"} and parsed.hostname:
        candidate = urlunsplit(
            (parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, "", "")
        )
    else:
        candidate = f"[{state}]"
    try:
        return require_text(
            candidate, "raw_destination", maximum=4000, allow_empty=True
        )
    except ValueError:
        return f"[{state}]"


def _safe_public_text(value: Any, *, maximum: int) -> str:
    candidate = value[:maximum] if isinstance(value, str) else ""
    try:
        return require_text(
            candidate, "public_evidence_text", maximum=maximum, allow_empty=True
        )
    except ValueError:
        return ""


def _revision_relation(repository_revision: str, evidence_revision: str) -> str:
    if not evidence_revision:
        return "unchecked"
    return "exact" if repository_revision == evidence_revision.lower() else "mismatch"


def _approved_query_keys(values: Iterable[str]) -> frozenset[str]:
    bounded = list(islice(values, MAX_APPROVED_QUERY_KEYS + 1))
    if len(bounded) > MAX_APPROVED_QUERY_KEYS:
        raise ValueError("too many approved query keys")
    if any(
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value)
        for value in bounded
    ):
        raise ValueError("approved query keys must be bounded public identifiers")
    return frozenset(bounded)


def _coverage(
    candidates: Sequence[PageCandidate],
    pages: Sequence[PageEntity],
    links: Sequence[LinkOccurrence],
) -> CoverageSummary:
    counts: dict[str, int] = defaultdict(int)
    for candidate in candidates:
        counts[candidate.resolution_state] += 1
    return CoverageSummary(
        len(candidates),
        len(pages),
        len(links),
        len(candidates),
        len(pages),
        len(links),
        tuple(sorted(counts.items())),
    )


def source_semantic_to_contract(
    result: Any,
    *,
    site_key: str,
    repository_revision: str,
    canonical_origin: str,
    approved_query_keys: Iterable[str] = (),
) -> AdapterResult:
    """Convert the bounded semantic lane result to the frozen evidence contract."""

    origin = _origin(canonical_origin)
    approved = _approved_query_keys(approved_query_keys)
    candidates_by_route: dict[str, PageCandidate] = {}
    unresolved: list[PageCandidate] = []
    links: list[LinkOccurrence] = []

    def route_candidate(route: str, path: str, location: str) -> PageCandidate:
        existing = candidates_by_route.get(route)
        if existing is not None:
            return existing
        if len(candidates_by_route) + len(unresolved) >= MAX_RECONCILED_CANDIDATES:
            raise ValueError("source evidence exceeds the reconciled candidate limit")
        candidate = PageCandidate(
            route,
            route,
            "source-only",
            _safe_relative(path, fallback="source/unknown.ts"),
            location[:1000],
            (("lane", "source-semantic"),),
        )
        candidates_by_route[route] = candidate
        return candidate

    for item in result.evidence:
        path = _safe_relative(item.source_path, fallback="source/unknown.ts")
        location = f"{path}:{item.source_line}:{item.source_column}"
        occurrence_location = f"{location}@{item.evidence_id}"
        source_route = item.source_route or "/"
        source_route, source_class = canonical_route(
            source_route, canonical_origin=origin, approved_query_keys=approved
        )
        if source_class != "topology":
            continue
        source = route_candidate(source_route, path, location)
        destination, classification = canonical_route(
            item.destination or "", canonical_origin=origin, approved_query_keys=approved
        )
        state = item.resolution_state
        if classification in NON_TOPOLOGY_STATES:
            state = classification
            destination = ""
        elif not destination:
            state = "dynamic-unknown" if state not in NON_TOPOLOGY_STATES else state
            unresolved.append(
                PageCandidate(
                    _safe_raw_destination(
                        item.raw_destination_expression, state
                    ),
                    "",
                    state,
                    path,
                    occurrence_location,
                    tuple(
                        sorted(
                            (
                                ("lane", "source-semantic"),
                                ("evidence", item.evidence_id),
                            )
                        )
                    ),
                )
            )
        else:
            route_candidate(destination, path, location)
            state = "source-only"
        layer = item.layer if item.layer in LINK_LAYERS else "contextual"
        if state == "action":
            layer = "action"
        links.append(
            LinkOccurrence(
                source.candidate_id,
                _safe_raw_destination(
                    item.destination or item.raw_destination_expression,
                    state,
                    destination,
                ),
                destination,
                state,
                path,
                occurrence_location,
                layer,
                confidence=float(item.confidence),
                provenance=(
                    ("evidence", item.evidence_id),
                    ("lane", "source-semantic"),
                ),
            )
        )
        if len(links) > MAX_RECONCILED_LINKS:
            raise ValueError("source evidence exceeds the reconciled relationship limit")

    candidates = tuple(
        sorted((*candidates_by_route.values(), *unresolved), key=lambda item: item.candidate_id)
    )
    names = assign_page_names(tuple(candidates_by_route))
    pages = tuple(
        PageEntity(
            candidate.candidate_id,
            route,
            _canonical_url(origin, route),
            names[route].short,
            names[route].full,
            names[route].source,
            names[route].confidence,
            "source-only",
            ("homepage",) if route == "/" else (),
        )
        for route, candidate in sorted(candidates_by_route.items())
    )
    relation = _revision_relation(repository_revision, result.repository_revision)
    batch = EvidenceBatch(
        site_key,
        "source-semantic",
        str(result.adapter_version),
        repository_revision,
        result.repository_revision if relation != "unchecked" else "",
        relation,
        candidates,
        pages,
        tuple(sorted(links, key=lambda item: item.occurrence_id)),
        _coverage(candidates, pages, links),
    )
    status = "partial" if unresolved or relation != "exact" else "succeeded"
    return AdapterResult(status, batch)


def artifact_evidence_to_contract(
    result: Any,
    *,
    site_key: str,
    repository_revision: str,
    canonical_origin: str,
    approved_query_keys: Iterable[str] = (),
) -> AdapterResult:
    """Convert bounded artifact/deployment evidence to the frozen contract."""

    origin = _origin(canonical_origin)
    approved = _approved_query_keys(approved_query_keys)
    candidates_by_route: dict[str, PageCandidate] = {}
    hints: dict[str, tuple[str, str]] = {}
    artifact_diagnostics = set(result.diagnostics)
    for route_evidence in result.routes:
        raw_route, classification = canonical_route(
            route_evidence.route, canonical_origin=origin, approved_query_keys=approved
        )
        if classification != "topology":
            continue
        route = raw_route
        canonical_conflict = (
            f"canonical-route-conflict:{route_evidence.source_path}"
            in artifact_diagnostics
        )
        if route_evidence.canonical_url:
            canonical, canonical_class = canonical_route(
                route_evidence.canonical_url,
                canonical_origin=origin,
                approved_query_keys=approved,
            )
            if canonical_class == "topology":
                route = canonical
            elif canonical_class == "external":
                canonical_conflict = True
        state = (
            "contradicted"
            if result.revision_state == "mismatched" or canonical_conflict
            else "redirect"
            if route_evidence.route_kind == "redirect"
            else "artifact-only"
        )
        path = _safe_relative(
            route_evidence.source_path, fallback="artifact/unknown.html"
        )
        candidates_by_route.setdefault(
            route,
            PageCandidate(
                raw_route,
                route,
                state,
                path,
                f"{path}:route",
                (("lane", "artifact-evidence"),),
            ),
        )
        if len(candidates_by_route) > MAX_RECONCILED_CANDIDATES:
            raise ValueError("artifact evidence exceeds the reconciled candidate limit")
        label = route_evidence.h1 or route_evidence.title
        if label:
            safe_label = _safe_public_text(label, maximum=240)
            if safe_label:
                hints[route] = (safe_label, "artifact-heading")
    links: list[LinkOccurrence] = []
    for item in result.links:
        source_route, source_class = canonical_route(
            item.source_route, canonical_origin=origin, approved_query_keys=approved
        )
        if source_class != "topology" or source_route not in candidates_by_route:
            continue
        destination, classification = canonical_route(
            item.destination, canonical_origin=origin, approved_query_keys=approved
        )
        state = (
            classification
            if classification in NON_TOPOLOGY_STATES
            else "artifact-only"
            if destination
            else "unresolved"
        )
        if destination and destination not in candidates_by_route:
            path = _safe_relative(item.source_path, fallback="artifact/unknown.html")
            candidates_by_route[destination] = PageCandidate(
                destination,
                destination,
                "artifact-only",
                path,
                f"{path}:destination",
                (("lane", "artifact-evidence"),),
            )
            if len(candidates_by_route) > MAX_RECONCILED_CANDIDATES:
                raise ValueError("artifact evidence exceeds the reconciled candidate limit")
        path = _safe_relative(item.source_path, fallback="artifact/unknown.html")
        links.append(
            LinkOccurrence(
                candidates_by_route[source_route].candidate_id,
                _safe_raw_destination(item.destination, state, destination),
                destination,
                state,
                path,
                item.source_location[:1000],
                "action" if state == "action" else "contextual",
                anchor_text=_safe_public_text(item.anchor_text, maximum=1000),
                provenance=(("content", item.content_hash), ("lane", "artifact-evidence")),
            )
        )
        if len(links) > MAX_RECONCILED_LINKS:
            raise ValueError("artifact evidence exceeds the reconciled relationship limit")
    candidates = tuple(sorted(candidates_by_route.values(), key=lambda item: item.candidate_id))
    names = assign_page_names(tuple(candidates_by_route), hints)
    pages = tuple(
        PageEntity(
            candidate.candidate_id,
            route,
            _canonical_url(origin, route),
            names[route].short,
            names[route].full,
            names[route].source,
            names[route].confidence,
            candidate.resolution_state,
            ("homepage",) if route == "/" else (),
        )
        for route, candidate in sorted(candidates_by_route.items())
        if candidate.resolution_state in ENTITY_STATES
    )
    relation = (
        "exact"
        if result.revision_state in {"matched", "associated"}
        and result.revision == repository_revision
        else "mismatch"
        if result.revision and result.revision != repository_revision
        else "unchecked"
    )
    batch = EvidenceBatch(
        site_key,
        "artifact-evidence",
        str(result.adapter_version),
        repository_revision,
        result.revision if relation != "unchecked" else "",
        relation,
        candidates,
        pages,
        tuple(sorted(links, key=lambda item: item.occurrence_id)),
        _coverage(candidates, pages, links),
    )
    return AdapterResult("succeeded" if relation == "exact" else "partial", batch)


def rendered_evidence_to_contract(
    result: Any,
    *,
    site_key: str,
    repository_revision: str,
    canonical_origin: str,
    approved_query_keys: Iterable[str] = (),
) -> AdapterResult:
    """Convert rendered-crawl evidence to the frozen evidence contract."""

    batch_result = result.evidence_batch
    origin = _origin(canonical_origin)
    evidence_origin = _origin(batch_result.target_origin)
    approved = _approved_query_keys(approved_query_keys)
    candidates_by_route: dict[str, PageCandidate] = {}
    hints: dict[str, tuple[str, str]] = {}
    state_by_route: dict[str, set[str]] = defaultdict(set)
    for item in batch_result.page_candidates:
        route, classification = canonical_route(
            item.requested_url,
            canonical_origin=evidence_origin,
            approved_query_keys=approved,
        )
        if classification != "topology":
            continue
        state_by_route[route].add(item.resolution_state)
        path = f"rendered/{item.viewport}.html"
        candidates_by_route.setdefault(
            route,
            PageCandidate(
                route,
                route,
                item.resolution_state,
                path,
                f"{path}:{route}",
                (("lane", "rendered-crawl"),),
            ),
        )
        if len(candidates_by_route) > MAX_RECONCILED_CANDIDATES:
            raise ValueError("rendered evidence exceeds the reconciled candidate limit")
        if item.title:
            safe_title = _safe_public_text(item.title, maximum=240)
            if safe_title:
                hints[route] = (safe_title, "rendered-title")
    for route, candidate in tuple(candidates_by_route.items()):
        states = state_by_route[route]
        state = next(iter(states)) if len(states) == 1 else "contradicted"
        if state not in ENTITY_STATES:
            state = "rendered-only" if state == "confirmed-page" else state
        candidates_by_route[route] = PageCandidate(
            candidate.raw_route,
            candidate.canonical_route,
            state,
            candidate.source_path,
            candidate.source_location,
            candidate.provenance,
        )
    links: list[LinkOccurrence] = []
    for occurrence_index, item in enumerate(batch_result.link_occurrences, 1):
        source_route, source_class = canonical_route(
            item.source_url,
            canonical_origin=evidence_origin,
            approved_query_keys=approved,
        )
        if source_class != "topology" or source_route not in candidates_by_route:
            continue
        destination, classification = canonical_route(
            item.target,
            canonical_origin=evidence_origin,
            approved_query_keys=approved,
        )
        state = classification if classification in NON_TOPOLOGY_STATES else item.resolution_state
        if destination:
            state = "rendered-only"
            candidates_by_route.setdefault(
                destination,
                PageCandidate(
                    destination,
                    destination,
                    "rendered-only",
                    f"rendered/{item.viewport}.html",
                    f"rendered/{item.viewport}.html:{destination}",
                    (("lane", "rendered-crawl"),),
                ),
            )
            if len(candidates_by_route) > MAX_RECONCILED_CANDIDATES:
                raise ValueError("rendered evidence exceeds the reconciled candidate limit")
        landmark = item.landmark.casefold()
        layer = (
            "action"
            if state == "action" or item.kind == "form"
            else "menu"
            if landmark in {"nav", "navigation", "header"}
            else "utility"
            if landmark == "footer"
            else "contextual"
        )
        path = f"rendered/{item.viewport}.html"
        links.append(
            LinkOccurrence(
                candidates_by_route[source_route].candidate_id,
                _safe_raw_destination(item.target, state, destination),
                destination,
                state,
                path,
                f"{path}:{item.provenance_hash[:24]}:{occurrence_index}",
                layer,
                anchor_text=_safe_public_text(item.text, maximum=1000),
                accessible_name=_safe_public_text(
                    item.accessible_name, maximum=1000
                ),
                landmark=_safe_public_text(item.landmark, maximum=200),
                nofollow=bool(item.nofollow),
                visible=bool(item.visible),
                viewport=item.viewport[:100],
                provenance=(
                    ("evidence", item.provenance_hash),
                    ("lane", "rendered-crawl"),
                ),
            )
        )
        if len(links) > MAX_RECONCILED_LINKS:
            raise ValueError("rendered evidence exceeds the reconciled relationship limit")
    candidates = tuple(sorted(candidates_by_route.values(), key=lambda item: item.candidate_id))
    names = assign_page_names(tuple(candidates_by_route), hints)
    pages = tuple(
        PageEntity(
            candidate.candidate_id,
            route,
            _canonical_url(origin, route),
            names[route].short,
            names[route].full,
            names[route].source,
            names[route].confidence,
            candidate.resolution_state
            if candidate.resolution_state in ENTITY_STATES
            else "rendered-only",
            ("homepage",) if route == "/" else (),
        )
        for route, candidate in sorted(candidates_by_route.items())
        if candidate.resolution_state not in {"missing", "redirect", "unchecked", "unresolved"}
    )
    relation = (
        "exact"
        if batch_result.revision_state == "matched"
        and batch_result.observed_revision == repository_revision
        else "mismatch"
    )
    batch = EvidenceBatch(
        site_key,
        "rendered-crawl",
        str(batch_result.adapter_version),
        repository_revision,
        batch_result.observed_revision,
        relation,
        candidates,
        pages,
        tuple(sorted(links, key=lambda item: item.occurrence_id)),
        _coverage(candidates, pages, links),
    )
    return AdapterResult("succeeded" if result.state == "accepted" else "partial", batch)


def _reconciled_state(states: set[str], revision_mismatch: bool) -> str:
    if revision_mismatch or "contradicted" in states:
        return "contradicted"
    positive = states & {"confirmed-page", "source-only", "artifact-only", "rendered-only"}
    if "missing" in states and positive:
        return "contradicted"
    if "redirect" in states:
        if positive - {"source-only"}:
            return "contradicted"
        return "redirect"
    if "excluded" in states and positive:
        return "contradicted"
    if "confirmed-page" in states or len(positive) >= 2:
        return "confirmed-page"
    for state in (
        "rendered-only",
        "artifact-only",
        "source-only",
        "redirect",
        "missing",
        "dynamic-unknown",
        "unchecked",
        "unresolved",
        "excluded",
    ):
        if state in states:
            return state
    return "unchecked"


def reconcile_adapter_results(
    results: Iterable[AdapterResult],
    *,
    site_key: str,
    repository_revision: str,
    canonical_origin: str,
) -> ReconciliationResult:
    """Reconcile exact, bounded lane batches without inventing topology."""

    if not _REVISION.fullmatch(repository_revision):
        raise ValueError("repository_revision must be an exact lowercase revision")
    origin = _origin(canonical_origin)
    bounded: list[AdapterResult] = []
    for item in results:
        bounded.append(item)
        if len(bounded) > MAX_ADAPTER_RESULTS:
            raise ValueError(f"at most {MAX_ADAPTER_RESULTS} adapter results may be reconciled")
    if not bounded:
        raise ValueError("at least one adapter result is required")

    lanes: list[LaneCoverage] = []
    route_evidence: dict[str, list[tuple[EvidenceBatch, PageCandidate]]] = defaultdict(list)
    unresolved: list[tuple[EvidenceBatch, PageCandidate]] = []
    page_hint_options: dict[str, list[tuple[str, str]]] = defaultdict(list)
    alias_options: dict[str, set[str]] = defaultdict(set)
    redirect_targets: dict[str, set[str]] = defaultdict(set)
    diagnostics: list[dict[str, str]] = []
    batch_ids: dict[int, str] = {}
    for result in bounded:
        batch = result.batch
        adapter = batch.adapter if batch else "unavailable"
        relation = batch.revision_relation if batch else "unchecked"
        lanes.append(
            LaneCoverage(
                adapter,
                result.status,
                relation,
                len(batch.candidates) if batch else 0,
                len(batch.pages) if batch else 0,
                len(batch.links) if batch else 0,
                len(result.diagnostics) + (len(batch.diagnostics) if batch else 0),
            )
        )
        if batch is None:
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "lane-unavailable",
                    "message": "One evidence lane was unavailable; its coverage remains unchecked.",
                }
            )
            continue
        if batch.site_key != site_key or batch.repository_revision != repository_revision:
            raise ValueError("adapter batch site or repository revision does not match reconciliation")
        batch_ids[id(batch)] = batch.batch_id
        for candidate in batch.candidates:
            if candidate.canonical_route:
                route_evidence[candidate.canonical_route].append((batch, candidate))
                if (
                    candidate.raw_route
                    and candidate.raw_route != candidate.canonical_route
                    and "?" not in candidate.raw_route
                    and "#" not in candidate.raw_route
                ):
                    alias_options[candidate.canonical_route].add(candidate.raw_route)
            else:
                unresolved.append((batch, candidate))
        for page in batch.pages:
            page_hint_options[page.canonical_route].append(
                (page.display_name_short, page.naming_source)
            )
            alias_options[page.canonical_route].update(page.aliases)
        candidate_routes = {
            candidate.candidate_id: candidate.canonical_route
            for candidate in batch.candidates
        }
        candidate_states = {
            candidate.candidate_id: candidate.resolution_state
            for candidate in batch.candidates
        }
        for link in batch.links:
            if (
                candidate_states.get(link.source_candidate_id) == "redirect"
                and link.canonical_destination
            ):
                redirect_targets[candidate_routes[link.source_candidate_id]].add(
                    link.canonical_destination
                )

    merged_candidates: dict[str, PageCandidate] = {}
    contradictions: list[str] = []
    exclusions: list[str] = []
    for route, evidence in sorted(route_evidence.items()):
        states = {candidate.resolution_state for _batch, candidate in evidence}
        mismatch = any(batch.revision_relation == "mismatch" for batch, _candidate in evidence)
        state = _reconciled_state(states, mismatch)
        provenance = tuple(
            sorted(
                {
                    ("batch", batch_ids[id(batch)])
                    for batch, _candidate in evidence
                }
                | {
                    ("candidate", candidate.candidate_id)
                    for _batch, candidate in evidence
                }
            )
        )
        merged_candidates[route] = PageCandidate(
            route,
            route,
            state,
            "reconciliation/routes.json",
            f"route:{stable_hash(route)[:24]}",
            provenance,
        )
        if len(merged_candidates) > MAX_RECONCILED_CANDIDATES:
            raise ValueError("reconciliation exceeds the candidate limit")
        if state == "contradicted":
            contradictions.append(route)
        if state == "excluded":
            exclusions.append(route)
    for alias, targets in sorted(redirect_targets.items()):
        if (
            alias in merged_candidates
            and merged_candidates[alias].resolution_state == "redirect"
            and len(targets) == 1
        ):
            target = next(iter(targets))
            if target in merged_candidates:
                alias_options[target].add(alias)

    unresolved_candidates = tuple(
        PageCandidate(
            candidate.raw_route,
            "",
            candidate.resolution_state,
            _safe_relative(
                f"evidence/{_safe_adapter(batch.adapter)}/{candidate.source_path}",
                fallback="evidence/unavailable/unknown.txt",
            ),
            candidate.source_location,
            tuple(
                sorted(
                    {
                        ("batch", batch_ids[id(batch)]),
                        ("candidate", candidate.candidate_id),
                    }
                )
            ),
        )
        for batch, candidate in sorted(
            unresolved, key=lambda item: (item[0].adapter, item[1].candidate_id)
        )
    )
    if len(merged_candidates) + len(unresolved_candidates) > MAX_RECONCILED_CANDIDATES:
        raise ValueError("reconciliation exceeds the candidate limit")
    page_hints = {
        route: min(values, key=lambda item: (item[1], item[0]))
        for route, values in page_hint_options.items()
    }
    names = assign_page_names(tuple(merged_candidates), page_hints)
    pages = tuple(
        PageEntity(
            candidate.candidate_id,
            route,
            _canonical_url(origin, route),
            names[route].short,
            names[route].full,
            names[route].source,
            names[route].confidence,
            candidate.resolution_state,
            ("homepage",) if route == "/" else (),
            tuple(sorted(alias_options.get(route, set()))),
        )
        for route, candidate in sorted(merged_candidates.items())
        if candidate.resolution_state in ENTITY_STATES
    )

    links: list[LinkOccurrence] = []
    for result in bounded:
        batch = result.batch
        if batch is None:
            continue
        candidate_routes = {
            candidate.candidate_id: candidate.canonical_route
            for candidate in batch.candidates
        }
        adapter = _safe_adapter(batch.adapter)
        for item in batch.links:
            source_route = candidate_routes.get(item.source_candidate_id, "")
            source = merged_candidates.get(source_route)
            if source is None or source.resolution_state not in ENTITY_STATES:
                continue
            destination = item.canonical_destination
            state = item.resolution_state
            if destination:
                destination_candidate = merged_candidates.get(destination)
                if destination_candidate is None:
                    destination = ""
                    state = "unresolved"
                elif destination_candidate.resolution_state == "contradicted":
                    state = "contradicted"
            if state in NON_TOPOLOGY_STATES:
                destination = ""
            path = _safe_relative(
                f"evidence/{adapter}/{item.source_path}",
                fallback=f"evidence/{adapter}/unknown.txt",
            )
            layer = item.layer if item.layer in LINK_LAYERS else "contextual"
            if state == "action":
                layer = "action"
            links.append(
                LinkOccurrence(
                    source.candidate_id,
                    _safe_raw_destination(
                        item.raw_destination, state, destination
                    ),
                    destination,
                    state,
                    path,
                    f"{adapter}:{item.source_location}"[:1000],
                    layer,
                    anchor_text=item.anchor_text,
                    accessible_name=item.accessible_name,
                    landmark=item.landmark,
                    confidence=item.confidence,
                    nofollow=item.nofollow,
                    visible=item.visible,
                    viewport=item.viewport,
                    provenance=tuple(
                        sorted(
                            {
                                *item.provenance,
                                ("batch", batch_ids[id(batch)]),
                                ("occurrence", item.occurrence_id),
                            }
                        )
                    ),
                )
            )
            if len(links) > MAX_RECONCILED_LINKS:
                raise ValueError("reconciliation exceeds the relationship limit")

    candidates = tuple(
        sorted(
            (*merged_candidates.values(), *unresolved_candidates),
            key=lambda item: item.candidate_id,
        )
    )
    links.sort(key=lambda item: item.occurrence_id)
    if contradictions:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "evidence-contradiction",
                "message": f"{len(contradictions)} route identities contain conflicting evidence.",
            }
        )
    if any(lane.revision_relation == "mismatch" for lane in lanes):
        diagnostics.append(
            {
                "severity": "warning",
                "code": "revision-mismatch",
                "message": "At least one evidence lane targets a different exact revision.",
            }
        )
    batch = EvidenceBatch(
        site_key,
        "reconciliation",
        ADAPTER_VERSION,
        repository_revision,
        repository_revision,
        "exact",
        candidates,
        pages,
        tuple(links),
        _coverage(candidates, pages, links),
        tuple(diagnostics[:100]),
    )
    lane_tuple = tuple(sorted(lanes, key=lambda item: (item.adapter, item.status)))
    body = {
        "batch_id": batch.batch_id,
        "lanes": [asdict(item) for item in lane_tuple],
        "contradictions": sorted(contradictions),
        "exclusions": sorted(exclusions),
    }
    return ReconciliationResult(
        batch,
        lane_tuple,
        tuple(sorted(contradictions)),
        tuple(sorted(exclusions)),
        stable_hash(body),
    )


def publish_reconciled_evidence(
    store: Any,
    result: ReconciliationResult,
    *,
    repository_snapshot_id: str,
    manifest_version_id: str,
    goal_definition_hash: str,
) -> str:
    """Publish a reconciled batch through the accepted atomic persistence API."""

    return store.publish_evidence_batch(
        result.batch,
        repository_snapshot_id=repository_snapshot_id,
        manifest_version_id=manifest_version_id,
        compiler_version="core21-reconciliation-2.1.0",
        projection_name="all-page-links",
        goal_definition_hash=goal_definition_hash,
    )
