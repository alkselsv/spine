from collections.abc import AsyncIterator
from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID

from spine.domain.sources import (
    SourceRevisionReceiptAssociation,
    VerificationCleanupOutcome,
    content_revision,
)
from spine.domain.sources.original_reference import ObjectReference
from spine.infrastructure.object_storage import (
    AuthorizedReadGrantRegistry,
    ExactAssociationContentReader,
)


NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def _contracts(content: bytes):
    digest = sha256(content).hexdigest()
    reference = ObjectReference(
        schema_version=1,
        object_id=UUID(int=5),
        storage_generation=UUID(int=6),
        digest_algorithm="sha256",
        digest_hex=digest,
        byte_length=len(content),
    )
    revision = content_revision(
        revision_id=UUID(int=4),
        source_object_id=UUID(int=3),
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        kind="content",
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata={"embedded_title": "Synthetic"},
        original_reference=reference,
        original_sha256=digest,
        byte_length=len(content),
        media_type="text/plain",
        observed_at=NOW,
    )
    association = SourceRevisionReceiptAssociation(
        association_id=UUID(int=7),
        source_object_id=revision.source_object_id,
        revision_id=revision.revision_id,
        provenance_id=UUID(int=8),
        workspace_id=revision.workspace_id,
        environment_id=revision.environment_id,
        original_receipt_kind="issue6.write_receipt",
        original_receipt_id=UUID(int=9),
        original_command_result_id=UUID(int=10),
        original_command_result_schema_version=1,
        original_upload_id=UUID(int=11),
        original_reference=reference,
        original_sha256=digest,
        byte_length=len(content),
        finalized_at=NOW,
        associated_at=NOW,
    )
    return revision, association


async def test_exact_association_reader_uses_one_shot_lease_and_idempotent_cleanup() -> None:
    content = b"hello\n"
    revision, association = _contracts(content)
    registry = AuthorizedReadGrantRegistry()

    class _Store:
        def open_bounded_read(self, lease: object, *, max_bytes: int) -> AsyncIterator[bytes]:
            registry.consume_read_lease(lease, now=NOW)  # type: ignore[arg-type]
            assert max_bytes == len(content)

            async def stream() -> AsyncIterator[bytes]:
                yield content

            return stream()

    reader = ExactAssociationContentReader(
        object_store=_Store(),  # type: ignore[arg-type]
        grant_registry=registry,
        clock=lambda: NOW,
    )

    observed = b"".join(
        [
            chunk
            async for chunk in reader.read_exact(
                revision=revision,
                association=association,
                max_bytes=len(content),
            )
        ]
    )

    assert observed == content
    for _ in range(2):
        assert (
            await reader.cleanup(revision=revision, association=association)
            is VerificationCleanupOutcome.NOT_REQUIRED
        )
