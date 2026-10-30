from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from spine.api.transcript_reviews import install_transcript_review_routes
from spine.application.transcript_review import (
    ExplicitTranscriptReviewPolicy,
    InMemoryTranscriptReviewRepository,
    TranscriptReviewPolicyOutcome,
    TranscriptReviewService,
)
from spine.domain.artifacts import SpeechToTextSegment, TranscriptionArtifact
from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    TRACE_ID,
    WORKSPACE_ID,
    authorized_app,
)

ACTOR = UUID("10000000-0000-0000-0000-000000000073")
NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def transcript() -> TranscriptionArtifact:
    return TranscriptionArtifact.create(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        source_object_id=UUID("30000000-0000-0000-0000-000000000001"),
        source_revision_id=UUID("40000000-0000-0000-0000-000000000001"),
        source_revision_digest="a" * 64,
        original_sha256="b" * 64,
        processing_job_id=UUID("50000000-0000-0000-0000-000000000001"),
        processing_profile_reference="transcription:v1",
        adapter_reference="fake-stt:v1",
        model_config_reference="fake-model:v1",
        recording_duration_ms=10_000,
        language="ru",
        segments=(SpeechToTextSegment(start_ms=1_000, end_ms=2_000, text="ASR текст"),),
        created_at=NOW,
    )


def headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(WORKSPACE_ID),
        "X-Environment-ID": str(ENVIRONMENT_ID),
    }


@pytest.mark.asyncio
async def test_transcript_review_api_records_authorized_correction_and_trace() -> None:
    artifact = transcript()

    class Artifacts:
        async def get_artifact(self, **kwargs):
            return artifact if kwargs["artifact_id"] == artifact.artifact_id else None

    app = authorized_app([])
    service = TranscriptReviewService(
        repository=InMemoryTranscriptReviewRepository(Artifacts()),
        policy=ExplicitTranscriptReviewPolicy(
            policy_reference="review-policy:v1",
            outcome=TranscriptReviewPolicyOutcome.REQUIRE,
        ),
        clock=lambda: NOW,
        identifier_factory=lambda: UUID("80000000-0000-0000-0000-000000000001"),
    )
    install_transcript_review_routes(app, service=service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/transcript-reviews/decide",
            headers=headers(),
            json={
                "schema_version": 1,
                "transcript_artifact_id": str(artifact.artifact_id),
                "transcript_artifact_digest": artifact.artifact_digest,
                "decision": "correct",
                "rationale": "Сверено с записью",
                "idempotency_key": "review-api-1",
                "corrected_segments": [
                    {"start_ms": 1_000, "end_ms": 2_000, "text": "Исправленный текст"}
                ],
            },
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["review"]["actor_id"] == str(ACTOR)
    assert data["review"]["actor_authority"] == "administrator"
    assert data["review"]["correlation_id"] == str(TRACE_ID)
    assert data["review"]["input_artifact_digest"] == artifact.artifact_digest
    assert data["corrected_artifact"]["segments"][0]["text"] == "Исправленный текст"


@pytest.mark.asyncio
async def test_transcript_review_api_rejects_stale_version_with_safe_conflict() -> None:
    artifact = transcript()

    class Artifacts:
        async def get_artifact(self, **kwargs):
            return artifact if kwargs["artifact_id"] == artifact.artifact_id else None

    app = authorized_app([])
    install_transcript_review_routes(
        app,
        service=TranscriptReviewService(
            repository=InMemoryTranscriptReviewRepository(Artifacts()),
            policy=ExplicitTranscriptReviewPolicy(
                policy_reference="review-policy:v1",
                outcome=TranscriptReviewPolicyOutcome.REQUIRE,
            ),
            clock=lambda: NOW,
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/transcript-reviews/decide",
            headers=headers(),
            json={
                "schema_version": 1,
                "transcript_artifact_id": str(artifact.artifact_id),
                "transcript_artifact_digest": "c" * 64,
                "decision": "approve",
                "rationale": "Проверено",
                "idempotency_key": "review-api-stale",
            },
        )

    assert response.status_code == 409
    assert response.json()["code"] == "spine.request.rejected"
    assert "ASR текст" not in response.text
