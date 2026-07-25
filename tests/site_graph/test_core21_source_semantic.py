from __future__ import annotations

import json
import unittest
from pathlib import Path

from boho_analytics_platform.site_graph.adapters.source_semantic import (
    SourceSemanticError,
    extract_source_semantic_evidence,
)


FIXTURE = Path(__file__).parent / "fixtures" / "core21" / "source_semantic"
GROUND_TRUTH = (
    Path(__file__).parent / "ground_truth" / "core21" / "source_semantic" / "expected.json"
)


class SourceSemanticAdapterTests(unittest.TestCase):
    def _sources(self) -> dict[str, str]:
        return {
            path.relative_to(FIXTURE).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(FIXTURE.rglob("*"))
            if path.is_file()
        }

    def test_fixture_meets_precision_recall_and_preserves_provenance(self) -> None:
        expected = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
        result = extract_source_semantic_evidence(self._sources())
        actual = [
            item.destination for item in result.evidence
            if item.resolution_state == "source-only"
        ]
        expected_destinations = expected["destinations"]
        true_positives = sum(
            min(actual.count(value), expected_destinations.count(value))
            for value in set(actual)
        )
        precision = true_positives / len(actual)
        recall = true_positives / len(expected_destinations)
        self.assertGreaterEqual(precision, expected["minimum_precision"])
        self.assertGreaterEqual(recall, expected["minimum_recall"])

        unresolved = {
            item.unresolved_reason for item in result.evidence
            if item.resolution_state == "unresolved"
        }
        self.assertTrue(set(expected["unresolved_reasons"]).issubset(unresolved))
        self.assertTrue(all(item.source_route == "/" for item in result.evidence))
        self.assertTrue(all(item.raw_destination_expression for item in result.evidence))
        self.assertTrue(all(item.component_or_call for item in result.evidence))
        self.assertTrue(all(item.layer_evidence for item in result.evidence))
        self.assertTrue(all(0.0 <= item.confidence <= 1.0 for item in result.evidence))
        imported = [item for item in result.evidence if item.destination == "/contact/"]
        self.assertTrue(any("import:" in ":".join(item.symbol_provenance) for item in imported))
        mapped = [item for item in result.evidence if item.resolution_kind == "bounded-map-property"]
        self.assertEqual([item.destination for item in mapped], ["/company/about/", "/pricing/"])

    def test_identical_runs_have_identical_order_and_hash(self) -> None:
        first = extract_source_semantic_evidence(self._sources())
        second = extract_source_semantic_evidence(dict(reversed(list(self._sources().items()))))
        self.assertEqual(first, second)
        self.assertEqual(first.as_dict(), second.as_dict())

    def test_runtime_conditional_and_unsupported_helpers_remain_unresolved(self) -> None:
        result = extract_source_semantic_evidence(
            {
                "app/page.tsx": (
                    "export default () => <main>"
                    "<a href={enabled ? '/yes/' : '/no/'}>Conditional</a>"
                    "<a href={unknownRoute('/invented/')}>Unknown helper</a>"
                    "<a href={window.location.pathname}>Runtime</a>"
                    "</main>;"
                )
            }
        )
        self.assertEqual([item.destination for item in result.evidence], [None, None, None])
        self.assertEqual(
            {item.unresolved_reason for item in result.evidence},
            {"conditional-expression", "environment-expression", "runtime-call"},
        )

    def test_ambiguous_cross_file_symbol_is_never_invented(self) -> None:
        result = extract_source_semantic_evidence(
            {
                "app/page.tsx": "const TARGET = '/one/'; <a href={TARGET}>One</a>",
                "src/other.ts": "const TARGET = '/two/';",
            }
        )
        self.assertEqual(len(result.evidence), 1)
        self.assertIsNone(result.evidence[0].destination)
        self.assertEqual(result.evidence[0].unresolved_reason, "unsupported-expression")

    def test_rejects_unsafe_paths_and_bounds(self) -> None:
        with self.assertRaises(SourceSemanticError):
            extract_source_semantic_evidence({"../secret.ts": "const X = '/';"})
        with self.assertRaisesRegex(SourceSemanticError, "paths must be text"):
            extract_source_semantic_evidence({1: "const X = '/';"})  # type: ignore[dict-item]
        with self.assertRaises(SourceSemanticError):
            extract_source_semantic_evidence(
                {"app/page.tsx": "x" * (4 * 1024 * 1024 + 1)}
            )
        with self.assertRaises(SourceSemanticError):
            extract_source_semantic_evidence(
                {"app/page.tsx": '<a href="/">Home</a>'},
                repository_revision="HEAD",
            )

    def test_action_layer_and_explicit_layer_are_evidence_backed(self) -> None:
        result = extract_source_semantic_evidence(
            {
                "app/page.tsx": (
                    '<form action="/submit/"></form>\n'
                    '<a data-link-layer="utility" href="/legal/">Legal</a>'
                )
            }
        )
        by_destination = {item.destination: item for item in result.evidence}
        self.assertEqual(by_destination["/submit/"].layer, "action")
        self.assertEqual(by_destination["/legal/"].layer, "utility")
        self.assertEqual(
            by_destination["/legal/"].layer_evidence, "explicit:data-link-layer"
        )

    def test_non_topology_destinations_use_explicit_states(self) -> None:
        result = extract_source_semantic_evidence(
            {
                "app/page.tsx": (
                    '<a href="#details">Details</a>'
                    '<a href="mailto:team@example.test">Email</a>'
                    '<a href="https://external.example/path">External</a>'
                )
            },
            repository_revision="a" * 40,
        )
        self.assertEqual(result.repository_revision, "a" * 40)
        self.assertEqual(
            [item.resolution_state for item in result.evidence],
            ["fragment", "action", "external"],
        )
        self.assertEqual(result.evidence[1].layer, "action")

    def test_bounded_multiline_map_resolves_but_runtime_collection_does_not(self) -> None:
        result = extract_source_semantic_evidence(
            {
                "app/page.tsx": (
                    "const items = [{ href: '/one/' }, { href: '/two/' }];\n"
                    "export default () => <main>{items.map((item) => (\n"
                    "  <a href={item.href}>Item</a>\n"
                    "))}{runtimeItems.map((item) => (\n"
                    "  <a href={item.href}>Runtime item</a>\n"
                    "))}</main>;"
                )
            }
        )
        self.assertEqual(
            [item.destination for item in result.evidence],
            ["/one/", "/two/", None],
        )
        self.assertEqual(result.evidence[-1].unresolved_reason, "unsupported-expression")


if __name__ == "__main__":
    unittest.main()
