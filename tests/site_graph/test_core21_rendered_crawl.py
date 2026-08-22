from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from boho_analytics_platform.site_graph.adapters.rendered_crawl import (
    MAX_ROUTES,
    BrowserCapture,
    CrawlAuthorization,
    RawAnchor,
    RawForm,
    RequestPolicy,
    RenderedCrawlError,
    Viewport,
    crawl_rendered_evidence,
    result_to_json,
)


REVISION = "a" * 40
FIXTURE = Path(__file__).parent / "fixtures/core21/rendered_crawl/replay.json"
ROUTES = Path(__file__).parent / "fixtures/core21/rendered_crawl/routes.json"
GROUND_TRUTH = Path(__file__).parent / "ground_truth/core21/rendered_crawl/summary.json"


def _capture(url: str, *, mobile: bool = False) -> BrowserCapture:
    return BrowserCapture(
        requested_url=url,
        final_url=url,
        http_status=200,
        canonical_url=url,
        robots="index,follow",
        title="Home",
        h1=("Welcome",),
        schema_types=("WebSite", "Organization"),
        dom_html="<main><h1>Welcome</h1></main>",
        anchors=(RawAnchor(
            "/about", "About us", "About Example Company", "navigation", True, True, ()
        ),),
        forms=() if mobile else (RawForm(
            "/contact/save", "Send", "Send message", "main", True, False
        ),),
        blocked_resources=() if mobile else ("tracker",),
        hydration_complete=not mobile,
    )


class _Session:
    def __init__(self, viewport: str, policy: RequestPolicy, outcomes=None) -> None:
        self.viewport = viewport
        self.policy = policy
        self.outcomes = outcomes or {}
        self.cleared = False
        self.closed = False

    def navigate(self, url: str, *, timeout_ms: int) -> BrowserCapture:
        assert self.policy.decide(url, resource_type="document", is_navigation=True).allowed
        outcome = self.outcomes.get((self.viewport, url))
        if outcome == "timeout":
            raise TimeoutError
        if outcome == "failure":
            raise RuntimeError("synthetic failure")
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, BrowserCapture):
            return outcome
        return _capture(url, mobile=self.viewport == "mobile")

    def clear_state(self) -> None:
        self.cleared = True

    def close(self) -> None:
        self.closed = True


class _Factory:
    def __init__(self, outcomes=None) -> None:
        self.outcomes = outcomes
        self.sessions: list[_Session] = []
        self.profile_directories: list[Path] = []

    def open_context(
        self, *, profile_directory: Path, viewport: Viewport, request_policy: RequestPolicy
    ) -> _Session:
        self.profile_directories.append(profile_directory)
        self.assert_profile_exists = profile_directory.is_dir()
        session = _Session(viewport.name, request_policy, self.outcomes)
        self.sessions.append(session)
        return session


class RenderedCrawlTests(unittest.TestCase):
    def authorization(self, **changes) -> CrawlAuthorization:
        values = {
            "target_origin": "https://example.test",
            "expected_revision": REVISION,
            "observed_revision": REVISION,
            "allowed_static_origins": ("https://static.example.test",),
        }
        values.update(changes)
        return CrawlAuthorization(**values)

    def test_request_policy_fails_closed(self) -> None:
        policy = RequestPolicy(self.authorization())
        self.assertTrue(policy.decide(
            "https://example.test/about", resource_type="document", is_navigation=True
        ).allowed)
        self.assertTrue(policy.decide(
            "https://static.example.test/app.js", resource_type="script"
        ).allowed)
        self.assertTrue(policy.decide(
            "https://example.test/assets/layout-segment-context-B76w5zU2.js",
            resource_type="script",
        ).allowed)
        blocked = [
            ("https://other.test/", "document", "GET", True),
            ("https://static.example.test/", "document", "GET", True),
            ("https://static.example.test/api", "fetch", "GET", False),
            ("https://example.test/login", "document", "GET", True),
            ("https://example.test/collect?analytics=1", "fetch", "GET", False),
            ("https://example.test/ads/collect", "image", "GET", False),
            ("https://example.test/assets/segment.js", "script", "GET", False),
            ("https://analytics.example.test/script.js", "script", "GET", False),
            ("https://example.test/contact/form", "fetch", "GET", False),
            ("https://example.test/form", "fetch", "POST", False),
            ("mailto:hello@example.test", "document", "GET", True),
            ("tel:+15555555555", "document", "GET", True),
        ]
        for url, resource_type, method, navigation in blocked:
            with self.subTest(url=url):
                self.assertFalse(policy.decide(
                    url, resource_type=resource_type, method=method, is_navigation=navigation
                ).allowed)
        self.assertEqual(
            policy.decide("https://[broken", resource_type="document").category,
            "invalid-url",
        )

    def test_loopback_capture_validates_an_explicit_public_canonical_origin(self) -> None:
        target = "http://127.0.0.1:60932"
        public = "https://public.example.test"
        authorization = self.authorization(
            target_origin=target,
            allowed_static_origins=(),
            expected_canonical_origin=public,
        )
        self.assertFalse(
            RequestPolicy(authorization).decide(
                f"{public}/", resource_type="document", is_navigation=True
            ).allowed
        )
        capture = BrowserCapture(
            requested_url=f"{target}/",
            final_url=f"{target}/",
            http_status=200,
            canonical_url=f"{public}/",
            title="Home",
            h1=("Home",),
            dom_html="<main><h1>Home</h1></main>",
            hydration_complete=True,
        )
        accepted = crawl_rendered_evidence(
            authorization,
            ["/"],
            _Factory(
                {
                    ("desktop", f"{target}/"): capture,
                    ("mobile", f"{target}/"): capture,
                }
            ),
        )
        self.assertEqual(
            {
                item.resolution_state
                for item in accepted.evidence_batch.page_candidates
            },
            {"confirmed-page"},
        )
        self.assertEqual(
            accepted.evidence_batch.expected_canonical_origin,
            public,
        )

        contradicted_capture = BrowserCapture(
            **{
                **capture.__dict__,
                "canonical_url": "https://other.example.test/",
            }
        )
        contradicted = crawl_rendered_evidence(
            authorization,
            ["/"],
            _Factory(
                {
                    ("desktop", f"{target}/"): contradicted_capture,
                    ("mobile", f"{target}/"): contradicted_capture,
                }
            ),
        )
        self.assertEqual(
            {
                item.resolution_state
                for item in contradicted.evidence_batch.page_candidates
            },
            {"contradicted"},
        )

    def test_capture_is_deterministic_and_cleans_every_context(self) -> None:
        factory = _Factory()
        first = crawl_rendered_evidence(self.authorization(), ["/"], factory)
        second_factory = _Factory()
        second = crawl_rendered_evidence(self.authorization(), ["/"], second_factory)
        self.assertEqual(result_to_json(first), result_to_json(second))
        self.assertEqual(first.state, "partial")
        self.assertEqual(first.evidence_batch.coverage.complete, 1)
        self.assertEqual(first.evidence_batch.coverage.partial, 1)
        self.assertEqual(
            {item.resolution_state for item in first.evidence_batch.link_occurrences},
            {"rendered-only", "action"},
        )
        self.assertTrue(all(session.cleared and session.closed for session in factory.sessions))
        self.assertTrue(factory.assert_profile_exists)
        self.assertTrue(all(not path.exists() for path in factory.profile_directories))

    def test_timeout_failure_and_external_redirect_remain_explicit(self) -> None:
        external = BrowserCapture(
            requested_url="https://example.test/redirect",
            final_url="https://evil.test/",
            http_status=200,
        )
        outcomes = {
            ("desktop", "https://example.test/timeout"): "timeout",
            ("mobile", "https://example.test/timeout"): "failure",
            ("desktop", "https://example.test/redirect"): external,
            ("mobile", "https://example.test/redirect"): external,
        }
        factory = _Factory(outcomes)
        result = crawl_rendered_evidence(
            self.authorization(), ["/timeout", "/redirect"], factory
        )
        states = {item.capture_state for item in result.evidence_batch.page_candidates}
        self.assertEqual(states, {"timeout", "failed", "blocked"})
        self.assertTrue(all(session.cleared and session.closed for session in factory.sessions))

    def test_cancellation_still_clears_and_closes_context(self) -> None:
        factory = _Factory({("desktop", "https://example.test/"): KeyboardInterrupt()})
        with self.assertRaises(KeyboardInterrupt):
            crawl_rendered_evidence(self.authorization(), ["/"], factory)
        self.assertEqual(len(factory.sessions), 1)
        self.assertTrue(factory.sessions[0].cleared)
        self.assertTrue(factory.sessions[0].closed)
        self.assertTrue(all(not path.exists() for path in factory.profile_directories))

    def test_revision_mismatch_is_unchecked_without_opening_browser(self) -> None:
        factory = _Factory()
        result = crawl_rendered_evidence(
            self.authorization(observed_revision="b" * 40), ["/"], factory
        )
        self.assertEqual(result.state, "unchecked")
        self.assertEqual(result.evidence_batch.coverage.revision_state, "mismatch")
        self.assertTrue(all(
            item.resolution_state == "contradicted"
            and item.capture_state == "unchecked"
            for item in result.evidence_batch.page_candidates
        ))
        self.assertEqual(factory.sessions, [])

    def test_invalid_routes_and_authorization_are_rejected_or_unchecked(self) -> None:
        with self.assertRaises(RenderedCrawlError):
            self.authorization(target_origin="https://user:secret@example.test")
        with self.assertRaises(RenderedCrawlError):
            self.authorization(target_origin="https://[broken")
        factory = _Factory()
        result = crawl_rendered_evidence(
            self.authorization(),
            ["https://other.test/", "/safe#fragment", "/safe?email=private", "\x00"],
            factory,
        )
        self.assertEqual(result.evidence_batch.coverage.unchecked, 8)
        self.assertEqual(factory.sessions, [])

        with self.assertRaisesRegex(RenderedCrawlError, "at most"):
            crawl_rendered_evidence(
                self.authorization(),
                (f"/route-{index}/" for index in range(MAX_ROUTES + 1)),
                factory,
            )

    def test_fixture_matches_ground_truth_shape(self) -> None:
        replay = json.loads(FIXTURE.read_text(encoding="utf-8"))
        ground_truth = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
        outcomes = {}
        for viewport, records in replay.items():
            for url, value in records.items():
                outcomes[(viewport, url)] = BrowserCapture(
                    requested_url=value["requested_url"],
                    final_url=value["final_url"],
                    http_status=value["http_status"],
                    canonical_url=value["canonical_url"],
                    robots=value["robots"],
                    title=value["title"],
                    h1=tuple(value["h1"]),
                    schema_types=tuple(value["schema_types"]),
                    dom_html=value["dom_html"],
                    anchors=tuple(RawAnchor(
                        item["href"], item["text"], item["accessible_name"],
                        item["landmark"], item["visible"], item["in_viewport"],
                        tuple(item["rel"]),
                    ) for item in value["anchors"]),
                    forms=tuple(RawForm(
                        item["action"], item["text"], item["accessible_name"],
                        item["landmark"], item["visible"], item["in_viewport"],
                    ) for item in value["forms"]),
                    console_failures=tuple(value["console_failures"]),
                    network_failures=tuple(value["network_failures"]),
                    blocked_resources=tuple(value["blocked_resources"]),
                    hydration_complete=value["hydration_complete"],
                )
        result = crawl_rendered_evidence(self.authorization(), ["/"], _Factory(outcomes))
        actual = {
            "adapter_state": result.state,
            "candidate_states": {
                "complete": result.evidence_batch.coverage.complete,
                "partial": result.evidence_batch.coverage.partial,
            },
            "link_resolution_states": sorted({
                item.resolution_state for item in result.evidence_batch.link_occurrences
            }),
            "revision_state": result.evidence_batch.coverage.revision_state,
        }
        self.assertEqual(actual, ground_truth)

    def test_capture_script_help(self) -> None:
        repository = Path(__file__).parents[2]
        result = subprocess.run(
            [sys.executable, "scripts/capture_site_graph_evidence.py", "--help"],
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--confirm-target", result.stdout)

    def test_capture_script_replays_without_network(self) -> None:
        repository = Path(__file__).parents[2]
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/capture_site_graph_evidence.py",
                    "--confirm-target",
                    "--target-origin", "https://example.test",
                    "--expected-revision", REVISION,
                    "--observed-revision", REVISION,
                    "--routes", str(ROUTES),
                    "--replay", str(FIXTURE),
                    "--output", str(output),
                ],
                cwd=repository,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["evidence_batch"]["coverage"]["complete"], 1)
            self.assertEqual(payload["evidence_batch"]["coverage"]["partial"], 1)


if __name__ == "__main__":
    unittest.main()
