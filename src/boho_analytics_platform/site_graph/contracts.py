"""Frozen Graph Evidence Core 2.1 adapter and persistence contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .models import (
    ADAPTER_STATUSES,
    LINK_LAYERS,
    NON_TOPOLOGY_STATES,
    RESOLUTION_STATES,
    REVISION_RELATIONS,
    bounded_json,
    normalize_diagnostics,
    require_choice,
    require_revision,
    require_relative_path,
    require_text,
    stable_hash,
    stable_id,
)


@dataclass(frozen=True, slots=True)
class PageCandidate:
    raw_route: str
    canonical_route: str
    resolution_state: str
    source_path: str
    source_location: str
    provenance: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        require_text(self.raw_route, "candidate.raw_route", maximum=4000, allow_empty=True)
        require_text(self.canonical_route, "candidate.canonical_route", maximum=2000, allow_empty=True)
        require_choice(self.resolution_state, RESOLUTION_STATES, "candidate.resolution_state")
        require_relative_path(self.source_path, "candidate.source_path")
        require_text(self.source_location, "candidate.source_location", maximum=1000)
        if self.provenance != tuple(sorted(set(self.provenance))):
            raise ValueError("candidate.provenance must be unique and sorted")
        bounded_json(dict(self.provenance), "candidate.provenance")
        if self.resolution_state in NON_TOPOLOGY_STATES and self.canonical_route:
            raise ValueError("action, fragment, and external candidates cannot become topology")
        if self.canonical_route and ("?" in self.canonical_route or "#" in self.canonical_route):
            raise ValueError("canonical candidate routes cannot contain query strings or fragments")

    @property
    def candidate_id(self) -> str:
        return stable_id("sgc", self.raw_route, self.canonical_route, self.source_path, self.source_location)

    @property
    def content_hash(self) -> str:
        return stable_hash(self.normalized())

    def normalized(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "raw_route": self.raw_route,
            "canonical_route": self.canonical_route,
            "resolution_state": self.resolution_state,
            "source_path": self.source_path,
            "source_location": self.source_location,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class PageEntity:
    candidate_id: str
    canonical_route: str
    canonical_url: str
    display_name_short: str
    display_name_full: str
    naming_source: str
    naming_confidence: float
    resolution_state: str
    roles: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.candidate_id, "page.candidate_id", maximum=64)
        require_text(self.canonical_route, "page.canonical_route", maximum=2000)
        require_text(self.canonical_url, "page.canonical_url", maximum=4000)
        require_text(self.display_name_short, "page.display_name_short", maximum=120)
        require_text(self.display_name_full, "page.display_name_full", maximum=240)
        require_text(self.naming_source, "page.naming_source", maximum=100)
        if isinstance(self.naming_confidence, bool) or not isinstance(self.naming_confidence, (int, float)):
            raise ValueError("page.naming_confidence must be numeric")
        if not 0 <= float(self.naming_confidence) <= 1:
            raise ValueError("page.naming_confidence must be from 0 to 1")
        require_choice(self.resolution_state, RESOLUTION_STATES, "page.resolution_state")
        if "?" in self.canonical_route or "#" in self.canonical_route:
            raise ValueError("canonical page routes cannot contain query strings or fragments")
        if "?" in self.canonical_url or "#" in self.canonical_url:
            raise ValueError("canonical page URLs cannot contain query strings or fragments")
        if self.roles != tuple(sorted(set(self.roles))) or self.aliases != tuple(sorted(set(self.aliases))):
            raise ValueError("page roles and aliases must be unique and sorted")
        for role in self.roles:
            require_text(role, "page.roles[]", maximum=100)
        for alias in self.aliases:
            require_text(alias, "page.aliases[]", maximum=2000)

    @property
    def page_id(self) -> str:
        return stable_id("sgp", self.candidate_id, self.canonical_route)

    def normalized(self) -> dict[str, Any]:
        return {"page_id": self.page_id, **asdict(self)}


@dataclass(frozen=True, slots=True)
class LinkOccurrence:
    source_candidate_id: str
    raw_destination: str
    canonical_destination: str
    resolution_state: str
    source_path: str
    source_location: str
    layer: str
    anchor_text: str = ""
    accessible_name: str = ""
    landmark: str = ""
    confidence: float = 1.0
    nofollow: bool = False
    visible: bool | None = None
    viewport: str = ""
    provenance: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        require_text(self.source_candidate_id, "link.source_candidate_id", maximum=64)
        require_text(self.raw_destination, "link.raw_destination", maximum=4000, allow_empty=True)
        require_text(self.canonical_destination, "link.canonical_destination", maximum=4000, allow_empty=True)
        require_choice(self.resolution_state, RESOLUTION_STATES, "link.resolution_state")
        require_relative_path(self.source_path, "link.source_path")
        require_text(self.source_location, "link.source_location", maximum=1000)
        require_choice(self.layer, LINK_LAYERS, "link.layer")
        for field_name, value, maximum in (
            ("anchor_text", self.anchor_text, 1000),
            ("accessible_name", self.accessible_name, 1000),
            ("landmark", self.landmark, 200),
            ("viewport", self.viewport, 100),
        ):
            require_text(value, f"link.{field_name}", maximum=maximum, allow_empty=True)
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise ValueError("link.confidence must be numeric")
        if not 0 <= float(self.confidence) <= 1:
            raise ValueError("link.confidence must be from 0 to 1")
        if not isinstance(self.nofollow, bool) or self.visible not in {True, False, None}:
            raise ValueError("link boolean fields are invalid")
        if self.provenance != tuple(sorted(set(self.provenance))):
            raise ValueError("link.provenance must be unique and sorted")
        bounded_json(dict(self.provenance), "link.provenance")
        if self.resolution_state in NON_TOPOLOGY_STATES and self.canonical_destination:
            raise ValueError("action, fragment, and external occurrences cannot become topology")
        if self.canonical_destination and ("?" in self.canonical_destination or "#" in self.canonical_destination):
            raise ValueError("canonical link destinations cannot contain query strings or fragments")
        if self.resolution_state == "action" and self.layer != "action":
            raise ValueError("action occurrences must use the action layer")

    @property
    def occurrence_id(self) -> str:
        return stable_id(
            "sgl", self.source_candidate_id, self.raw_destination, self.source_path,
            self.source_location, self.layer, self.viewport,
        )

    @property
    def topology_eligible(self) -> bool:
        return self.resolution_state not in NON_TOPOLOGY_STATES and bool(self.canonical_destination)

    def normalized(self) -> dict[str, Any]:
        return {
            "occurrence_id": self.occurrence_id,
            **asdict(self),
            "provenance": dict(self.provenance),
            "topology_eligible": self.topology_eligible,
        }


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    route_total: int
    page_total: int
    relationship_total: int
    analyzed_routes: int
    analyzed_pages: int
    analyzed_relationships: int
    state_counts: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        values = (
            self.route_total, self.page_total, self.relationship_total,
            self.analyzed_routes, self.analyzed_pages, self.analyzed_relationships,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("coverage counts must be non-negative integers")
        if (
            self.analyzed_routes > self.route_total
            or self.analyzed_pages > self.page_total
            or self.analyzed_relationships > self.relationship_total
        ):
            raise ValueError("analyzed coverage cannot exceed analytical totals")
        if self.state_counts != tuple(sorted(set(self.state_counts))):
            raise ValueError("coverage state_counts must be unique and sorted")
        for state, count in self.state_counts:
            require_choice(state, RESOLUTION_STATES, "coverage.state")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("coverage state counts must be non-negative integers")

    @property
    def coverage_id(self) -> str:
        return stable_id("sgv", self.normalized())

    def normalized(self) -> dict[str, Any]:
        return {
            "route_total": self.route_total,
            "page_total": self.page_total,
            "relationship_total": self.relationship_total,
            "analyzed_routes": self.analyzed_routes,
            "analyzed_pages": self.analyzed_pages,
            "analyzed_relationships": self.analyzed_relationships,
            "state_counts": dict(self.state_counts),
        }


@dataclass(frozen=True, slots=True)
class EvidenceBatch:
    site_key: str
    adapter: str
    adapter_version: str
    repository_revision: str
    evidence_revision: str
    revision_relation: str
    candidates: tuple[PageCandidate, ...]
    pages: tuple[PageEntity, ...]
    links: tuple[LinkOccurrence, ...]
    coverage: CoverageSummary
    diagnostics: tuple[dict[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if len(self.candidates) > 100_000 or len(self.pages) > 100_000 or len(self.links) > 2_000_000:
            raise ValueError("evidence batch exceeds bounded record limits")
        require_text(self.site_key, "batch.site_key", maximum=100)
        require_text(self.adapter, "batch.adapter", maximum=100)
        require_text(self.adapter_version, "batch.adapter_version", maximum=100)
        require_revision(self.repository_revision, "batch.repository_revision")
        require_revision(self.evidence_revision, "batch.evidence_revision", allow_empty=True)
        require_choice(self.revision_relation, REVISION_RELATIONS, "batch.revision_relation")
        if self.revision_relation == "exact" and self.repository_revision != self.evidence_revision:
            raise ValueError("exact evidence must use the repository revision")
        if self.revision_relation == "mismatch" and self.repository_revision == self.evidence_revision:
            raise ValueError("revision mismatch must preserve different revisions")
        if self.revision_relation == "unchecked" and self.evidence_revision:
            raise ValueError("unchecked revision evidence must not claim a revision")
        candidate_ids = [item.candidate_id for item in self.candidates]
        page_ids = [item.page_id for item in self.pages]
        link_ids = [item.occurrence_id for item in self.links]
        if any(len(values) != len(set(values)) for values in (candidate_ids, page_ids, link_ids)):
            raise ValueError("batch identities must be unique")
        known_candidates = set(candidate_ids)
        for page in self.pages:
            if page.candidate_id not in known_candidates:
                raise ValueError(f"page references unknown candidate: {page.candidate_id}")
        for link in self.links:
            if link.source_candidate_id not in known_candidates:
                raise ValueError(f"link references unknown source candidate: {link.source_candidate_id}")
        if (
            self.coverage.route_total < len(self.candidates)
            or self.coverage.page_total < len(self.pages)
            or self.coverage.relationship_total < len(self.links)
        ):
            raise ValueError("coverage analytical totals cannot be capped below emitted evidence")
        normalize_diagnostics(self.diagnostics)

    @property
    def batch_id(self) -> str:
        return stable_id(
            "sgb", self.site_key, self.adapter, self.adapter_version,
            self.repository_revision, self.evidence_revision, self.revision_relation,
            self.content_hash,
        )

    @property
    def content_hash(self) -> str:
        return stable_hash(self.normalized(include_id=False))

    def normalized(self, *, include_id: bool = True) -> dict[str, Any]:
        value = {
            "site_key": self.site_key,
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "repository_revision": self.repository_revision,
            "evidence_revision": self.evidence_revision,
            "revision_relation": self.revision_relation,
            "candidates": [item.normalized() for item in sorted(self.candidates, key=lambda item: item.candidate_id)],
            "pages": [item.normalized() for item in sorted(self.pages, key=lambda item: item.page_id)],
            "links": [item.normalized() for item in sorted(self.links, key=lambda item: item.occurrence_id)],
            "coverage": {"coverage_id": self.coverage.coverage_id, **self.coverage.normalized()},
            "diagnostics": list(normalize_diagnostics(self.diagnostics)),
        }
        return {"batch_id": self.batch_id, **value} if include_id else value


@dataclass(frozen=True, slots=True)
class AdapterResult:
    status: str
    batch: EvidenceBatch | None
    diagnostics: tuple[dict[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        require_choice(self.status, ADAPTER_STATUSES, "adapter_result.status")
        normalize_diagnostics(self.diagnostics)
        if self.status in {"succeeded", "partial"} and self.batch is None:
            raise ValueError("successful or partial adapter results require an evidence batch")

    @property
    def content_hash(self) -> str:
        return stable_hash({
            "status": self.status,
            "batch_id": self.batch.batch_id if self.batch else None,
            "diagnostics": list(normalize_diagnostics(self.diagnostics)),
        })
