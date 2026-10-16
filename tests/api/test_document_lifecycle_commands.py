from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

import httpx

from spine.api.document_lifecycle_commands import install_document_lifecycle_command_routes
from spine.application.document_lifecycle import (
    LifecycleOperationKind,
    LifecycleOperationStatus,
    ReplaceDocumentCommand,
    SourceLifecycleOperationReceipt,
    TombstoneDocumentCommand,
)
from spine.application.persistence.errors import IdempotencyConflictError

from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    authorized_app,
)


SOURCE_OBJECT_ID = UUID("94000000-0000-0000-0000-000000000001")
CURRENT_REVISION_ID = UUID("94000000-0000-0000-0000-000000000002")
REPLACEMENT_REVISION_ID = UUID("94000000-0000-0000-0000-000000000003")
TOMBSTONE_REVISION_ID = UUID("94000000-0000-0000-0000-000000000004")
ACCEPTANCE_ID = UUID("94000000-0000-0000-0000-000000000005")
PROCESSING_RUN_ID = UUID("94000000-0000-0000-0000-000000000006")
RECEIPT_ID = UUID("94000000-0000-0000-0000-000000000007")
OPERATION_ID = UUID("94000000-0000-0000-0000-000000000008")
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


class FakeDocumentLifecycleService:
    def __init__(self) -> None:
        self.commands: list[ReplaceDocumentCommand | TombstoneDocumentCommand] = []
        self._digests: dict[str, str] = {}
        self.receipt = _receipt(LifecycleOperationKind.REPLACEMENT)

    async def replace(self, *, context: object, command: ReplaceDocumentCommand):
        del context
        self._record(command)
        self.commands.append(command)
        return self.receipt.model_copy(update={"replay": len(self.commands) > 1})

    async def tombstone(self, *, context: object, command: TombstoneDocumentCommand):
        del context
        self._record(command)
        self.commands.append(command)
        return _receipt(LifecycleOperationKind.TOMBSTONE).model_copy(
            update={
                "accepted_revision_id": TOMBSTONE_REVISION_ID,
                "processing_run_id": None,
                "status": LifecycleOperationStatus.COMPLETED,
                "completed_at": NOW,
                "replay": len(self.commands) > 1,
            }
        )

    async def status(self, *, context: object, operation_id: UUID):
        scope = getattr(context, "scope", None)
        if (
            getattr(scope, "workspace_id", None) != WORKSPACE_ID
            or getattr(scope, "environment_id", None) != ENVIRONMENT_ID
            or operation_id != OPERATION_ID
        ):
            return None
        return self.receipt

    def _record(self, command: ReplaceDocumentCommand | TombstoneDocumentCommand) -> None:
        digest = hashlib.sha256(
            json.dumps(
                command.model_dump(mode="json", exclude={"idempotency_key"}),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        previous = self._digests.setdefault(command.idempotency_key, digest)
        if previous != digest:
            raise IdempotencyConflictError("protected command data")


def _receipt(kind: LifecycleOperationKind) -> SourceLifecycleOperationReceipt:
    return SourceLifecycleOperationReceipt(
        operation_id=OPERATION_ID,
        operation_kind=kind,
        source_object_id=SOURCE_OBJECT_ID,
        expected_current_revision_id=CURRENT_REVISION_ID,
        accepted_revision_id=REPLACEMENT_REVISION_ID,
        canonical_acceptance_id=ACCEPTANCE_ID,
        processing_run_id=PROCESSING_RUN_ID,
        status=LifecycleOperationStatus.PROCESSING_DISPATCHED,
        idempotency_receipt_id=RECEIPT_ID,
        accepted_at=NOW,
        completed_at=None,
        safe_failure_code=None,
        retryable=False,
        replay=False,
    )


def _app(service: FakeDocumentLifecycleService):
    application = authorized_app([])
    install_document_lifecycle_command_routes(application, service=service)
    return application


def _headers(*, key: str = "lifecycle-001", workspace_id: UUID = WORKSPACE_ID):
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(workspace_id),
        "X-Environment-ID": str(ENVIRONMENT_ID),
        "Idempotency-Key": key,
    }


async def test_replacement_uses_trusted_scope_and_returns_typed_receipt() -> None:
    service = FakeDocumentLifecycleService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/documents/{SOURCE_OBJECT_ID}/replacement",
            headers=_headers(),
            json={
                "expected_current_revision_id": str(CURRENT_REVISION_ID),
                "upload_intent": {
                    "upload_identity": "handbook",
                    "declared_media_type": "text/markdown",
                    "declared_filename": "handbook.md",
                    "storage_request_reference": "staged-upload:request-001",
                },
            },
        )

    assert response.status_code == 202
    assert response.json() == {
        "schema_version": 1,
        "operation_id": str(OPERATION_ID),
        "operation_kind": "replacement",
        "source_object_id": str(SOURCE_OBJECT_ID),
        "expected_current_revision_id": str(CURRENT_REVISION_ID),
        "accepted_revision_id": str(REPLACEMENT_REVISION_ID),
        "canonical_acceptance_id": str(ACCEPTANCE_ID),
        "processing_run_id": str(PROCESSING_RUN_ID),
        "status": "processing_dispatched",
        "status_url": f"http://test/api/v1/documents/lifecycle/operations/{OPERATION_ID}",
        "idempotency_receipt_id": str(RECEIPT_ID),
        "accepted_at": "2026-10-16T12:00:00Z",
        "completed_at": None,
        "safe_failure_code": None,
        "retryable": False,
        "replay": False,
    }
    command = service.commands[0]
    assert command.workspace_id == WORKSPACE_ID
    assert command.environment_id == ENVIRONMENT_ID
    assert command.source_object_id == SOURCE_OBJECT_ID


async def test_tombstone_returns_terminal_receipt_without_processing() -> None:
    service = FakeDocumentLifecycleService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/documents/{SOURCE_OBJECT_ID}/tombstone",
            headers=_headers(),
            json={
                "expected_current_revision_id": str(CURRENT_REVISION_ID),
                "deletion_reason": "source_deleted",
            },
        )

    assert response.status_code == 202
    assert response.json()["operation_kind"] == "tombstone"
    assert response.json()["status"] == "completed"
    assert response.json()["processing_run_id"] is None


async def test_reused_key_with_changed_lifecycle_data_returns_safe_conflict() -> None:
    service = FakeDocumentLifecycleService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            f"/api/v1/documents/{SOURCE_OBJECT_ID}/tombstone",
            headers=_headers(),
            json={
                "expected_current_revision_id": str(CURRENT_REVISION_ID),
                "deletion_reason": "source_deleted",
            },
        )
        conflict = await client.post(
            f"/api/v1/documents/{SOURCE_OBJECT_ID}/tombstone",
            headers=_headers(),
            json={
                "expected_current_revision_id": str(CURRENT_REVISION_ID),
                "deletion_reason": "legal_erasure",
            },
        )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "spine.persistence.idempotency_conflict"
    assert "protected command data" not in conflict.text


async def test_lifecycle_request_rejects_browser_owned_authority_fields() -> None:
    service = FakeDocumentLifecycleService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/documents/{SOURCE_OBJECT_ID}/replacement",
            headers=_headers(),
            json={
                "expected_current_revision_id": str(CURRENT_REVISION_ID),
                "upload_intent": {
                    "upload_identity": "handbook",
                    "declared_media_type": "text/markdown",
                    "declared_filename": "handbook.md",
                    "storage_request_reference": "staged-upload:request-001",
                },
                "source_revision": {"revision_id": str(REPLACEMENT_REVISION_ID)},
            },
        )

    assert response.status_code == 422
    assert service.commands == []


async def test_lifecycle_status_is_scope_isolated() -> None:
    service = FakeDocumentLifecycleService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/documents/lifecycle/operations/{OPERATION_ID}",
            headers=_headers(workspace_id=OTHER_WORKSPACE_ID),
        )

    assert response.status_code in {403, 404}
    assert "handbook" not in response.text
