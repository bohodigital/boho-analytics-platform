"""Small, dependency-free runtime build identity helpers."""

from __future__ import annotations

import os
import re

from . import __version__
from .storage import SCHEMA_VERSION


_OBJECT_ID = re.compile(r"[0-9a-f]{7,64}\Z")


def _object_id(name: str) -> str:
    value = os.environ.get(name, "unknown").strip().casefold()
    return value if value == "unknown" or _OBJECT_ID.fullmatch(value) else "invalid"


def build_identity() -> dict[str, object]:
    """Return the immutable identity injected by the deployment unit."""

    return {
        "version": __version__,
        "build_commit": _object_id("BOHO_ANALYTICS_BUILD_COMMIT"),
        "build_tree": _object_id("BOHO_ANALYTICS_BUILD_TREE"),
        "database_schema": SCHEMA_VERSION,
    }


def version_string() -> str:
    identity = build_identity()
    return (
        f"{identity['version']} "
        f"(commit={identity['build_commit']}; tree={identity['build_tree']}; "
        f"db-schema={identity['database_schema']})"
    )
