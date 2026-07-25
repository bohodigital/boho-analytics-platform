#!/usr/bin/env python3
"""Capture Graph Evidence Core 2.1 rendered evidence from an injected replay.

No live browser is bundled.  The replay format exists for deterministic,
public-safe fixtures and for separately reviewed browser wrappers.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boho_analytics_platform.site_graph.adapters.rendered_crawl import (
    BrowserCapture,
    CrawlAuthorization,
    RawAnchor,
    RawForm,
    RequestPolicy,
    Viewport,
    crawl_rendered_evidence,
    result_to_json,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture deterministic rendered Site Graph evidence without live-site access."
    )
    parser.add_argument("--owner-authorized", action="store_true", help="confirm the target and revision are owner-authorized")
    parser.add_argument("--target-origin", required=True, help="exact authorized HTTP(S) origin")
    parser.add_argument(
        "--expected-canonical-origin",
        help="exact public origin expected in canonical tags (defaults to target origin)",
    )
    parser.add_argument("--expected-revision", required=True, help="exact expected Git/deployment revision")
    parser.add_argument("--observed-revision", required=True, help="exact observed Git/deployment revision")
    parser.add_argument("--static-origin", action="append", default=[], help="narrowly authorized static origin (repeatable)")
    parser.add_argument("--routes", type=Path, required=True, help="JSON array of routes")
    parser.add_argument("--replay", type=Path, required=True, help="deterministic browser replay JSON")
    parser.add_argument("--output", type=Path, help="write JSON to this file instead of stdout")
    parser.add_argument("--timeout-ms", type=int, default=15_000)
    return parser


def _bounded_json(path: Path, maximum: int) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path} must be a regular, non-symlink file")
    if path.stat().st_size > maximum:
        raise ValueError(f"{path} exceeds its size limit")
    with path.open("rb") as source:
        raw = source.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError(f"{path} exceeds its size limit")
    return json.loads(raw.decode("utf-8", errors="strict"))


def _capture(value: dict[str, Any]) -> BrowserCapture:
    anchors = tuple(RawAnchor(
        href=str(item.get("href", "")),
        text=str(item.get("text", "")),
        accessible_name=str(item.get("accessible_name", "")),
        landmark=str(item.get("landmark", "")),
        visible=bool(item.get("visible", False)),
        in_viewport=bool(item.get("in_viewport", False)),
        rel=tuple(str(part) for part in item.get("rel", [])),
    ) for item in value.get("anchors", []))
    forms = tuple(RawForm(
        action=str(item.get("action", "")),
        text=str(item.get("text", "")),
        accessible_name=str(item.get("accessible_name", "")),
        landmark=str(item.get("landmark", "")),
        visible=bool(item.get("visible", False)),
        in_viewport=bool(item.get("in_viewport", False)),
    ) for item in value.get("forms", []))
    return BrowserCapture(
        requested_url=str(value["requested_url"]),
        final_url=str(value["final_url"]),
        http_status=value.get("http_status"),
        canonical_url=value.get("canonical_url"),
        robots=value.get("robots"),
        title=str(value.get("title", "")),
        h1=tuple(str(item) for item in value.get("h1", [])),
        schema_types=tuple(str(item) for item in value.get("schema_types", [])),
        dom_html=str(value.get("dom_html", "")),
        anchors=anchors,
        forms=forms,
        console_failures=tuple(str(item) for item in value.get("console_failures", [])),
        network_failures=tuple(str(item) for item in value.get("network_failures", [])),
        blocked_resources=tuple(str(item) for item in value.get("blocked_resources", [])),
        hydration_complete=bool(value.get("hydration_complete", True)),
    )


@dataclass
class _ReplaySession:
    records: dict[str, dict[str, Any]]
    viewport: str
    policy: RequestPolicy
    closed: bool = False
    cleared: bool = False

    def navigate(self, url: str, *, timeout_ms: int) -> BrowserCapture:
        decision = self.policy.decide(url, resource_type="document", is_navigation=True)
        if not decision.allowed:
            raise RuntimeError("replay navigation rejected by request policy")
        value = self.records.get(self.viewport, {}).get(url)
        if value is None:
            raise TimeoutError(url)
        if value.get("outcome") == "timeout":
            raise TimeoutError(url)
        if value.get("outcome") == "failed":
            raise RuntimeError("fixture navigation failure")
        return _capture(value)

    def clear_state(self) -> None:
        self.cleared = True

    def close(self) -> None:
        self.closed = True


class _ReplayFactory:
    def __init__(self, records: dict[str, dict[str, Any]]) -> None:
        self.records = records

    def open_context(
        self, *, profile_directory: Path, viewport: Viewport, request_policy: RequestPolicy
    ) -> _ReplaySession:
        if not profile_directory.is_dir():
            raise RuntimeError("temporary profile directory was not created")
        return _ReplaySession(self.records, viewport.name, request_policy)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.owner_authorized:
        parser.error("--owner-authorized is required; capture targets may not be inferred")
    routes = _bounded_json(args.routes, 2 * 1024 * 1024)
    replay = _bounded_json(args.replay, 16 * 1024 * 1024)
    if not isinstance(routes, list) or not all(isinstance(item, str) for item in routes):
        parser.error("--routes must contain a JSON array of strings")
    if not isinstance(replay, dict):
        parser.error("--replay must contain a JSON object keyed by viewport and URL")
    authorization = CrawlAuthorization(
        args.target_origin,
        args.expected_revision,
        args.observed_revision,
        tuple(args.static_origin),
        args.expected_canonical_origin,
    )
    result = crawl_rendered_evidence(
        authorization, routes, _ReplayFactory(replay), timeout_ms=args.timeout_ms
    )
    rendered = result_to_json(result) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
