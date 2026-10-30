from datetime import datetime, timezone
from dataclasses import replace
from uuid import UUID

import pytest

from spine.application.transcript_review import (
    ExplicitTranscriptReviewPolicy,
    InMemoryTranscriptReviewRepository,
    ReviewTranscriptCommand,
    TranscriptReviewConflictError,
    TranscriptReviewPolicyOutcome,
    TranscriptReviewReplayConflictError,
    TranscriptReviewService,
    TranscriptReviewStaleError,
)
from spine.domain.artifacts import (
    SpeechToTextSegment,
    TranscriptReviewDecision,
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


def artifact(*, segments=None) -> TranscriptionArtifact:
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
        segments=segments
        or (SpeechToTextSegment(start_ms=1_000, end_ms=2_000, text="ASR текст"),),
        created_at=NOW,
    )


def command(
    transcript: TranscriptionArtifact,
    *,
    key: str = "review-1",
    decision: TranscriptReviewDecision = TranscriptReviewDecision.APPROVE,
    rationale: str = "Проверено",
    corrected: tuple[SpeechToTextSegment, ...] = (),
) -> ReviewTranscriptCommand:
    return ReviewTranscriptCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        transcript_artifact_id=transcript.artifact_id,
        expected_artifact_digest=transcript.artifact_digest,
        decision=decision,
        rationale=rationale,
        actor_id=ACTOR,
        actor_authority="transcript_reviewer:v1",
        idempotency_key=key,
        correlation_id=CORRELATION,
        corrected_segments=corrected,
    )


def service(
    transcript: TranscriptionArtifact, outcome=TranscriptReviewPolicyOutcome.REQUIRE
):
    class Artifacts:
        async def get_artifact(self, **kwargs):
            return (
                transcript if kwargs["artifact_id"] == transcript.artifact_id else None
            )

    sequence = iter(
        UUID(f"80000000-0000-0000-0000-{index:012d}") for index in range(1, 8)
    )
    return TranscriptReviewService(
        repository=InMemoryTranscriptReviewRepository(Artifacts()),
        policy=ExplicitTranscriptReviewPolicy(
            policy_reference="review-policy:v1",
            outcome=outcome,
        ),
        clock=lambda: NOW,
        identifier_factory=lambda: next(sequence),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "eligible"),
    [
        (TranscriptReviewPolicyOutcome.ALLOW, True),
        (TranscriptReviewPolicyOutcome.REQUIRE, True),
        (TranscriptReviewPolicyOutcome.DENY, False),
    ],
)
async def test_policy_controls_publication_eligibility(policy, eligible) -> None:
    transcript = artifact()
    result = await service(transcript, policy).review(command(transcript))

    assert result.review.publication_eligible is eligible
    assert result.review.policy_reference == "review-policy:v1"


@pytest.mark.asyncio
async def test_correction_creates_successor_and_preserves_asr_on_exact_replay() -> None:
    transcript = artifact()
    reviewer = service(transcript)
    correction = command(
        transcript,
        decision=TranscriptReviewDecision.CORRECT,
        corrected=(
            SpeechToTextSegment(
                start_ms=1_000, end_ms=2_000, text="Исправленный текст"
            ),
        ),
    )

    first = await reviewer.review(correction)
    replay = await reviewer.review(correction)

    assert first.corrected_artifact is not None
    assert first.corrected_artifact.original_artifact_id == transcript.artifact_id
    assert first.corrected_artifact.segments[0].text == "Исправленный текст"
    assert transcript.segments[0].text == "ASR текст"
    assert replay.replay is True
    assert replay.review.review_id == first.review.review_id
    assert replay.corrected_artifact == first.corrected_artifact


@pytest.mark.asyncio
async def test_partial_correction_preserves_other_segments_and_timing() -> None:
    transcript = artifact(
        segments=(
            SpeechToTextSegment(start_ms=1_000, end_ms=2_000, text="ASR текст"),
            SpeechToTextSegment(start_ms=2_000, end_ms=3_000, text="Без изменений"),
        )
    )
    result = await service(transcript).review(
        command(
            transcript,
            decision=TranscriptReviewDecision.CORRECT,
            corrected=(
                SpeechToTextSegment(start_ms=1_000, end_ms=2_000, text="Исправлено"),
            ),
        )
    )

    assert result.corrected_artifact is not None
    assert tuple(segment.text for segment in result.corrected_artifact.segments) == (
        "Исправлено",
        "Без изменений",
    )
    assert result.corrected_artifact.segments[1].start_ms == 2_000


@pytest.mark.asyncio
async def test_rejected_review_is_never_publication_eligible() -> None:
    transcript = artifact()
    result = await service(transcript).review(
        command(transcript, decision=TranscriptReviewDecision.REJECT)
    )

    assert result.review.decision is TranscriptReviewDecision.REJECT
    assert result.review.publication_eligible is False


@pytest.mark.asyncio
async def test_stale_artifact_digest_is_rejected_before_policy_call() -> None:
    transcript = artifact()
    reviewer = service(transcript)
    stale = replace(command(transcript), expected_artifact_digest="c" * 64)

    with pytest.raises(TranscriptReviewStaleError, match="stale"):
        await reviewer.review(stale)


@pytest.mark.asyncio
async def test_distinct_review_command_cannot_overwrite_exact_artifact_review() -> None:
    transcript = artifact()
    reviewer = service(transcript)
    await reviewer.review(command(transcript))

    with pytest.raises(TranscriptReviewConflictError):
        await reviewer.review(
            command(transcript, key="review-2", rationale="второй акт")
        )


@pytest.mark.asyncio
async def test_reused_idempotency_key_with_changed_payload_is_rejected() -> None:
    transcript = artifact()
    reviewer = service(transcript)
    await reviewer.review(command(transcript))

    with pytest.raises(TranscriptReviewReplayConflictError):
        await reviewer.review(command(transcript, rationale="другое решение"))
