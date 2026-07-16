"""Bounded, sanitized read models for the loopback site-graph dashboard."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any

from .analysis import PROJECTION_LAYERS
from .storage import SiteGraphStore


MAX_VISUAL_NODES = 36
MAX_VISUAL_EDGES = 60
MAX_CANDIDATE_EDGES = 5_000


def _distance_buckets(metrics: dict[str, dict[str, float]]) -> dict[str, int]:
    result = {"goal": 0, "1": 0, "2": 0, "3": 0, "4+": 0, "menu-only": 0, "unreachable": 0}
    for values in metrics.values():
        distance = int(values.get("goal_distance", -1))
        if distance < 0:
            key = "menu-only" if values.get("menu_dependence", 0) else "unreachable"
        elif distance == 0:
            key = "goal"
        elif distance <= 3:
            key = str(distance)
        else:
            key = "4+"
        result[key] += 1
    return result


class SiteGraphReportService:
    def __init__(self, store: SiteGraphStore) -> None:
        self.store = store

    def sites(self) -> list[dict[str, Any]]:
        with self.store.connect(readonly=True) as db:
            rows = db.execute(
                """SELECT g.site_key,COUNT(*) AS snapshots,MAX(g.created_at) AS latest_at
                   FROM site_graph_snapshots g GROUP BY g.site_key ORDER BY g.site_key"""
            ).fetchall()
        return [{"key": row["site_key"], "snapshots": row["snapshots"], "latest_at": row["latest_at"]} for row in rows]

    def summary(
        self,
        *,
        site_key: str | None = None,
        selected_page: str | None = None,
        layers: tuple[str, ...] = ("contextual", "related", "action"),
    ) -> dict[str, Any]:
        if not layers or len(layers) != len(set(layers)) or any(layer not in PROJECTION_LAYERS["full"] for layer in layers):
            raise ValueError("invalid site graph layer selection")
        with self.store.connect(readonly=True) as db:
            if site_key is None:
                selected = db.execute(
                    "SELECT site_key FROM site_graph_snapshots ORDER BY created_at DESC,id DESC LIMIT 1"
                ).fetchone()
                site_key = selected["site_key"] if selected else None
            graph = db.execute(
                """SELECT g.*,r.revision,r.captured_at,r.clean,m.manifest_hash,m.canonical_json
                   FROM site_graph_snapshots g
                   JOIN site_graph_repository_snapshots r ON r.id=g.repository_snapshot_id
                   JOIN site_graph_manifest_versions m ON m.id=g.manifest_version_id
                   WHERE g.site_key=? AND g.projection_name='contextual'
                   ORDER BY g.created_at DESC,g.id DESC LIMIT 1""",
                (site_key,),
            ).fetchone() if site_key else None
            if graph is None:
                return {
                    "schema_version": 1,
                    "projection": "contextual",
                    "sites": self.sites(),
                    "site": None,
                    "empty": True,
                    "notice": "No compiled site graph snapshot is available.",
                }
            manifest = json.loads(graph["canonical_json"])
            pages = db.execute(
                "SELECT id,fact_key,route,canonical_url FROM site_graph_page_facts WHERE repository_snapshot_id=? ORDER BY route,id",
                (graph["repository_snapshot_id"],),
            ).fetchall()
            placeholders = ",".join("?" for _ in layers)
            links = db.execute(
                f"""SELECT l.id,l.occurrence_key,l.canonical_destination,l.layer,l.anchor_text,l.confidence,
                           l.crawlable,l.external,p.route AS source_route
                    FROM site_graph_link_occurrences l
                    JOIN site_graph_page_facts p ON p.id=l.source_page_fact_id
                    WHERE l.repository_snapshot_id=? AND l.layer IN ({placeholders})
                      AND l.crawlable=1 AND l.external=0
                    ORDER BY l.occurrence_key,l.id LIMIT ?""",
                (graph["repository_snapshot_id"], *layers, MAX_CANDIDATE_EDGES),
            ).fetchall()
            layer_count_rows = db.execute(
                """SELECT layer,COUNT(*) AS occurrences FROM site_graph_link_occurrences
                   WHERE repository_snapshot_id=? GROUP BY layer ORDER BY layer""",
                (graph["repository_snapshot_id"],),
            ).fetchall()
            link_occurrence_count = db.execute(
                "SELECT COUNT(*) FROM site_graph_link_occurrences WHERE repository_snapshot_id=?",
                (graph["repository_snapshot_id"],),
            ).fetchone()[0]
            projection_occurrence_count = db.execute(
                f"""SELECT COUNT(*) FROM site_graph_link_occurrences
                    WHERE repository_snapshot_id=? AND layer IN ({placeholders})
                      AND crawlable=1 AND external=0""",
                (graph["repository_snapshot_id"], *layers),
            ).fetchone()[0]
            metric_rows = db.execute(
                """SELECT p.route,n.metric_name,n.metric_value
                   FROM site_graph_node_metrics n JOIN site_graph_page_facts p ON p.id=n.page_fact_id
                   WHERE n.graph_snapshot_id=? ORDER BY p.route,n.metric_name""",
                (graph["id"],),
            ).fetchall()
            component_rows = db.execute(
                """SELECT component_key,node_ids_json,edge_ids_json FROM site_graph_components
                   WHERE graph_snapshot_id=? AND component_type='strongly_connected' ORDER BY component_key LIMIT 100""",
                (graph["id"],),
            ).fetchall()
            finding_rows = db.execute(
                """SELECT finding_type,severity,affected_nodes_json FROM site_graph_findings
                   WHERE graph_snapshot_id=? ORDER BY severity,finding_type,finding_key LIMIT 500""",
                (graph["id"],),
            ).fetchall()
            snapshot_count = db.execute(
                "SELECT COUNT(*) FROM site_graph_snapshots WHERE site_key=? AND projection_name='contextual'",
                (site_key,),
            ).fetchone()[0]

        canonical_to_route = {row["canonical_url"]: row["route"] for row in pages}
        routes = {row["route"] for row in pages}
        metrics: dict[str, dict[str, float]] = defaultdict(dict)
        for row in metric_rows:
            metrics[row["route"]][row["metric_name"]] = row["metric_value"]
        layer_counts: dict[str, int] = {layer: 0 for layer in sorted(PROJECTION_LAYERS["full"])}
        layer_counts.update({row["layer"]: row["occurrences"] for row in layer_count_rows})
        edges: list[dict[str, Any]] = []
        adjacency: dict[str, set[str]] = {route: set() for route in routes}
        reverse: dict[str, set[str]] = {route: set() for route in routes}
        for row in links:
            destination = canonical_to_route.get(row["canonical_destination"], row["canonical_destination"])
            if row["layer"] not in layers or not row["crawlable"] or row["external"] or destination not in routes:
                continue
            edge = {
                "id": row["occurrence_key"],
                "source": row["source_route"],
                "destination": destination,
                "layer": row["layer"],
                "anchor": row["anchor_text"],
                "confidence": row["confidence"],
            }
            edges.append(edge)
            adjacency[edge["source"]].add(destination)
            reverse[destination].add(edge["source"])
        edges.sort(key=lambda item: (item["source"], item["destination"], item["layer"], item["id"]))
        if selected_page is not None and selected_page not in routes:
            raise ValueError("unknown selected site graph page")
        neighborhood_routes = set(routes)
        if selected_page:
            neighborhood_routes = {selected_page}
            frontier = {selected_page}
            for _ in range(2):
                following: set[str] = set()
                for route in frontier:
                    following.update(adjacency[route])
                    following.update(reverse[route])
                following -= neighborhood_routes
                neighborhood_routes.update(following)
                frontier = following
        ranked_routes = sorted(
            neighborhood_routes,
            key=lambda route: (
                0 if route == selected_page else 1,
                -metrics[route].get("internal_authority", 0),
                route,
            ),
        )[:MAX_VISUAL_NODES]
        visual_routes = set(ranked_routes)
        visual_edges = [edge for edge in edges if edge["source"] in visual_routes and edge["destination"] in visual_routes][:MAX_VISUAL_EDGES]
        visual_nodes = [{
            "route": route,
            "goal_distance": int(metrics[route].get("goal_distance", -1)),
            "authority": round(metrics[route].get("internal_authority", 0), 8),
            "selected": route == selected_page,
        } for route in ranked_routes]
        findings: dict[str, int] = defaultdict(int)
        finding_details: list[dict[str, Any]] = []
        for row in finding_rows:
            findings[row["finding_type"]] += 1
            finding_details.append({
                "type": row["finding_type"],
                "severity": row["severity"],
                "nodes": json.loads(row["affected_nodes_json"]),
            })
        components = [{
            "key": row["component_key"],
            "nodes": json.loads(row["node_ids_json"]),
            "internal_edges": len(json.loads(row["edge_ids_json"])),
        } for row in component_rows]
        menu_dependent = sum(1 for values in metrics.values() if values.get("menu_dependence", 0))
        return {
            "schema_version": 1,
            "projection": "contextual",
            "selected_layers": list(layers),
            "sites": self.sites(),
            "site": {"key": site_key, "display_name": manifest["site"]["display_name"]},
            "revision": graph["revision"],
            "manifest_hash": graph["manifest_hash"],
            "snapshot": {"id": graph["id"], "captured_at": graph["captured_at"], "clean": bool(graph["clean"]), "count": snapshot_count},
            "coverage": {"pages": len(pages), "link_occurrences": link_occurrence_count},
            "overview": {
                "projection_edges": projection_occurrence_count,
                "components": len(components),
                "menu_dependent_pages": menu_dependent,
                "contextual_dead_ends": findings.get("contextual_dead_end", 0),
                "orphans": findings.get("orphan", 0),
                "traps": findings.get("trap", 0),
                "bottlenecks": findings.get("bottleneck", 0),
            },
            "layer_counts": layer_counts,
            "goal_distance_buckets": _distance_buckets(metrics),
            "findings": dict(sorted(findings.items())),
            "finding_details": finding_details,
            "components": components,
            "neighborhood": {
                "selected_page": selected_page,
                "radius": 2 if selected_page else None,
                "routes": ranked_routes,
            },
            "visualization": {
                "bounded": True,
                "node_limit": MAX_VISUAL_NODES,
                "edge_limit": MAX_VISUAL_EDGES,
                "candidate_edge_limit": MAX_CANDIDATE_EDGES,
                "candidate_edges_truncated": projection_occurrence_count > len(links),
                "nodes": visual_nodes,
                "edges": visual_edges,
            },
            "structural_evidence_notice": "Structural evidence only. Link topology does not imply visitor behavior or conversion performance.",
            "empty": False,
        }
