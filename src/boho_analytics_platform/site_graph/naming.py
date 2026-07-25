"""Deterministic, identity-preserving names for reconciled Site Graph pages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping


_ACRONYMS = {
    "ai": "AI",
    "api": "API",
    "b2b": "B2B",
    "d1": "D1",
    "ga4": "GA4",
    "seo": "SEO",
}
_SPACE = re.compile(r"[-_]+")


@dataclass(frozen=True, slots=True)
class PageName:
    canonical_route: str
    short: str
    full: str
    source: str
    confidence: float


def _words(value: str) -> str:
    words = _SPACE.sub(" ", value).strip().split()
    return " ".join(_ACRONYMS.get(word.casefold(), word.title()) for word in words)


def _route_name(route: str) -> tuple[str, str]:
    if route == "/":
        return "Home", "Home"
    parts = [part for part in route.strip("/").split("/") if part]
    short = _words(parts[-1]) or route
    full = " / ".join(_words(part) or part for part in parts)
    return short, full


def assign_page_names(
    routes: list[str] | tuple[str, ...],
    hints: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, PageName]:
    """Assign stable names while disambiguating duplicate short labels.

    ``hints`` maps a canonical route to ``(label, source)``. Empty labels are
    ignored. The canonical route remains the identity and is never replaced by
    a display label.
    """

    if len(routes) != len(set(routes)):
        raise ValueError("page-name routes must be unique")
    hints = hints or {}
    provisional: dict[str, PageName] = {}
    for route in sorted(routes):
        short, full = _route_name(route)
        hint = hints.get(route)
        if hint and isinstance(hint[0], str) and hint[0].strip():
            hinted = " ".join(hint[0].strip().split())[:120]
            source = hint[1].strip()[:100] if isinstance(hint[1], str) else ""
            provisional[route] = PageName(
                route,
                hinted,
                full if full != short else hinted,
                source or "evidence-label",
                0.95,
            )
        else:
            provisional[route] = PageName(route, short, full, "canonical-route", 0.8)

    counts: dict[str, int] = {}
    for value in provisional.values():
        counts[value.short.casefold()] = counts.get(value.short.casefold(), 0) + 1

    output: dict[str, PageName] = {}
    for route, value in provisional.items():
        short = value.short
        if counts[short.casefold()] > 1:
            parent = route.strip("/").rsplit("/", 1)[0]
            qualifier = _words(parent.rsplit("/", 1)[-1]) if parent else "Home"
            short = f"{short} — {qualifier}"[:120]
        output[route] = PageName(
            route,
            short,
            value.full[:240],
            value.source,
            value.confidence,
        )
    return output
