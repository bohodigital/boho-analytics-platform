"""Fact-first SQLite persistence for site-graph evidence and derived artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..storage import SQLiteMetricStore
from .contracts import EvidenceBatch
from .manifest import SiteGraphManifest, validate_repository_remote


LINK_LAYERS = {"menu", "breadcrumb", "contextual", "related", "action", "utility"}
MAX_EVIDENCE_BYTES = 64 * 1024
MAX_FACT_BATCH_BYTES = 64 * 1024 * 1024
MAX_FINDING_BYTES = 1024 * 1024


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _id(namespace: str, *values: str) -> str:
    digest = hashlib.sha256(_json([namespace, *values]).encode("utf-8")).hexdigest()
    return f"{namespace}_{digest[:32]}"


def _text(value: str, where: str, *, allow_empty: bool = False, maximum: int = 5000) -> str:
    if not isinstance(value, str) or (not allow_empty and not value) or len(value) > maximum or "\x00" in value:
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise ValueError(f"{where} must be {qualifier} of at most {maximum} characters")
    return value


def _boolean(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{where} must be true or false")
    return value


def _bounded_json(value: Any, where: str, maximum: int) -> str:
    try:
        result = _json(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where} must be finite JSON data") from exc
    if len(result.encode("utf-8")) > maximum:
        raise ValueError(f"{where} exceeds {maximum} serialized bytes")
    return result


def _immutable(db: sqlite3.Connection, table: str, record_id: str, record_hash: str) -> None:
    row = db.execute(f"SELECT record_hash FROM {table} WHERE id=?", (record_id,)).fetchone()
    if row is None or row["record_hash"] != record_hash:
        raise ValueError(f"immutable {table} key collides with different content")


@dataclass(frozen=True)
class PageFact:
    fact_key: str
    route: str
    canonical_url: str
    source_path: str
    evidence: dict[str, Any]
    content_hash: str


@dataclass(frozen=True)
class LinkOccurrence:
    occurrence_key: str
    source_fact_key: str
    raw_destination: str
    canonical_destination: str
    anchor_text: str
    context_excerpt: str
    source_location: str
    landmark: str
    layer: str
    confidence: float
    evidence: dict[str, Any]
    repeated_template: bool = False
    crawlable: bool = True
    nofollow: bool = False
    external: bool = False
    fragment: bool = False
    action_kind: str | None = None


@dataclass
class SiteGraphStore:
    path: str | Path
    _base: SQLiteMetricStore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._base = SQLiteMetricStore(self.path)

    def connect(self, *, readonly: bool = False):
        return self._base.connect(readonly=readonly)

    def initialize(self) -> None:
        self._base.initialize()

    def integrity_check(self) -> str:
        return self._base.integrity_check()

    def backup(self, destination: str | Path) -> Path:
        return self._base.backup(destination)

    def restore(self, source: str | Path, *, confirmed: bool = False) -> None:
        self._base.restore(source, confirmed=confirmed)

    def save_manifest(self, manifest: SiteGraphManifest) -> str:
        record_id = _id("sgm", manifest.manifest_hash)
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO site_graph_manifest_versions
                   (id,manifest_hash,site_key,schema_version,canonical_json,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (record_id, manifest.manifest_hash, manifest.site.key, manifest.schema_version, manifest.canonical_json, _now()),
            )
            row = db.execute(
                "SELECT manifest_hash,site_key,schema_version,canonical_json FROM site_graph_manifest_versions WHERE id=?",
                (record_id,),
            ).fetchone()
            expected = (manifest.manifest_hash, manifest.site.key, manifest.schema_version, manifest.canonical_json)
            if row is None or tuple(row) != expected:
                raise ValueError("immutable manifest key collides with different content")
        return record_id

    def start_ingest(self, *, manifest_version_id: str, site_key: str, analysis_mode: str) -> str:
        _text(site_key, "site_key", maximum=100)
        if analysis_mode not in {"source-only", "build"}:
            raise ValueError("analysis_mode must be source-only or build")
        run_id = f"sgi_{uuid.uuid4().hex}"
        with self.connect() as db:
            manifest = db.execute(
                "SELECT site_key FROM site_graph_manifest_versions WHERE id=?", (manifest_version_id,)
            ).fetchone()
            if manifest is None:
                raise ValueError("unknown manifest version")
            if manifest["site_key"] != site_key:
                raise ValueError("ingest site_key does not match manifest")
            db.execute(
                """INSERT INTO site_graph_ingest_runs
                   (id,manifest_version_id,site_key,analysis_mode,started_at,status)
                   VALUES (?,?,?,?,?,?)""",
                (run_id, manifest_version_id, site_key, analysis_mode, _now(), "running"),
            )
        return run_id

    def finish_ingest(self, ingest_run_id: str, *, status: str) -> None:
        if status not in {"succeeded", "failed"}:
            raise ValueError("finished ingest status must be succeeded or failed")
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE site_graph_ingest_runs SET status=?,finished_at=?
                   WHERE id=? AND status='running'""",
                (status, _now(), ingest_run_id),
            )
            if cursor.rowcount != 1:
                row = db.execute("SELECT status FROM site_graph_ingest_runs WHERE id=?", (ingest_run_id,)).fetchone()
                if row is None:
                    raise ValueError("unknown ingest run")
                raise ValueError(f"ingest run is not running: {row['status']}")

    def save_repository_snapshot(
        self,
        *,
        ingest_run_id: str,
        site_key: str,
        repository_identity: str,
        remote_url: str,
        revision: str,
        ref: str,
        clean: bool,
        content_hash: str,
    ) -> str:
        values = {
            "site_key": _text(site_key, "site_key", maximum=100),
            "repository_identity": _text(repository_identity, "repository_identity", maximum=500),
            "remote_url": validate_repository_remote(remote_url, "remote_url"),
            "revision": _text(revision, "revision", maximum=128),
            "ref": _text(ref, "ref", maximum=255),
            "clean": _boolean(clean, "clean"),
            "content_hash": _text(content_hash, "content_hash", maximum=128),
        }
        record_id = _id("sgr", ingest_run_id)
        with self.connect() as db:
            run = db.execute("SELECT site_key,status FROM site_graph_ingest_runs WHERE id=?", (ingest_run_id,)).fetchone()
            if run is None:
                raise ValueError("unknown ingest run")
            if run["site_key"] != site_key:
                raise ValueError("repository snapshot site_key does not match ingest run")
            if run["status"] != "running":
                raise ValueError(f"repository snapshot requires a running ingest run, got {run['status']}")
            db.execute(
                """INSERT OR IGNORE INTO site_graph_repository_snapshots
                   (id,ingest_run_id,site_key,repository_identity,remote_url,revision,ref,clean,content_hash,captured_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (record_id, ingest_run_id, values["site_key"], values["repository_identity"], values["remote_url"],
                 values["revision"], values["ref"], int(values["clean"]), values["content_hash"], _now()),
            )
            row = db.execute(
                """SELECT site_key,repository_identity,remote_url,revision,ref,clean,content_hash
                   FROM site_graph_repository_snapshots WHERE id=?""", (record_id,)
            ).fetchone()
            expected = (
                values["site_key"], values["repository_identity"], values["remote_url"], values["revision"],
                values["ref"], int(values["clean"]), values["content_hash"],
            )
            if row is None or tuple(row) != expected:
                raise ValueError("immutable repository snapshot key collides with different content")
        return record_id

    def save_fact_batch(
        self,
        repository_snapshot_id: str,
        *,
        pages: list[PageFact],
        links: list[LinkOccurrence],
        _connection: sqlite3.Connection | None = None,
    ) -> None:
        if len(pages) > 100_000 or len(links) > 2_000_000:
            raise ValueError("fact batch exceeds bounded limits")
        manager = nullcontext(_connection) if _connection is not None else self.connect()
        with manager as db:
            if db.execute("SELECT 1 FROM site_graph_repository_snapshots WHERE id=?", (repository_snapshot_id,)).fetchone() is None:
                raise ValueError("unknown repository snapshot")
            seen_pages: set[str] = set()
            batch_bytes = 0
            for page in pages:
                if page.fact_key in seen_pages:
                    raise ValueError(f"duplicate page fact in batch: {page.fact_key}")
                seen_pages.add(page.fact_key)
                payload = {
                    "fact_key": _text(page.fact_key, "page.fact_key", maximum=500),
                    "route": _text(page.route, "page.route", maximum=2000),
                    "canonical_url": _text(page.canonical_url, "page.canonical_url", maximum=4000),
                    "source_path": _text(page.source_path, "page.source_path", maximum=2000),
                    "evidence": page.evidence,
                    "content_hash": _text(page.content_hash, "page.content_hash", maximum=128),
                }
                evidence_json = _bounded_json(payload["evidence"], "page evidence", MAX_EVIDENCE_BYTES)
                record_json = _json(payload)
                batch_bytes += len(record_json.encode("utf-8"))
                if batch_bytes > MAX_FACT_BATCH_BYTES:
                    raise ValueError(f"fact batch exceeds {MAX_FACT_BATCH_BYTES} serialized bytes")
                record_hash = hashlib.sha256(record_json.encode("utf-8")).hexdigest()
                record_id = _id("sgp", repository_snapshot_id, page.fact_key)
                db.execute(
                    """INSERT OR IGNORE INTO site_graph_page_facts
                       (id,repository_snapshot_id,fact_key,route,canonical_url,source_path,evidence_json,content_hash,record_hash,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (record_id, repository_snapshot_id, payload["fact_key"], payload["route"], payload["canonical_url"],
                     payload["source_path"], evidence_json, payload["content_hash"], record_hash, _now()),
                )
                _immutable(db, "site_graph_page_facts", record_id, record_hash)

            source_rows = db.execute(
                "SELECT id,fact_key FROM site_graph_page_facts WHERE repository_snapshot_id=?", (repository_snapshot_id,)
            ).fetchall()
            source_ids = {row["fact_key"]: row["id"] for row in source_rows}
            seen_links: set[str] = set()
            for link in links:
                if link.occurrence_key in seen_links:
                    raise ValueError(f"duplicate link occurrence in batch: {link.occurrence_key}")
                seen_links.add(link.occurrence_key)
                if link.source_fact_key not in source_ids:
                    raise ValueError(f"unknown source fact: {link.source_fact_key}")
                if link.layer not in LINK_LAYERS:
                    raise ValueError(f"unknown link layer: {link.layer}")
                if isinstance(link.confidence, bool) or not isinstance(link.confidence, (int, float)) or not 0 <= link.confidence <= 1:
                    raise ValueError("link confidence must be from 0 to 1")
                for field_name in ("repeated_template", "crawlable", "nofollow", "external", "fragment"):
                    _boolean(getattr(link, field_name), f"link.{field_name}")
                payload = {
                    "occurrence_key": _text(link.occurrence_key, "link.occurrence_key", maximum=500),
                    "source_fact_key": link.source_fact_key,
                    "raw_destination": _text(link.raw_destination, "link.raw_destination", allow_empty=True, maximum=4000),
                    "canonical_destination": _text(link.canonical_destination, "link.canonical_destination", allow_empty=True, maximum=4000),
                    "anchor_text": _text(link.anchor_text, "link.anchor_text", allow_empty=True, maximum=2000),
                    "context_excerpt": _text(link.context_excerpt, "link.context_excerpt", allow_empty=True, maximum=5000),
                    "source_location": _text(link.source_location, "link.source_location", maximum=2000),
                    "landmark": _text(link.landmark, "link.landmark", allow_empty=True, maximum=200),
                    "layer": link.layer,
                    "confidence": float(link.confidence),
                    "repeated_template": link.repeated_template,
                    "crawlable": link.crawlable,
                    "nofollow": link.nofollow,
                    "external": link.external,
                    "fragment": link.fragment,
                    "action_kind": None if link.action_kind is None else _text(link.action_kind, "link.action_kind", maximum=100),
                    "evidence": link.evidence,
                }
                evidence_json = _bounded_json(payload["evidence"], "link evidence", MAX_EVIDENCE_BYTES)
                record_json = _json(payload)
                batch_bytes += len(record_json.encode("utf-8"))
                if batch_bytes > MAX_FACT_BATCH_BYTES:
                    raise ValueError(f"fact batch exceeds {MAX_FACT_BATCH_BYTES} serialized bytes")
                record_hash = hashlib.sha256(record_json.encode("utf-8")).hexdigest()
                record_id = _id("sgl", repository_snapshot_id, link.occurrence_key)
                db.execute(
                    """INSERT OR IGNORE INTO site_graph_link_occurrences
                       (id,repository_snapshot_id,occurrence_key,source_page_fact_id,raw_destination,canonical_destination,
                        anchor_text,context_excerpt,source_location,landmark,layer,confidence,repeated_template,crawlable,
                        nofollow,external,fragment,action_kind,evidence_json,record_hash,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (record_id, repository_snapshot_id, payload["occurrence_key"], source_ids[link.source_fact_key],
                     payload["raw_destination"], payload["canonical_destination"], payload["anchor_text"],
                     payload["context_excerpt"], payload["source_location"], payload["landmark"], payload["layer"],
                     payload["confidence"], int(payload["repeated_template"]), int(payload["crawlable"]),
                     int(payload["nofollow"]), int(payload["external"]), int(payload["fragment"]), payload["action_kind"],
                     evidence_json, record_hash, _now()),
                )
                _immutable(db, "site_graph_link_occurrences", record_id, record_hash)

    def publish_evidence_batch(
        self,
        batch: EvidenceBatch,
        *,
        repository_snapshot_id: str,
        manifest_version_id: str,
        compiler_version: str,
        projection_name: str,
        goal_definition_hash: str,
    ) -> str:
        """Atomically publish one Core 2.1 batch through current fact-first tables."""

        with self.connect() as db:
            repository = db.execute(
                """SELECT r.site_key,r.revision,i.manifest_version_id
                     FROM site_graph_repository_snapshots r
                     JOIN site_graph_ingest_runs i ON i.id=r.ingest_run_id
                    WHERE r.id=?""",
                (repository_snapshot_id,),
            ).fetchone()
            if repository is None:
                raise ValueError("unknown repository snapshot")
            if repository["site_key"] != batch.site_key:
                raise ValueError("evidence batch site does not match repository snapshot")
            if repository["revision"] != batch.repository_revision:
                raise ValueError("evidence batch repository revision does not match repository snapshot")
            if repository["manifest_version_id"] != manifest_version_id:
                raise ValueError("evidence batch manifest does not match repository ingest")

            batch_id = batch.batch_id
            candidate_by_id = {candidate.candidate_id: candidate for candidate in batch.candidates}
            pages_by_candidate = {page.candidate_id: page for page in batch.pages}
            carrier_ids = sorted(pages_by_candidate)
            if not carrier_ids and (batch.candidates or batch.links):
                raise ValueError("persisted evidence requires at least one page entity carrier")
            carried_candidates: dict[str, list[dict[str, Any]]] = {
                candidate_id: [] for candidate_id in carrier_ids
            }
            unresolved_index = 0
            for candidate in sorted(batch.candidates, key=lambda item: item.candidate_id):
                if candidate.candidate_id in pages_by_candidate:
                    carrier_id = candidate.candidate_id
                else:
                    carrier_id = carrier_ids[unresolved_index % len(carrier_ids)]
                    unresolved_index += 1
                carried_candidates[carrier_id].append(candidate.normalized())

            page_facts: list[PageFact] = []
            for candidate_id in carrier_ids:
                candidate = candidate_by_id[candidate_id]
                page = pages_by_candidate[candidate_id]
                evidence: dict[str, Any] = {
                    "evidence_batch_id": batch_id,
                    "adapter": batch.adapter,
                    "adapter_version": batch.adapter_version,
                    "revision_relation": batch.revision_relation,
                    "candidate_evidence": carried_candidates[candidate_id],
                }
                if candidate_id == carrier_ids[0]:
                    evidence["repository_revision"] = batch.repository_revision
                    evidence["evidence_revision"] = batch.evidence_revision
                    evidence["coverage"] = {
                        "coverage_id": batch.coverage.coverage_id,
                        **batch.coverage.normalized(),
                    }
                    evidence["diagnostics"] = batch.normalized(include_id=False)["diagnostics"]
                # Validate the distributed schema-4 carrier before entering persistence.
                _bounded_json(evidence, "Core 2.1 page evidence", MAX_EVIDENCE_BYTES)
                page_facts.append(PageFact(
                    candidate.candidate_id,
                    candidate.canonical_route,
                    page.canonical_url,
                    candidate.source_path,
                    evidence,
                    candidate.content_hash,
                ))
            persisted_source_ids = {page.fact_key for page in page_facts}
            unknown_sources = sorted({
                link.source_candidate_id for link in batch.links
                if link.source_candidate_id not in persisted_source_ids
            })
            if unknown_sources:
                raise ValueError(f"link source has no persisted page entity: {unknown_sources[0]}")
            link_facts = [
                LinkOccurrence(
                    occurrence_key=link.occurrence_id,
                    source_fact_key=link.source_candidate_id,
                    raw_destination=link.raw_destination,
                    canonical_destination=link.canonical_destination,
                    anchor_text=link.anchor_text,
                    context_excerpt="",
                    source_location=link.source_location,
                    landmark=link.landmark,
                    layer=link.layer,
                    confidence=float(link.confidence),
                    evidence={
                        "accessible_name": link.accessible_name,
                        "evidence_batch_id": batch_id,
                        "provenance": dict(link.provenance),
                        "resolution_state": link.resolution_state,
                        "topology_eligible": link.topology_eligible,
                        "viewport": link.viewport,
                        "visible": link.visible,
                    },
                    crawlable=link.topology_eligible,
                    nofollow=link.nofollow,
                    external=link.resolution_state == "external",
                    fragment=link.resolution_state == "fragment",
                    action_kind="action" if link.resolution_state == "action" else None,
                )
                for link in batch.links
            ]
            self.save_fact_batch(
                repository_snapshot_id, pages=page_facts, links=link_facts, _connection=db
            )

            page_fact_rows = db.execute(
                "SELECT id,fact_key FROM site_graph_page_facts WHERE repository_snapshot_id=?",
                (repository_snapshot_id,),
            ).fetchall()
            page_fact_ids = {row["fact_key"]: row["id"] for row in page_fact_rows}
            for page in batch.pages:
                evidence = page.normalized()
                record_id = _id("sge", batch_id, page.page_id)
                page_fact_id = page_fact_ids[page.candidate_id]
                evidence_json = _bounded_json(evidence, "page entity evidence", MAX_EVIDENCE_BYTES)
                values = (
                    record_id, repository_snapshot_id, page_fact_id, "core21-page",
                    page.canonical_route, float(page.naming_confidence), evidence_json,
                )
                db.execute(
                    """INSERT OR IGNORE INTO site_graph_page_entities
                       (id,repository_snapshot_id,page_fact_id,entity_type,entity_value,confidence,evidence_json)
                       VALUES (?,?,?,?,?,?,?)""",
                    values,
                )
                row = db.execute(
                    """SELECT repository_snapshot_id,page_fact_id,entity_type,entity_value,confidence,evidence_json
                         FROM site_graph_page_entities WHERE id=?""",
                    (record_id,),
                ).fetchone()
                if row is None or tuple(row) != values[1:]:
                    raise ValueError("immutable site_graph_page_entities key collides with different content")

            graph_id = self.save_graph_snapshot(
                site_key=batch.site_key,
                repository_snapshot_id=repository_snapshot_id,
                manifest_version_id=manifest_version_id,
                compiler_version=compiler_version,
                projection_name=projection_name,
                goal_definition_hash=goal_definition_hash,
                content_hash=batch.content_hash,
                _connection=db,
                _identity_seed=batch_id,
            )
            return graph_id

    def save_graph_snapshot(
        self,
        *,
        site_key: str,
        repository_snapshot_id: str,
        manifest_version_id: str,
        compiler_version: str,
        projection_name: str,
        goal_definition_hash: str,
        content_hash: str,
        _connection: sqlite3.Connection | None = None,
        _identity_seed: str | None = None,
    ) -> str:
        values = {
            "site_key": _text(site_key, "site_key", maximum=100),
            "compiler_version": _text(compiler_version, "compiler_version", maximum=100),
            "projection_name": _text(projection_name, "projection_name", maximum=100),
            "goal_definition_hash": _text(goal_definition_hash, "goal_definition_hash", maximum=128),
            "content_hash": _text(content_hash, "content_hash", maximum=128),
        }
        record_id = _id(
            "sgg", _identity_seed or repository_snapshot_id, manifest_version_id, values["compiler_version"],
            values["projection_name"], values["goal_definition_hash"], values["content_hash"],
        )
        def persist(db: sqlite3.Connection) -> None:
            provenance = db.execute(
                """SELECT r.site_key,i.manifest_version_id
                   FROM site_graph_repository_snapshots r
                   JOIN site_graph_ingest_runs i ON i.id=r.ingest_run_id
                   WHERE r.id=?""", (repository_snapshot_id,)
            ).fetchone()
            if provenance is None:
                raise ValueError("unknown repository snapshot")
            if provenance["site_key"] != site_key or provenance["manifest_version_id"] != manifest_version_id:
                raise ValueError("graph snapshot provenance does not match repository ingest")
            db.execute(
                """INSERT OR IGNORE INTO site_graph_snapshots
                   (id,site_key,repository_snapshot_id,manifest_version_id,compiler_version,projection_name,
                    goal_definition_hash,content_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (record_id, values["site_key"], repository_snapshot_id, manifest_version_id, values["compiler_version"],
                 values["projection_name"], values["goal_definition_hash"], values["content_hash"], _now()),
            )
            row = db.execute(
                """SELECT site_key,repository_snapshot_id,manifest_version_id,compiler_version,
                          projection_name,goal_definition_hash,content_hash
                     FROM site_graph_snapshots WHERE id=?""",
                (record_id,),
            ).fetchone()
            expected = (
                values["site_key"], repository_snapshot_id, manifest_version_id,
                values["compiler_version"], values["projection_name"],
                values["goal_definition_hash"], values["content_hash"],
            )
            if row is None or tuple(row) != expected:
                raise ValueError("immutable graph snapshot key collides with different provenance")
        if _connection is None:
            with self.connect() as db:
                persist(db)
        else:
            persist(_connection)
        return record_id

    def save_finding(
        self,
        *,
        graph_snapshot_id: str,
        finding_key: str,
        finding_type: str,
        severity: str,
        algorithm: str,
        parameters: dict[str, Any],
        affected_nodes: list[str],
        affected_edges: list[str],
        source_fact_keys: list[str],
        content_hash: str,
        _connection: sqlite3.Connection | None = None,
    ) -> str:
        if severity not in {"info", "warning", "critical"}:
            raise ValueError("finding severity must be info, warning, or critical")
        if not isinstance(affected_nodes, list) or not all(isinstance(item, str) and item for item in affected_nodes):
            raise ValueError("affected_nodes must be a list of non-empty strings")
        if not isinstance(affected_edges, list) or not all(isinstance(item, str) and item for item in affected_edges):
            raise ValueError("affected_edges must be a list of non-empty strings")
        if not isinstance(source_fact_keys, list) or not source_fact_keys or not all(isinstance(item, str) and item for item in source_fact_keys):
            raise ValueError("source_fact_keys must contain at least one non-empty source fact key")
        if any(len(items) != len(set(items)) for items in (affected_nodes, affected_edges, source_fact_keys)):
            raise ValueError("finding evidence lists must not contain duplicates")
        if len(affected_nodes) > 100_000 or len(affected_edges) > 1_000_000 or len(source_fact_keys) > 100_000:
            raise ValueError("finding evidence exceeds bounded limits")
        payload = {
            "finding_key": _text(finding_key, "finding_key", maximum=500),
            "finding_type": _text(finding_type, "finding_type", maximum=200),
            "severity": severity,
            "algorithm": _text(algorithm, "algorithm", maximum=200),
            "parameters": parameters,
            "affected_nodes": affected_nodes,
            "affected_edges": affected_edges,
            "source_fact_keys": source_fact_keys,
            "content_hash": _text(content_hash, "content_hash", maximum=128),
        }
        parameters_json = _bounded_json(parameters, "finding parameters", MAX_EVIDENCE_BYTES)
        affected_nodes_json = _bounded_json(affected_nodes, "finding affected_nodes", MAX_FINDING_BYTES)
        affected_edges_json = _bounded_json(affected_edges, "finding affected_edges", MAX_FINDING_BYTES)
        source_fact_keys_json = _bounded_json(source_fact_keys, "finding source_fact_keys", MAX_FINDING_BYTES)
        finding_bytes = sum(len(value.encode("utf-8")) for value in (
            parameters_json, affected_nodes_json, affected_edges_json, source_fact_keys_json,
        ))
        if finding_bytes > MAX_FINDING_BYTES:
            raise ValueError(f"finding evidence exceeds {MAX_FINDING_BYTES} serialized bytes")
        record_hash = _hash(payload)
        record_id = _id("sgf", graph_snapshot_id, finding_key)

        def persist(db: sqlite3.Connection) -> None:
            graph = db.execute(
                "SELECT repository_snapshot_id FROM site_graph_snapshots WHERE id=?", (graph_snapshot_id,)
            ).fetchone()
            if graph is None:
                raise ValueError("unknown graph snapshot")
            repository_snapshot_id = graph["repository_snapshot_id"]
            page_rows = db.execute(
                "SELECT fact_key,route FROM site_graph_page_facts WHERE repository_snapshot_id=?",
                (repository_snapshot_id,),
            ).fetchall()
            fact_keys = {row["fact_key"] for row in page_rows}
            node_keys = fact_keys | {row["route"] for row in page_rows}
            unknown_facts = sorted(set(source_fact_keys) - fact_keys)
            if unknown_facts:
                raise ValueError(f"unknown source fact for graph snapshot: {unknown_facts[0]}")
            unknown_nodes = sorted(set(affected_nodes) - node_keys)
            if unknown_nodes:
                raise ValueError(f"unknown affected node for graph snapshot: {unknown_nodes[0]}")
            if affected_edges:
                occurrence_rows = db.execute(
                    "SELECT id,occurrence_key FROM site_graph_link_occurrences WHERE repository_snapshot_id=?",
                    (repository_snapshot_id,),
                ).fetchall()
                aggregate_rows = db.execute(
                    "SELECT id FROM site_graph_edge_aggregates WHERE repository_snapshot_id=?",
                    (repository_snapshot_id,),
                ).fetchall()
                edge_keys = {value for row in occurrence_rows for value in (row["id"], row["occurrence_key"])}
                edge_keys.update(row["id"] for row in aggregate_rows)
                unknown_edges = sorted(set(affected_edges) - edge_keys)
                if unknown_edges:
                    raise ValueError(f"unknown affected edge for graph snapshot: {unknown_edges[0]}")
            db.execute(
                """INSERT OR IGNORE INTO site_graph_findings
                   (id,graph_snapshot_id,finding_key,finding_type,severity,algorithm,parameters_json,
                    affected_nodes_json,affected_edges_json,source_fact_keys_json,content_hash,record_hash,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record_id, graph_snapshot_id, payload["finding_key"], payload["finding_type"], severity,
                 payload["algorithm"], parameters_json, affected_nodes_json, affected_edges_json,
                 source_fact_keys_json, payload["content_hash"], record_hash, _now()),
            )
            _immutable(db, "site_graph_findings", record_id, record_hash)
        if _connection is None:
            with self.connect() as db:
                persist(db)
        else:
            persist(_connection)
        return record_id
