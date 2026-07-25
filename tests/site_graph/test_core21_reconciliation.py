from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from boho_analytics_platform.site_graph.contracts import (
    AdapterResult,
    CoverageSummary,
    EvidenceBatch,
    LinkOccurrence,
    PageCandidate,
    PageEntity,
)
from boho_analytics_platform.site_graph.reconciliation import (
    artifact_evidence_to_contract,
    canonical_route,
    reconcile_adapter_results,
    rendered_evidence_to_contract,
    source_semantic_to_contract,
)
from boho_analytics_platform.site_graph.adapters.source_semantic import (
    extract_source_semantic_evidence,
)


REVISION = "a" * 40
ORIGIN = "https://fixture.example"


def lane(
    adapter: str,
    states: dict[str, str],
    *,
    links: tuple[tuple[str, str, str, str], ...] = (),
    relation: str = "exact",
) -> AdapterResult:
    candidates = {
        route: PageCandidate(
            route,
            route,
            state,
            f"{adapter}/routes.txt",
            f"{adapter}:{index}",
        )
        for index, (route, state) in enumerate(sorted(states.items()), 1)
    }
    pages = tuple(
        PageEntity(
            candidate.candidate_id,
            route,
            f"{ORIGIN}{route}" if route != "/" else f"{ORIGIN}/",
            "Home" if route == "/" else route.strip("/").title(),
            "Home" if route == "/" else route.strip("/").title(),
            f"{adapter}-label",
            0.8,
            candidate.resolution_state,
            ("homepage",) if route == "/" else (),
        )
        for route, candidate in sorted(candidates.items())
        if candidate.resolution_state in {
            "confirmed-page",
            "source-only",
            "artifact-only",
            "rendered-only",
            "contradicted",
        }
    )
    occurrences = tuple(
        LinkOccurrence(
            candidates[source].candidate_id,
            destination,
            "" if state in {"action", "fragment", "external"} else destination,
            state,
            f"{adapter}/routes.txt",
            f"{adapter}:link:{index}",
            "action" if state == "action" else layer,
        )
        for index, (source, destination, state, layer) in enumerate(links, 1)
    )
    evidence_revision = REVISION if relation == "exact" else "b" * 40 if relation == "mismatch" else ""
    batch = EvidenceBatch(
        "fixture-site",
        adapter,
        "2.1",
        REVISION,
        evidence_revision,
        relation,
        tuple(candidates.values()),
        pages,
        occurrences,
        CoverageSummary(
            len(candidates),
            len(pages),
            len(occurrences),
            len(candidates),
            len(pages),
            len(occurrences),
            tuple(sorted((state, list(states.values()).count(state)) for state in set(states.values()))),
        ),
    )
    return AdapterResult("succeeded" if relation == "exact" else "partial", batch)


class Core21ReconciliationTests(unittest.TestCase):
    def test_semantic_conversion_preserves_same_coordinate_occurrences(self) -> None:
        native = extract_source_semantic_evidence(
            {
                "app/page.tsx": (
                    "const links = [{ href: '/shared/' }, { href: '/shared/' }];\n"
                    "links.map((item) => <a href={item.href}>Shared</a>);"
                )
            },
            repository_revision=REVISION,
        )

        converted = source_semantic_to_contract(
            native,
            site_key="fixture-site",
            repository_revision=REVISION,
            canonical_origin=ORIGIN,
        )

        shared = [
            item
            for item in converted.batch.links
            if item.canonical_destination == "/shared/"
        ]
        self.assertEqual(len(shared), 2)
        self.assertEqual(len({item.occurrence_id for item in shared}), 2)
        self.assertEqual(len({item.source_location for item in shared}), 2)
        self.assertTrue(
            all(
                item.source_location.startswith("app/page.tsx:2:")
                and "@"
                in item.source_location
                for item in shared
            )
        )

    def test_semantic_conversion_sanitizes_unresolved_raw_expressions(self) -> None:
        native = extract_source_semantic_evidence(
            {
                "app/page.tsx": (
                    '<a href={"person@example.com"}>Contact</a>\n'
                    '<a href={"/search?token=private"}>Search</a>'
                )
            },
            repository_revision=REVISION,
        )

        converted = source_semantic_to_contract(
            native,
            site_key="fixture-site",
            repository_revision=REVISION,
            canonical_origin=ORIGIN,
        )

        unresolved = [
            item
            for item in converted.batch.candidates
            if item.resolution_state == "dynamic-unknown"
        ]
        self.assertEqual(len(unresolved), 2)
        self.assertEqual(
            {item.raw_route for item in unresolved},
            {"[dynamic-unknown]"},
        )
        serialized = "\n".join(
            (
                *(item.raw_route for item in converted.batch.candidates),
                *(item.raw_destination for item in converted.batch.links),
            )
        )
        self.assertNotIn("person@example.com", serialized)
        self.assertNotIn("token=private", serialized)

    def test_artifact_conversion_sanitizes_labels_and_withholds_unknown_revision(self) -> None:
        route = SimpleNamespace(
            route="/",
            source_path="index.html",
            route_kind="html",
            title="Home",
            h1="Home",
            canonical_url=f"{ORIGIN}/",
            indexable=True,
            schema_types=(),
            content_hash="b" * 64,
            revision_state="associated",
        )
        about = SimpleNamespace(
            route="/about/",
            source_path="about/index.html",
            route_kind="html",
            title="About",
            h1="About",
            canonical_url=f"{ORIGIN}/about/",
            indexable=True,
            schema_types=(),
            content_hash="c" * 64,
            revision_state="associated",
        )
        link = SimpleNamespace(
            source_route="/",
            destination="/about/",
            source_path="index.html",
            source_location="index.html:1:1",
            anchor_text="person@example.com",
            element="a",
            content_hash="d" * 64,
        )
        action = SimpleNamespace(
            source_route="/",
            destination="mailto:person@example.com",
            source_path="index.html",
            source_location="index.html:2:1",
            anchor_text="Contact",
            element="a",
            content_hash="e" * 64,
        )
        converted = artifact_evidence_to_contract(
            SimpleNamespace(
                revision_state="mismatched",
                revision=REVISION,
                adapter_version="core21-r1",
                routes=(route, about),
                links=(link, action),
                diagnostics=("revision-mismatch:routes.json",),
            ),
            site_key="fixture-site",
            repository_revision=REVISION,
            canonical_origin=ORIGIN,
        )

        self.assertEqual(converted.status, "partial")
        self.assertEqual(converted.batch.revision_relation, "unchecked")
        self.assertEqual(converted.batch.evidence_revision, "")
        self.assertEqual(
            next(
                item.anchor_text
                for item in converted.batch.links
                if item.canonical_destination == "/about/"
            ),
            "",
        )
        self.assertEqual(
            next(
                item.layer
                for item in converted.batch.links
                if item.resolution_state == "action"
            ),
            "action",
        )
        self.assertTrue(
            all(
                item.resolution_state == "contradicted"
                for item in converted.batch.candidates
            )
        )

    def test_rendered_conversion_projects_loopback_routes_to_public_origin(self) -> None:
        repeated_link = SimpleNamespace(
            source_url="http://127.0.0.1:60932/",
            target="http://127.0.0.1:60932/about/",
            resolution_state="rendered-only",
            viewport="desktop",
            kind="anchor",
            landmark="nav",
            text="About",
            accessible_name="",
            nofollow=False,
            visible=True,
            provenance_hash="f" * 64,
        )
        converted = rendered_evidence_to_contract(
            SimpleNamespace(
                state="accepted",
                evidence_batch=SimpleNamespace(
                    adapter_version="core21-r1",
                    target_origin="http://127.0.0.1:60932",
                    revision_state="matched",
                    observed_revision=REVISION,
                    page_candidates=(
                        SimpleNamespace(
                            requested_url="http://127.0.0.1:60932/",
                            resolution_state="confirmed-page",
                            viewport="desktop",
                            title="Home",
                        ),
                    ),
                    link_occurrences=(repeated_link, repeated_link),
                ),
            ),
            site_key="fixture-site",
            repository_revision=REVISION,
            canonical_origin=ORIGIN,
        )

        self.assertEqual(converted.status, "succeeded")
        self.assertEqual(converted.batch.pages[0].canonical_url, f"{ORIGIN}/")
        self.assertEqual(len(converted.batch.links), 2)
        self.assertEqual(
            len({item.occurrence_id for item in converted.batch.links}), 2
        )

    def test_supported_lane_subsets_and_degraded_rendered_are_deterministic(self) -> None:
        source = lane(
            "source-semantic",
            {"/": "source-only", "/about/": "source-only"},
            links=(("/", "/about/", "source-only", "contextual"),),
        )
        artifact = lane(
            "artifact-evidence",
            {"/": "artifact-only", "/about/": "artifact-only"},
        )
        rendered = lane(
            "rendered-crawl",
            {"/": "confirmed-page", "/about/": "rendered-only"},
        )
        unavailable = AdapterResult(
            "failed",
            None,
            (
                {
                    "severity": "warning",
                    "code": "rendered-unavailable",
                    "message": "Rendered evidence was not available for this reviewed run.",
                },
            ),
        )

        expected_states = (
            ((source,), {"/": "source-only", "/about/": "source-only"}),
            ((source, artifact), {"/": "confirmed-page", "/about/": "confirmed-page"}),
            ((source, rendered), {"/": "confirmed-page", "/about/": "confirmed-page"}),
            ((source, artifact, rendered), {
                "/": "confirmed-page",
                "/about/": "confirmed-page",
            }),
            ((source, artifact, unavailable), {
                "/": "confirmed-page",
                "/about/": "confirmed-page",
            }),
        )
        for inputs, expected in expected_states:
            with self.subTest(lanes=len(inputs)):
                first = reconcile_adapter_results(
                    inputs,
                    site_key="fixture-site",
                    repository_revision=REVISION,
                    canonical_origin=ORIGIN,
                )
                second = reconcile_adapter_results(
                    reversed(inputs),
                    site_key="fixture-site",
                    repository_revision=REVISION,
                    canonical_origin=ORIGIN,
                )
                actual = {
                    item.canonical_route: item.resolution_state
                    for item in first.batch.candidates
                    if item.canonical_route
                }
                self.assertEqual(actual, expected)
                self.assertEqual(first.content_hash, second.content_hash)
                self.assertEqual(first.batch.content_hash, second.batch.content_hash)

    def test_input_batch_identity_is_hashed_once_per_reconciliation(self) -> None:
        source = lane(
            "source-semantic",
            {"/": "source-only"},
            links=tuple(
                ("/", f"/target-{index}/", "source-only", "contextual")
                for index in range(50)
            ),
        )
        original = EvidenceBatch.batch_id.fget
        assert original is not None
        calls: dict[int, int] = {}

        def counted(batch: EvidenceBatch) -> str:
            calls[id(batch)] = calls.get(id(batch), 0) + 1
            return original(batch)

        with patch.object(EvidenceBatch, "batch_id", property(counted)):
            reconcile_adapter_results(
                (source,),
                site_key="fixture-site",
                repository_revision=REVISION,
                canonical_origin=ORIGIN,
            )

        self.assertEqual(calls[id(source.batch)], 1)

    def test_conflicts_revision_mismatch_and_non_topology_remain_explicit(self) -> None:
        source = lane(
            "source-semantic",
            {"/": "source-only", "/about/": "source-only"},
            links=(
                ("/", "/about/", "source-only", "contextual"),
                ("/", "", "action", "action"),
                ("/", "", "fragment", "contextual"),
            ),
        )
        rendered_missing = lane(
            "rendered-crawl",
            {"/": "confirmed-page", "/about/": "missing"},
        )
        mismatched = lane(
            "artifact-evidence",
            {"/mismatch/": "artifact-only"},
            relation="mismatch",
        )
        result = reconcile_adapter_results(
            (source, rendered_missing, mismatched),
            site_key="fixture-site",
            repository_revision=REVISION,
            canonical_origin=ORIGIN,
        )
        states = {
            item.canonical_route: item.resolution_state
            for item in result.batch.candidates
            if item.canonical_route
        }
        self.assertEqual(states["/about/"], "contradicted")
        self.assertEqual(states["/mismatch/"], "contradicted")
        self.assertEqual(result.contradictions, ("/about/", "/mismatch/"))
        self.assertTrue(
            all(
                not item.canonical_destination
                for item in result.batch.links
                if item.resolution_state in {"action", "fragment"}
            )
        )
        page_routes = {page.canonical_route for page in result.batch.pages}
        self.assertNotIn("", page_routes)

    def test_query_slash_alias_and_origin_rules_fail_closed(self) -> None:
        self.assertEqual(
            canonical_route(
                "/about?view=compact",
                canonical_origin=ORIGIN,
                approved_query_keys=frozenset({"view"}),
            ),
            ("/about/", "topology"),
        )
        self.assertEqual(
            canonical_route("/about?token=value", canonical_origin=ORIGIN),
            ("", "unresolved"),
        )
        self.assertEqual(
            canonical_route("https://other.example/about", canonical_origin=ORIGIN),
            ("", "external"),
        )
        self.assertEqual(
            canonical_route("#details", canonical_origin=ORIGIN),
            ("", "fragment"),
        )

    def test_redirects_become_aliases_without_becoming_page_topology(self) -> None:
        artifact = lane(
            "artifact-evidence",
            {
                "/": "artifact-only",
                "/about/": "artifact-only",
                "/old/": "redirect",
            },
            links=(("/old/", "/about/", "artifact-only", "contextual"),),
        )
        result = reconcile_adapter_results(
            (artifact,),
            site_key="fixture-site",
            repository_revision=REVISION,
            canonical_origin=ORIGIN,
        )
        pages = {page.canonical_route: page for page in result.batch.pages}
        self.assertNotIn("/old/", pages)
        self.assertIn("/old/", pages["/about/"].aliases)
        self.assertFalse(
            any(
                link.source_candidate_id
                == next(
                    candidate.candidate_id
                    for candidate in result.batch.candidates
                    if candidate.canonical_route == "/old/"
                )
                for link in result.batch.links
            )
        )

    def test_source_reference_does_not_contradict_observed_redirect(self) -> None:
        source = lane(
            "source-semantic",
            {"/": "source-only", "/old/": "source-only"},
            links=(("/", "/old/", "source-only", "contextual"),),
        )
        redirect = lane(
            "artifact-evidence",
            {"/": "artifact-only", "/new/": "artifact-only", "/old/": "redirect"},
            links=(("/old/", "/new/", "artifact-only", "contextual"),),
        )
        result = reconcile_adapter_results(
            (source, redirect),
            site_key="fixture-site",
            repository_revision=REVISION,
            canonical_origin=ORIGIN,
        )
        states = {
            item.canonical_route: item.resolution_state
            for item in result.batch.candidates
            if item.canonical_route
        }
        self.assertEqual(states["/old/"], "redirect")
        self.assertNotIn("/old/", result.contradictions)
        pages = {page.canonical_route: page for page in result.batch.pages}
        self.assertNotIn("/old/", pages)
        self.assertIn("/old/", pages["/new/"].aliases)

        for observed_state in ("confirmed-page", "artifact-only", "rendered-only"):
            with self.subTest(observed_state=observed_state):
                observed_page = lane(
                    "rendered-crawl",
                    {"/old/": observed_state},
                )
                contradicted = reconcile_adapter_results(
                    (source, redirect, observed_page),
                    site_key="fixture-site",
                    repository_revision=REVISION,
                    canonical_origin=ORIGIN,
                )
                self.assertEqual(contradicted.contradictions, ("/old/",))

    def test_result_count_is_bounded_before_consuming_an_unbounded_iterator(self) -> None:
        source = lane("source-semantic", {"/": "source-only"})

        def too_many():
            while True:
                yield source

        with self.assertRaisesRegex(ValueError, "at most"):
            reconcile_adapter_results(
                too_many(),
                site_key="fixture-site",
                repository_revision=REVISION,
                canonical_origin=ORIGIN,
            )


if __name__ == "__main__":
    unittest.main()
