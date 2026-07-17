"""Honest, read-only display models for the site-graph dashboard.

The compiler remains the authority for graph analysis.  This module only
aggregates already persisted occurrence facts so every bounded visualization
can disclose what it includes, what it omits, and where to inspect the complete
selected projection.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections.abc import Iterator, Sequence
from typing import Any

from .analysis import PROJECTION_LAYERS
from .dashboard import MAX_VISUAL_EDGES, MAX_VISUAL_NODES, SiteGraphReportService


EDGE_TABLE_PAGE_SIZE = 1_000
CSV_PAGE_SIZE = 500
SNAPSHOT_DIFF_EDGE_LIMIT = 5_000
SNAPSHOT_DIFF_SAMPLE_LIMIT = 12
_EDGE_SORTS = {
    "source": "source_route",
    "destination": "destination_route",
    "layer": "layer",
    "occurrences": "occurrence_count",
}
_SAFE_METADATA_LABEL = re.compile(r"[A-Za-z0-9_. -]{1,80}\Z")


def _pretty_name(route: str) -> str:
    if route == "/":
        return "Home"
    leaf = route.rstrip("/").rsplit("/", 1)[-1]
    words = leaf.replace("-", " ").replace("_", " ").strip().split()
    replacements = {
        "seo": "SEO",
        "b2b": "B2B",
        "ai": "AI",
        "api": "API",
        "ga4": "GA4",
        "d1": "D1",
    }
    return " ".join(replacements.get(word.casefold(), word.title()) for word in words) or route


def _metadata_label(value: Any) -> str:
    return value if isinstance(value, str) and _SAFE_METADATA_LABEL.fullmatch(value) else ""


class SiteGraphDisplayReportService(SiteGraphReportService):
    """Extend the existing bounded report with complete display accounting."""

    def summary(
        self,
        *,
        site_key: str | None = None,
        selected_page: str | None = None,
        layers: tuple[str, ...] = ("contextual", "related", "action"),
        graph_mode: str = "auto",
        edge_query: str = "",
        edge_sort: str = "source",
        edge_order: str = "asc",
        edge_page: int = 1,
    ) -> dict[str, Any]:
        self._validate_display_request(
            layers=layers,
            graph_mode=graph_mode,
            edge_sort=edge_sort,
            edge_order=edge_order,
            edge_page=edge_page,
        )
        edge_query = edge_query.strip()[:100]
        report = super().summary(site_key=site_key, selected_page=selected_page, layers=layers)
        if report["empty"]:
            report.update(
                {
                    "display": self._empty_display(layers=layers, graph_mode=graph_mode),
                    "edge_table": self._empty_edge_table(
                        query=edge_query, sort=edge_sort, order=edge_order, page=edge_page
                    ),
                }
            )
            return report

        repository_snapshot_id = self._latest_repository_snapshot_id(report["site"]["key"])
        if repository_snapshot_id is None:
            raise RuntimeError("compiled graph is missing its repository snapshot")

        total_nodes, total_occurrences, unresolved_relationships = self._complete_totals(
            repository_snapshot_id, layers
        )
        edge_table = self._edge_page(
            repository_snapshot_id,
            layers,
            query=edge_query,
            sort=edge_sort,
            order=edge_order,
            page=edge_page,
            page_size=EDGE_TABLE_PAGE_SIZE,
        )
        total_unique_edges = edge_table["total_unique_edges"]
        full_graph_available = (
            selected_page is None
            and total_nodes <= MAX_VISUAL_NODES
            and total_unique_edges <= MAX_VISUAL_EDGES
        )
        resolved_graph_mode = (
            "full" if graph_mode == "full" and full_graph_available
            else "full" if graph_mode == "auto" and full_graph_available
            else "bounded"
        )

        visual_nodes = report["visualization"]["nodes"]
        for node in visual_nodes:
            node["pretty_name"] = _pretty_name(node["route"])
        neighborhood_candidates_truncated = bool(
            selected_page and report["visualization"]["candidate_edges_truncated"]
        )
        visual_routes = [node["route"] for node in visual_nodes]
        edge_limit = total_unique_edges if resolved_graph_mode == "full" else MAX_VISUAL_EDGES
        visual_edges = self._edge_rows(
            repository_snapshot_id,
            layers,
            routes=visual_routes,
            limit=edge_limit,
            offset=0,
            sort="occurrences",
            order="desc",
        )
        report["visualization"].update(
            {
                "bounded": resolved_graph_mode != "full",
                "edges": [self._visual_edge(row) for row in visual_edges],
            }
        )

        displayed_nodes = len(visual_nodes)
        displayed_unique_edges = len(visual_edges)
        represented_occurrences = sum(row["occurrence_count"] for row in visual_edges)
        truncation_reasons = self._truncation_reasons(
            selected_page=selected_page,
            total_nodes=total_nodes,
            displayed_nodes=displayed_nodes,
            total_unique_edges=total_unique_edges,
            displayed_unique_edges=displayed_unique_edges,
            total_occurrences=total_occurrences,
            represented_occurrences=represented_occurrences,
            unresolved_relationships=unresolved_relationships,
            neighborhood_candidates_truncated=neighborhood_candidates_truncated,
            candidate_occurrence_limit=report["visualization"]["candidate_edge_limit"],
        )
        report["display"] = {
            "projection": report["projection"],
            "layers": list(layers),
            "filters": {
                "selected_page": selected_page,
                "edge_query": edge_query or None,
                "edge_query_scope": "complete-table-and-export",
            },
            "aggregation_mode": "source-destination-layer",
            "requested_graph_mode": graph_mode,
            "graph_mode": resolved_graph_mode,
            "full_graph_available": full_graph_available,
            "thresholds": {
                "nodes": MAX_VISUAL_NODES,
                "unique_edges": MAX_VISUAL_EDGES,
            },
            "total_nodes": total_nodes,
            "displayed_nodes": displayed_nodes,
            "total_unique_edges": total_unique_edges,
            "displayed_unique_edges": displayed_unique_edges,
            "total_occurrences": total_occurrences,
            "represented_occurrences": represented_occurrences,
            "unresolved_relationships": unresolved_relationships,
            "truncated": bool(truncation_reasons),
            "truncation_reasons": truncation_reasons,
        }
        report["edge_table"] = edge_table
        report["snapshot_diff"] = self._snapshot_diff(report["site"]["key"], repository_snapshot_id, layers)
        return report

    def iter_edge_csv(
        self,
        *,
        site_key: str | None = None,
        layers: tuple[str, ...] = ("contextual", "related", "action"),
        edge_query: str = "",
        edge_sort: str = "source",
        edge_order: str = "asc",
    ) -> Iterator[str]:
        """Yield a complete, sanitized CSV without loading all rows at once."""
        self._validate_display_request(
            layers=layers,
            graph_mode="auto",
            edge_sort=edge_sort,
            edge_order=edge_order,
            edge_page=1,
        )
        header = (
            "source_pretty_name",
            "source_route",
            "destination_pretty_name",
            "destination_route",
            "layer",
            "occurrence_count",
            "anchor_sample",
            "landmark_sample",
            "confidence_min",
            "confidence_max",
            "repeated_template_occurrences",
            "nofollow_occurrences",
            "evidence_source",
            "evidence_classification",
        )
        repository_snapshot_id = self._latest_repository_snapshot_id(site_key)
        if repository_snapshot_id is None:
            yield self._csv_line(header)
            return
        query = edge_query.strip()[:100]
        cte, cte_params = self._edge_cte(repository_snapshot_id, layers)
        where, where_params = self._edge_where(query=query, routes=None)
        order_sql = _EDGE_SORTS[edge_sort]
        direction = "DESC" if edge_order == "desc" else "ASC"
        with self.store.connect(readonly=True) as db:
            cursor = db.execute(
                f"""{cte}
                    SELECT * FROM resolved_edges{where}
                    ORDER BY {order_sql} {direction},source_route,destination_route,layer""",
                (*cte_params, *where_params),
            )
            yield self._csv_line(header)
            while rows := cursor.fetchmany(CSV_PAGE_SIZE):
                for raw_row in rows:
                    row = self._edge_model(raw_row)
                    yield self._csv_line(
                        (
                            row["source"]["pretty_name"],
                            row["source"]["route"],
                            row["destination"]["pretty_name"],
                            row["destination"]["route"],
                            row["layer"],
                            row["occurrence_count"],
                            row["evidence"]["anchor_sample"],
                            row["evidence"]["landmark_sample"],
                            row["evidence"]["confidence_min"],
                            row["evidence"]["confidence_max"],
                            row["evidence"]["repeated_template_occurrences"],
                            row["evidence"]["nofollow_occurrences"],
                            row["evidence"]["source"],
                            row["evidence"]["classification"],
                        )
                    )

    @staticmethod
    def _validate_display_request(
        *, layers: tuple[str, ...], graph_mode: str, edge_sort: str, edge_order: str, edge_page: int
    ) -> None:
        allowed_layers = PROJECTION_LAYERS["full"]
        if not layers or len(layers) != len(set(layers)) or any(layer not in allowed_layers for layer in layers):
            raise ValueError("invalid site graph layer selection")
        if graph_mode not in {"auto", "full", "bounded"}:
            raise ValueError("invalid graph mode")
        if edge_sort not in _EDGE_SORTS or edge_order not in {"asc", "desc"} or edge_page < 1:
            raise ValueError("invalid edge table selection")

    def _latest_repository_snapshot_id(self, site_key: str | None) -> str | None:
        with self.store.connect(readonly=True) as db:
            if site_key is None:
                row = db.execute(
                    """SELECT repository_snapshot_id FROM site_graph_snapshots
                       WHERE projection_name='contextual'
                       ORDER BY created_at DESC,id DESC LIMIT 1"""
                ).fetchone()
            else:
                row = db.execute(
                    """SELECT repository_snapshot_id FROM site_graph_snapshots
                       WHERE site_key=? AND projection_name='contextual'
                       ORDER BY created_at DESC,id DESC LIMIT 1""",
                    (site_key,),
                ).fetchone()
        return row["repository_snapshot_id"] if row else None

    def _snapshot_diff(
        self, site_key: str, repository_snapshot_id: str, layers: tuple[str, ...]
    ) -> dict[str, Any]:
        current = self._snapshot_row(repository_snapshot_id)
        previous = self._previous_snapshot_row(site_key, repository_snapshot_id)
        if not current or not previous:
            return {
                "available": False,
                "reason": "No previous distinct repository snapshot is available for this site and projection.",
                "current": current,
                "previous": None,
                "limit": SNAPSHOT_DIFF_EDGE_LIMIT,
            }

        current_pages = self._page_routes(repository_snapshot_id)
        previous_pages = self._page_routes(previous["repository_snapshot_id"])
        current_edges = self._edge_identities(repository_snapshot_id, layers, limit=SNAPSHOT_DIFF_EDGE_LIMIT + 1)
        previous_edges = self._edge_identities(
            previous["repository_snapshot_id"], layers, limit=SNAPSHOT_DIFF_EDGE_LIMIT + 1
        )
        edge_limited = len(current_edges) > SNAPSHOT_DIFF_EDGE_LIMIT or len(previous_edges) > SNAPSHOT_DIFF_EDGE_LIMIT
        if edge_limited:
            current_edges = current_edges[:SNAPSHOT_DIFF_EDGE_LIMIT]
            previous_edges = previous_edges[:SNAPSHOT_DIFF_EDGE_LIMIT]
        current_edge_set = set(current_edges)
        previous_edge_set = set(previous_edges)
        added_edges = sorted(current_edge_set - previous_edge_set)
        removed_edges = sorted(previous_edge_set - current_edge_set)
        added_pages = sorted(current_pages - previous_pages)
        removed_pages = sorted(previous_pages - current_pages)
        return {
            "available": True,
            "current": current,
            "previous": previous,
            "limit": SNAPSHOT_DIFF_EDGE_LIMIT,
            "limited": edge_limited,
            "pages": {
                "added": len(added_pages),
                "removed": len(removed_pages),
                "unchanged": len(current_pages & previous_pages),
                "added_sample": added_pages[:SNAPSHOT_DIFF_SAMPLE_LIMIT],
                "removed_sample": removed_pages[:SNAPSHOT_DIFF_SAMPLE_LIMIT],
            },
            "edges": {
                "added": len(added_edges),
                "removed": len(removed_edges),
                "unchanged": len(current_edge_set & previous_edge_set),
                "added_sample": [self._edge_identity_model(edge) for edge in added_edges[:SNAPSHOT_DIFF_SAMPLE_LIMIT]],
                "removed_sample": [self._edge_identity_model(edge) for edge in removed_edges[:SNAPSHOT_DIFF_SAMPLE_LIMIT]],
            },
        }

    def _snapshot_row(self, repository_snapshot_id: str) -> dict[str, Any] | None:
        with self.store.connect(readonly=True) as db:
            row = db.execute(
                """SELECT g.id AS graph_snapshot_id,g.created_at,r.id AS repository_snapshot_id,
                          r.revision,r.captured_at
                     FROM site_graph_snapshots g
                     JOIN site_graph_repository_snapshots r ON r.id=g.repository_snapshot_id
                    WHERE r.id=? AND g.projection_name='contextual'
                    ORDER BY g.created_at DESC,g.id DESC LIMIT 1""",
                (repository_snapshot_id,),
            ).fetchone()
        return dict(row) if row else None

    def _previous_snapshot_row(self, site_key: str, repository_snapshot_id: str) -> dict[str, Any] | None:
        with self.store.connect(readonly=True) as db:
            row = db.execute(
                """SELECT g.id AS graph_snapshot_id,g.created_at,r.id AS repository_snapshot_id,
                          r.revision,r.captured_at
                     FROM site_graph_snapshots g
                     JOIN site_graph_repository_snapshots r ON r.id=g.repository_snapshot_id
                    WHERE g.site_key=? AND g.projection_name='contextual' AND r.id<>?
                    ORDER BY g.created_at DESC,g.id DESC LIMIT 1""",
                (site_key, repository_snapshot_id),
            ).fetchone()
        return dict(row) if row else None

    def _page_routes(self, repository_snapshot_id: str) -> set[str]:
        with self.store.connect(readonly=True) as db:
            rows = db.execute(
                "SELECT route FROM site_graph_page_facts WHERE repository_snapshot_id=?",
                (repository_snapshot_id,),
            ).fetchall()
        return {row["route"] for row in rows}

    def _edge_identities(
        self, repository_snapshot_id: str, layers: tuple[str, ...], *, limit: int
    ) -> list[tuple[str, str, str]]:
        cte, cte_params = self._edge_cte(repository_snapshot_id, layers)
        with self.store.connect(readonly=True) as db:
            rows = db.execute(
                f"""{cte}
                    SELECT source_route,destination_route,layer FROM resolved_edges
                    ORDER BY source_route,destination_route,layer
                    LIMIT ?""",
                (*cte_params, limit),
            ).fetchall()
        return [(row["source_route"], row["destination_route"], row["layer"]) for row in rows]

    @staticmethod
    def _edge_identity_model(edge: tuple[str, str, str]) -> dict[str, str]:
        source, destination, layer = edge
        return {"source": source, "destination": destination, "layer": layer}

    @staticmethod
    def _edge_cte(repository_snapshot_id: str, layers: Sequence[str]) -> tuple[str, list[Any]]:
        placeholders = ",".join("?" for _ in layers)
        sql = f"""
            WITH destination_candidates AS (
                SELECT route AS destination_key,route AS destination_route
                  FROM site_graph_page_facts WHERE repository_snapshot_id=?
                UNION ALL
                SELECT canonical_url AS destination_key,route AS destination_route
                  FROM site_graph_page_facts WHERE repository_snapshot_id=?
            ),
            destination_lookup AS (
                SELECT destination_key,MIN(destination_route) AS destination_route
                  FROM destination_candidates GROUP BY destination_key
            ),
            selected_occurrences AS (
                SELECT source.route AS source_route,
                       link.canonical_destination,
                       link.layer,link.anchor_text,link.landmark,link.confidence,
                       link.repeated_template,link.nofollow,link.evidence_json,
                       destination.destination_route
                  FROM site_graph_link_occurrences link
                  JOIN site_graph_page_facts source ON source.id=link.source_page_fact_id
                  LEFT JOIN destination_lookup destination
                    ON destination.destination_key=link.canonical_destination
                 WHERE link.repository_snapshot_id=?
                   AND link.layer IN ({placeholders})
                   AND link.crawlable=1 AND link.external=0
            ),
            resolved_edges AS (
                SELECT source_route,destination_route,layer,
                       COUNT(*) AS occurrence_count,
                       MIN(CASE WHEN anchor_text<>'' THEN anchor_text END) AS anchor_sample,
                       MIN(CASE WHEN landmark<>'' THEN landmark END) AS landmark_sample,
                       MIN(confidence) AS confidence_min,MAX(confidence) AS confidence_max,
                       SUM(repeated_template) AS repeated_template_occurrences,
                       SUM(nofollow) AS nofollow_occurrences,
                       MIN(evidence_json) AS evidence_json
                  FROM selected_occurrences
                 WHERE destination_route IS NOT NULL
                 GROUP BY source_route,destination_route,layer
            )
        """
        return sql, [repository_snapshot_id, repository_snapshot_id, repository_snapshot_id, *layers]

    @staticmethod
    def _edge_where(*, query: str, routes: Sequence[str] | None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            term = f"%{query.casefold()}%"
            clauses.append(
                "(LOWER(source_route) LIKE ? OR LOWER(destination_route) LIKE ? OR LOWER(layer) LIKE ?)"
            )
            params.extend((term, term, term))
        if routes is not None:
            placeholders = ",".join("?" for _ in routes)
            if not placeholders:
                clauses.append("0")
            else:
                clauses.append(
                    f"source_route IN ({placeholders}) AND destination_route IN ({placeholders})"
                )
                params.extend(routes)
                params.extend(routes)
        return (" WHERE " + " AND ".join(f"({clause})" for clause in clauses)) if clauses else "", params

    def _edge_rows(
        self,
        repository_snapshot_id: str,
        layers: tuple[str, ...],
        *,
        query: str = "",
        routes: Sequence[str] | None = None,
        limit: int,
        offset: int,
        sort: str,
        order: str,
    ) -> list[dict[str, Any]]:
        cte, cte_params = self._edge_cte(repository_snapshot_id, layers)
        where, where_params = self._edge_where(query=query, routes=routes)
        order_sql = _EDGE_SORTS[sort]
        direction = "DESC" if order == "desc" else "ASC"
        with self.store.connect(readonly=True) as db:
            rows = db.execute(
                f"""{cte}
                    SELECT * FROM resolved_edges{where}
                    ORDER BY {order_sql} {direction},source_route,destination_route,layer
                    LIMIT ? OFFSET ?""",
                (*cte_params, *where_params, limit, offset),
            ).fetchall()
        return [self._edge_model(row) for row in rows]

    def _edge_count(
        self, repository_snapshot_id: str, layers: tuple[str, ...], *, query: str = ""
    ) -> int:
        cte, cte_params = self._edge_cte(repository_snapshot_id, layers)
        where, where_params = self._edge_where(query=query, routes=None)
        with self.store.connect(readonly=True) as db:
            row = db.execute(
                f"{cte} SELECT COUNT(*) AS value FROM resolved_edges{where}",
                (*cte_params, *where_params),
            ).fetchone()
        return int(row["value"])

    def _edge_page(
        self,
        repository_snapshot_id: str,
        layers: tuple[str, ...],
        *,
        query: str,
        sort: str,
        order: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        total = self._edge_count(repository_snapshot_id, layers)
        filtered = total if not query else self._edge_count(repository_snapshot_id, layers, query=query)
        page_count = max(1, math.ceil(filtered / page_size))
        page = min(page, page_count)
        rows = self._edge_rows(
            repository_snapshot_id,
            layers,
            query=query,
            limit=page_size,
            offset=(page - 1) * page_size,
            sort=sort,
            order=order,
        )
        return {
            "total_unique_edges": total,
            "filtered_unique_edges": filtered,
            "displayed_rows": len(rows),
            "query": query,
            "sort": sort,
            "order": order,
            "page": page,
            "page_size": page_size,
            "page_count": page_count,
            "rows": rows,
        }

    def _complete_totals(
        self, repository_snapshot_id: str, layers: tuple[str, ...]
    ) -> tuple[int, int, int]:
        placeholders = ",".join("?" for _ in layers)
        params = (repository_snapshot_id, *layers)
        cte, cte_params = self._edge_cte(repository_snapshot_id, layers)
        with self.store.connect(readonly=True) as db:
            total_nodes = db.execute(
                "SELECT COUNT(*) FROM site_graph_page_facts WHERE repository_snapshot_id=?",
                (repository_snapshot_id,),
            ).fetchone()[0]
            total_occurrences = db.execute(
                f"""SELECT COUNT(*) FROM site_graph_link_occurrences
                    WHERE repository_snapshot_id=? AND layer IN ({placeholders})
                      AND crawlable=1 AND external=0""",
                params,
            ).fetchone()[0]
            unresolved = db.execute(
                f"""{cte}
                    SELECT COUNT(*) FROM (
                        SELECT source_route,canonical_destination,layer
                          FROM selected_occurrences
                         WHERE destination_route IS NULL
                         GROUP BY source_route,canonical_destination,layer
                    )""",
                tuple(cte_params),
            ).fetchone()[0]
        return int(total_nodes), int(total_occurrences), int(unresolved)

    @staticmethod
    def _edge_model(row: Any) -> dict[str, Any]:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            evidence = {}
        return {
            "source": {"pretty_name": _pretty_name(row["source_route"]), "route": row["source_route"]},
            "destination": {
                "pretty_name": _pretty_name(row["destination_route"]),
                "route": row["destination_route"],
            },
            "layer": row["layer"],
            "occurrence_count": int(row["occurrence_count"]),
            "evidence": {
                "anchor_sample": row["anchor_sample"] or "",
                "landmark_sample": row["landmark_sample"] or "",
                "confidence_min": round(float(row["confidence_min"] or 0), 4),
                "confidence_max": round(float(row["confidence_max"] or 0), 4),
                "repeated_template_occurrences": int(row["repeated_template_occurrences"] or 0),
                "nofollow_occurrences": int(row["nofollow_occurrences"] or 0),
                "source": _metadata_label(evidence.get("evidence_source", evidence.get("source", ""))),
                "classification": _metadata_label(evidence.get("classification", "")),
            },
        }

    @staticmethod
    def _visual_edge(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": f'{row["source"]["route"]}|{row["destination"]["route"]}|{row["layer"]}',
            "source": row["source"]["route"],
            "source_name": row["source"]["pretty_name"],
            "destination": row["destination"]["route"],
            "destination_name": row["destination"]["pretty_name"],
            "layer": row["layer"],
            "anchor": row["evidence"]["anchor_sample"],
            "confidence": row["evidence"]["confidence_min"],
            "occurrence_count": row["occurrence_count"],
        }

    @staticmethod
    def _truncation_reasons(
        *,
        selected_page: str | None,
        total_nodes: int,
        displayed_nodes: int,
        total_unique_edges: int,
        displayed_unique_edges: int,
        total_occurrences: int,
        represented_occurrences: int,
        unresolved_relationships: int,
        neighborhood_candidates_truncated: bool,
        candidate_occurrence_limit: int,
    ) -> list[str]:
        reasons: list[str] = []
        nodes_or_edges_omitted = displayed_nodes < total_nodes or displayed_unique_edges < total_unique_edges
        if selected_page and nodes_or_edges_omitted:
            reasons.append(f"The visualization is filtered to the two-hop neighborhood of {selected_page}.")
        if nodes_or_edges_omitted and (
            total_nodes > MAX_VISUAL_NODES or total_unique_edges > MAX_VISUAL_EDGES
        ):
            reasons.append(
                f"The safe full-graph threshold is {MAX_VISUAL_NODES} nodes and "
                f"{MAX_VISUAL_EDGES} unique edges; this selection has {total_nodes} nodes and "
                f"{total_unique_edges} unique edges."
            )
        if neighborhood_candidates_truncated:
            reasons.append(
                f"The two-hop neighborhood calculation inspected at most {candidate_occurrence_limit} "
                "selected link occurrences."
            )
        if represented_occurrences < total_occurrences:
            reasons.append(
                f"Displayed unique edges represent {represented_occurrences} of "
                f"{total_occurrences} selected link occurrences."
            )
        if unresolved_relationships:
            reasons.append(
                f"{unresolved_relationships} selected relationship"
                f'{"s" if unresolved_relationships != 1 else ""} could not be resolved to a known page.'
            )
        return reasons

    @staticmethod
    def _empty_display(*, layers: tuple[str, ...], graph_mode: str) -> dict[str, Any]:
        return {
            "projection": "contextual",
            "layers": list(layers),
            "filters": {
                "selected_page": None,
                "edge_query": None,
                "edge_query_scope": "complete-table-and-export",
            },
            "aggregation_mode": "source-destination-layer",
            "requested_graph_mode": graph_mode,
            "graph_mode": "full",
            "full_graph_available": True,
            "thresholds": {"nodes": MAX_VISUAL_NODES, "unique_edges": MAX_VISUAL_EDGES},
            "total_nodes": 0,
            "displayed_nodes": 0,
            "total_unique_edges": 0,
            "displayed_unique_edges": 0,
            "total_occurrences": 0,
            "represented_occurrences": 0,
            "unresolved_relationships": 0,
            "truncated": False,
            "truncation_reasons": [],
        }

    @staticmethod
    def _empty_edge_table(*, query: str, sort: str, order: str, page: int) -> dict[str, Any]:
        return {
            "total_unique_edges": 0,
            "filtered_unique_edges": 0,
            "displayed_rows": 0,
            "query": query,
            "sort": sort,
            "order": order,
            "page": page,
            "page_size": EDGE_TABLE_PAGE_SIZE,
            "page_count": 1,
            "rows": [],
        }

    @staticmethod
    def _csv_line(values: Sequence[Any]) -> str:
        output = io.StringIO(newline="")
        safe_values = []
        for value in values:
            if isinstance(value, str):
                stripped = value.lstrip(" \t\r\n")
                if stripped.startswith(("=", "+", "-", "@")):
                    value = "'" + value
            safe_values.append(value)
        csv.writer(output, lineterminator="\n").writerow(safe_values)
        return output.getvalue()
