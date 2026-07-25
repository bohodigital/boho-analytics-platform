from __future__ import annotations

import unittest

from boho_analytics_platform.site_graph.contracts import (
    AdapterResult,
    CoverageSummary,
    EvidenceBatch,
    LinkOccurrence,
    PageCandidate,
    PageEntity,
)
from boho_analytics_platform.site_graph.models import RESOLUTION_STATES


REVISION = "a" * 40


def sample_batch(*, reverse: bool = False, route_total: int = 2) -> EvidenceBatch:
    home = PageCandidate("/", "/", "confirmed-page", "src/pages/home.tsx", "home.tsx:1")
    about = PageCandidate("/about", "/about", "source-only", "src/pages/about.tsx", "about.tsx:1")
    candidates = (about, home) if reverse else (home, about)
    pages = (
        PageEntity(home.candidate_id, "/", "https://fixture.example/", "Home", "Home",
                   "route-title", 1.0, "confirmed-page", ("homepage",)),
        PageEntity(about.candidate_id, "/about", "https://fixture.example/about", "About",
                   "About", "route-title", 0.9, "source-only"),
    )
    links = (
        LinkOccurrence(
            home.candidate_id, "/about", "/about", "confirmed-page",
            "src/pages/home.tsx", "home.tsx:12", "contextual", "About",
        ),
    )
    return EvidenceBatch(
        "fixture-site", "fixture-adapter", "2.1.0", REVISION, REVISION, "exact",
        candidates, pages, links,
        CoverageSummary(route_total, 2, 1, 2, 2, 1, (("confirmed-page", 2),)),
    )


class Core21ContractTests(unittest.TestCase):
    def test_resolution_state_vocabulary_is_exact(self):
        self.assertEqual(RESOLUTION_STATES, {
            "confirmed-page", "redirect", "missing", "source-only", "artifact-only",
            "rendered-only", "dynamic-unknown", "contradicted", "unresolved",
            "excluded", "unchecked", "action", "fragment", "external",
        })

    def test_batch_identity_and_order_are_deterministic(self):
        first = sample_batch()
        second = sample_batch(reverse=True)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.batch_id, second.batch_id)
        self.assertEqual(first.normalized(), second.normalized())
        self.assertEqual(AdapterResult("succeeded", first).content_hash,
                         AdapterResult("succeeded", second).content_hash)

    def test_analytical_totals_may_exceed_emitted_visual_rows(self):
        batch = sample_batch(route_total=20)
        self.assertEqual(batch.coverage.route_total, 20)
        self.assertEqual(len(batch.candidates), 2)

    def test_action_fragment_and_external_evidence_cannot_form_topology(self):
        home = sample_batch().candidates[0]
        for state, layer in (("action", "action"), ("fragment", "contextual"), ("external", "utility")):
            occurrence = LinkOccurrence(
                home.candidate_id, "#target", "", state,
                "src/pages/home.tsx", f"home.tsx:{state}", layer,
            )
            self.assertFalse(occurrence.topology_eligible)
        with self.assertRaisesRegex(ValueError, "cannot become topology"):
            LinkOccurrence(
                home.candidate_id, "mailto:test", "/invented", "action",
                "src/pages/home.tsx", "home.tsx:20", "action",
            )

    def test_revision_mismatch_and_unchecked_states_are_explicit(self):
        original = sample_batch()
        mismatched = EvidenceBatch(
            original.site_key, original.adapter, original.adapter_version,
            REVISION, "b" * 40, "mismatch", original.candidates, original.pages,
            original.links, original.coverage,
        )
        self.assertEqual(mismatched.revision_relation, "mismatch")
        with self.assertRaisesRegex(ValueError, "unchecked"):
            EvidenceBatch(
                original.site_key, original.adapter, original.adapter_version,
                REVISION, REVISION, "unchecked", original.candidates, original.pages,
                original.links, original.coverage,
            )

    def test_references_counts_diagnostics_and_private_text_fail_closed(self):
        batch = sample_batch()
        with self.assertRaisesRegex(ValueError, "analytical totals"):
            EvidenceBatch(
                batch.site_key, batch.adapter, batch.adapter_version,
                REVISION, REVISION, "exact", batch.candidates, batch.pages, batch.links,
                CoverageSummary(1, 2, 1, 1, 2, 1),
            )
        with self.assertRaisesRegex(ValueError, "private or secret"):
            AdapterResult("failed", None, ({
                "severity": "error", "code": "bad",
                "message": "contact person@example.com",
            },))
        with self.assertRaisesRegex(ValueError, "repository-relative"):
            PageCandidate("/", "/", "confirmed-page", "/tmp/site.tsx", "site.tsx:1")
        with self.assertRaisesRegex(ValueError, "private or secret"):
            LinkOccurrence(
                batch.candidates[0].candidate_id, "/landing?query=private", "/landing",
                "confirmed-page", "src/pages/home.tsx", "home.tsx:1", "contextual",
            )
        with self.assertRaisesRegex(ValueError, "require an evidence batch"):
            AdapterResult("partial", None)


if __name__ == "__main__":
    unittest.main()
