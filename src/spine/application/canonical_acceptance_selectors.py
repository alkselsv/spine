"""Server-side resolution of persisted canonical acceptance selectors."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from spine.domain.sources.canonical_acceptance_selector import (
    CanonicalAcceptanceSelectorHead,
    TrustedCanonicalAcceptanceSelector,
    TrustedSourceSelectorContext,
    selector_applies,
)


class CanonicalAcceptanceSelectorError(RuntimeError):
    """A selector is missing, stale, unsupported, or ambiguous."""


class CanonicalAcceptanceSelectorReader(Protocol):
    async def list_current(
        self, *, workspace_id: UUID, environment_id: UUID
    ) -> tuple[tuple[TrustedCanonicalAcceptanceSelector, CanonicalAcceptanceSelectorHead], ...]: ...


class TrustedCanonicalSourceContextReader(Protocol):
    """Load authority facts from persisted source and provenance records."""

    async def get_source_context(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        source_revision_id: UUID,
    ) -> TrustedSourceSelectorContext | None: ...


class ResolvedCanonicalAcceptanceSelector:
    """Exact selector and current-head evidence selected for one source."""

    __slots__ = ("selector", "head")

    def __init__(
        self,
        selector: TrustedCanonicalAcceptanceSelector,
        head: CanonicalAcceptanceSelectorHead,
    ) -> None:
        self.selector = selector
        self.head = head


class TrustedCanonicalAcceptanceSelectorResolver:
    """Resolve only persisted source authority; callers cannot choose a version."""

    def __init__(
        self,
        reader: CanonicalAcceptanceSelectorReader,
        source_reader: TrustedCanonicalSourceContextReader,
    ) -> None:
        self._reader = reader
        self._source_reader = source_reader

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        source_revision_id: UUID,
    ) -> ResolvedCanonicalAcceptanceSelector:
        source = await self._source_reader.get_source_context(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=source_object_id,
            source_revision_id=source_revision_id,
        )
        if source is None or (
            source.workspace_id != workspace_id
            or source.environment_id != environment_id
            or source.source_object_id != source_object_id
        ):
            raise CanonicalAcceptanceSelectorError(
                "Persisted canonical source authority is unavailable."
            )
        current = await self._reader.list_current(
            workspace_id=workspace_id,
            environment_id=environment_id,
        )
        # Match the non-policy portion first. A policy mismatch on an otherwise
        # applicable authority is stale and must deny rather than look missing.
        authority_matches = tuple(
            pair for pair in current if _authority_applies(pair[0], source)
        )
        if len(authority_matches) != 1:
            raise CanonicalAcceptanceSelectorError(
                "Canonical acceptance selector is missing or ambiguous."
            )
        selector, head = authority_matches[0]
        if (
            head.workspace_id != source.workspace_id
            or head.environment_id != source.environment_id
            or head.selector_series_id != selector.selector_series_id
            or head.current_selector_id != selector.selector_id
            or head.cas_version != selector.selector_version
        ):
            raise CanonicalAcceptanceSelectorError(
                "Canonical acceptance selector head is stale."
            )
        if not selector_applies(selector, source):
            raise CanonicalAcceptanceSelectorError(
                "Canonical acceptance selector authority is stale."
            )
        if selector.required_canonical_acceptance_schema_version not in (1, 2):
            raise CanonicalAcceptanceSelectorError(
                "Canonical acceptance schema version is unsupported."
            )
        return ResolvedCanonicalAcceptanceSelector(selector, head)


def _authority_applies(
    selector: TrustedCanonicalAcceptanceSelector,
    source: TrustedSourceSelectorContext,
) -> bool:
    return selector.authority_identity == source.authority_identity


__all__ = [
    "CanonicalAcceptanceSelectorError",
    "CanonicalAcceptanceSelectorReader",
    "ResolvedCanonicalAcceptanceSelector",
    "TrustedCanonicalSourceContextReader",
    "TrustedCanonicalAcceptanceSelectorResolver",
]
