from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID

import httpx

from spine.api.ingestion import install_ingestion_routes
from spine.api.authorization import ApiAuthorizationReadiness, ApiAuthorizationRuntime
from spine.application.document_ingestion import DocumentIngestionStatus
from spine.application.ingestion_api import (
    IngestionStatusSnapshot,
    IngestionSubmission,
)
from spine.application.persistence.errors import IdempotencyConflictError
from spine.domain.ingestion import IngestionItemState, IngestionRunState, ProcessingCoverage
from spine.auth import RoutePolicyRegistry
from spine.api.main import create_app

from tests.api.test_authorization import ENVIRONMENT_ID, OTHER_WORKSPACE_ID, WORKSPACE_ID, authorized_app


RUN_ID = UUID("90000000-0000-0000-0000-000000000001")
ITEM_ID = UUID("90000000-0000-0000-0000-000000000002")
SOURCE_OBJECT_ID = UUID("90000000-0000-0000-0000-000000000003")
REVISION_ID = UUID("90000000-0000-0000-0000-000000000004")
BINDING_ID = UUID("90000000-0000-0000-0000-000000000005")
RECEIPT_ID = UUID("90000000-0000-0000-0000-000000000006")
ARTIFACT_ID = UUID("90000000-0000-0000-0000-000000000007")


class FakeIngestionService:
    def __init__(self) -> None:
        self.submission_status = DocumentIngestionStatus.SUCCEEDED
        self.submissions: list[tuple[object, object, bytes]] = []
        self.status_requests: list[tuple[object, UUID, UUID]] = []
        self._digests: dict[str, str] = {}
        self.snapshot = IngestionStatusSnapshot(
            run_id=RUN_ID,
            item_id=ITEM_ID,
            run_state=IngestionRunState.SUCCEEDED,
            item_state=IngestionItemState.PROCESSED,
            source_object_id=SOURCE_OBJECT_ID,
            source_revision_id=REVISION_ID,
            binding_id=BINDING_ID,
            processing_receipt_id=RECEIPT_ID,
            processing_coverage=ProcessingCoverage.COMPLETE,
            parser_name="text",
            artifact_id=ARTIFACT_ID,
            artifact_digest="a" * 64,
            warning_ids=(),
            failure_ids=(),
        )

    async def submit(self, *, context: object, command: object, chunks: AsyncIterable[bytes]) -> IngestionSubmission:
        digest = hashlib.sha256(
            json.dumps(
                command.model_dump(mode="json", exclude={"idempotency_key"}),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        current = self._digests.setdefault(command.idempotency_key, digest)
        if current != digest:
            raise IdempotencyConflictError("safe conflict")
        content = b"".join([chunk async for chunk in chunks])
        self.submissions.append((context, command, content))
        return IngestionSubmission(
            run_id=RUN_ID,
            item_id=ITEM_ID,
            source_object_id=SOURCE_OBJECT_ID,
            source_revision_id=REVISION_ID,
            binding_id=BINDING_ID,
            status=self.submission_status,
            replay=len(self.submissions) > 1,
            processing_receipt_id=RECEIPT_ID,
            parser_name="text",
            warning_ids=(),
            failure_ids=(),
        )

    async def status(self, *, context: object, run_id: UUID, item_id: UUID) -> IngestionStatusSnapshot | None:
        self.status_requests.append((context, run_id, item_id))
        if (run_id, item_id) != (RUN_ID, ITEM_ID):
            return None
        return self.snapshot


class OversizedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"123"
        yield b"45"


def _app(service: FakeIngestionService, *, max_upload_bytes: int = 64):
    class Bound:
        def authorize_with_scope_resolver(self, **kwargs: object):
            del kwargs

            class Session:
                async def __aenter__(self):
                    return SimpleNamespace(
                        request_context=object(),
                        persistence_context=object(),
                    )

                async def __aexit__(self, *args: object) -> None:
                    del args

            return Session()

    class Composer:
        def bind_route(self, route_id: str):
            del route_id
            return Bound()

    async def readiness() -> ApiAuthorizationReadiness:
        return ApiAuthorizationReadiness(ready=True, code="authorization.ready")

    route_policies = RoutePolicyRegistry()
    application = create_app(
        authorization_runtime=ApiAuthorizationRuntime(
            composer=Composer(),  # type: ignore[arg-type]
            route_policies=route_policies,
            readiness_probe=readiness,
        ),
        document_ingestion_service=service,
        max_upload_bytes=max_upload_bytes,
    )
    return application


def _headers(*, workspace_id: UUID = WORKSPACE_ID) -> dict[str, str]:
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(workspace_id),
        "X-Environment-ID": str(ENVIRONMENT_ID),
        "Idempotency-Key": "upload-001",
        "X-Filename": "handbook.md",
        "Content-Type": "text/markdown",
    }


async def test_authorized_upload_returns_stable_status_reference_and_streams_chunks() -> None:
    service = FakeIngestionService()
    application = _app(service)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/uploads",
            headers=_headers(),
            content=b"# hello\n",
        )

    assert response.status_code == 202
    assert response.json() == {
        "schema_version": 1,
        "run_id": str(RUN_ID),
        "item_id": str(ITEM_ID),
        "status_url": f"http://test/api/v1/ingestion/runs/{RUN_ID}/items/{ITEM_ID}",
        "status": "succeeded",
        "acceptance": "accepted",
        "processing": "succeeded",
        "terminal": True,
        "replay": False,
        "source_object_id": str(SOURCE_OBJECT_ID),
        "source_revision_id": str(REVISION_ID),
        "binding_id": str(BINDING_ID),
        "processing_receipt_id": str(RECEIPT_ID),
        "warning_ids": [],
        "failure_ids": [],
    }
    assert service.submissions[0][2] == b"# hello\n"


async def test_same_upload_replays_with_stable_identity() -> None:
    service = FakeIngestionService()
    application = _app(service)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/documents/uploads", headers=_headers(), content=b"# hello\n"
        )
        replay = await client.post(
            "/api/v1/documents/uploads", headers=_headers(), content=b"# hello\n"
        )

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["run_id"] == first.json()["run_id"]
    assert replay.json()["item_id"] == first.json()["item_id"]
    assert replay.json()["replay"] is True


async def test_same_upload_key_with_changed_content_returns_safe_conflict() -> None:
    service = FakeIngestionService()
    application = _app(service)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/documents/uploads", headers=_headers(), content=b"# hello\n"
        )
        conflict = await client.post(
            "/api/v1/documents/uploads", headers=_headers(), content=b"# changed\n"
        )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "spine.persistence.idempotency_conflict"
    assert "safe conflict" not in conflict.text


async def test_status_exposes_acceptance_processing_and_receipt_without_content() -> None:
    service = FakeIngestionService()
    application = _app(service)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/ingestion/runs/{RUN_ID}/items/{ITEM_ID}",
            headers=_headers(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["acceptance"] == "accepted"
    assert body["processing"] == "succeeded"
    assert body["processing_receipt"] == {
        "receipt_id": str(RECEIPT_ID),
        "parser_name": "text",
        "coverage": "complete",
        "artifact_id": str(ARTIFACT_ID),
        "artifact_digest": "a" * 64,
    }
    assert "hello" not in response.text


async def test_pre_acceptance_failure_status_keeps_acceptance_pending() -> None:
    service = FakeIngestionService()
    service.snapshot = replace(
        service.snapshot,
        run_state=IngestionRunState.FAILED,
        item_state=IngestionItemState.PROCESSING_FAILED,
        processing_receipt_id=None,
        processing_coverage=None,
        parser_name=None,
        artifact_id=None,
        artifact_digest=None,
        failure_ids=(UUID("90000000-0000-0000-0000-000000000008"),),
        failure_reason_codes=("pre_acceptance_retryable",),
    )
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/ingestion/runs/{RUN_ID}/items/{ITEM_ID}",
            headers=_headers(),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["acceptance"] == "pending"


async def test_successful_retry_accepts_despite_retained_pre_acceptance_failure() -> None:
    service = FakeIngestionService()
    service.snapshot = replace(
        service.snapshot,
        failure_ids=(UUID("90000000-0000-0000-0000-000000000008"),),
        failure_reason_codes=("pre_acceptance_retryable",),
    )
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/ingestion/runs/{RUN_ID}/items/{ITEM_ID}",
            headers=_headers(),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert response.json()["acceptance"] == "accepted"


async def test_unsupported_format_has_explicit_non_success_status() -> None:
    service = FakeIngestionService()
    service.submission_status = DocumentIngestionStatus.UNSUPPORTED
    application = _app(service)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/uploads",
            headers={**_headers(), "Content-Type": "application/pdf"},
            content=b"synthetic pdf bytes",
        )

    assert response.status_code == 202
    assert response.json()["status"] == "unsupported"
    assert response.json()["processing"] == "unsupported"
    assert response.json()["acceptance"] == "accepted"
    assert response.json()["terminal"] is True


async def test_oversized_upload_is_rejected_before_service_receives_bytes() -> None:
    service = FakeIngestionService()
    application = _app(service, max_upload_bytes=4)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/uploads",
            headers={**_headers(), "Content-Length": "5"},
            content=b"12345",
        )

    assert response.status_code == 413
    assert response.json()["code"] == "spine.ingestion.payload_too_large"
    assert service.submissions == []


async def test_oversized_idempotency_key_is_rejected_before_service_receives_bytes() -> None:
    service = FakeIngestionService()
    application = _app(service)
    headers = _headers()
    headers["Idempotency-Key"] = "x" * 256
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/uploads",
            headers=headers,
            content=b"# hello\n",
        )

    assert response.status_code == 400
    assert response.json()["code"] == "spine.ingestion.invalid_request"
    assert service.submissions == []


async def test_invalid_content_digest_is_rejected_before_service_receives_bytes() -> None:
    service = FakeIngestionService()
    application = _app(service)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/uploads",
            headers={**_headers(), "X-Content-SHA256": "a" * 65},
            content=b"# hello\n",
        )

    assert response.status_code == 400
    assert response.json()["code"] == "spine.ingestion.invalid_request"
    assert service.submissions == []


async def test_streaming_limit_rejects_without_content_length() -> None:
    service = FakeIngestionService()
    application = _app(service, max_upload_bytes=4)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/uploads",
            headers=_headers(),
            content=OversizedStream(),
        )

    assert response.status_code == 413
    assert response.json()["code"] == "spine.ingestion.payload_too_large"
    assert service.submissions == []


async def test_missing_authentication_is_rejected_before_ingestion_service() -> None:
    service = FakeIngestionService()
    application = _app(service)
    transport = httpx.ASGITransport(app=application)
    headers = _headers()
    headers.pop("Authorization")

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/uploads",
            headers=headers,
            content=b"# secret\n",
        )

    assert response.status_code == 401
    assert response.json()["code"] == "spine.authentication.invalid"
    assert service.submissions == []


async def test_cross_workspace_upload_is_denied_without_leaking_service_or_content() -> None:
    service = FakeIngestionService()
    application = authorized_app([])
    install_ingestion_routes(application, service=service)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/uploads",
            headers=_headers(workspace_id=OTHER_WORKSPACE_ID),
            content=b"protected customer content",
        )

    assert response.status_code == 403
    assert response.json()["code"] == "spine.authorization.denied"
    assert "protected customer content" not in response.text
    assert service.submissions == []


async def test_cross_workspace_status_is_denied_before_service() -> None:
    service = FakeIngestionService()
    application = authorized_app([])
    install_ingestion_routes(application, service=service)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/ingestion/runs/{RUN_ID}/items/{ITEM_ID}",
            headers=_headers(workspace_id=OTHER_WORKSPACE_ID),
        )

    assert response.status_code == 403
    assert response.json()["code"] == "spine.authorization.denied"
    assert service.status_requests == []


def test_ingestion_openapi_declares_binary_body_and_stable_headers() -> None:
    service = FakeIngestionService()
    operation = _app(service).openapi()["paths"]["/api/v1/documents/uploads"]["post"]

    assert operation["requestBody"]["required"] is True
    assert operation["requestBody"]["content"]["text/markdown"]["schema"] == {
        "type": "string",
        "format": "binary",
    }
    parameter_names = {parameter["name"] for parameter in operation["parameters"]}
    assert {"Idempotency-Key", "X-Upload-Identity", "X-Filename", "X-Content-SHA256"} <= parameter_names
