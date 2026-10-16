from __future__ import annotations

from uuid import UUID

import httpx
import pytest

from spine.api.parsed_content import install_parsed_content_routes
from spine.application.parsed_content_inspection import (
    ParsedContentInspectionQuery,
    ParsedContentInspectionResult,
    ParsedContentInspectionStatus,
)
from spine.application.persistence.parsed_artifacts import ParsedArtifactMetadata
from spine.ingestion.parsers import parse_document
from tests.domain.artifacts.test_parsed_documents import revision
from tests.api.test_authorization import ENVIRONMENT_ID, WORKSPACE_ID, authorized_app


SOURCE_ID = UUID("52000000-0000-0000-0000-000000000001")
REVISION_ID = UUID("52000000-0000-0000-0000-000000000002")
RECEIPT_ID = UUID("52000000-0000-0000-0000-000000000003")


class FakeParsedContentService:
    async def inspect(self, **kwargs):
        assert kwargs["query"].source_object_id == SOURCE_ID
        assert kwargs["query"].source_revision_id == REVISION_ID
        assert kwargs["query"].processing_receipt_id == RECEIPT_ID
        return ParsedContentInspectionResult(
            status=ParsedContentInspectionStatus.CONTENT_UNAVAILABLE,
        )


class ReadyParsedContentService:
    def __init__(self) -> None:
        self.document = parse_document(revision(b"alpha\nbeta\n"), b"alpha\nbeta\n")
        self.queries: list[ParsedContentInspectionQuery] = []

    async def inspect(self, *, query, **kwargs):
        del kwargs
        self.queries.append(query)
        return ParsedContentInspectionResult(
            status=ParsedContentInspectionStatus.READY,
            metadata=ParsedArtifactMetadata.from_document(self.document),
            segments=(self.document.segments[0],),
            next_after_segment_id=self.document.segments[0].segment_id,
        )


def headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(WORKSPACE_ID),
        "X-Environment-ID": str(ENVIRONMENT_ID),
    }


@pytest.mark.asyncio
async def test_parsed_content_route_returns_safe_unavailable_result() -> None:
    application = authorized_app([])
    install_parsed_content_routes(
        application,
        service=FakeParsedContentService(),  # type: ignore[arg-type]
        cursor_secret=b"issue-342-parsed-content-cursor-secret",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/knowledge/documents/{SOURCE_ID}/revisions/{REVISION_ID}/parsed-content",
            params={"processing_receipt_id": str(RECEIPT_ID)},
            headers=headers(),
        )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "content_unavailable"
    assert str(SOURCE_ID) not in response.json()["data"]
    assert "text" not in response.text


@pytest.mark.asyncio
async def test_parsed_content_rejects_malformed_locator_without_echoing_it() -> None:
    application = authorized_app([])
    install_parsed_content_routes(
        application,
        service=FakeParsedContentService(),  # type: ignore[arg-type]
        cursor_secret=b"issue-342-parsed-content-cursor-secret",
    )
    malformed = "{\"kind\":\"unknown\",\"secret\":\"protected\"}"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/knowledge/documents/{SOURCE_ID}/revisions/{REVISION_ID}/parsed-content",
            params={
                "processing_receipt_id": str(RECEIPT_ID),
                "locator": malformed,
            },
            headers=headers(),
        )

    assert response.status_code == 400
    assert response.json()["code"] == "spine.knowledge.parsed_content.invalid_request"
    assert "protected" not in response.text


@pytest.mark.asyncio
async def test_parsed_content_ready_response_has_typed_locator_and_scope_bound_cursor() -> None:
    service = ReadyParsedContentService()
    application = authorized_app([])
    install_parsed_content_routes(
        application,
        service=service,  # type: ignore[arg-type]
        cursor_secret=b"issue-342-parsed-content-cursor-secret",
    )
    headers_ = headers()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        first = await client.get(
            f"/api/v1/knowledge/documents/{SOURCE_ID}/revisions/{REVISION_ID}/parsed-content",
            params={
                "processing_receipt_id": str(RECEIPT_ID),
                "page_size": 1,
            },
            headers=headers_,
        )
        cursor = first.json()["data"]["next_cursor"]
        replay = await client.get(
            f"/api/v1/knowledge/documents/{SOURCE_ID}/revisions/{REVISION_ID}/parsed-content",
            params={
                "processing_receipt_id": str(RECEIPT_ID),
                "page_size": 1,
                "cursor": cursor,
            },
            headers=headers_,
        )
        tampered = await client.get(
            f"/api/v1/knowledge/documents/{SOURCE_ID}/revisions/{REVISION_ID}/parsed-content",
            params={
                "processing_receipt_id": str(UUID("52000000-0000-0000-0000-000000000004")),
                "page_size": 1,
                "cursor": cursor,
            },
            headers=headers_,
        )

    assert first.status_code == 200
    assert first.json()["data"]["status"] == "ready"
    assert first.json()["data"]["items"][0]["text"] == "alpha"
    assert first.json()["data"]["items"][0]["locator"]["kind"] == "physical_line"
    assert replay.status_code == 200
    assert service.queries[1].after_artifact_id == service.document.artifact_id
    assert tampered.status_code == 400
    assert tampered.json()["code"] == "spine.request.invalid_cursor"
    assert "alpha" not in tampered.text
