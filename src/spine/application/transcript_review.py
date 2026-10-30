"""Authorized, idempotent review of one immutable transcript artifact."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import Field

from spine.domain.artifacts import (
    CorrectedTranscriptArtifact,
    SpeechToTextSegment,
    TranscriptReviewDecision,
    TranscriptReviewRecord,
    TranscriptionArtifact,
)
from spine.domain.common import DefinitionModel, VersionIdentifier


class TranscriptReviewError(ValueError):
    """Base class for stable review command failures."""


class TranscriptReviewStaleError(TranscriptReviewError):
    """The requested digest does not identify the current exact artifact."""


class TranscriptReviewConflictError(TranscriptReviewError):
    """A different review already owns the exact artifact version."""


class TranscriptReviewReplayConflictError(TranscriptReviewError):
    """An idempotency key was reused with a different command."""


class TranscriptReviewPolicyOutcome(str, Enum):
    ALLOW = "allow"
    REQUIRE = "require"
    DENY = "deny"


class TranscriptReviewPolicyDecision(DefinitionModel):
    schema_version: str = "transcript-review-policy-decision:v1"
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


class TranscriptReviewService:
    def __init__(
        self,
        *,
        repository: TranscriptReviewRepository,
        policy: TranscriptReviewPolicy,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        identifier_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._clock = clock
        self._identifier_factory = identifier_factory

    async def review(self, command: ReviewTranscriptCommand) -> TranscriptReviewResult:
        artifact = await self._repository.get_artifact(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            artifact_id=command.transcript_artifact_id,
        )
        if artifact is None:
            raise TranscriptReviewStaleError("transcript artifact is unavailable")
        if artifact.artifact_digest != command.expected_artifact_digest:
            raise TranscriptReviewStaleError("transcript artifact version is stale")
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
        return TranscriptReviewResult(stored_review, stored_corrected, replay)


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
    "TranscriptReviewReplayConflictError",
    "TranscriptReviewResult",
    "TranscriptReviewService",
    "TranscriptReviewStaleError",
]
