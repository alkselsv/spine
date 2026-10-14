from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from uuid import UUID

import httpx

from spine.api.batch_ingestion import install_batch_ingestion_routes
from spine.application.batch_ingestion import (
    BatchDocumentInput,
    BatchDocumentUploadCommand,
)
from spine.application.batch_ingestion_api import (
    BatchIngestionFailureSnapshot,
    BatchIngestionItemSnapshot,
    BatchIngestionSnapshot,
)
from spine.application.persistence.errors import IdempotencyConflictError
from spine.auth import (
    AuthenticatedAlias,
    AuthenticationAlias,
    InMemoryAuthorizationDirectory,
)
from spine.domain.ingestion import (
    IngestionItemState,
    IngestionRunState,
    ProcessingCoverage,
)

from tests.api.test_authorization import (
    CONFIGURATION_VERSION,
    ENVIRONMENT_ID,
    OTHER_ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    StaticAuthentication,
    authorization_entry,
    authorized_app,
)


RUN_ID = UUID("91000000-0000-0000-0000-000000000001")
SUCCESS_ITEM_ID = UUID("91000000-0000-0000-0000-000000000002")
FAILED_ITEM_ID = UUID("91000000-0000-0000-0000-000000000003")
SOURCE_OBJECT_ID = UUID("91000000-0000-0000-0000-000000000004")
REVISION_ID = UUID("91000000-0000-0000-0000-000000000005")
BINDING_ID = UUID("91000000-0000-0000-0000-000000000006")
RECEIPT_ID = UUID("91000000-0000-0000-0000-000000000007")
ARTIFACT_ID = UUID("91000000-0000-0000-0000-000000000008")
FAILURE_ID = UUID("91000000-0000-0000-0000-000000000009")
OTHER_AUTHENTICATION_ALIAS = AuthenticationAlias(
    issuer="https://identity.example.test",
    subject="synthetic-other-workspace-administrator",
)


class FakeBatchIngestionService:
    def __init__(self) -> None:
        self.submissions: list[
            tuple[object, BatchDocumentUploadCommand, tuple[bytes, ...]]
        ] = []
        self._digests: dict[str, str] = {}
        self.snapshot = BatchIngestionSnapshot(
            run_id=RUN_ID,
            run_state=IngestionRunState.PARTIALLY_SUCCEEDED,
            replay=False,
            items=(
                BatchIngestionItemSnapshot(
                    item_id=SUCCESS_ITEM_ID,
                    input_ordinal=1,
                    input_identity="file-a",
                    item_state=IngestionItemState.PROCESSED,
                    attempt=7,
                    retryable=False,
                    source_object_id=SOURCE_OBJECT_ID,
                    source_revision_id=REVISION_ID,
                    binding_id=BINDING_ID,
                    processing_receipt_id=RECEIPT_ID,
                    processing_coverage=ProcessingCoverage.COMPLETE,
                    parser_name="markdown",
                    artifact_id=ARTIFACT_ID,
                    artifact_digest="a" * 64,
                    warning_ids=(),
                    failure_ids=(),
                ),
                BatchIngestionItemSnapshot(
                    item_id=FAILED_ITEM_ID,
                    input_ordinal=2,
                    input_identity="file-b",
                    item_state=IngestionItemState.PROCESSING_FAILED,
                    attempt=5,
                    retryable=True,
                    source_object_id=None,
                    source_revision_id=None,
                    binding_id=None,
                    processing_receipt_id=None,
                    processing_coverage=ProcessingCoverage.FAILED,
                    parser_name=None,
                    artifact_id=None,
                    artifact_digest=None,
                    warning_ids=(),
                    failure_ids=(FAILURE_ID,),
                    failures=(
                        BatchIngestionFailureSnapshot(
                            failure_id=FAILURE_ID,
                            attempt=5,
                            stage="parsing",
                            code="parser_failed",
                            retryable=True,
                        ),
                    ),
                ),
            ),
        )

    async def submit(
        self,
        *,
        context: object,
        command: BatchDocumentUploadCommand,
        items: Sequence[BatchDocumentInput],
    ) -> BatchIngestionSnapshot:
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
        buffered: list[bytes] = []
        for item in items:
            buffered.append(b"".join([chunk async for chunk in item.chunks]))
        contents = tuple(buffered)
        self.submissions.append((context, command, contents))
        return BatchIngestionSnapshot(
            run_id=self.snapshot.run_id,
            run_state=self.snapshot.run_state,
            items=self.snapshot.items,
            replay=len(self.submissions) > 1,
        )

    async def status(
        self,
        *,
        context: object,
        run_id: UUID,
    ) -> BatchIngestionSnapshot | None:
        scope = getattr(context, "scope", None)
        if (
            getattr(scope, "workspace_id", None) != WORKSPACE_ID
            or getattr(scope, "environment_id", None) != ENVIRONMENT_ID
        ):
            return None
        return self.snapshot if run_id == RUN_ID else None


class MultipartStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def _app(
    service: FakeBatchIngestionService,
    *,
    max_files: int = 4,
    max_file_bytes: int = 64,
    max_batch_bytes: int = 1024,
):
    application = authorized_app([])
    install_batch_ingestion_routes(
        application,
        service=service,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_batch_bytes=max_batch_bytes,
    )
    return application


def _other_workspace_app(service: FakeBatchIngestionService):
    entry = authorization_entry(OTHER_AUTHENTICATION_ALIAS).model_copy(
        update={
            "workspace_id": OTHER_WORKSPACE_ID,
            "environment_id": OTHER_ENVIRONMENT_ID,
            "environment_workspace_id": OTHER_WORKSPACE_ID,
        }
    )
    authentication = StaticAuthentication(
        AuthenticatedAlias(
            alias=OTHER_AUTHENTICATION_ALIAS,
            authentication_configuration_version=CONFIGURATION_VERSION,
        )
    )
    application = authorized_app(
        [],
        authentication=authentication,
        directory=InMemoryAuthorizationDirectory(entries=(entry,)),
    )
    install_batch_ingestion_routes(application, service=service)
    return application


def _headers(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
) -> dict[str, str]:
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(workspace_id),
        "X-Environment-ID": str(environment_id),
        "Idempotency-Key": "batch-upload-001",
    }


def _files(
    *,
    first: bytes = b"# first\n",
) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [
        ("file-a", ("first.md", first, "text/markdown")),
        ("file-b", ("second.txt", b"second\n", "text/plain")),
    ]


async def test_batch_upload_streams_files_and_exposes_independent_outcomes() -> None:
    service = FakeBatchIngestionService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/batches",
            headers=_headers(),
            files=_files(),
        )

    assert response.status_code == 202
    body = response.json()
    assert body["run_id"] == str(RUN_ID)
    assert body["status_url"] == f"http://test/api/v1/ingestion/runs/{RUN_ID}"
    assert body["status"] == "partial"
    assert body["terminal"] is True
    assert body["replay"] is False
    assert [item["status"] for item in body["items"]] == ["succeeded", "failed"]
    assert body["items"][0]["processing_receipt"] == {
        "receipt_id": str(RECEIPT_ID),
        "parser_name": "markdown",
        "coverage": "complete",
        "artifact_id": str(ARTIFACT_ID),
        "artifact_digest": "a" * 64,
    }
    assert body["items"][1]["retryable"] is True
    assert body["items"][1]["failure_ids"] == [str(FAILURE_ID)]
    assert body["items"][1]["failures"] == [
        {
            "failure_id": str(FAILURE_ID),
            "attempt": 5,
            "stage": "parsing",
            "code": "parser_failed",
            "retryable": True,
        }
    ]
    _, command, contents = service.submissions[0]
    assert contents == (b"# first\n", b"second\n")
    assert command.items[0].expected_sha256 == hashlib.sha256(b"# first\n").hexdigest()
    assert command.items[0].expected_byte_length == 8


async def test_same_batch_replays_with_stable_run_id() -> None:
    service = FakeBatchIngestionService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/documents/batches", headers=_headers(), files=_files()
        )
        replay = await client.post(
            "/api/v1/documents/batches", headers=_headers(), files=_files()
        )
    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["run_id"] == first.json()["run_id"]
    assert replay.json()["replay"] is True


async def test_same_idempotency_key_with_changed_content_conflicts() -> None:
    service = FakeBatchIngestionService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/documents/batches", headers=_headers(), files=_files()
        )
        conflict = await client.post(
            "/api/v1/documents/batches",
            headers=_headers(),
            files=_files(first=b"changed\n"),
        )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "spine.persistence.idempotency_conflict"


async def test_batch_polling_returns_all_items() -> None:
    service = FakeBatchIngestionService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        found = await client.get(
            f"/api/v1/ingestion/runs/{RUN_ID}", headers=_headers()
        )
    assert found.status_code == 200
    assert [item["input_identity"] for item in found.json()["items"]] == [
        "file-a",
        "file-b",
    ]


async def test_unknown_batch_run_fails_closed() -> None:
    service = FakeBatchIngestionService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/ingestion/runs/91000000-0000-0000-0000-000000000099",
            headers=_headers(),
        )

    assert response.status_code == 404
    assert response.json()["code"] == "spine.ingestion.not_found"
    assert "file-a" not in response.text


async def test_batch_rejects_too_many_files() -> None:
    service = FakeBatchIngestionService()
    count_transport = httpx.ASGITransport(app=_app(service, max_files=1))

    async with httpx.AsyncClient(
        transport=count_transport, base_url="http://test"
    ) as client:
        too_many = await client.post(
            "/api/v1/documents/batches", headers=_headers(), files=_files()
        )
    assert too_many.status_code == 400
    assert too_many.json()["code"] == "spine.ingestion.invalid_request"
    assert service.submissions == []


async def test_batch_rejects_oversized_file() -> None:
    service = FakeBatchIngestionService()
    size_transport = httpx.ASGITransport(app=_app(service, max_file_bytes=4))

    async with httpx.AsyncClient(
        transport=size_transport, base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/documents/batches", headers=_headers(), files=_files()
        )

    assert response.status_code == 413
    assert response.json()["code"] == "spine.ingestion.payload_too_large"
    assert service.submissions == []


async def test_batch_streaming_limit_applies_without_content_length() -> None:
    service = FakeBatchIngestionService()
    transport = httpx.ASGITransport(
        app=_app(service, max_file_bytes=64, max_batch_bytes=80)
    )
    boundary = "spine-boundary"
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file-a"; filename="a.md"\r\n'
        "Content-Type: text/markdown\r\n\r\n"
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/batches",
            headers={
                **_headers(),
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Transfer-Encoding": "chunked",
            },
            content=MultipartStream(prefix, b"protected bytes", suffix),
        )

    assert response.status_code == 413
    assert response.json()["code"] == "spine.ingestion.payload_too_large"
    assert "protected bytes" not in response.text
    assert service.submissions == []


async def test_duplicate_file_parts_are_rejected() -> None:
    service = FakeBatchIngestionService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        duplicate = await client.post(
            "/api/v1/documents/batches",
            headers=_headers(),
            files=[
                ("file-a", ("a.md", b"a", "text/markdown")),
                ("file-a", ("b.md", b"b", "text/markdown")),
            ],
        )
    assert duplicate.status_code == 400
    assert service.submissions == []


async def test_non_file_parts_are_rejected_without_content_leak() -> None:
    service = FakeBatchIngestionService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/batches",
            headers=_headers(),
            data={"file-a": "protected text"},
        )

    assert response.status_code == 400
    assert "protected text" not in response.text
    assert service.submissions == []


async def test_malformed_multipart_is_rejected_without_content_leak() -> None:
    service = FakeBatchIngestionService()
    transport = httpx.ASGITransport(app=_app(service))
    boundary = "spine-boundary"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/batches",
            headers={
                **_headers(),
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            content=b"protected malformed multipart",
        )

    assert response.status_code == 400
    assert response.json()["code"] == "spine.ingestion.invalid_request"
    assert "protected malformed multipart" not in response.text
    assert service.submissions == []


async def test_cross_workspace_upload_is_denied_before_parsing_or_service() -> None:
    service = FakeBatchIngestionService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/batches",
            headers=_headers(workspace_id=OTHER_WORKSPACE_ID),
            files=[
                (
                    "file-a",
                    ("secret.md", b"protected customer content", "text/markdown"),
                )
            ],
        )
    assert response.status_code == 403
    assert response.json()["code"] == "spine.authorization.denied"
    assert "protected customer content" not in response.text
    assert service.submissions == []


async def test_cross_workspace_status_is_denied_before_service() -> None:
    service = FakeBatchIngestionService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/ingestion/runs/{RUN_ID}",
            headers=_headers(workspace_id=OTHER_WORKSPACE_ID),
        )

    assert response.status_code == 403
    assert response.json()["code"] == "spine.authorization.denied"


async def test_valid_second_workspace_cannot_observe_first_workspace_run() -> None:
    service = FakeBatchIngestionService()
    transport = httpx.ASGITransport(app=_other_workspace_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/ingestion/runs/{RUN_ID}",
            headers=_headers(
                workspace_id=OTHER_WORKSPACE_ID,
                environment_id=OTHER_ENVIRONMENT_ID,
            ),
        )

    assert response.status_code == 404
    assert response.json()["code"] == "spine.ingestion.not_found"
    assert "file-a" not in response.text


async def test_cancelled_batch_preserves_completed_item_outcome() -> None:
    service = FakeBatchIngestionService()
    service.snapshot = replace(
        service.snapshot,
        run_state=IngestionRunState.CANCELLED,
        items=(
            service.snapshot.items[0],
            replace(
                service.snapshot.items[1],
                item_state=IngestionItemState.PROCESSING,
                processing_coverage=None,
                failures=(),
                failure_ids=(),
                retryable=False,
            ),
        ),
    )
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/ingestion/runs/{RUN_ID}", headers=_headers()
        )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert [item["status"] for item in response.json()["items"]] == [
        "succeeded",
        "cancelled",
    ]


def test_batch_openapi_declares_bounded_multipart_and_polling_contract() -> None:
    schema = _app(FakeBatchIngestionService()).openapi()
    operation = schema["paths"]["/api/v1/documents/batches"]["post"]
    body_schema = operation["requestBody"]["content"]["multipart/form-data"]["schema"]

    assert body_schema["maxProperties"] == 4
    assert body_schema["additionalProperties"] == {
        "type": "string",
        "format": "binary",
    }
    assert "TXT and Markdown" in body_schema["description"]
    assert any(
        parameter["name"] == "Idempotency-Key"
        for parameter in operation["parameters"]
    )
    assert "/api/v1/ingestion/runs/{run_id}" in schema["paths"]
