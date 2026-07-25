from __future__ import annotations

import copy
import re
import unittest
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
TEST_FIXTURE = ROOT / "tests/site_graph/fixtures/core21/public_core21_contract.yaml"
TEST_GROUND_TRUTH = ROOT / "tests/site_graph/ground_truth/core21/public_core21_ground_truth.yaml"
EXAMPLE_FIXTURE = ROOT / "examples/site-graph/fixtures/core21/public_core21_contract.yaml"
EXAMPLE_GROUND_TRUTH = ROOT / "examples/site-graph/ground_truth/core21/public_core21_ground_truth.yaml"

REQUIRED_SCENARIOS = {
    "actions",
    "alternate_goal_paths",
    "arrays_maps_spreads",
    "canonical_conflicts",
    "conditions",
    "direct_links",
    "dominators_gateways",
    "duplicate_occurrences",
    "dynamic_routes",
    "fragments",
    "healthy_and_trapped_sccs",
    "interrupted_publication",
    "invented_route_regressions",
    "menu_home_dependence",
    "nested_props",
    "projection_consistency",
    "redirects",
    "route_registries",
    "snapshot_changes",
    "source_artifact_rendered_disagreements",
    "unresolved_expressions",
    "visual_limit_analytical_total",
    "wrapped_links",
}
RESOLUTION_STATES = {
    "action",
    "artifact-only",
    "confirmed-page",
    "contradicted",
    "dynamic-unknown",
    "excluded",
    "external",
    "fragment",
    "missing",
    "redirect",
    "rendered-only",
    "source-only",
    "unchecked",
    "unresolved",
}
PROVENANCE_KINDS = {"artifact", "rendered", "source", "synthetic-control"}
PROJECTIONS = {"all-internal", "contextual", "navigation"}
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
ROUTE_PATTERN = re.compile(r"^/(?:[a-z0-9][a-z0-9_-]*/)*$")
HEX_40 = re.compile(r"^[0-9a-f]{40}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "Google client secret": re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
}
PRIVATE_PATTERNS = {
    "Windows user path": re.compile(r"[A-Z]:\\Users\\", re.IGNORECASE),
    "private deployment path": re.compile(re.escape("/srv/" + "local1/")),
    "private host login": re.compile(r"[a-z][a-z0-9_-]*@(?:internal|private)\.invalid"),
    "home directory": re.compile(r"/Users/[^/]+/"),
}


class FixtureContractError(AssertionError):
    pass


def load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise FixtureContractError(f"{path.name} must contain a mapping")
    return payload


def assert_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown or missing:
        raise FixtureContractError(f"{label} fields differ: missing={missing}, unknown={unknown}")


def assert_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise FixtureContractError(f"{label} has invalid id: {value!r}")
    return value


def assert_unique_sorted(items: list[dict[str, Any]], label: str) -> set[str]:
    ids = [assert_id(item.get("id"), label) for item in items]
    if len(ids) != len(set(ids)):
        raise FixtureContractError(f"{label} contains duplicate ids")
    if ids != sorted(ids):
        raise FixtureContractError(f"{label} ids are not deterministic sorted order")
    return set(ids)


def iter_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [text for child in value for text in iter_strings(child)]
    if isinstance(value, dict):
        return [text for child in value.values() for text in iter_strings(child)]
    return []


def validate_public_safe(*payloads: dict[str, Any]) -> None:
    for text in iter_strings(payloads):
        for label, pattern in (SECRET_PATTERNS | PRIVATE_PATTERNS).items():
            if pattern.search(text):
                raise FixtureContractError(f"{label} found in fixture content")


def validate_contract(fixture: dict[str, Any], truth: dict[str, Any]) -> None:
    assert_keys(
        fixture,
        {"schema_version", "fixture_id", "description", "site", "routes", "scenarios"},
        "fixture",
    )
    assert_keys(
        truth,
        {
            "schema_version",
            "fixture_id",
            "resolution_states",
            "evidence",
            "graph_expectations",
            "snapshot_expectations",
            "publication_expectations",
            "projection_expectations",
            "display_expectations",
            "regression_expectations",
        },
        "ground truth",
    )
    if fixture["schema_version"] != "2.1" or truth["schema_version"] != "2.1":
        raise FixtureContractError("schema_version must be 2.1")
    if fixture["fixture_id"] != "public_graph_evidence_core21":
        raise FixtureContractError("unexpected fixture_id")
    if fixture["fixture_id"] != truth["fixture_id"]:
        raise FixtureContractError("fixture_id mismatch")
    validate_public_safe(fixture, truth)

    site = fixture["site"]
    assert_keys(
        site,
        {"origin", "repository_revision", "deployment_revision", "analyzer_independent"},
        "site",
    )
    if site["origin"] != "https://graph-core21.example":
        raise FixtureContractError("site must use the reserved synthetic origin")
    if not HEX_40.fullmatch(site["repository_revision"]):
        raise FixtureContractError("repository_revision must be a synthetic full revision")
    if not HEX_40.fullmatch(site["deployment_revision"]):
        raise FixtureContractError("deployment_revision must be a synthetic full revision")
    if site["analyzer_independent"] is not True:
        raise FixtureContractError("fixture must declare analyzer independence")

    routes = fixture["routes"]
    scenarios = fixture["scenarios"]
    if not isinstance(routes, list) or not isinstance(scenarios, list):
        raise FixtureContractError("routes and scenarios must be lists")
    route_ids = assert_unique_sorted(routes, "routes")
    scenario_ids = assert_unique_sorted(scenarios, "scenarios")
    scenario_categories: set[str] = set()
    route_paths: set[str] = set()
    for route in routes:
        assert_keys(route, {"id", "path", "canonical_path", "goal"}, f"route {route.get('id')}")
        if not ROUTE_PATTERN.fullmatch(route["path"]):
            raise FixtureContractError(f"route has invalid path: {route['path']!r}")
        if not ROUTE_PATTERN.fullmatch(route["canonical_path"]):
            raise FixtureContractError(f"route has invalid canonical_path: {route['canonical_path']!r}")
        if route["path"] in route_paths:
            raise FixtureContractError("route paths must be unique")
        route_paths.add(route["path"])
        if not isinstance(route["goal"], bool):
            raise FixtureContractError("route goal must be boolean")
    for scenario in scenarios:
        assert_keys(
            scenario,
            {"id", "category", "description", "input_forms", "expected_evidence"},
            f"scenario {scenario.get('id')}",
        )
        scenario_categories.add(scenario["category"])
        if not scenario["input_forms"] or scenario["input_forms"] != sorted(set(scenario["input_forms"])):
            raise FixtureContractError(f"scenario input_forms must be non-empty, unique, sorted: {scenario['id']}")
        if scenario["expected_evidence"] != sorted(set(scenario["expected_evidence"])):
            raise FixtureContractError(f"scenario evidence ids must be unique and sorted: {scenario['id']}")
    if scenario_categories != REQUIRED_SCENARIOS:
        raise FixtureContractError(
            f"scenario coverage differs: missing={sorted(REQUIRED_SCENARIOS - scenario_categories)}, "
            f"extra={sorted(scenario_categories - REQUIRED_SCENARIOS)}"
        )

    if truth["resolution_states"] != sorted(RESOLUTION_STATES):
        raise FixtureContractError("resolution states must equal the frozen state vocabulary")
    evidence = truth["evidence"]
    evidence_ids = assert_unique_sorted(evidence, "evidence")
    occurrence_keys: set[str] = set()
    seen_states: set[str] = set()
    seen_kinds: set[str] = set()
    for item in evidence:
        assert_keys(
            item,
            {
                "id",
                "scenario_id",
                "source_route",
                "target_route",
                "raw_target",
                "resolution_state",
                "provenance_kind",
                "revision",
                "occurrence_key",
                "layer",
            },
            f"evidence {item.get('id')}",
        )
        if item["scenario_id"] not in scenario_ids:
            raise FixtureContractError(f"evidence references unknown scenario: {item['id']}")
        if item["source_route"] not in route_ids:
            raise FixtureContractError(f"evidence references unknown source route: {item['id']}")
        target = item["target_route"]
        state = item["resolution_state"]
        if state not in RESOLUTION_STATES:
            raise FixtureContractError(f"evidence has invalid resolution state: {item['id']}")
        if target is not None and target not in route_ids:
            raise FixtureContractError(f"evidence invents target route: {item['id']}")
        if state in {
            "action",
            "dynamic-unknown",
            "excluded",
            "external",
            "fragment",
            "missing",
            "unchecked",
            "unresolved",
        } and target is not None:
            raise FixtureContractError(f"non-topology evidence must not have a target route: {item['id']}")
        if state == "external" and not str(item["raw_target"]).startswith("https://external.example/"):
            raise FixtureContractError(f"external evidence must use reserved example origin: {item['id']}")
        if state == "unresolved" and item["raw_target"] is not None:
            raise FixtureContractError(f"unresolved evidence must not invent raw target: {item['id']}")
        if item["provenance_kind"] not in PROVENANCE_KINDS:
            raise FixtureContractError(f"evidence has invalid provenance kind: {item['id']}")
        if not HEX_40.fullmatch(item["revision"]):
            raise FixtureContractError(f"evidence has invalid revision: {item['id']}")
        if item["occurrence_key"] in occurrence_keys:
            raise FixtureContractError("duplicate occurrence keys are forbidden")
        occurrence_keys.add(item["occurrence_key"])
        seen_states.add(state)
        seen_kinds.add(item["provenance_kind"])
    if seen_states != RESOLUTION_STATES:
        raise FixtureContractError("evidence must exercise every frozen resolution state")
    if seen_kinds != PROVENANCE_KINDS:
        raise FixtureContractError("evidence must exercise every provenance kind")
    for scenario in scenarios:
        for evidence_id in scenario["expected_evidence"]:
            if evidence_id not in evidence_ids:
                raise FixtureContractError(f"scenario references unknown evidence: {scenario['id']}")

    graph = truth["graph_expectations"]
    assert_keys(graph, {"components", "dominators", "alternate_goal_paths"}, "graph expectations")
    component_ids = assert_unique_sorted(graph["components"], "graph components")
    if not component_ids:
        raise FixtureContractError("graph components must not be empty")
    classifications = {component["classification"] for component in graph["components"]}
    if not {"healthy-scc", "trap-scc"}.issubset(classifications):
        raise FixtureContractError("healthy and trapped SCC expectations are required")
    for component in graph["components"]:
        assert_keys(component, {"id", "classification", "routes", "reaches_goal"}, f"component {component['id']}")
        if not set(component["routes"]).issubset(route_ids):
            raise FixtureContractError(f"component references unknown route: {component['id']}")
    for dominator in graph["dominators"]:
        assert_keys(dominator, {"route", "dominates", "gateway"}, "dominator")
        if dominator["route"] not in route_ids or not set(dominator["dominates"]).issubset(route_ids):
            raise FixtureContractError("dominator references unknown route")
    for paths in graph["alternate_goal_paths"]:
        assert_keys(paths, {"source", "goal", "edge_disjoint_paths"}, "alternate path")
        if paths["source"] not in route_ids or paths["goal"] not in route_ids:
            raise FixtureContractError("alternate path references unknown route")
        if len(paths["edge_disjoint_paths"]) < 2:
            raise FixtureContractError("alternate goal expectation requires at least two paths")
        for path in paths["edge_disjoint_paths"]:
            if path[0] != paths["source"] or path[-1] != paths["goal"]:
                raise FixtureContractError("alternate path endpoints do not match source and goal")
            if not set(path).issubset(route_ids):
                raise FixtureContractError("alternate path contains unknown route")

    snapshots = truth["snapshot_expectations"]
    if [item["kind"] for item in snapshots] != ["added", "changed", "removed"]:
        raise FixtureContractError("snapshot expectations must cover added, changed, removed in deterministic order")
    for item in snapshots:
        assert_keys(item, {"id", "kind", "before", "after"}, f"snapshot {item.get('id')}")

    publication = truth["publication_expectations"]
    assert_keys(publication, {"published_snapshot", "interrupted_snapshot", "visible_snapshot"}, "publication")
    if publication["visible_snapshot"] != publication["published_snapshot"]:
        raise FixtureContractError("interrupted publication must leave the prior snapshot visible")
    if publication["interrupted_snapshot"] == publication["visible_snapshot"]:
        raise FixtureContractError("interrupted snapshot must differ from visible snapshot")

    projections = truth["projection_expectations"]
    if set(projections) != PROJECTIONS:
        raise FixtureContractError("projection expectations must cover the frozen projections")
    for name, projection in projections.items():
        assert_keys(projection, {"layers", "node_total", "edge_total", "occurrence_total"}, f"projection {name}")
        if projection["layers"] != sorted(set(projection["layers"])):
            raise FixtureContractError(f"projection layers must be unique and sorted: {name}")
        if min(projection["node_total"], projection["edge_total"], projection["occurrence_total"]) < 0:
            raise FixtureContractError(f"projection totals must be non-negative: {name}")
        if projection["edge_total"] > projection["occurrence_total"]:
            raise FixtureContractError(f"projection edge total exceeds occurrences: {name}")
    if projections["all-internal"]["edge_total"] != (
        projections["contextual"]["edge_total"] + projections["navigation"]["edge_total"]
    ):
        raise FixtureContractError("projection edge totals are inconsistent")
    if projections["all-internal"]["occurrence_total"] != (
        projections["contextual"]["occurrence_total"] + projections["navigation"]["occurrence_total"]
    ):
        raise FixtureContractError("projection occurrence totals are inconsistent")

    display = truth["display_expectations"]
    assert_keys(
        display,
        {"analytical_node_total", "analytical_edge_total", "visual_node_limit", "visual_edge_limit", "truncated"},
        "display",
    )
    if display["analytical_node_total"] <= display["visual_node_limit"]:
        raise FixtureContractError("node fixture must exceed the visual limit")
    if display["analytical_edge_total"] <= display["visual_edge_limit"]:
        raise FixtureContractError("edge fixture must exceed the visual limit")
    if display["truncated"] is not True:
        raise FixtureContractError("visual fixture must disclose truncation")

    regressions = truth["regression_expectations"]
    assert_keys(regressions, {"declared_routes", "candidate_routes", "invented_routes"}, "regressions")
    if regressions["declared_routes"] != sorted(route_paths):
        raise FixtureContractError("regression declared routes must match fixture routes")
    if set(regressions["candidate_routes"]) - set(regressions["declared_routes"]) != set(regressions["invented_routes"]):
        raise FixtureContractError("invented routes must be the exact undeclared candidate set")

    expected_sequence = [evidence_id for scenario in scenarios for evidence_id in scenario["expected_evidence"]]
    all_expected = set(expected_sequence)
    if all_expected != evidence_ids or len(expected_sequence) != len(evidence_ids):
        raise FixtureContractError("all evidence must be assigned to exactly one scenario")


class Core21FixtureContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_yaml(TEST_FIXTURE)
        self.truth = load_yaml(TEST_GROUND_TRUTH)

    def assert_invalid(self, fixture: dict[str, Any], truth: dict[str, Any], pattern: str) -> None:
        with self.assertRaisesRegex(FixtureContractError, pattern):
            validate_contract(fixture, truth)

    def test_contract_is_valid_and_analyzer_independent(self) -> None:
        validate_contract(self.fixture, self.truth)

    def test_public_examples_match_test_contracts_exactly(self) -> None:
        self.assertEqual(load_yaml(EXAMPLE_FIXTURE), self.fixture)
        self.assertEqual(load_yaml(EXAMPLE_GROUND_TRUTH), self.truth)

    def test_validator_rejects_missing_scenario_coverage(self) -> None:
        fixture = copy.deepcopy(self.fixture)
        fixture["scenarios"].pop()
        self.assert_invalid(fixture, self.truth, "scenario coverage differs")

    def test_validator_rejects_duplicate_occurrence_identity(self) -> None:
        truth = copy.deepcopy(self.truth)
        truth["evidence"][1]["occurrence_key"] = truth["evidence"][0]["occurrence_key"]
        self.assert_invalid(self.fixture, truth, "duplicate occurrence keys")

    def test_validator_rejects_invented_topology(self) -> None:
        truth = copy.deepcopy(self.truth)
        truth["evidence"][0]["target_route"] = "invented"
        self.assert_invalid(self.fixture, truth, "invents target route")

    def test_validator_rejects_revision_shape_and_mismatched_publication(self) -> None:
        fixture = copy.deepcopy(self.fixture)
        fixture["site"]["repository_revision"] = "main"
        self.assert_invalid(fixture, self.truth, "repository_revision")

        truth = copy.deepcopy(self.truth)
        truth["publication_expectations"]["visible_snapshot"] = truth["publication_expectations"]["interrupted_snapshot"]
        self.assert_invalid(self.fixture, truth, "prior snapshot visible")

    def test_validator_rejects_visual_totals_as_analytical_totals(self) -> None:
        truth = copy.deepcopy(self.truth)
        truth["display_expectations"]["analytical_node_total"] = truth["display_expectations"]["visual_node_limit"]
        self.assert_invalid(self.fixture, truth, "exceed the visual limit")

    def test_validator_rejects_private_and_secret_shaped_content(self) -> None:
        fixture = copy.deepcopy(self.fixture)
        fixture["scenarios"][0]["description"] = "/Users/example/private/site"
        self.assert_invalid(fixture, self.truth, "home directory")

        truth = copy.deepcopy(self.truth)
        truth["evidence"][0]["raw_target"] = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz1234567890ABCD"
        self.assert_invalid(self.fixture, truth, "GitHub token")

    def test_validator_rejects_unknown_fields_and_nondeterministic_order(self) -> None:
        fixture = copy.deepcopy(self.fixture)
        fixture["routes"][0]["extra"] = True
        self.assert_invalid(fixture, self.truth, "fields differ")

        fixture = copy.deepcopy(self.fixture)
        fixture["scenarios"][0], fixture["scenarios"][1] = fixture["scenarios"][1], fixture["scenarios"][0]
        self.assert_invalid(fixture, self.truth, "deterministic sorted order")


if __name__ == "__main__":
    unittest.main()
