from uuid import UUID

import httpx
import pytest

from spine.api.transcript_evidence import install_transcript_evidence_routes
from spine.application.transcript_inspection import (
    TranscriptInspectionResult,
    TranscriptInspectionStatus,
    TranscriptReviewState,
)
from spine.domain.artifacts import TimeRangeLocator
from tests.api.test_authorization import ENVIRONMENT_ID, WORKSPACE_ID, authorized_app


LOCATOR = TimeRangeLocator(
    source_object_id=UUID("30000000-0000-0000-0000-000000000001"),
    source_revision_id=UUID("40000000-0000-0000-0000-000000000001"),
    source_revision_digest="a" * 64,
    transcript_artifact_id=UUID("50000000-0000-0000-0000-000000000001"),
    transcript_artifact_digest="b" * 64,
    transcript_artifact_schema_version="transcription-artifact:v1",
    start_ms=1_000,
    end_ms=2_500,
    segment_ids=(UUID("60000000-0000-0000-0000-000000000001"),),
)


class Inspector:
    def __init__(self, status: TranscriptInspectionStatus) -> None:
        self.status = status
        self.call = None

    async def inspect(self, **kwargs):
        self.call = kwargs
        if self.status is not TranscriptInspectionStatus.CURRENT:
            return TranscriptInspectionResult(status=self.status)
        return TranscriptInspectionResult(
            status=self.status,
            locator=kwargs["locator"],
            source_object_id=kwargs["locator"].source_object_id,
            source_revision_id=kwargs["locator"].source_revision_id,
            transcript_artifact_id=kwargs["locator"].transcript_artifact_id,
            transcript_artifact_digest=kwargs["locator"].transcript_artifact_digest,
            transcript_artifact_schema_version="transcription-artifact:v1",
            publication_id=UUID("70000000-0000-0000-0000-000000000001"),
            publication_state="active",
            review_state=TranscriptReviewState.APPROVED,
            partial=False,
            recording_duration_ms=10_000,
            text="authorized segment",
        )


def headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(WORKSPACE_ID),
        "X-Environment-ID": str(ENVIRONMENT_ID),
    }


@pytest.mark.asyncio
async def test_transcript_inspection_api_returns_scoped_locator_and_safe_metadata() -> None:
    application = authorized_app([])
    service = Inspector(TranscriptInspectionStatus.CURRENT)
    install_transcript_evidence_routes(application, service=service)  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/transcript-evidence/inspect",
            headers=headers(),
            json={"schema_version": 1, "locator": LOCATOR.model_dump(mode="json")},
        )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "current"
    assert response.json()["data"]["locator"]["start_ms"] == 1_000
    assert response.json()["data"]["review_state"] == "approved"
    assert response.json()["data"]["text"] == "authorized segment"
    assert service.call["workspace_id"] == WORKSPACE_ID
    assert service.call["environment_id"] == ENVIRONMENT_ID
    assert "original_reference" not in response.json()["data"]


@pytest.mark.asyncio
async def test_denied_transcript_inspection_api_is_content_and_metadata_free() -> None:
    application = authorized_app([])
    install_transcript_evidence_routes(
        application,
        service=Inspector(TranscriptInspectionStatus.PERMISSION_DENIED),  # type: ignore[arg-type]
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/transcript-evidence/inspect",
            headers=headers(),
            json={"schema_version": 1, "locator": LOCATOR.model_dump(mode="json")},
        )

    result = response.json()["data"]
    assert response.status_code == 200
    assert result["status"] == "permission_denied"
    assert result["locator"] is None
    assert result["source_revision_id"] is None
    assert result["transcript_artifact_id"] is None
    assert result["text"] is None
