"""Infrastructure adapter for storing email MIME part originals."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterable, Callable
from uuid import UUID

from spine.application.email_attachments import EmailAttachmentUnavailable
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.storage.upload import OriginalUploadOrchestrator
from spine.domain.artifacts.email_attachment import EmailAttachmentWriteReceipt
from spine.infrastructure.object_storage.contracts import OriginalUploadCommandInput


class OriginalUploadEmailAttachmentWriter:
    """Write MIME part bytes through the idempotent original upload use case."""

    def __init__(
        self,
        *,
        uploads: OriginalUploadOrchestrator,
        context_factory: Callable[[UUID, UUID], TrustedPersistenceContext],
    ) -> None:
        self._uploads = uploads
        self._context_factory = context_factory

    async def store(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_revision_id: UUID,
        part_path: str,
        extraction_profile: str,
        media_type: str,
        filename: str | None,
        content: bytes,
        sha256: str,
    ) -> EmailAttachmentWriteReceipt:
        if hashlib.sha256(content).hexdigest() != sha256:
            raise EmailAttachmentUnavailable()
        idempotency_key = "email-attachment:" + hashlib.sha256(
            f"{workspace_id}:{environment_id}:{source_revision_id}:{part_path}:{extraction_profile}".encode()
        ).hexdigest()
        command = OriginalUploadCommandInput(
            operation_name="email.attachment.original",
            operation_schema_version=1,
            idempotency_key=idempotency_key,
            workspace_id=workspace_id,
            environment_id=environment_id,
            expected_sha256=sha256,
            expected_byte_length=len(content),
            declared_media_type=media_type,
            declared_filename=filename,
            purpose="email.attachment",
        )

        async def chunks() -> AsyncIterable[bytes]:
            yield content

        outcome = await self._uploads.upload(
            self._context_factory(workspace_id, environment_id), command, chunks()
        )
        receipt = outcome.write_receipt
        return EmailAttachmentWriteReceipt(
            upload_id=receipt.upload_id,
            object_reference=receipt.object_reference,
            observed_sha256=receipt.observed_content.observed_sha256,
            observed_byte_length=receipt.observed_content.observed_byte_length,
            verification_status=receipt.verification.status.value,
            storage_status=receipt.storage_status,
            finalized_at=receipt.finalized_at,
        )
