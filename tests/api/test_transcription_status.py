from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from spine.api.transcriptions import install_transcription_routes
from spine.application.transcription_status import (
    TranscriptionStatus,
    TranscriptionStatusSnapshot,
)
from tests.api.test_authorization import ENVIRONMENT_ID, WORKSPACE_ID, authorized_app


JOB_ID = UUID("50000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 11, 4, 9, 30, tzinfo=timezone.utc)


class StatusReader:
    def __init__(self, result: TranscriptionStatusSnapshot) -> None:
        self.result = result
        self.call: dict[str, object] | None = None

    async def get_status(self, **kwargs: object) -> TranscriptionStatusSnapshot:
        self.call = kwargs
        return self.result


def headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(WORKSPACE_ID),
        "X-Environment-ID": str(ENVIRONMENT_ID),
    }


@pytest.mark.asyncio
async def test_transcription_status_api_returns_safe_pinned_processing_result() -> None:
    service = StatusReader(
        TranscriptionStatusSnapshot(
            status=TranscriptionStatus.COMPLETED,
            job_id=JOB_ID,
            source_object_id=UUID("30000000-0000-0000-0000-000000000001"),
            source_revision_id=UUID("40000000-0000-0000-0000-000000000001"),
            processing_profile_reference="transcription:russian-v1",
            adapter_reference="local-asr:v1",
            model_config_reference="russian-model:v1",
            terminal_code="completed",
            retryable=None,
            cancellation_requested=False,
            created_at=NOW,
            completed_at=NOW,
        )
    )
    application = authorized_app([])
    install_transcription_routes(application, service=service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/transcriptions/{JOB_ID}", headers=headers()
        )

    assert response.status_code == 200
    result = response.json()["data"]
    assert result == {
        "schema_version": "transcription-status:v1",
        "status": "completed",
        "job_id": str(JOB_ID),
        "source_object_id": "30000000-0000-0000-0000-000000000001",
        "source_revision_id": "40000000-0000-0000-0000-000000000001",
        "processing_profile_reference": "transcription:russian-v1",
        "adapter_reference": "local-asr:v1",
        "model_config_reference": "russian-model:v1",
        "terminal_code": "completed",
        "retryable": None,
        "cancellation_requested": False,
        "created_at": "2026-11-04T09:30:00Z",
        "completed_at": "2026-11-04T09:30:00Z",
    }
    assert service.call is not None
    assert service.call["job_id"] == JOB_ID
    assert service.call["workspace_id"] == WORKSPACE_ID
    assert service.call["environment_id"] == ENVIRONMENT_ID
    assert "text" not in response.text
    assert "original_reference" not in response.text
    assert "execution_lease" not in response.text
    assert "artifact" not in response.text
