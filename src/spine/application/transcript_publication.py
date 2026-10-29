"""Governed transcript publication and the Context Broker read seam."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.artifacts import (
    TranscriptPublication,
    TranscriptPublicationApproval,
    TranscriptPublicationState,
    TranscriptProjectionSegment,
    TranscriptProjectionState,
    TranscriptionArtifact,
    TranscriptionArtifactOutcome,
    TimeRangeLocator,
)
from spine.domain.common import DefinitionModel, VersionIdentifier


class TranscriptPublicationDeniedError(ValueError):
    """The publication policy or current source authority denied publication."""


class TranscriptContextOutcome(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


class TranscriptAuthorizationSnapshot(DefinitionModel):
    """Opaque current source authorization facts used by publication/read gates."""

    schema_version: str = "transcript-authorization-snapshot:v1"
    allowed: bool
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    current_revision_id: UUID | None = None
    current_revision_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    authorization_generation: int = Field(ge=1)
    reason_code: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_shape(self) -> "TranscriptAuthorizationSnapshot":
        if any(
            value.int == 0
            for value in (
                self.workspace_id,
                self.environment_id,
                self.source_object_id,
            )
        ):
            raise ValueError("transcript authorization scope identifiers must be non-zero")
        if self.allowed and (
            self.current_revision_id is None or self.current_revision_digest is None
        ):
            raise ValueError("allowed transcript authorization requires current revision")
        if self.current_revision_id is not None and self.current_revision_id.int == 0:
            raise ValueError("current transcript revision must be non-zero")
        return self


class TranscriptPublicationDecision(DefinitionModel):
    schema_version: str = "transcript-publication-decision:v1"
    allowed: bool
    policy_reference: VersionIdentifier
    reason_code: str = Field(min_length=1, max_length=64)
    approval_id: UUID | None = None

    @model_validator(mode="after")
    def validate_approval(self) -> "TranscriptPublicationDecision":
        if self.approval_id is not None and self.approval_id.int == 0:
            raise ValueError("transcript publication approval must be non-zero")
        return self


class TranscriptPublicationPolicy(Protocol):
    async def decide(
        self,
        *,
        artifact: TranscriptionArtifact,
        approval: TranscriptPublicationApproval | None,
    ) -> TranscriptPublicationDecision: ...


@dataclass(frozen=True, slots=True)
class ExplicitTranscriptPublicationPolicy:
    """A small injected policy with explicit review and partial-output rules."""

    policy_reference: VersionIdentifier
    require_human_approval: bool = True
    allow_partial: bool = False

    async def decide(
        self,
        *,
        artifact: TranscriptionArtifact,
        approval: TranscriptPublicationApproval | None,
    ) -> TranscriptPublicationDecision:
        if artifact.outcome is TranscriptionArtifactOutcome.PARTIAL and not self.allow_partial:
            return TranscriptPublicationDecision(
                allowed=False,
                policy_reference=self.policy_reference,
                reason_code="partial_disallowed",
            )
        if self.require_human_approval:
            if approval is None:
                return TranscriptPublicationDecision(
                    allowed=False,
                    policy_reference=self.policy_reference,
                    reason_code="approval_required",
                )
            if (
                not approval.approved
                or approval.workspace_id != artifact.workspace_id
                or approval.environment_id != artifact.environment_id
                or approval.policy_reference != self.policy_reference
                or approval.transcript_artifact_id != artifact.artifact_id
                or approval.transcript_artifact_digest != artifact.artifact_digest
            ):
                return TranscriptPublicationDecision(
                    allowed=False,
                    policy_reference=self.policy_reference,
                    reason_code="approval_invalid",
                )
            return TranscriptPublicationDecision(
                allowed=True,
                policy_reference=self.policy_reference,
                reason_code="approved",
                approval_id=approval.approval_id,
            )
        return TranscriptPublicationDecision(
            allowed=True,
            policy_reference=self.policy_reference,
            reason_code="policy_allows",
            approval_id=None,
        )


class TranscriptArtifactRepository(Protocol):
    async def get_artifact(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> TranscriptionArtifact | None: ...


class TranscriptPublicationRepository(TranscriptArtifactRepository, Protocol):
    async def create_or_replay(
        self, publication: TranscriptPublication
    ) -> tuple[TranscriptPublication, bool]: ...

    async def get_publication(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        transcript_artifact_id: UUID,
    ) -> TranscriptPublication | None: ...

    async def transition(
        self,
        *,
        publication_id: UUID,
        workspace_id: UUID,
        environment_id: UUID,
        state: TranscriptPublicationState | None = None,
        projection_state: TranscriptProjectionState | None = None,
    ) -> TranscriptPublication: ...

    async def rebuild_projection(
        self,
        *,
        publication_id: UUID,
        projection_version: VersionIdentifier,
        workspace_id: UUID | None = None,
        environment_id: UUID | None = None,
    ) -> TranscriptPublication: ...

    async def rollback_projection(
        self,
        *,
        publication_id: UUID,
        projection_version: VersionIdentifier,
        workspace_id: UUID | None = None,
        environment_id: UUID | None = None,
    ) -> TranscriptPublication: ...


class InMemoryTranscriptPublicationRepository:
    """Tenant-scoped publication ledger used by application/contract tests."""

    def __init__(self, artifacts: TranscriptArtifactRepository) -> None:
        self._artifacts = artifacts
        self._lock = asyncio.Lock()
        self._current: dict[tuple[UUID, UUID, UUID], TranscriptPublication] = {}
        self._history: list[TranscriptPublication] = []

    async def get_artifact(self, **kwargs: object) -> TranscriptionArtifact | None:
        return await self._artifacts.get_artifact(**kwargs)  # type: ignore[arg-type]

    async def create_or_replay(
        self, publication: TranscriptPublication
    ) -> tuple[TranscriptPublication, bool]:
        key = (
            publication.workspace_id,
            publication.environment_id,
            publication.transcript_artifact_id,
        )
        async with self._lock:
            existing = self._current.get(key)
            if existing is not None:
                if existing.publication_digest != publication.publication_digest:
                    raise ValueError("transcript publication conflicts with history")
                return existing, True
            self._current[key] = publication
            self._history.append(publication)
            return publication, False

    async def get_publication(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        transcript_artifact_id: UUID,
    ) -> TranscriptPublication | None:
        async with self._lock:
            return self._current.get(
                (workspace_id, environment_id, transcript_artifact_id)
            )

    async def transition(
        self,
        *,
        publication_id: UUID,
        workspace_id: UUID,
        environment_id: UUID,
        state: TranscriptPublicationState | None = None,
        projection_state: TranscriptProjectionState | None = None,
    ) -> TranscriptPublication:
        async with self._lock:
            key, current = next(
                (
                    (key, publication)
                    for key, publication in self._current.items()
                    if publication.publication_id == publication_id
                    and publication.workspace_id == workspace_id
                    and publication.environment_id == environment_id
                ),
                (None, None),
            )
            if key is None or current is None:
                raise KeyError("transcript publication is unavailable")
            updated = current.with_state(
                state=state or current.state,
                projection_state=projection_state or current.projection_state,
            )
            self._current[key] = updated
            self._history.append(updated)
            return updated

    async def revoke(self, publication_id: UUID) -> TranscriptPublication:
        current = await self._publication_by_id(publication_id)
        if current is None:
            raise KeyError("transcript publication is unavailable")
        return await self.transition(
            publication_id=publication_id,
            workspace_id=current.workspace_id,
            environment_id=current.environment_id,
            state=TranscriptPublicationState.REVOKED,
        )

    async def mark_projection_stale(self, publication_id: UUID) -> TranscriptPublication:
        current = await self._publication_by_id(publication_id)
        if current is None:
            raise KeyError("transcript publication is unavailable")
        return await self.transition(
            publication_id=publication_id,
            workspace_id=current.workspace_id,
            environment_id=current.environment_id,
            projection_state=TranscriptProjectionState.STALE,
        )

    async def rebuild_projection(
        self,
        *,
        publication_id: UUID,
        projection_version: VersionIdentifier,
        workspace_id: UUID | None = None,
        environment_id: UUID | None = None,
    ) -> TranscriptPublication:
        current = await self._publication_by_id(publication_id)
        if current is None:
            raise KeyError("transcript publication is unavailable")
        successor = current.successor_projection(projection_version=projection_version)
        key = (current.workspace_id, current.environment_id, current.transcript_artifact_id)
        async with self._lock:
            self._current[key] = successor
            self._history.append(successor)
        return successor

    async def rollback_projection(
        self,
        *,
        publication_id: UUID,
        projection_version: VersionIdentifier,
        workspace_id: UUID | None = None,
        environment_id: UUID | None = None,
    ) -> TranscriptPublication:
        return await self.rebuild_projection(
            publication_id=publication_id,
            projection_version=projection_version,
        )

    async def _publication_by_id(
        self, publication_id: UUID
    ) -> TranscriptPublication | None:
        async with self._lock:
            return next(
                (
                    publication
                    for publication in self._current.values()
                    if publication.publication_id == publication_id
                ),
                None,
            )

    async def history_count(self) -> int:
        async with self._lock:
            return len(self._history)


class TranscriptAuthorizationPort(Protocol):
    async def recheck(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        source_revision_id: UUID,
        transcript_artifact_id: UUID,
    ) -> TranscriptAuthorizationSnapshot: ...


@dataclass(frozen=True, slots=True)
class PublishTranscriptCommand:
    workspace_id: UUID
    environment_id: UUID
    transcript_artifact_id: UUID
    approval: TranscriptPublicationApproval | None = None


@dataclass(frozen=True, slots=True)
class TranscriptPublicationResult:
    publication: TranscriptPublication
    replay: bool


@dataclass(frozen=True, slots=True)
class TranscriptPublicationService:
    repository: TranscriptPublicationRepository
    policy: TranscriptPublicationPolicy
    authorization: TranscriptAuthorizationPort
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    projection_version: VersionIdentifier = "transcript-projection:v1"

    async def publish(
        self, command: PublishTranscriptCommand
    ) -> TranscriptPublicationResult:
        artifact = await self.repository.get_artifact(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            artifact_id=command.transcript_artifact_id,
        )
        if artifact is None or (
            artifact.workspace_id,
            artifact.environment_id,
        ) != (command.workspace_id, command.environment_id):
            raise TranscriptPublicationDeniedError("transcript publication unavailable")
        decision = await self.policy.decide(artifact=artifact, approval=command.approval)
        if not decision.allowed:
            raise TranscriptPublicationDeniedError(
                f"transcript publication denied: {decision.reason_code}"
            )
        if decision.approval_id is not None and (
            command.approval is None or command.approval.approval_id != decision.approval_id
        ):
            raise TranscriptPublicationDeniedError("transcript publication approval is invalid")
        authorization = await self.authorization.recheck(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            source_object_id=artifact.source_object_id,
            source_revision_id=artifact.source_revision_id,
            transcript_artifact_id=artifact.artifact_id,
        )
        if not _authorization_matches(authorization, artifact):
            raise TranscriptPublicationDeniedError("transcript source authorization is unavailable")
        publication = TranscriptPublication.create(
            artifact=artifact,
            policy_reference=decision.policy_reference,
            approval_id=decision.approval_id,
            projection_version=self.projection_version,
            published_at=self.clock(),
        )
        stored, replay = await self.repository.create_or_replay(publication)
        return TranscriptPublicationResult(publication=stored, replay=replay)


class TranscriptContextRequest(DefinitionModel):
    schema_version: str = "transcript-context-request:v1"
    request_id: UUID
    workspace_id: UUID
    environment_id: UUID
    locator: TimeRangeLocator

    @model_validator(mode="after")
    def validate_ids(self) -> "TranscriptContextRequest":
        if any(
            value.int == 0
            for value in (self.request_id, self.workspace_id, self.environment_id)
        ):
            raise ValueError("transcript context request identifiers must be non-zero")
        return self


class TranscriptContextResponse(DefinitionModel):
    schema_version: str = "transcript-context-response:v1"
    outcome: TranscriptContextOutcome
    reason_code: str = Field(min_length=1, max_length=64)
    segments: tuple[TranscriptProjectionSegment, ...] = ()
    publication_id: UUID | None = None

    @model_validator(mode="after")
    def validate_disclosure(self) -> "TranscriptContextResponse":
        if self.outcome is not TranscriptContextOutcome.ALLOWED and self.segments:
            raise ValueError("non-allowed transcript response cannot carry segments")
        if self.outcome is TranscriptContextOutcome.ALLOWED and not self.segments:
            raise ValueError("allowed transcript response requires segments")
        if self.publication_id is not None and self.publication_id.int == 0:
            raise ValueError("transcript publication id must be non-zero")
        return self


@dataclass(frozen=True, slots=True)
class TranscriptContextBroker:
    """The only read path that can disclose published transcript text."""

    repository: TranscriptPublicationRepository
    authorization: TranscriptAuthorizationPort

    async def retrieve(self, request: TranscriptContextRequest) -> TranscriptContextResponse:
        publication = await self.repository.get_publication(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            transcript_artifact_id=request.locator.transcript_artifact_id,
        )
        if publication is None or publication.state is not TranscriptPublicationState.ACTIVE:
            return _unavailable("publication_unavailable")
        if publication.projection_state is not TranscriptProjectionState.CURRENT:
            return _unavailable("projection_stale")
        if not _publication_matches_locator(publication, request.locator):
            return _unavailable("locator_unavailable")

        authorization = await self.authorization.recheck(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            source_object_id=publication.source_object_id,
            source_revision_id=publication.source_revision_id,
            transcript_artifact_id=publication.transcript_artifact_id,
        )
        if not authorization.allowed:
            return TranscriptContextResponse(
                outcome=TranscriptContextOutcome.DENIED,
                reason_code="authorization_denied",
            )
        if not _authorization_matches_publication(authorization, publication):
            return TranscriptContextResponse(
                outcome=TranscriptContextOutcome.STALE,
                reason_code="source_revision_stale",
            )

        artifact = await self.repository.get_artifact(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            artifact_id=publication.transcript_artifact_id,
        )
        if artifact is None or artifact.artifact_digest != publication.transcript_artifact_digest:
            return _unavailable("artifact_unavailable")
        if artifact.outcome is TranscriptionArtifactOutcome.PARTIAL and not publication.segments:
            return _unavailable("partial_artifact_unavailable")

        segments = tuple(
            segment
            for segment in publication.segments
            if _segment_matches_locator(segment, request.locator)
        )
        if not segments:
            return _unavailable("locator_unavailable")
        return TranscriptContextResponse(
            outcome=TranscriptContextOutcome.ALLOWED,
            reason_code="allowed",
            segments=segments,
            publication_id=publication.publication_id,
        )


def _authorization_matches(
    authorization: TranscriptAuthorizationSnapshot, artifact: TranscriptionArtifact
) -> bool:
    return bool(
        authorization.allowed
        and authorization.workspace_id == artifact.workspace_id
        and authorization.environment_id == artifact.environment_id
        and authorization.source_object_id == artifact.source_object_id
        and authorization.current_revision_id == artifact.source_revision_id
        and authorization.current_revision_digest == artifact.source_revision_digest
    )


def _authorization_matches_publication(
    authorization: TranscriptAuthorizationSnapshot,
    publication: TranscriptPublication,
) -> bool:
    return bool(
        authorization.workspace_id == publication.workspace_id
        and authorization.environment_id == publication.environment_id
        and authorization.source_object_id == publication.source_object_id
        and authorization.current_revision_id == publication.source_revision_id
        and authorization.current_revision_digest == publication.source_revision_digest
    )


def _publication_matches_locator(
    publication: TranscriptPublication, locator: TimeRangeLocator
) -> bool:
    return bool(
        locator.source_object_id == publication.source_object_id
        and locator.source_revision_id == publication.source_revision_id
        and locator.source_revision_digest == publication.source_revision_digest
        and locator.transcript_artifact_digest == publication.transcript_artifact_digest
        and locator.transcript_artifact_schema_version
        == publication.transcript_artifact_schema_version
        and locator.end_ms > locator.start_ms
    )


def _segment_matches_locator(
    segment: TranscriptProjectionSegment, locator: TimeRangeLocator
) -> bool:
    if locator.segment_ids and segment.segment_id not in locator.segment_ids:
        return False
    return segment.locator.start_ms < locator.end_ms and segment.locator.end_ms > locator.start_ms


def _unavailable(reason_code: str) -> TranscriptContextResponse:
    return TranscriptContextResponse(
        outcome=TranscriptContextOutcome.UNAVAILABLE,
        reason_code=reason_code,
    )


__all__ = [
    "ExplicitTranscriptPublicationPolicy",
    "InMemoryTranscriptPublicationRepository",
    "PublishTranscriptCommand",
    "TranscriptArtifactRepository",
    "TranscriptAuthorizationPort",
    "TranscriptAuthorizationSnapshot",
    "TranscriptContextBroker",
    "TranscriptContextOutcome",
    "TranscriptContextRequest",
    "TranscriptContextResponse",
    "TranscriptPublicationDecision",
    "TranscriptPublicationDeniedError",
    "TranscriptPublicationPolicy",
    "TranscriptPublicationRepository",
    "TranscriptPublicationResult",
    "TranscriptPublicationService",
]
