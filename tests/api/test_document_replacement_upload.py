from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import httpx

from spine.api.document_replacement_uploads import (
    install_document_replacement_upload_route,
)
from spine.application.ingestion_api import IngestionSubmission
from spine.application.document_ingestion import DocumentIngestionStatus
from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    authorized_app,
)

SOURCE_ID = UUID("40000000-0000-4000-8000-000000000001")
REVISION_ID = UUID("50000000-0000-4000-8000-000000000001")


class FakeReplacementService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object, bytes]] = []
        self.status = DocumentIngestionStatus.SUCCEEDED

    async def replace(
        self, *, context: object, command: object, chunks: AsyncIterator[bytes]
    ):
        self.calls.append(
            (context, command, b"".join([chunk async for chunk in chunks]))
        )
        return IngestionSubmission(
            run_id=UUID("60000000-0000-4000-8000-000000000001"),
            item_id=UUID("70000000-0000-4000-8000-000000000001"),
            source_object_id=SOURCE_ID,
            source_revision_id=UUID("80000000-0000-4000-8000-000000000001"),
            binding_id=UUID("90000000-0000-4000-8000-000000000001"),
            status=self.status,
            replay=len(self.calls) > 1,
            processing_receipt_id=None,
            parser_name=None,
            warning_ids=(),
            failure_ids=(),
        )


def _app(service: FakeReplacementService):
    application = authorized_app([])
    install_document_replacement_upload_route(
        application, service=service, max_upload_bytes=64
    )
    return application


def _headers(workspace_id: UUID = WORKSPACE_ID):
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(workspace_id),
        "X-Environment-ID": str(ENVIRONMENT_ID),
        "Idempotency-Key": "replacement-upload-001",
        "If-Match": str(REVISION_ID),
        "X-Filename": "handbook.md",
        "Content-Type": "text/markdown",
    }


async def test_replacement_upload_uses_expected_revision_and_keeps_storage_private() -> (
    None
):
    service = FakeReplacementService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/documents/{SOURCE_ID}/replacement-upload",
            headers=_headers(),
            content=b"# new content",
        )

    assert response.status_code == 202
    assert response.json()["source_object_id"] == str(SOURCE_ID)
    assert service.calls[0][1].expected_current_revision_id == REVISION_ID
    assert service.calls[0][1].declared_filename == "handbook.md"
    assert service.calls[0][2] == b"# new content"
    assert "storage_request_reference" not in response.text


async def test_replacement_upload_rejects_another_workspace_without_forwarding_bytes() -> (
    None
):
    service = FakeReplacementService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/documents/{SOURCE_ID}/replacement-upload",
            headers=_headers(OTHER_WORKSPACE_ID),
            content=b"protected bytes",
        )

    assert response.status_code == 403
    assert service.calls == []


async def test_replacement_upload_rejects_invalid_expected_revision() -> None:
    service = FakeReplacementService()
    headers = _headers()
    headers["If-Match"] = "not-a-revision"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/documents/{SOURCE_ID}/replacement-upload",
            headers=headers,
            content=b"# content",
        )

    assert response.status_code == 400
    assert service.calls == []


async def test_replacement_upload_replay_returns_same_revision_receipt() -> None:
    service = FakeReplacementService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)), base_url="http://test"
    ) as client:
        first = await client.post(
            f"/api/v1/documents/{SOURCE_ID}/replacement-upload",
            headers=_headers(),
            content=b"# new content",
        )
        replay = await client.post(
            f"/api/v1/documents/{SOURCE_ID}/replacement-upload",
            headers=_headers(),
            content=b"# new content",
        )

    assert first.status_code == replay.status_code == 202
    assert first.json()["source_revision_id"] == replay.json()["source_revision_id"]
    assert replay.json()["replay"] is True
