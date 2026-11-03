from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.domain.artifacts.email_attachment import EmailAttachmentWriteReceipt
from spine.domain.sources.original_reference import ObjectReference
from spine.infrastructure.object_storage.contracts import (
    IntegrityResult,
    IntegrityStatus,
    ObservedContentIdentity,
    WriteReceipt,
)
from spine.infrastructure.persistence.email_attachment_uploads import (
    OriginalUploadEmailAttachmentWriter,
)


WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")
REVISION = UUID("40000000-0000-4000-8000-000000000001")


class UploadFake:
    def __init__(self, receipt: WriteReceipt) -> None:
        self.receipt = receipt
        self.calls = []

    async def upload(self, context, command, chunks):
        self.calls.append((context, command, chunks))
        return SimpleNamespace(write_receipt=self.receipt)


@pytest.mark.asyncio
async def test_original_upload_adapter_returns_verified_domain_receipt() -> None:
    content = b"attachment bytes"
    digest = hashlib.sha256(content).hexdigest()
    reference = ObjectReference(
        schema_version=1,
        object_id=UUID("30000000-0000-4000-8000-000000000001"),
        storage_generation=UUID("30000000-0000-4000-8000-000000000002"),
        digest_algorithm="sha256",
        digest_hex=digest,
        byte_length=len(content),
    )
    receipt = WriteReceipt(
        schema_version=1,
        upload_id=UUID("30000000-0000-4000-8000-000000000003"),
        object_reference=reference,
        observed_content=ObservedContentIdentity(
            observed_sha256=digest, observed_byte_length=len(content)
        ),
        verification=IntegrityResult(
            status=IntegrityStatus.VERIFIED,
            algorithm="sha256",
            expected_digest=digest,
            observed_digest=digest,
            expected_length=len(content),
            observed_length=len(content),
        ),
        storage_status="finalized_verified",
        finalized_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )
    uploads = UploadFake(receipt)
    adapter = OriginalUploadEmailAttachmentWriter(
        uploads=uploads,
        context_factory=lambda workspace_id, environment_id: (workspace_id, environment_id),
    )

    result = await adapter.store(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_revision_id=REVISION,
        part_path="1.2",
        extraction_profile="email-attachment-extractor:v1:" + "b" * 64,
        media_type="application/octet-stream",
        filename="attachment.bin",
        content=content,
        sha256=digest,
    )

    assert result == EmailAttachmentWriteReceipt(
        upload_id=receipt.upload_id,
        object_reference=reference,
        observed_sha256=digest,
        observed_byte_length=len(content),
        verification_status="verified",
        storage_status="finalized_verified",
        finalized_at=receipt.finalized_at,
    )
    assert len(uploads.calls) == 1
    assert uploads.calls[0][1].operation_name == "email.attachment.original"
