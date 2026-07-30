"""Stable dependency-inversion contracts for replaceable platform adapters."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from .config import BindingConfig, ConnectionConfig
from .models import (
    AnalyticsDefinition,
    CapabilitySnapshot,
    DefinitionActivation,
    DefinitionChange,
    DefinitionIdentity,
    DefinitionPackageResult,
    DefinitionVersion,
    MetricPoint,
    QueryWindow,
)


PAGEVIEW_ACQUISITION_SOURCES = frozenset({"google-analytics", "umami"})
PAGEVIEW_DATA_RESULT_KIND = "data:explicit-pageviews-v1"
PAGEVIEW_EMPTY_RESULT_KIND = "empty:explicit-pageviews-v1"
PAGEVIEW_RESULT_KINDS = frozenset({
    PAGEVIEW_DATA_RESULT_KIND,
    PAGEVIEW_EMPTY_RESULT_KIND,
})


def explicit_pageview_result_kind(source: object, result_kind: object) -> bool:
    """Return whether a provider run proves the post-upgrade pageview contract."""

    return (
        source in PAGEVIEW_ACQUISITION_SOURCES
        and result_kind in PAGEVIEW_RESULT_KINDS
    )


def mark_pageview_result_kind(source: object, result_kind: str | None) -> str | None:
    """Mark only fresh successful pageview-provider acquisitions."""

    if source not in PAGEVIEW_ACQUISITION_SOURCES:
        return result_kind
    return {
        "data": PAGEVIEW_DATA_RESULT_KIND,
        "empty": PAGEVIEW_EMPTY_RESULT_KIND,
    }.get(result_kind, result_kind)


def public_result_kind(result_kind: object) -> object:
    """Keep operational status output stable while the ledger carries proof."""

    return {
        PAGEVIEW_DATA_RESULT_KIND: "data",
        PAGEVIEW_EMPTY_RESULT_KIND: "empty",
    }.get(result_kind, result_kind)


@runtime_checkable
class CredentialLease(Protocol):
    """Opaque, bounded access to provider credential fields."""

    def read(self, field: str) -> bytes | None:
        """Return one field without providing a printable credential mapping."""

    def close(self) -> None:
        """Release and best-effort clear the leased credential material."""


@runtime_checkable
class CredentialProvider(Protocol):
    def acquire(self, reference: str) -> CredentialLease:
        """Resolve an opaque configuration reference to a bounded lease."""


@dataclass(frozen=True, slots=True)
class SyncRequest:
    binding: BindingConfig
    window: QueryWindow
    metric_groups: tuple[str, ...]


@runtime_checkable
class Connector(Protocol):
    provider: str

    def probe(
        self, connection: ConnectionConfig, credential: CredentialLease
    ) -> CapabilitySnapshot:
        """Discover the account's usable read-only capabilities."""

    def collect(
        self,
        connection: ConnectionConfig,
        credential: CredentialLease,
        request: SyncRequest,
    ) -> Iterable[MetricPoint]:
        """Collect one bounded, restart-safe window of normalized data."""


@runtime_checkable
class MetricStore(Protocol):
    def upsert(self, points: Iterable[MetricPoint]) -> int:
        """Idempotently persist normalized points and return the affected count."""

    def query(
        self,
        *,
        client_id: str,
        site_ids: Sequence[str],
        metric_ids: Sequence[str],
        window: QueryWindow,
    ) -> Iterable[MetricPoint]:
        """Read tenant-authorized normalized points for a resolved report request."""


@runtime_checkable
class DefinitionStore(Protocol):
    """Transactional immutable-definition registry boundary."""

    def apply_definition_package(
        self,
        definitions: Iterable[AnalyticsDefinition],
        *,
        recipient_inputs: Mapping[
            DefinitionIdentity, tuple[Sequence[str], bytes]
        ] | None = None,
        retirements: Iterable[DefinitionIdentity] = (),
        transaction_time: datetime | None = None,
    ) -> DefinitionPackageResult:
        """Validate fully, then atomically version, activate, and retire a package."""

    def activate_definition_version(
        self, version_id: str, *, transaction_time: datetime | None = None
    ) -> DefinitionChange:
        """Reactivate a retained version without editing its immutable row."""

    def retire_definition(
        self,
        identity: DefinitionIdentity,
        *,
        transaction_time: datetime | None = None,
    ) -> DefinitionActivation:
        """Retire exactly one current activation."""

    def get_current_definition(
        self, identity: DefinitionIdentity
    ) -> tuple[DefinitionVersion, DefinitionActivation] | None:
        """Return sanitized current state for one bounded scoped key."""
