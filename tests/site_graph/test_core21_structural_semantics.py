from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from boho_analytics_platform.site_graph.analysis import (
    analyze_structural_semantics,
    compile_graph,
)
from tests.site_graph.test_analysis import seed_site_graph
from tests.site_graph.test_core21_reconciliation import REVISION, lane


class Core21StructuralSemanticsTests(unittest.TestCase):
    def test_findings_use_aligned_projection_and_real_occurrence_ids(self) -> None:
        result = lane(
            "reconciliation",
            {
                "/": "confirmed-page",
                "/menu-only/": "confirmed-page",
                "/dead/": "confirmed-page",
                "/true-orphan/": "confirmed-page",
            },
            links=(
                ("/", "/menu-only/", "confirmed-page", "menu"),
                ("/", "/dead/", "confirmed-page", "contextual"),
            ),
        )
        assert result.batch is not None
        summary = analyze_structural_semantics(
            result.batch,
            selected_layers=("contextual",),
            goal_routes=("/dead/",),
        )
        self.assertEqual(summary.true_orphans, ("/true-orphan/",))
        self.assertEqual(summary.contextual_orphans, ("/menu-only/",))
        self.assertEqual(summary.menu_dependent, ("/menu-only/",))
        self.assertEqual(summary.homepage_dependent, ("/dead/", "/menu-only/"))
        self.assertEqual(summary.global_shell_dependent, ("/menu-only/",))
        self.assertIn("/dead/", summary.full_goal_reachable)
        self.assertIn("/dead/", summary.selected_goal_reachable)
        actual_occurrence_ids = {
            item.occurrence_id for item in result.batch.links
        }
        referenced = {
            occurrence_id
            for finding in summary.findings
            for occurrence_id in finding.evidence_occurrence_ids
        }
        self.assertLessEqual(referenced, actual_occurrence_ids)
        self.assertNotIn("trap", {item.finding_type for item in summary.findings})
        self.assertNotIn("bottleneck", {item.finding_type for item in summary.findings})

    def test_repeated_analysis_is_stable_and_layers_are_validated(self) -> None:
        result = lane(
            "reconciliation",
            {"/": "confirmed-page", "/about/": "confirmed-page"},
            links=(("/", "/about/", "confirmed-page", "contextual"),),
        )
        assert result.batch is not None
        first = analyze_structural_semantics(result.batch)
        second = analyze_structural_semantics(result.batch)
        self.assertEqual(first, second)
        with self.assertRaisesRegex(ValueError, "known link layers"):
            analyze_structural_semantics(result.batch, selected_layers=("invented",))

    def test_compiler_persists_corrected_types_with_a_legacy_summary_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, site_key = seed_site_graph(Path(temporary) / "graph.db")
            summary = compile_graph(store, site_key=site_key, projection="contextual")
            self.assertEqual(summary["findings"]["true_orphan"], 1)
            self.assertEqual(summary["findings"]["orphan"], 1)
            with store.connect(readonly=True) as db:
                finding_types = {
                    row[0]
                    for row in db.execute(
                        "SELECT finding_type FROM site_graph_findings"
                    ).fetchall()
                }
            self.assertIn("true_orphan", finding_types)
            self.assertNotIn("orphan", finding_types)


if __name__ == "__main__":
    unittest.main()
