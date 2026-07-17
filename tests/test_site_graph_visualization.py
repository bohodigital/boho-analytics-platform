import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from boho_analytics_platform import web
from boho_analytics_platform.site_graph import reporting


def _payload():
    edge_row = {
        "source": {"pretty_name": "Home", "route": "/"},
        "destination": {"pretty_name": "Start", "route": "/start/"},
        "layer": "action",
        "occurrence_count": 3,
        "evidence": {
            "anchor_sample": "Start",
            "landmark_sample": "main",
            "source": "typescript",
            "classification": "bounded-source-heuristic",
            "confidence_min": 0.88,
            "confidence_max": 0.88,
        },
    }
    return {
        "site": {"key": "boho", "display_name": "Boho"},
        "sites": [{"key": "boho"}],
        "selected_layers": ["contextual", "action"],
        "revision": "abcdef1234567890",
        "manifest_hash": "0123456789abcdef",
        "snapshot": {"id": "sgg_test", "captured_at": "2026-07-17T00:00:00+00:00", "clean": True, "count": 2},
        "coverage": {"pages": 2, "link_occurrences": 3},
        "layer_counts": {"contextual": 0, "related": 0, "action": 3, "menu": 0, "breadcrumb": 0, "utility": 0},
        "neighborhood": {"selected_page": None},
        "overview": {
            "components": 1,
            "orphans": 0,
            "traps": 0,
            "bottlenecks": 0,
            "contextual_dead_ends": 0,
            "menu_dependent_pages": 0,
            "projection_edges": 3,
        },
        "goal_distance_buckets": {
            "goal": 1,
            "1": 1,
            "2": 0,
            "3": 0,
            "4+": 0,
            "menu-only": 0,
            "unreachable": 0,
        },
        "display": {
            "requested_graph_mode": "auto",
            "graph_mode": "bounded",
            "displayed_nodes": 2,
            "total_nodes": 2,
            "displayed_unique_edges": 1,
            "total_unique_edges": 1,
            "represented_occurrences": 3,
            "total_occurrences": 3,
            "unresolved_relationships": 0,
            "truncated": False,
            "truncation_reasons": [],
            "thresholds": {"nodes": 36, "unique_edges": 60},
            "layers": ["contextual", "action"],
            "projection": "contextual",
            "filters": {"selected_page": None, "edge_query": None},
            "full_graph_available": True,
        },
        "visualization": {
            "nodes": [
                {"route": "/", "pretty_name": "Home", "goal_distance": 1, "authority": 0.2, "selected": False},
                {"route": "/start/", "pretty_name": "Start", "goal_distance": 0, "authority": 0.8, "selected": False},
            ],
            "edges": [
                {
                    "source": "/",
                    "source_name": "Home",
                    "destination": "/start/",
                    "destination_name": "Start",
                    "layer": "action",
                    "occurrence_count": 3,
                    "anchor": "Start",
                    "confidence": 0.88,
                }
            ],
        },
        "edge_table": {
            "query": "",
            "sort": "source",
            "order": "asc",
            "page": 1,
            "page_count": 1,
            "displayed_rows": 1,
            "filtered_unique_edges": 1,
            "total_unique_edges": 1,
            "rows": [edge_row],
        },
        "snapshot_diff": {
            "available": True,
            "current": {"revision": "abcdef1234567890", "repository_snapshot_id": "repo_current"},
            "previous": {"revision": "1234567890abcdef", "repository_snapshot_id": "repo_previous"},
            "limit": 5000,
            "limited": False,
            "pages": {
                "added": 1,
                "removed": 0,
                "unchanged": 1,
                "added_sample": ["/start/"],
                "removed_sample": [],
            },
            "edges": {
                "added": 1,
                "removed": 0,
                "unchanged": 0,
                "added_sample": [{"source": "/", "destination": "/start/", "layer": "action"}],
                "removed_sample": [],
            },
        },
    }


class SiteGraphVisualizationTests(unittest.TestCase):
    def test_svg_exposes_interactive_node_edge_contract(self):
        html = web._site_graph_svg(_payload())

        self.assertIn("data-site-graph-stage", html)
        self.assertIn("data-graph-node", html)
        self.assertIn("data-graph-edge", html)
        self.assertIn("data-graph-inspector", html)
        self.assertIn('data-graph-node-name="Home"', html)
        self.assertIn('data-source-name="Home"', html)
        self.assertIn('<path class="graph-edge action"', html)
        self.assertIn('class="graph-map-help"', html)
        self.assertIn("circles are pages, arrows are internal links", html)
        self.assertIn("graph-cluster-label", html)
        self.assertIn("is-key", html)
        self.assertIn('tabindex="0"', html)
        self.assertIn('role="button"', html)

    def test_css_and_js_keep_graph_quiet_but_inspectable(self):
        self.assertIn(".graph-label", web.CSS)
        self.assertIn("opacity:0", web.CSS)
        self.assertIn(".graph-node-group.is-key .graph-label", web.CSS)
        self.assertIn("[data-graph-inspector]", web.JS)
        self.assertIn("Escape", web.JS)
        self.assertIn("stage.dataset.graphPinned", web.JS)

    def test_analysis_panels_render_required_surfaces(self):
        html = web._site_graph_analysis_panels(_payload())

        for label in (
            "Complete page table",
            "Adjacency matrix",
            "Resilience view",
            "Entry-to-goal structural view",
            "Snapshot diff",
            "Evidence rollup",
        ):
            self.assertIn(label, html)

    def test_default_edge_table_page_size_covers_current_boho_graph(self):
        self.assertGreaterEqual(reporting.EDGE_TABLE_PAGE_SIZE, 1000)

    def test_disclosure_links_all_internal_layers(self):
        html = web._site_graph_disclosure(_payload())

        self.assertIn("Account for all internal layers", html)
        self.assertIn("stored internal link occurrences across all layers", html)


if __name__ == "__main__":
    unittest.main()
