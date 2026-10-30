from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.domain.artifacts import (
    CorrectedTranscriptArtifact,
    SpeechToTextSegment,
    TranscriptReviewDecision,
    TranscriptReviewRecord,
    TranscriptionArtifact,
)

WORKSPACE = UUID("10000000-0000-0000-0000-000000000001")
ENVIRONMENT = UUID("20000000-0000-0000-0000-000000000001")
SOURCE = UUID("30000000-0000-0000-0000-000000000001")
REVISION = UUID("40000000-0000-0000-0000-000000000001")
JOB = UUID("50000000-0000-0000-0000-000000000001")
ACTOR = UUID("60000000-0000-0000-0000-000000000001")
CORRELATION = UUID("70000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def artifact() -> TranscriptionArtifact:
    return TranscriptionArtifact.create(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        source_revision_digest="a" * 64,
        original_sha256="b" * 64,
        processing_job_id=JOB,
        processing_profile_reference="transcription:v1",
        adapter_reference="fake-stt:v1",
        model_config_reference="fake-model:v1",
        recording_duration_ms=10_000,
        language="ru",
        segments=(SpeechToTextSegment(start_ms=100, end_ms=200, text="исправить"),),
    )


def record(
    transcript: TranscriptionArtifact,
    *,
    corrected_id: UUID | None = None,
    review_id: UUID = UUID("80000000-0000-0000-0000-000000000001"),
):
    return TranscriptReviewRecord.create(
        artifact=transcript,
        review_id=review_id,
        idempotency_key="review-command-1",
        decision=(
            TranscriptReviewDecision.CORRECT
            if corrected_id
            else TranscriptReviewDecision.APPROVE
        ),
        rationale="Сверено с записью",
        actor_id=ACTOR,
        actor_authority="transcript_reviewer:v1",
        policy_reference="review-policy:v2",
        publication_eligible=True,
        correlation_id=CORRELATION,
        corrected_artifact_id=corrected_id,
        decided_at=NOW,
    )


def test_transcript_review_pins_exact_artifact_and_actor_authority() -> None:
    transcript = artifact()
    review = record(transcript)

    assert review.input_artifact_id == transcript.artifact_id
    assert review.input_artifact_digest == transcript.artifact_digest
    assert review.actor_id == ACTOR
    assert review.actor_authority == "transcript_reviewer:v1"
    assert review.correlation_id == CORRELATION


def test_transcript_correction_is_a_distinct_immutable_successor() -> None:
    transcript = artifact()
    review_id = UUID("80000000-0000-0000-0000-000000000001")
    corrected = CorrectedTranscriptArtifact.create(
        original=transcript,
        review_id=review_id,
        segments=(SpeechToTextSegment(start_ms=100, end_ms=200, text="исправлено"),),
        created_at=NOW,
    )
    review = record(transcript, corrected_id=corrected.artifact_id, review_id=review_id)

    assert corrected.artifact_id != transcript.artifact_id
    assert corrected.original_artifact_id == transcript.artifact_id
    assert corrected.original_artifact_digest == transcript.artifact_digest
    assert corrected.segments[0].text == "исправлено"
    assert transcript.segments[0].text == "исправить"
    assert review.corrected_artifact_id == corrected.artifact_id


def test_correction_review_requires_successor_artifact() -> None:
    with pytest.raises(ValueError, match="successor artifact"):
        TranscriptReviewRecord.create(
            artifact=artifact(),
            review_id=UUID("80000000-0000-0000-0000-000000000002"),
            idempotency_key="review-command-1",
            decision=TranscriptReviewDecision.CORRECT,
            rationale="Сверено с записью",
            actor_id=ACTOR,
            actor_authority="transcript_reviewer:v1",
            policy_reference="review-policy:v2",
            publication_eligible=True,
            correlation_id=CORRELATION,
            corrected_artifact_id=None,
            decided_at=NOW,
        )
