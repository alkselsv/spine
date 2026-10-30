"""Immutable human review records and corrected transcript artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, StrictBool, StrictInt, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.artifacts.transcription import (
    SpeechToTextSegment,
    TranscriptSegment,
    TranscriptionArtifact,
    TranscriptionArtifactOutcome,
)


class TranscriptReviewDecision(str, Enum):
    APPROVE = "approve"
    CORRECT = "correct"
    REJECT = "reject"


class TranscriptReviewRecord(DefinitionModel):
    """Append-only decision against one exact ASR artifact version."""

    schema_version: Literal["transcript-review:v1"] = "transcript-review:v1"
    review_id: UUID
    review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=200)
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_artifact_id: UUID
    input_artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_artifact_schema_version: VersionIdentifier
    decision: TranscriptReviewDecision
    rationale: str = Field(min_length=1, max_length=8_000)
    actor_id: UUID
    actor_authority: str = Field(min_length=1, max_length=200)
    policy_reference: VersionIdentifier
    publication_eligible: StrictBool
    correlation_id: UUID
    corrected_artifact_id: UUID | None = None
    decided_at: datetime

    @model_validator(mode="after")
    def validate_record(self) -> "TranscriptReviewRecord":
        if any(
            value.int == 0
            for value in (
                self.review_id,
                self.workspace_id,
                self.environment_id,
                self.source_object_id,
                self.source_revision_id,
                self.input_artifact_id,
                self.actor_id,
                self.correlation_id,
            )
        ):
            raise ValueError("transcript review identifiers must be non-zero")
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise ValueError("transcript review timestamp must be timezone-aware")
        if self.decision is TranscriptReviewDecision.CORRECT:
            if (
                self.corrected_artifact_id is None
                or self.corrected_artifact_id.int == 0
            ):
                raise ValueError("correction review requires a successor artifact")
        elif self.corrected_artifact_id is not None:
            raise ValueError("only correction reviews may have a successor artifact")
        if (
            self.decision is TranscriptReviewDecision.REJECT
            and self.publication_eligible
        ):
            raise ValueError(
                "rejected transcript review cannot be publication eligible"
            )
        expected = _review_digest(
            self.model_dump(mode="json", exclude={"review_digest", "review_id"})
        )
        if expected != self.review_digest:
            raise ValueError("transcript review digest does not match its content")
        return self

    @classmethod
    def create(
        cls,
        *,
        artifact: TranscriptionArtifact,
        review_id: UUID,
        idempotency_key: str,
        decision: TranscriptReviewDecision,
        rationale: str,
        actor_id: UUID,
        actor_authority: str,
        policy_reference: str,
        publication_eligible: bool,
        correlation_id: UUID,
        corrected_artifact_id: UUID | None,
        decided_at: datetime,
    ) -> "TranscriptReviewRecord":
        values = {
            "schema_version": "transcript-review:v1",
            "idempotency_key": idempotency_key,
            "workspace_id": artifact.workspace_id,
            "environment_id": artifact.environment_id,
            "source_object_id": artifact.source_object_id,
            "source_revision_id": artifact.source_revision_id,
            "source_revision_digest": artifact.source_revision_digest,
            "input_artifact_id": artifact.artifact_id,
            "input_artifact_digest": artifact.artifact_digest,
            "input_artifact_schema_version": artifact.schema_version,
            "decision": decision.value,
            "rationale": rationale,
            "actor_id": actor_id,
            "actor_authority": actor_authority,
            "policy_reference": policy_reference,
            "publication_eligible": publication_eligible,
            "correlation_id": correlation_id,
            "corrected_artifact_id": corrected_artifact_id,
            "decided_at": decided_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        digest = _review_digest(values)
        return cls(
            **values,
            review_id=review_id,
            review_digest=digest,
        )


class CorrectedTranscriptArtifact(DefinitionModel):
    """Immutable human correction that preserves the original ASR artifact."""

    schema_version: Literal["human-corrected-transcript:v1"] = (
        "human-corrected-transcript:v1"
    )
    artifact_id: UUID
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    recording_duration_ms: StrictInt = Field(gt=0)
    outcome: TranscriptionArtifactOutcome
    original_artifact_id: UUID
    original_artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_id: UUID
    segments: tuple[TranscriptSegment, ...]
    created_at: datetime

    @model_validator(mode="after")
    def validate_artifact(self) -> "CorrectedTranscriptArtifact":
        if any(
            value.int == 0
            for value in (
                self.artifact_id,
                self.workspace_id,
                self.environment_id,
                self.source_object_id,
                self.source_revision_id,
                self.original_artifact_id,
                self.review_id,
            )
        ):
            raise ValueError("corrected transcript identifiers must be non-zero")
        if tuple(segment.ordinal for segment in self.segments) != tuple(
            range(1, len(self.segments) + 1)
        ):
            raise ValueError("corrected transcript segment ordinals must be contiguous")
        if any(
            segment.end_ms > self.recording_duration_ms for segment in self.segments
        ):
            raise ValueError("corrected transcript segment exceeds recording duration")
        if any(
            segment.segment_id
            != uuid5(
                NAMESPACE_URL,
                f"spine:corrected-transcript-segment:v1:{self.review_id}:{segment.ordinal}",
            )
            for segment in self.segments
        ):
            raise ValueError(
                "corrected transcript segment id does not match review lineage"
            )
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("corrected transcript timestamp must be timezone-aware")
        digest = _corrected_digest(
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            source_object_id=self.source_object_id,
            source_revision_id=self.source_revision_id,
            source_revision_digest=self.source_revision_digest,
            recording_duration_ms=self.recording_duration_ms,
            outcome=self.outcome,
            original_artifact_id=self.original_artifact_id,
            original_artifact_digest=self.original_artifact_digest,
            review_id=self.review_id,
            segments=self.segments,
        )
        if digest != self.artifact_digest:
            raise ValueError("corrected transcript digest does not match its content")
        if self.artifact_id != uuid5(
            NAMESPACE_URL, f"spine:human-corrected-transcript:v1:{digest}"
        ):
            raise ValueError("corrected transcript id does not match its content")
        return self

    @classmethod
    def create(
        cls,
        *,
        original: TranscriptionArtifact,
        review_id: UUID,
        segments: tuple[SpeechToTextSegment, ...],
        created_at: datetime | None = None,
    ) -> "CorrectedTranscriptArtifact":
        corrected_segments = tuple(
            TranscriptSegment(
                segment_id=uuid5(
                    NAMESPACE_URL,
                    f"spine:corrected-transcript-segment:v1:{review_id}:{ordinal}",
                ),
                ordinal=ordinal,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text,
                anonymous_speaker_label=segment.anonymous_speaker_label,
                signals=segment.signals,
            )
            for ordinal, segment in enumerate(segments, start=1)
        )
        digest = _corrected_digest(
            workspace_id=original.workspace_id,
            environment_id=original.environment_id,
            source_object_id=original.source_object_id,
            source_revision_id=original.source_revision_id,
            source_revision_digest=original.source_revision_digest,
            recording_duration_ms=original.recording_duration_ms,
            outcome=original.outcome,
            original_artifact_id=original.artifact_id,
            original_artifact_digest=original.artifact_digest,
            review_id=review_id,
            segments=corrected_segments,
        )
        return cls(
            artifact_id=uuid5(
                NAMESPACE_URL, f"spine:human-corrected-transcript:v1:{digest}"
            ),
            artifact_digest=digest,
            workspace_id=original.workspace_id,
            environment_id=original.environment_id,
            source_object_id=original.source_object_id,
            source_revision_id=original.source_revision_id,
            source_revision_digest=original.source_revision_digest,
            recording_duration_ms=original.recording_duration_ms,
            outcome=original.outcome,
            original_artifact_id=original.artifact_id,
            original_artifact_digest=original.artifact_digest,
            review_id=review_id,
            segments=corrected_segments,
            created_at=created_at or datetime.now(timezone.utc),
        )


def _review_digest(values: dict[str, object]) -> str:
    payload = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _corrected_digest(**values: object) -> str:
    canonical = {
        **values,
        "segments": [segment.model_dump(mode="json") for segment in values["segments"]],  # type: ignore[union-attr]
    }
    return _review_digest(canonical)


__all__ = [
    "CorrectedTranscriptArtifact",
    "TranscriptReviewDecision",
    "TranscriptReviewRecord",
]
