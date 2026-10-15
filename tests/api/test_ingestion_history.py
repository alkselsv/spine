from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID

import httpx

from spine.api.ingestion_history import install_ingestion_history_routes
from spine.application.ingestion_history import (
    DocumentProcessingHistorySnapshot,
    IngestionHistoryFailureSnapshot,
    IngestionHistoryPage,
    IngestionRunHistorySnapshot,
)
from spine.domain.ingestion import IngestionItemState, IngestionRunState, ProcessingCoverage
from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    OTHER_ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    authorized_app,
)


RUN_ID = UUID("92000000-0000-0000-0000-000000000001")
ITEM_ID = UUID("92000000-0000-0000-0000-000000000002")
SOURCE_OBJECT_ID = UUID("92000000-0000-0000-0000-000000000003")
REVISION_ID = UUID("92000000-0000-0000-0000-000000000004")
BINDING_ID = UUID("92000000-0000-0000-0000-000000000005")
RECEIPT_ID = UUID("92000000-0000-0000-0000-000000000006")
FAILURE_ID = UUID("92000000-0000-0000-0000-000000000007")


class FakeIngestionHistoryService:
    async def list_runs(self, *, context: object, limit: int, after: object | None):
        del context, limit, after
        return IngestionHistoryPage(
            items=(
                IngestionRunHistorySnapshot(
                    run_id=RUN_ID,
                    requested_at=datetime(2026, 10, 15, 9, tzinfo=timezone.utc),
                    run_state=IngestionRunState.PARTIALLY_SUCCEEDED,
                    items=(
                        DocumentProcessingHistorySnapshot(
                            run_id=RUN_ID,
                            run_state=IngestionRunState.PARTIALLY_SUCCEEDED,
                            item_id=ITEM_ID,
                            input_ordinal=1,
                            input_identity="customer-handbook",
                            item_state=IngestionItemState.PROCESSING_FAILED,
                            attempt=2,
                            retryable=True,
                            source_object_id=SOURCE_OBJECT_ID,
                            source_revision_id=REVISION_ID,
                            binding_id=BINDING_ID,
                            processing_receipt_id=RECEIPT_ID,
                            processing_coverage=ProcessingCoverage.FAILED,
                            parser_name="markdown",
                            artifact_id=None,
                            artifact_digest=None,
                            warning_ids=(),
                            failure_ids=(FAILURE_ID,),
                            warnings=(),
                            failures=(
                                IngestionHistoryFailureSnapshot(
                                    failure_id=FAILURE_ID,
                                    attempt=2,
                                    stage="parsing",
                                    code="parser_failed",
                                    retryable=True,
                                ),
                            ),
                            occurred_at=datetime(2026, 10, 15, 9, tzinfo=timezone.utc),
                        ),
                    ),
                ),
            ),
            has_more=False,
        )

    async def list_processing_history(self, *, context: object, limit: int, after: object | None):
        del context, limit
        run_page = await self.list_runs(context=None, limit=1, after=after)
        return IngestionHistoryPage(
            items=(run_page.items[0].items[0],) if run_page.items else (),
            has_more=False,
        )


class CursorHistoryService(FakeIngestionHistoryService):
    async def list_runs(self, *, context: object, limit: int, after: object | None):
        page = await super().list_runs(context=context, limit=limit, after=after)
        if after is None:
            return IngestionHistoryPage(items=page.items, has_more=True)
        assert getattr(after, "identity", None) == RUN_ID
        return IngestionHistoryPage(items=(), has_more=False)


class CancelledHistoryService(FakeIngestionHistoryService):
    async def list_processing_history(self, *, context: object, limit: int, after: object | None):
        page = await super().list_processing_history(
            context=context,
            limit=limit,
            after=after,
        )
        item = page.items[0]
        return IngestionHistoryPage(
            items=(replace(item, run_state=IngestionRunState.CANCELLED),),
            has_more=False,
        )


def _app() -> object:
    application = authorized_app([])
    install_ingestion_history_routes(
        application,
        service=FakeIngestionHistoryService(),
        cursor_secret=b"issue-295-cursor-secret",
    )

    return application


def _headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(WORKSPACE_ID),
        "X-Environment-ID": str(ENVIRONMENT_ID),
    }


async def test_run_history_exposes_scoped_stable_processing_outcomes() -> None:
    application = _app()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/ingestion/runs", headers=_headers())

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "workspace_id": str(WORKSPACE_ID),
        "environment_id": str(ENVIRONMENT_ID),
        "trace_id": "50000000-0000-0000-0000-000000000073",
        "items": [
            {
                "run_id": str(RUN_ID),
                "requested_at": "2026-10-15T09:00:00Z",
                "status": "failed",
                "run_state": "partially_succeeded",
                "acceptance": "accepted",
                "processing": "failed",
                "terminal": True,
                "status_url": f"http://test/api/v1/ingestion/runs/{RUN_ID}",
                "items": [
                    {
                        "run_id": str(RUN_ID),
                        "run_state": "partially_succeeded",
                        "item_id": str(ITEM_ID),
                        "status_url": f"http://test/api/v1/ingestion/runs/{RUN_ID}/items/{ITEM_ID}",
                        "input_ordinal": 1,
                        "input_identity": "customer-handbook",
                        "status": "failed",
                        "item_state": "processing_failed",
                        "acceptance": "accepted",
                        "processing": "failed",
                        "terminal": True,
                        "retryable": True,
                        "attempt": 2,
                        "coverage": "failed",
                        "source_object_id": str(SOURCE_OBJECT_ID),
                        "source_revision_id": str(REVISION_ID),
                        "binding_id": str(BINDING_ID),
                        "processing_receipt": {
                            "receipt_id": str(RECEIPT_ID),
                            "parser_name": "markdown",
                            "coverage": "failed",
                            "artifact_id": None,
                            "artifact_digest": None,
                        },
                        "warning_ids": [],
                        "failure_ids": [str(FAILURE_ID)],
                        "warnings": [],
                        "failures": [
                            {
                                "failure_id": str(FAILURE_ID),
                                "attempt": 2,
                                "stage": "parsing",
                                "code": "parser_failed",
                                "retryable": True,
                            }
                        ],
                    }
                ],
            }
        ],
        "limit": 50,
        "next_cursor": None,
    }


async def test_history_cursor_is_scope_bound_and_replay_safe() -> None:
    application = authorized_app([])
    install_ingestion_history_routes(
        application,
        service=CursorHistoryService(),
        cursor_secret=b"issue-295-cursor-secret",
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(
            "/api/v1/ingestion/runs?limit=1",
            headers=_headers(),
        )
        cursor = first.json()["next_cursor"]
        replay = await client.get(
            f"/api/v1/ingestion/runs?limit=1&cursor={cursor}",
            headers=_headers(),
        )
        tampered = await client.get(
            f"/api/v1/ingestion/runs?limit=1&cursor={cursor[:-1]}x",
            headers=_headers(),
        )

    assert first.status_code == 200
    assert cursor
    assert replay.status_code == 200
    assert replay.json()["items"] == []
    assert tampered.status_code == 400
    assert tampered.json()["code"] == "spine.request.invalid_cursor"


async def test_processing_history_links_each_item_to_its_polling_resource() -> None:
    transport = httpx.ASGITransport(app=_app())

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/documents/processing-history",
            headers=_headers(),
        )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["run_id"] == str(RUN_ID)
    assert item["item_id"] == str(ITEM_ID)
    assert item["status_url"] == (
        f"http://test/api/v1/ingestion/runs/{RUN_ID}/items/{ITEM_ID}"
    )
    assert item["retryable"] is True
    assert item["failure_ids"] == [str(FAILURE_ID)]


async def test_history_denies_foreign_scope_without_enumerating_rows() -> None:
    transport = httpx.ASGITransport(app=_app())
    headers = {
        **_headers(),
        "X-Workspace-ID": str(OTHER_WORKSPACE_ID),
        "X-Environment-ID": str(OTHER_ENVIRONMENT_ID),
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/ingestion/runs", headers=headers)

    assert response.status_code == 403
    assert response.json()["code"] == "spine.authorization.denied"
    assert str(WORKSPACE_ID) not in response.text
    assert str(RUN_ID) not in response.text


async def test_history_rejects_invalid_collection_query() -> None:
    transport = httpx.ASGITransport(app=_app())

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        invalid_limit = await client.get(
            "/api/v1/ingestion/runs?limit=0",
            headers=_headers(),
        )
        oversized_cursor = await client.get(
            "/api/v1/ingestion/runs?cursor=" + ("x" * 4097),
            headers=_headers(),
        )

    assert invalid_limit.status_code == 422
    assert oversized_cursor.status_code == 422


async def test_processing_history_exposes_cancelled_run_state() -> None:
    application = authorized_app([])
    install_ingestion_history_routes(
        application,
        service=CancelledHistoryService(),
        cursor_secret=b"issue-295-cursor-secret",
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/documents/processing-history",
            headers=_headers(),
        )

    assert response.status_code == 200
    assert response.json()["items"][0]["run_state"] == "cancelled"
    assert response.json()["items"][0]["status"] == "failed"
    assert response.json()["items"][0]["terminal"] is True
