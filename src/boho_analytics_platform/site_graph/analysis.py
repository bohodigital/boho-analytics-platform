"""Deterministic, dependency-free compilation of immutable site-graph facts."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from .contracts import EvidenceBatch, LinkOccurrence
from .models import LINK_LAYERS, stable_hash
from .storage import SiteGraphStore, _id, _json


COMPILER_VERSION = "site-graph-core21-reconciliation-2.1.0"
PROJECTION_LAYERS = {
    "full": frozenset({"menu", "breadcrumb", "contextual", "related", "action", "utility"}),
    "navigation": frozenset({"menu", "breadcrumb", "utility"}),
    "contextual": frozenset({"contextual", "related", "action"}),
    "action": frozenset({"action"}),
    "goal": frozenset({"contextual", "related", "action"}),
    "authority": frozenset({"contextual", "related"}),
}


@dataclass(frozen=True, slots=True)
class StructuralFinding:
    finding_type: str
    route: str
    evidence_occurrence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StructuralSummary:
    selected_layers: tuple[str, ...]
    pages: int
    full_relationships: int
    selected_relationships: int
    true_orphans: tuple[str, ...]
    contextual_orphans: tuple[str, ...]
    contextual_dead_ends: tuple[str, ...]
    menu_dependent: tuple[str, ...]
    homepage_dependent: tuple[str, ...]
    global_shell_dependent: tuple[str, ...]
    full_goal_reachable: tuple[str, ...]
    selected_goal_reachable: tuple[str, ...]
    findings: tuple[StructuralFinding, ...]
    content_hash: str


def _reachable(start: str, adjacency: Mapping[str, set[str]]) -> set[str]:
    if start not in adjacency:
        return set()
    seen = {start}
    pending = deque([start])
    while pending:
        node = pending.popleft()
        for destination in sorted(adjacency[node]):
            if destination not in seen:
                seen.add(destination)
                pending.append(destination)
    return seen


def analyze_structural_semantics(
    batch: EvidenceBatch,
    *,
    selected_layers: Iterable[str] = ("contextual", "related"),
    goal_routes: Iterable[str] = (),
) -> StructuralSummary:
    """Calculate only deterministic, evidence-backed structural semantics."""

    layers = tuple(sorted(set(selected_layers)))
    if not layers or any(layer not in LINK_LAYERS for layer in layers):
        raise ValueError("selected layers must be a non-empty subset of known link layers")
    pages = {page.canonical_route: page for page in batch.pages}
    pages_by_candidate = {page.candidate_id: page for page in batch.pages}
    full = {route: set() for route in pages}
    selected = {route: set() for route in pages}
    without_menu = {route: set() for route in pages}
    incoming_full: dict[str, list[LinkOccurrence]] = {route: [] for route in pages}
    incoming_selected: dict[str, list[LinkOccurrence]] = {route: [] for route in pages}
    for link in batch.links:
        source_page = pages_by_candidate.get(link.source_candidate_id)
        destination = link.canonical_destination
        if source_page is None or not link.topology_eligible or destination not in pages:
            continue
        source = source_page.canonical_route
        full[source].add(destination)
        incoming_full[destination].append(link)
        if link.layer != "menu":
            without_menu[source].add(destination)
        if link.layer in layers:
            selected[source].add(destination)
            incoming_selected[destination].append(link)

    full_reachable = _reachable("/", full)
    without_menu_reachable = _reachable("/", without_menu)
    goals = {route for route in goal_routes if route in pages}
    reverse_full = {route: set() for route in pages}
    reverse_selected = {route: set() for route in pages}
    for source, destinations in full.items():
        for destination in destinations:
            reverse_full[destination].add(source)
    for source, destinations in selected.items():
        for destination in destinations:
            reverse_selected[destination].add(source)
    full_goal = (
        set().union(*(_reachable(goal, reverse_full) for goal in goals)) if goals else set()
    )
    selected_goal = (
        set().union(*(_reachable(goal, reverse_selected) for goal in goals))
        if goals
        else set()
    )
    true_orphans = tuple(
        route for route in sorted(pages) if route != "/" and not incoming_full[route]
    )
    contextual_orphans = tuple(
        route
        for route in sorted(pages)
        if route != "/" and incoming_full[route] and not incoming_selected[route]
    )
    dead_ends = tuple(
        route for route in sorted(pages) if route not in goals and not selected[route]
    )
    menu_dependent = tuple(sorted(full_reachable - without_menu_reachable))
    homepage_dependent = (
        tuple(
            route
            for route in sorted(pages)
            if route != "/"
            and incoming_full[route]
            and {link.source_candidate_id for link in incoming_full[route]}
            == {pages["/"].candidate_id}
        )
        if "/" in pages
        else ()
    )
    global_shell_dependent = tuple(
        route
        for route in sorted(pages)
        if route != "/"
        and incoming_full[route]
        and {link.layer for link in incoming_full[route]}.issubset({"menu", "utility"})
    )
    findings: list[StructuralFinding] = []
    for finding_type, routes, evidence in (
        ("true_orphan", true_orphans, incoming_full),
        ("contextual_orphan", contextual_orphans, incoming_full),
        ("contextual_dead_end", dead_ends, incoming_selected),
        ("menu_dependence", menu_dependent, incoming_full),
        ("homepage_dependence", homepage_dependent, incoming_full),
        ("global_shell_dependence", global_shell_dependent, incoming_full),
    ):
        for route in routes:
            findings.append(
                StructuralFinding(
                    finding_type,
                    route,
                    tuple(sorted(link.occurrence_id for link in evidence[route])),
                )
            )
    body = {
        "batch_id": batch.batch_id,
        "selected_layers": layers,
        "full": {key: sorted(value) for key, value in sorted(full.items())},
        "selected": {key: sorted(value) for key, value in sorted(selected.items())},
        "findings": [asdict(item) for item in findings],
        "full_goal_reachable": sorted(full_goal),
        "selected_goal_reachable": sorted(selected_goal),
    }
    return StructuralSummary(
        layers,
        len(pages),
        sum(len(value) for value in full.values()),
        sum(len(value) for value in selected.values()),
        true_orphans,
        contextual_orphans,
        dead_ends,
        menu_dependent,
        homepage_dependent,
        global_shell_dependent,
        tuple(sorted(full_goal)),
        tuple(sorted(selected_goal)),
        tuple(findings),
        stable_hash(body),
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _strongly_connected(nodes: list[str], adjacency: dict[str, set[str]]) -> list[list[str]]:
    ordered_nodes = sorted(nodes)
    visited: set[str] = set()
    finish_order: list[str] = []
    for start in ordered_nodes:
        if start in visited:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                finish_order.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            stack.append((node, True))
            for destination in reversed(sorted(adjacency[node])):
                if destination not in visited:
                    stack.append((destination, False))

    reverse = {node: set() for node in ordered_nodes}
    for source, destinations in adjacency.items():
        for destination in destinations:
            reverse[destination].add(source)
    components: list[list[str]] = []
    assigned: set[str] = set()
    for start in reversed(finish_order):
        if start in assigned:
            continue
        component: list[str] = []
        stack = [(start, False)]
        while stack:
            node, _expanded = stack.pop()
            if node in assigned:
                continue
            assigned.add(node)
            component.append(node)
            for source in reversed(sorted(reverse[node])):
                if source not in assigned:
                    stack.append((source, False))
        components.append(sorted(component))
    for node in nodes:
        if node not in assigned:
            components.append([node])
    return sorted(components, key=lambda item: (item[0], len(item), item))


def _goal_routes(manifest: dict[str, Any], routes: list[str]) -> set[str]:
    result: set[str] = set()
    page_rules = manifest.get("page_rules", [])
    roles_by_route: dict[str, set[str]] = {route: set() for route in routes}
    for rule in page_rules:
        pattern = re.compile(rule["path_regex"])
        for route in routes:
            if pattern.search(route):
                roles_by_route[route].update(rule["roles"])
    for goal in manifest.get("goals", []):
        result.update(route for route in goal.get("paths", []) if route in roles_by_route)
        goal_roles = set(goal.get("roles", []))
        result.update(route for route, roles in roles_by_route.items() if goal_roles & roles)
    return result


def _goal_distances(nodes: list[str], adjacency: dict[str, set[str]], goals: set[str]) -> dict[str, int]:
    reverse: dict[str, set[str]] = {node: set() for node in nodes}
    for source, destinations in adjacency.items():
        for destination in destinations:
            reverse[destination].add(source)
    distances = {node: 0 for node in goals}
    queue = deque(sorted(goals))
    while queue:
        destination = queue.popleft()
        for source in sorted(reverse[destination]):
            if source not in distances:
                distances[source] = distances[destination] + 1
                queue.append(source)
    return {node: distances.get(node, -1) for node in nodes}


def _authority(nodes: list[str], adjacency: dict[str, set[str]]) -> dict[str, float]:
    if not nodes:
        return {}
    score = {node: 1.0 / len(nodes) for node in nodes}
    for _ in range(30):
        next_score = {node: 0.15 / len(nodes) for node in nodes}
        for source in nodes:
            destinations = adjacency[source]
            if destinations:
                share = 0.85 * score[source] / len(destinations)
                for destination in destinations:
                    next_score[destination] += share
            else:
                share = 0.85 * score[source] / len(nodes)
                for destination in nodes:
                    next_score[destination] += share
        score = next_score
    return score


def _bucket_distances(distances: dict[str, int]) -> dict[str, int]:
    buckets = {"goal": 0, "1": 0, "2": 0, "3": 0, "4+": 0, "menu-only": 0, "unreachable": 0}
    for distance in distances.values():
        key = "unreachable" if distance < 0 else "goal" if distance == 0 else str(distance) if distance <= 3 else "4+"
        buckets[key] += 1
    return buckets


def _maybe_interrupt(boundary: str, requested: str | None) -> None:
    if requested == boundary:
        raise RuntimeError(f"interrupted after {boundary}")


def compile_graph(
    store: SiteGraphStore,
    *,
    site_key: str,
    projection: str = "contextual",
    _interrupt_after: str | None = None,
) -> dict[str, Any]:
    """Compile the latest successful repository snapshot into deterministic graph artifacts."""
    if projection not in PROJECTION_LAYERS:
        raise ValueError(f"unknown graph projection: {projection}")
    with store.connect(readonly=True) as db:
        repository = db.execute(
            """SELECT r.*,i.manifest_version_id,m.canonical_json,m.manifest_hash
               FROM site_graph_repository_snapshots r
               JOIN site_graph_ingest_runs i ON i.id=r.ingest_run_id AND i.status='succeeded'
               JOIN site_graph_manifest_versions m ON m.id=i.manifest_version_id
               WHERE r.site_key=? ORDER BY r.captured_at DESC,r.id DESC LIMIT 1""",
            (site_key,),
        ).fetchone()
        if repository is None:
            raise ValueError(f"no successful site graph ingest exists for site: {site_key}")
        page_rows = db.execute(
            "SELECT id,fact_key,route,canonical_url FROM site_graph_page_facts WHERE repository_snapshot_id=? ORDER BY route,id",
            (repository["id"],),
        ).fetchall()
        link_rows = db.execute(
            """SELECT l.*,p.route AS source_route
               FROM site_graph_link_occurrences l
               JOIN site_graph_page_facts p ON p.id=l.source_page_fact_id
               WHERE l.repository_snapshot_id=? ORDER BY l.occurrence_key,l.id""",
            (repository["id"],),
        ).fetchall()

    manifest = json.loads(repository["canonical_json"])
    route_to_page = {row["route"]: row for row in page_rows}
    page_id_to_route = {row["id"]: row["route"] for row in page_rows}
    canonical_to_route = {row["canonical_url"]: row["route"] for row in page_rows}
    nodes = sorted(route_to_page)
    selected_layers = PROJECTION_LAYERS[projection]
    aggregates: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    full_adjacency = {node: set() for node in nodes}
    adjacency = {node: set() for node in nodes}
    without_menu_adjacency = {node: set() for node in nodes}
    incoming_sources: dict[str, set[str]] = {node: set() for node in nodes}
    incoming_layers: dict[str, set[str]] = {node: set() for node in nodes}
    for row in link_rows:
        if not row["crawlable"] or row["external"]:
            continue
        destination = canonical_to_route.get(row["canonical_destination"], row["canonical_destination"])
        if destination not in route_to_page:
            continue
        full_adjacency[row["source_route"]].add(destination)
        incoming_sources[destination].add(row["source_route"])
        incoming_layers[destination].add(row["layer"])
        if row["layer"] != "menu":
            without_menu_adjacency[row["source_route"]].add(destination)
        if row["layer"] in selected_layers:
            adjacency[row["source_route"]].add(destination)
            aggregates[(row["source_page_fact_id"], destination, row["layer"])].append(row)

    goals = _goal_routes(manifest, nodes)
    distances = _goal_distances(nodes, adjacency, goals)
    in_degree = {node: 0 for node in nodes}
    full_in_degree = {node: 0 for node in nodes}
    for destinations in adjacency.values():
        for destination in destinations:
            in_degree[destination] += 1
    for destinations in full_adjacency.values():
        for destination in destinations:
            full_in_degree[destination] += 1
    full_reachable = _reachable("/", full_adjacency)
    without_menu_reachable = _reachable("/", without_menu_adjacency)
    menu_dependent = full_reachable - without_menu_reachable
    authority = _authority(nodes, adjacency)
    components = _strongly_connected(nodes, adjacency)
    content_hash = _sha({
        "repository": repository["content_hash"],
        "manifest": repository["manifest_hash"],
        "projection": projection,
        "edges": [(key, [row["occurrence_key"] for row in value]) for key, value in sorted(aggregates.items())],
    })
    goal_hash = _sha(manifest.get("goals", []))
    findings: dict[str, int] = defaultdict(int)
    finding_rows: list[dict[str, Any]] = []
    for route in nodes:
        route_findings: list[str] = []
        if route != "/" and full_in_degree[route] == 0:
            route_findings.append("true_orphan")
        elif route != "/" and in_degree[route] == 0:
            route_findings.append("contextual_orphan")
        if route not in goals and not adjacency[route]:
            route_findings.append("contextual_dead_end")
        if route in menu_dependent:
            route_findings.append("menu_dependence")
        if route != "/" and incoming_sources[route] == {"/"}:
            route_findings.append("homepage_dependence")
        if (
            route != "/"
            and incoming_layers[route]
            and incoming_layers[route].issubset({"menu", "utility"})
        ):
            route_findings.append("global_shell_dependence")
        for finding_type in route_findings:
            finding_rows.append({
                "finding_key": f"{finding_type}:{route}",
                "finding_type": finding_type,
                "severity": "warning",
                "algorithm": COMPILER_VERSION,
                "parameters": {
                    "projection": projection,
                    "layers": sorted(selected_layers),
                    "full_graph_basis": True,
                },
                "affected_nodes": [route],
                "affected_edges": [],
                "source_fact_keys": [route_to_page[route]["fact_key"]],
                "content_hash": _sha([finding_type, route, content_hash]),
            })
            findings[finding_type] += 1
    if findings.get("true_orphan"):
        findings["orphan"] = findings["true_orphan"]

    aggregate_ids: dict[tuple[str, str, str], str] = {}
    with store.connect() as db:
        graph_id = store.save_graph_snapshot(
            site_key=site_key,
            repository_snapshot_id=repository["id"],
            manifest_version_id=repository["manifest_version_id"],
            compiler_version=COMPILER_VERSION,
            projection_name=projection,
            goal_definition_hash=goal_hash,
            content_hash=content_hash,
            _connection=db,
        )
        _maybe_interrupt("snapshot", _interrupt_after)
        for (source_page_id, destination, layer), occurrences in sorted(aggregates.items()):
            aggregate_id = _id("sge", repository["id"], source_page_id, destination, layer)
            aggregate_ids[(source_page_id, destination, layer)] = aggregate_id
            db.execute(
                """INSERT OR IGNORE INTO site_graph_edge_aggregates
                   (id,repository_snapshot_id,source_page_fact_id,canonical_destination,layer,occurrence_count,aggregate_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (aggregate_id, repository["id"], source_page_id, destination, layer, len(occurrences), _json({
                    "occurrence_ids": [row["id"] for row in occurrences],
                    "occurrence_keys": [row["occurrence_key"] for row in occurrences],
                })),
            )
        _maybe_interrupt("aggregates", _interrupt_after)
        for route, page in sorted(route_to_page.items()):
            metrics = {
                "in_degree": float(in_degree[route]),
                "out_degree": float(len(adjacency[route])),
                "goal_distance": float(distances[route]),
                "internal_authority": float(authority[route]),
                "menu_dependence": float(route in menu_dependent),
            }
            for metric_name, metric_value in metrics.items():
                metric_id = _id("sgn", graph_id, page["id"], metric_name, COMPILER_VERSION)
                db.execute(
                    """INSERT OR IGNORE INTO site_graph_node_metrics
                       (id,graph_snapshot_id,page_fact_id,metric_name,metric_value,algorithm,parameters_json)
                       VALUES (?,?,?,?,?,?,?)""",
                    (metric_id, graph_id, page["id"], metric_name, metric_value, COMPILER_VERSION,
                     _json({"projection": projection, "layers": sorted(selected_layers)})),
                )
        _maybe_interrupt("metrics", _interrupt_after)
        for position, component in enumerate(components):
            edge_ids = sorted(
                aggregate_id for (source_page_id, destination, _layer), aggregate_id in aggregate_ids.items()
                if page_id_to_route[source_page_id] in component and destination in component
            )
            component_key = f"scc-{position:05d}-{_sha(component)[:10]}"
            db.execute(
                """INSERT OR IGNORE INTO site_graph_components
                   (id,graph_snapshot_id,component_key,component_type,node_ids_json,edge_ids_json,algorithm,parameters_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (_id("sgc", graph_id, component_key), graph_id, component_key, "strongly_connected",
                 _json(component), _json(edge_ids), "kosaraju-iterative", _json({"projection": projection})),
            )
        _maybe_interrupt("components", _interrupt_after)
        for finding in finding_rows:
            store.save_finding(
                graph_snapshot_id=graph_id,
                finding_key=finding["finding_key"],
                finding_type=finding["finding_type"],
                severity=finding["severity"],
                algorithm=finding["algorithm"],
                parameters=finding["parameters"],
                affected_nodes=finding["affected_nodes"],
                affected_edges=finding["affected_edges"],
                source_fact_keys=finding["source_fact_keys"],
                content_hash=finding["content_hash"],
                _connection=db,
            )
        _maybe_interrupt("findings", _interrupt_after)

    return {
        "schema_version": 1,
        "graph_snapshot_id": graph_id,
        "site_key": site_key,
        "projection": projection,
        "nodes": len(nodes),
        "edges": len(aggregates),
        "components": len(components),
        "goals": len(goals),
        "goal_distance_buckets": _bucket_distances(distances),
        "findings": dict(sorted(findings.items())),
        "content_hash": content_hash,
    }
