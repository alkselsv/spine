"""Authorized resolution of transcription time ranges to immutable audio originals."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spine.domain.artifacts.transcription import (
    TranscriptionArtifact,
    TranscriptionTimeRangeLocator,
)
from spine.domain.sources import RevisionKind, SourceRevision
from spine.domain.sources.original_reference import ObjectReference


class TranscriptionEvidenceResolutionError(Exception):
    """A locator cannot be resolved without weakening immutable lineage."""


class AudioEvidenceAccessDeniedError(Exception):
    """Current policy denied access before any audio reference was disclosed."""


class AudioEvidenceAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    source_revision_id: UUID
    artifact_id: UUID
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class AudioEvidenceAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    decision_id: UUID
    policy_reference: str = Field(min_length=1, max_length=128)
    reason_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_.:-]{0,63}$",
    )

    @model_validator(mode="after")
    def validate_decision(self) -> "AudioEvidenceAuthorization":
        if self.decision_id.int == 0:
            raise ValueError("evidence authorization decision id must be non-zero")
        if self.allowed != (self.reason_code == "allowed"):
            raise ValueError("evidence authorization outcome and reason do not match")
        return self


class AudioEvidenceAuthorizer(Protocol):
    async def authorize(
        self, request: AudioEvidenceAccessRequest
    ) -> AudioEvidenceAuthorization: ...


class AudioRevisionResolver(Protocol):
    async def resolve_revision(
        self, *, workspace_id: UUID, environment_id: UUID, revision_id: UUID
    ) -> SourceRevision | None: ...


class TranscriptionArtifactResolver(Protocol):
    async def get_artifact(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> TranscriptionArtifact | None: ...


class ResolvedAudioTimeRange(BaseModel):
    """Authorized navigation target on the immutable original recording."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    locator: TranscriptionTimeRangeLocator
    authorization: AudioEvidenceAuthorization
    original_reference: ObjectReference
    media_type: str
    recording_duration_ms: int = Field(gt=0)


class TranscriptionEvidenceResolver:
    def __init__(
        self,
        *,
        artifacts: TranscriptionArtifactResolver,
        revisions: AudioRevisionResolver,
        authorizer: AudioEvidenceAuthorizer,
    ) -> None:
        self._artifacts = artifacts
        self._revisions = revisions
        self._authorizer = authorizer

    async def resolve(
        self, locator: TranscriptionTimeRangeLocator
    ) -> ResolvedAudioTimeRange:
        authorization = await self._authorizer.authorize(
            AudioEvidenceAccessRequest(
                workspace_id=locator.workspace_id,
                environment_id=locator.environment_id,
                source_revision_id=locator.source_revision_id,
                artifact_id=locator.artifact_id,
                start_ms=locator.start_ms,
                end_ms=locator.end_ms,
            )
        )
        if not authorization.allowed:
            raise AudioEvidenceAccessDeniedError(
                "audio evidence access is not authorized"
            )
        artifact = await self._artifacts.get_artifact(
            workspace_id=locator.workspace_id,
            environment_id=locator.environment_id,
            artifact_id=locator.artifact_id,
        )
        if artifact is None:
            raise TranscriptionEvidenceResolutionError(
                "transcription artifact is unavailable"
            )
        try:
            artifact.validate_time_range_locator(locator)
        except ValueError as error:
            raise TranscriptionEvidenceResolutionError(str(error)) from None
        revision = await self._revisions.resolve_revision(
            workspace_id=locator.workspace_id,
            environment_id=locator.environment_id,
            revision_id=locator.source_revision_id,
        )
        if (
            revision is None
            or revision.kind is not RevisionKind.CONTENT
            or revision.workspace_id != locator.workspace_id
            or revision.environment_id != locator.environment_id
            or revision.source_object_id != locator.source_object_id
            or revision.revision_id != locator.source_revision_id
            or revision.revision_digest != locator.source_revision_digest
            or revision.original_sha256 != locator.original_sha256
            or revision.original_reference is None
            or revision.media_type is None
        ):
            raise TranscriptionEvidenceResolutionError(
                "time-range locator source lineage is stale or mismatched"
            )
        return ResolvedAudioTimeRange(
            locator=locator,
            authorization=authorization,
            original_reference=revision.original_reference,
            media_type=revision.media_type,
            recording_duration_ms=artifact.recording_duration_ms,
        )


__all__ = [
    "AudioEvidenceAccessDeniedError",
    "AudioEvidenceAccessRequest",
    "AudioEvidenceAuthorization",
    "AudioEvidenceAuthorizer",
    "AudioRevisionResolver",
    "ResolvedAudioTimeRange",
    "TranscriptionArtifactResolver",
    "TranscriptionEvidenceResolutionError",
    "TranscriptionEvidenceResolver",
]
