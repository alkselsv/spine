"""Authorized inspection of one exact, published transcript time range."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from spine.application.transcript_publication import (
    TranscriptContextBroker,
    TranscriptContextOutcome,
    TranscriptContextRequest,
    TranscriptAuthorizationSnapshot,
    TranscriptPublicationRepository,
)
from spine.application.parsed_content_inspection import ParsedContentInspectionRequester
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.domain.artifacts import (
    TimeRangeLocator,
    TranscriptPublication,
    TranscriptPublicationState,
    TranscriptProjectionState,
    TranscriptionArtifact,
    TranscriptionArtifactOutcome,
    TranscriptionTimeRangeLocator,
)
from spine.domain.common import DefinitionModel


class TranscriptInspectionStatus(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    SUPERSEDED = "superseded"
    REVIEW_REQUIRED = "review_required"
    PARTIAL = "partial"
    DEGRADED = "degraded"
    PERMISSION_DENIED = "permission_denied"


class TranscriptReviewState(str, Enum):
    APPROVED = "approved"
    NOT_REQUIRED = "not_required"


class TranscriptInspectionResult(DefinitionModel):
    """Safe result; only current authorized results carry transcript text."""

    schema_version: str = "transcript-inspection-result:v1"
    status: TranscriptInspectionStatus
    locator: TimeRangeLocator | None = None
    source_object_id: UUID | None = None
    source_revision_id: UUID | None = None
    transcript_artifact_id: UUID | None = None
    transcript_artifact_digest: str | None = None
    transcript_artifact_schema_version: str | None = None
    publication_id: UUID | None = None
    publication_state: str | None = None
    review_state: TranscriptReviewState | None = None
    partial: bool | None = None
    recording_duration_ms: int | None = Field(default=None, gt=0)
    text: str | None = None

    @model_validator(mode="after")
    def validate_disclosure(self) -> "TranscriptInspectionResult":
        protected = (
            self.locator,
            self.source_object_id,
            self.source_revision_id,
            self.transcript_artifact_id,
            self.transcript_artifact_digest,
            self.transcript_artifact_schema_version,
            self.publication_id,
            self.publication_state,
            self.review_state,
            self.partial,
            self.recording_duration_ms,
            self.text,
        )
        if self.status is TranscriptInspectionStatus.CURRENT:
            if any(value is None for value in protected):
                raise ValueError("current transcript inspection requires exact evidence metadata")
        elif any(value is not None for value in protected):
            raise ValueError("unavailable transcript inspection cannot disclose metadata")
        return self


class TranscriptEvidenceAuthorization(Protocol):
    async def recheck(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        source_revision_id: UUID,
        source_revision_digest: str,
        transcript_artifact_id: UUID,
    ) -> TranscriptAuthorizationSnapshot: ...


@dataclass(frozen=True, slots=True)
class _ScopedTranscriptAuthorization:
    authorization: TranscriptEvidenceAuthorization
    context: TrustedPersistenceContext
    requester: ParsedContentInspectionRequester
    source_revision_digest: str

    async def recheck(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        source_revision_id: UUID,
        transcript_artifact_id: UUID,
    ) -> TranscriptAuthorizationSnapshot:
        return await self.authorization.recheck(
            context=self.context,
            requester=self.requester,
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=source_object_id,
            source_revision_id=source_revision_id,
            source_revision_digest=self.source_revision_digest,
            transcript_artifact_id=transcript_artifact_id,
        )


class TranscriptEvidenceInspector:
    """Revalidate publication and Context Broker authorization on every read."""

    def __init__(
        self,
        *,
        publications: TranscriptPublicationRepository,
        authorization: TranscriptEvidenceAuthorization,
    ) -> None:
        self._publications = publications
        self._authorization = authorization

    async def inspect(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        workspace_id: UUID,
        environment_id: UUID,
        locator: TimeRangeLocator,
    ) -> TranscriptInspectionResult:
        try:
            context.validate_shape()
        except Exception:
            return TranscriptInspectionResult(status=TranscriptInspectionStatus.DEGRADED)
        if (
            not isinstance(context.scope, EnvironmentScope)
            or context.scope.workspace_id != workspace_id
            or context.scope.environment_id != environment_id
            or requester.subject_id != context.acting_subject_id
        ):
            return TranscriptInspectionResult(
                status=TranscriptInspectionStatus.PERMISSION_DENIED
            )
        authorization = await self._authorization.recheck(
            context=context,
            requester=requester,
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=locator.source_object_id,
            source_revision_id=locator.source_revision_id,
            source_revision_digest=locator.source_revision_digest,
            transcript_artifact_id=locator.transcript_artifact_id,
        )
        if not authorization.allowed:
            return TranscriptInspectionResult(
                status=TranscriptInspectionStatus.PERMISSION_DENIED
            )
        if (
            authorization.workspace_id != workspace_id
            or authorization.environment_id != environment_id
            or authorization.source_object_id != locator.source_object_id
        ):
            return TranscriptInspectionResult(status=TranscriptInspectionStatus.DEGRADED)
        if (
            authorization.current_revision_id != locator.source_revision_id
            or authorization.current_revision_digest != locator.source_revision_digest
        ):
            return TranscriptInspectionResult(status=TranscriptInspectionStatus.STALE)
        publication = await self._publications.get_publication(
            workspace_id=workspace_id,
            environment_id=environment_id,
            transcript_artifact_id=locator.transcript_artifact_id,
        )
        if publication is None:
            return TranscriptInspectionResult(
                status=TranscriptInspectionStatus.REVIEW_REQUIRED
            )
        if publication.state is TranscriptPublicationState.SUPERSEDED:
            return TranscriptInspectionResult(status=TranscriptInspectionStatus.SUPERSEDED)
        if publication.state is TranscriptPublicationState.STALE:
            return TranscriptInspectionResult(status=TranscriptInspectionStatus.STALE)
        if publication.state is not TranscriptPublicationState.ACTIVE:
            return TranscriptInspectionResult(status=TranscriptInspectionStatus.DEGRADED)
        if publication.projection_state is not TranscriptProjectionState.CURRENT:
            return TranscriptInspectionResult(status=TranscriptInspectionStatus.DEGRADED)
        if not _publication_matches_locator(publication, workspace_id, environment_id, locator):
            return TranscriptInspectionResult(status=TranscriptInspectionStatus.STALE)

        artifact = await self._publications.get_artifact(
            workspace_id=workspace_id,
            environment_id=environment_id,
            artifact_id=locator.transcript_artifact_id,
        )
        if artifact is None:
            return TranscriptInspectionResult(status=TranscriptInspectionStatus.DEGRADED)
        if (
            artifact.workspace_id != workspace_id
            or artifact.environment_id != environment_id
            or artifact.source_object_id != locator.source_object_id
            or artifact.source_revision_id != locator.source_revision_id
            or artifact.source_revision_digest != locator.source_revision_digest
        ):
            return TranscriptInspectionResult(status=TranscriptInspectionStatus.STALE)
        try:
            artifact.validate_time_range_locator(_artifact_locator(locator, artifact))
        except ValueError:
            return TranscriptInspectionResult(status=TranscriptInspectionStatus.STALE)
        partial = artifact.outcome is TranscriptionArtifactOutcome.PARTIAL
        if partial and not publication.partial_allowed:
            return TranscriptInspectionResult(status=TranscriptInspectionStatus.PARTIAL)

        broker = TranscriptContextBroker(
            repository=self._publications,
            authorization=_ScopedTranscriptAuthorization(
                authorization=self._authorization,
                context=context,
                requester=requester,
                source_revision_digest=locator.source_revision_digest,
            ),
        )
        context_result = await broker.retrieve(
            TranscriptContextRequest(
                request_id=uuid5(NAMESPACE_URL, f"spine:transcript-inspection:{locator.digest}"),
                workspace_id=workspace_id,
                environment_id=environment_id,
                locator=locator,
            )
        )
        if context_result.outcome is TranscriptContextOutcome.DENIED:
            return TranscriptInspectionResult(
                status=TranscriptInspectionStatus.PERMISSION_DENIED
            )
        if context_result.outcome is not TranscriptContextOutcome.ALLOWED:
            return TranscriptInspectionResult(status=TranscriptInspectionStatus.STALE)
        if len(context_result.segments) != 1:
            return TranscriptInspectionResult(status=TranscriptInspectionStatus.DEGRADED)

        return TranscriptInspectionResult(
            status=TranscriptInspectionStatus.CURRENT,
            locator=locator,
            source_object_id=publication.source_object_id,
            source_revision_id=publication.source_revision_id,
            transcript_artifact_id=publication.transcript_artifact_id,
            transcript_artifact_digest=publication.transcript_artifact_digest,
            transcript_artifact_schema_version=publication.transcript_artifact_schema_version,
            publication_id=publication.publication_id,
            publication_state=publication.state.value,
            review_state=(
                TranscriptReviewState.APPROVED
                if publication.approval_id is not None
                else TranscriptReviewState.NOT_REQUIRED
            ),
            partial=partial,
            recording_duration_ms=artifact.recording_duration_ms,
            text=context_result.segments[0].text,
        )


def _publication_matches_locator(
    publication: TranscriptPublication,
    workspace_id: UUID,
    environment_id: UUID,
    locator: TimeRangeLocator,
) -> bool:
    return (
        publication.workspace_id == workspace_id
        and publication.environment_id == environment_id
        and publication.source_object_id == locator.source_object_id
        and publication.source_revision_id == locator.source_revision_id
        and publication.source_revision_digest == locator.source_revision_digest
        and publication.transcript_artifact_id == locator.transcript_artifact_id
        and publication.transcript_artifact_digest == locator.transcript_artifact_digest
        and publication.transcript_artifact_schema_version
        == locator.transcript_artifact_schema_version
        and publication.projection_version == locator.projection_version
    )


def _artifact_locator(
    locator: TimeRangeLocator, artifact: TranscriptionArtifact
) -> TranscriptionTimeRangeLocator:
    """Map the publication locator to the artifact's independently checked locator."""
    return TranscriptionTimeRangeLocator(
        workspace_id=artifact.workspace_id,
        environment_id=artifact.environment_id,
        source_object_id=locator.source_object_id,
        source_revision_id=locator.source_revision_id,
        source_revision_digest=locator.source_revision_digest,
        original_sha256=artifact.original_sha256,
        artifact_id=locator.transcript_artifact_id,
        artifact_digest=locator.transcript_artifact_digest,
        artifact_schema_version=locator.transcript_artifact_schema_version,
        start_ms=locator.start_ms,
        end_ms=locator.end_ms,
    )


__all__ = [
    "TranscriptEvidenceAuthorization",
    "TranscriptEvidenceInspector",
    "TranscriptInspectionResult",
    "TranscriptInspectionStatus",
    "TranscriptReviewState",
]
