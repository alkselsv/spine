"""Authorized, idempotent review of one immutable transcript artifact."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from spine.domain.artifacts import (
    CorrectedTranscriptArtifact,
    SpeechToTextSegment,
    TranscriptReviewDecision,
    TranscriptReviewRecord,
    TranscriptionArtifact,
)
from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.artifacts import TranscriptPublicationApproval
from spine.application.parsed_content_inspection import ParsedContentInspectionRequester
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.transcript_publication import (
    ExplicitTranscriptPublicationPolicy,
    PublishTranscriptCommand,
    TranscriptPublicationDeniedError,
    TranscriptPublicationRepository,
    TranscriptPublicationService,
    TranscriptPublicationState,
    TranscriptProjectionState,
    TranscriptAuthorizationSnapshot,
)


class TranscriptReviewError(ValueError):
    """Base class for stable review command failures."""


class TranscriptReviewStaleError(TranscriptReviewError):
    """The requested digest does not identify the current exact artifact."""


class TranscriptReviewConflictError(TranscriptReviewError):
    """A different review already owns the exact artifact version."""


class TranscriptReviewReplayConflictError(TranscriptReviewError):
    """An idempotency key was reused with a different command."""


class TranscriptReviewAuthorizationError(TranscriptReviewError):
    """The reviewer lacks current authority to inspect the source revision."""


class TranscriptReviewPolicyOutcome(str, Enum):
    ALLOW = "allow"
    REQUIRE = "require"
    DENY = "deny"


class TranscriptReviewPolicyDecision(DefinitionModel):
    schema_version: Literal["transcript-review-policy-decision:v1"] = (
        "transcript-review-policy-decision:v1"
    )
    outcome: TranscriptReviewPolicyOutcome
    policy_reference: VersionIdentifier
    reason_code: str = Field(min_length=1, max_length=64)


class TranscriptReviewPolicy(Protocol):
    async def decide(
        self,
        *,
        artifact: TranscriptionArtifact,
        decision: TranscriptReviewDecision,
        corrected_artifact: CorrectedTranscriptArtifact | None,
    ) -> TranscriptReviewPolicyDecision: ...


class TranscriptReviewAuthorization(Protocol):
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


class TranscriptReviewArtifactStatus(str, Enum):
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    CORRECTED = "corrected"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    STALE = "stale"
    PERMISSION_DENIED = "permission_denied"
    UNAVAILABLE = "unavailable"


class TranscriptReviewInspection(DefinitionModel):
    schema_version: Literal["transcript-review-inspection:v1"] = (
        "transcript-review-inspection:v1"
    )
    status: TranscriptReviewArtifactStatus
    artifact: TranscriptionArtifact | None = None
    review: TranscriptReviewRecord | None = None
    corrected_artifact: CorrectedTranscriptArtifact | None = None

    @model_validator(mode="after")
    def validate_disclosure(self) -> "TranscriptReviewInspection":
        if self.status in {
            TranscriptReviewArtifactStatus.STALE,
            TranscriptReviewArtifactStatus.PERMISSION_DENIED,
            TranscriptReviewArtifactStatus.UNAVAILABLE,
        } and any((self.artifact, self.review, self.corrected_artifact)):
            raise ValueError("unavailable transcript review inspection cannot disclose data")
        if (
            self.status is TranscriptReviewArtifactStatus.REVIEW_REQUIRED
            and self.artifact is None
        ):
            raise ValueError("review-required inspection needs the exact ASR artifact")
        if (
            self.status is TranscriptReviewArtifactStatus.CORRECTED
            and self.corrected_artifact is None
        ):
            raise ValueError("corrected inspection needs the successor artifact")
        return self


@dataclass(frozen=True, slots=True)
class ExplicitTranscriptReviewPolicy:
    """Injected policy mode; the review service itself has no mandatory-review rule."""

    policy_reference: VersionIdentifier
    outcome: TranscriptReviewPolicyOutcome

    async def decide(
        self,
        *,
        artifact: TranscriptionArtifact,
        decision: TranscriptReviewDecision,
        corrected_artifact: CorrectedTranscriptArtifact | None,
    ) -> TranscriptReviewPolicyDecision:
        del artifact, corrected_artifact
        return TranscriptReviewPolicyDecision(
            outcome=self.outcome,
            policy_reference=self.policy_reference,
            reason_code=(
                "review_rejected"
                if decision is TranscriptReviewDecision.REJECT
                else f"policy_{self.outcome.value}"
            ),
        )


class TranscriptReviewRepository(Protocol):
    async def get_artifact(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> TranscriptionArtifact | None: ...

    async def get_review(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> TranscriptReviewRecord | None: ...

    async def get_corrected_artifact(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> CorrectedTranscriptArtifact | None: ...

    async def create_or_replay(
        self,
        *,
        review: TranscriptReviewRecord,
        corrected_artifact: CorrectedTranscriptArtifact | None,
        command_digest: str,
    ) -> tuple[TranscriptReviewRecord, CorrectedTranscriptArtifact | None, bool]: ...


class InMemoryTranscriptReviewRepository:
    """Atomic fake repository implementing tenant-scoped replay and concurrency."""

    def __init__(self, artifacts: object) -> None:
        self._artifacts = artifacts
        self._lock = asyncio.Lock()
        self._reviews: dict[
            tuple[UUID, UUID, UUID],
            tuple[TranscriptReviewRecord, CorrectedTranscriptArtifact | None, str],
        ] = {}
        self._keys: dict[
            tuple[UUID, UUID, UUID, str],
            tuple[TranscriptReviewRecord, CorrectedTranscriptArtifact | None, str],
        ] = {}

    async def get_artifact(self, **kwargs: object) -> TranscriptionArtifact | None:
        return await self._artifacts.get_artifact(**kwargs)  # type: ignore[attr-defined]

    async def get_review(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> TranscriptReviewRecord | None:
        async with self._lock:
            value = self._reviews.get((workspace_id, environment_id, artifact_id))
            return value[0] if value is not None else None

    async def get_corrected_artifact(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> CorrectedTranscriptArtifact | None:
        async with self._lock:
            for (
                stored_workspace,
                stored_environment,
                _,
            ), value in self._reviews.items():
                corrected = value[1]
                if (
                    corrected is not None
                    and (stored_workspace, stored_environment)
                    == (workspace_id, environment_id)
                    and corrected.artifact_id == artifact_id
                ):
                    return corrected
        return None

    async def create_or_replay(
        self,
        *,
        review: TranscriptReviewRecord,
        corrected_artifact: CorrectedTranscriptArtifact | None,
        command_digest: str,
    ) -> tuple[TranscriptReviewRecord, CorrectedTranscriptArtifact | None, bool]:
        artifact_key = (
            review.workspace_id,
            review.environment_id,
            review.input_artifact_id,
        )
        key_key = (*artifact_key[:2], review.actor_id, review.idempotency_key)
        async with self._lock:
            keyed = self._keys.get(key_key)
            if keyed is not None:
                if keyed[2] != command_digest:
                    raise TranscriptReviewReplayConflictError(
                        "review idempotency key conflicts"
                    )
                return keyed[0], keyed[1], True
            existing = self._reviews.get(artifact_key)
            if existing is not None:
                raise TranscriptReviewConflictError(
                    "transcript artifact has already been reviewed"
                )
            value = (review, corrected_artifact, command_digest)
            self._reviews[artifact_key] = value
            self._keys[key_key] = value
            return review, corrected_artifact, False


@dataclass(frozen=True, slots=True)
class ReviewTranscriptCommand:
    workspace_id: UUID
    environment_id: UUID
    transcript_artifact_id: UUID
    expected_artifact_digest: str
    decision: TranscriptReviewDecision
    rationale: str
    actor_id: UUID
    actor_authority: str
    idempotency_key: str
    correlation_id: UUID
    corrected_segments: tuple[SpeechToTextSegment, ...] = ()


@dataclass(frozen=True, slots=True)
class TranscriptReviewResult:
    review: TranscriptReviewRecord
    corrected_artifact: CorrectedTranscriptArtifact | None
    replay: bool


class TranscriptReviewPublicationPort(Protocol):
    async def apply(
        self,
        *,
        review: TranscriptReviewRecord,
        corrected_artifact: CorrectedTranscriptArtifact | None,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class TranscriptReviewPublicationGateway:
    """Apply review eligibility to the current transcript publication."""

    repository: TranscriptPublicationRepository
    authorization: TranscriptReviewAuthorization
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def apply(
        self,
        *,
        review: TranscriptReviewRecord,
        corrected_artifact: CorrectedTranscriptArtifact | None,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
    ) -> None:
        previous = await self.repository.get_publication(
            workspace_id=review.workspace_id,
            environment_id=review.environment_id,
            transcript_artifact_id=review.input_artifact_id,
        )
        if not review.publication_eligible:
            if (
                previous is not None
                and previous.state is TranscriptPublicationState.ACTIVE
            ):
                await self.repository.transition(
                    publication_id=previous.publication_id,
                    workspace_id=review.workspace_id,
                    environment_id=review.environment_id,
                    state=TranscriptPublicationState.REVOKED,
                    projection_state=TranscriptProjectionState.STALE,
                )
            return
        if review.decision is TranscriptReviewDecision.CORRECT:
            if corrected_artifact is None:
                raise TranscriptReviewError("correction artifact is unavailable")
            if (
                previous is not None
                and previous.state is TranscriptPublicationState.ACTIVE
            ):
                await self.repository.transition(
                    publication_id=previous.publication_id,
                    workspace_id=review.workspace_id,
                    environment_id=review.environment_id,
                    state=TranscriptPublicationState.SUPERSEDED,
                    projection_state=TranscriptProjectionState.STALE,
                )
            artifact_id = corrected_artifact.artifact_id
            artifact_digest = corrected_artifact.artifact_digest
            approver_id = review.actor_id
        else:
            # An already active exact artifact is current and needs no duplicate projection.
            if previous is not None and previous.state is TranscriptPublicationState.ACTIVE:
                return
            artifact_id = review.input_artifact_id
            artifact_digest = review.input_artifact_digest
            approver_id = review.actor_id
        approval = TranscriptPublicationApproval(
            approval_id=review.review_id,
            workspace_id=review.workspace_id,
            environment_id=review.environment_id,
            transcript_artifact_id=artifact_id,
            transcript_artifact_digest=artifact_digest,
            policy_reference=review.policy_reference,
            approved=True,
            approver_id=approver_id,
            decided_at=review.decided_at,
        )
        service = TranscriptPublicationService(
            repository=self.repository,
            policy=ExplicitTranscriptPublicationPolicy(
                policy_reference=review.policy_reference,
                require_human_approval=True,
                allow_partial=False,
            ),
            authorization=_ScopedTranscriptPublicationAuthorization(
                authorization=self.authorization,
                context=context,
                requester=requester,
                source_revision_digest=review.source_revision_digest,
            ),
            clock=self.clock,
        )
        try:
            await service.publish(
                PublishTranscriptCommand(
                    workspace_id=review.workspace_id,
                    environment_id=review.environment_id,
                    transcript_artifact_id=artifact_id,
                    approval=approval,
                )
            )
        except TranscriptPublicationDeniedError as error:
            raise TranscriptReviewError(
                "reviewed transcript is not publication eligible"
            ) from error


@dataclass(frozen=True, slots=True)
class _ScopedTranscriptPublicationAuthorization:
    authorization: TranscriptReviewAuthorization
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


class TranscriptReviewService:
    def __init__(
        self,
        *,
        repository: TranscriptReviewRepository,
        policy: TranscriptReviewPolicy,
        authorization: TranscriptReviewAuthorization | None = None,
        publication_gateway: TranscriptReviewPublicationPort | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        identifier_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._authorization = authorization
        self._publication_gateway = publication_gateway
        self._clock = clock
        self._identifier_factory = identifier_factory

    async def review(
        self,
        command: ReviewTranscriptCommand,
        *,
        context: TrustedPersistenceContext | None = None,
        requester: ParsedContentInspectionRequester | None = None,
    ) -> TranscriptReviewResult:
        artifact = await self._repository.get_artifact(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            artifact_id=command.transcript_artifact_id,
        )
        if artifact is None:
            raise TranscriptReviewStaleError("transcript artifact is unavailable")
        if artifact.artifact_digest != command.expected_artifact_digest:
            raise TranscriptReviewStaleError("transcript artifact version is stale")
        await self._authorize_artifact(
            artifact=artifact,
            context=context,
            requester=requester,
        )
        if command.decision is TranscriptReviewDecision.CORRECT:
            if not command.corrected_segments:
                raise TranscriptReviewError(
                    "correction must include at least one segment"
                )
            corrections = {
                (segment.start_ms, segment.end_ms): segment
                for segment in command.corrected_segments
            }
            if len(corrections) != len(command.corrected_segments):
                raise TranscriptReviewError("correction segment ranges must be unique")
            original_by_range = {
                (segment.start_ms, segment.end_ms): segment
                for segment in artifact.segments
            }
            if not corrections.keys() <= original_by_range.keys():
                raise TranscriptReviewError(
                    "correction must target exact existing segment ranges"
                )
        elif command.corrected_segments:
            raise TranscriptReviewError(
                "only correction decisions accept corrected segments"
            )

        review_id = self._identifier_factory()
        corrected_artifact = (
            CorrectedTranscriptArtifact.create(
                original=artifact,
                review_id=review_id,
                segments=tuple(
                    SpeechToTextSegment(
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                        text=corrections.get(
                            (segment.start_ms, segment.end_ms), segment
                        ).text,
                        anonymous_speaker_label=segment.anonymous_speaker_label,
                        signals=segment.signals,
                    )
                    for segment in artifact.segments
                ),
                created_at=self._clock(),
            )
            if command.decision is TranscriptReviewDecision.CORRECT
            else None
        )
        policy = await self._policy.decide(
            artifact=artifact,
            decision=command.decision,
            corrected_artifact=corrected_artifact,
        )
        review = TranscriptReviewRecord.create(
            artifact=artifact,
            review_id=review_id,
            idempotency_key=command.idempotency_key,
            decision=command.decision,
            rationale=command.rationale,
            actor_id=command.actor_id,
            actor_authority=command.actor_authority,
            policy_reference=policy.policy_reference,
            publication_eligible=(
                policy.outcome is not TranscriptReviewPolicyOutcome.DENY
                and command.decision is not TranscriptReviewDecision.REJECT
            ),
            correlation_id=command.correlation_id,
            corrected_artifact_id=(
                corrected_artifact.artifact_id if corrected_artifact else None
            ),
            decided_at=self._clock(),
        )
        command_digest = _command_digest(
            command, policy_reference=policy.policy_reference
        )
        stored_review, stored_corrected, replay = (
            await self._repository.create_or_replay(
                review=review,
                corrected_artifact=corrected_artifact,
                command_digest=command_digest,
            )
        )
        if self._publication_gateway is not None:
            if context is None or requester is None:
                raise TranscriptReviewAuthorizationError(
                    "publication requires current reviewer authority"
                )
            await self._publication_gateway.apply(
                review=stored_review,
                corrected_artifact=stored_corrected,
                context=context,
                requester=requester,
            )
        return TranscriptReviewResult(stored_review, stored_corrected, replay)

    async def inspect(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        artifact_id: UUID,
        expected_artifact_digest: str,
        context: TrustedPersistenceContext | None = None,
        requester: ParsedContentInspectionRequester | None = None,
    ) -> TranscriptReviewInspection:
        artifact = await self._repository.get_artifact(
            workspace_id=workspace_id,
            environment_id=environment_id,
            artifact_id=artifact_id,
        )
        if artifact is None:
            return TranscriptReviewInspection(
                status=TranscriptReviewArtifactStatus.UNAVAILABLE
            )
        if artifact.artifact_digest != expected_artifact_digest:
            return TranscriptReviewInspection(status=TranscriptReviewArtifactStatus.STALE)
        try:
            await self._authorize_artifact(
                artifact=artifact,
                context=context,
                requester=requester,
            )
        except TranscriptReviewAuthorizationError:
            return TranscriptReviewInspection(
                status=TranscriptReviewArtifactStatus.PERMISSION_DENIED
            )
        except TranscriptReviewStaleError:
            return TranscriptReviewInspection(status=TranscriptReviewArtifactStatus.STALE)

        review = await self._repository.get_review(
            workspace_id=workspace_id,
            environment_id=environment_id,
            artifact_id=artifact_id,
        )
        if review is None:
            return TranscriptReviewInspection(
                status=TranscriptReviewArtifactStatus.REVIEW_REQUIRED,
                artifact=artifact,
            )
        if review.decision is TranscriptReviewDecision.REJECT:
            return TranscriptReviewInspection(
                status=TranscriptReviewArtifactStatus.REJECTED,
                review=review,
            )
        if review.decision is TranscriptReviewDecision.CORRECT:
            corrected = await self._repository.get_corrected_artifact(
                workspace_id=workspace_id,
                environment_id=environment_id,
                artifact_id=review.corrected_artifact_id,
            )
            return TranscriptReviewInspection(
                status=TranscriptReviewArtifactStatus.SUPERSEDED,
                review=review,
                corrected_artifact=corrected,
            )
        return TranscriptReviewInspection(
            status=TranscriptReviewArtifactStatus.APPROVED,
            artifact=artifact,
            review=review,
        )

    async def _authorize_artifact(
        self,
        *,
        artifact: TranscriptionArtifact,
        context: TrustedPersistenceContext | None,
        requester: ParsedContentInspectionRequester | None,
    ) -> None:
        if self._authorization is None:
            return
        if context is None or requester is None:
            raise TranscriptReviewAuthorizationError("review authority is unavailable")
        try:
            context.validate_shape()
        except Exception as error:
            raise TranscriptReviewAuthorizationError(
                "review authority is unavailable"
            ) from error
        if (
            not isinstance(context.scope, EnvironmentScope)
            or context.scope.workspace_id != artifact.workspace_id
            or context.scope.environment_id != artifact.environment_id
            or requester.subject_id != context.acting_subject_id
        ):
            raise TranscriptReviewAuthorizationError("review authority is unavailable")
        snapshot = await self._authorization.recheck(
            context=context,
            requester=requester,
            workspace_id=artifact.workspace_id,
            environment_id=artifact.environment_id,
            source_object_id=artifact.source_object_id,
            source_revision_id=artifact.source_revision_id,
            source_revision_digest=artifact.source_revision_digest,
            transcript_artifact_id=artifact.artifact_id,
        )
        if not snapshot.allowed:
            raise TranscriptReviewAuthorizationError("review source access was revoked")
        if (
            snapshot.workspace_id != artifact.workspace_id
            or snapshot.environment_id != artifact.environment_id
            or snapshot.source_object_id != artifact.source_object_id
        ):
            raise TranscriptReviewAuthorizationError("review source authority is invalid")
        if (
            snapshot.current_revision_id != artifact.source_revision_id
            or snapshot.current_revision_digest != artifact.source_revision_digest
        ):
            raise TranscriptReviewStaleError("transcript source revision is stale")


def _command_digest(command: ReviewTranscriptCommand, *, policy_reference: str) -> str:
    payload = {
        "workspace_id": str(command.workspace_id),
        "environment_id": str(command.environment_id),
        "transcript_artifact_id": str(command.transcript_artifact_id),
        "expected_artifact_digest": command.expected_artifact_digest,
        "decision": command.decision.value,
        "rationale": command.rationale,
        "actor_id": str(command.actor_id),
        "actor_authority": command.actor_authority,
        "idempotency_key": command.idempotency_key,
        "corrected_segments": [
            segment.model_dump(mode="json") for segment in command.corrected_segments
        ],
        "policy_reference": policy_reference,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "ExplicitTranscriptReviewPolicy",
    "InMemoryTranscriptReviewRepository",
    "ReviewTranscriptCommand",
    "TranscriptReviewConflictError",
    "TranscriptReviewError",
    "TranscriptReviewPolicy",
    "TranscriptReviewPolicyDecision",
    "TranscriptReviewPolicyOutcome",
    "TranscriptReviewPublicationGateway",
    "TranscriptReviewPublicationPort",
    "TranscriptReviewReplayConflictError",
    "TranscriptReviewResult",
    "TranscriptReviewService",
    "TranscriptReviewStaleError",
]
