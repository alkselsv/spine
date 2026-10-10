"""Internal storage port; raw physical reads are deliberately not public."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from typing import Protocol
from uuid import UUID

from .contracts import (
    AuthorizedOriginalReadGrant,
    DeletionApproval,
    DeletionResult,
    IntegrityResult,
    ObjectReference,
    ObservedContentIdentity,
    OriginalUploadCommandInput,
    ReconciliationInventoryEntry,
    UploadCommandState,
    WriteReceipt,
)


class OriginalObjectStore(Protocol):
    """Trusted application port for immutable original bytes.

    Implementations must not add a raw ``read(reference)`` operation. Reading
    requires a one-shot grant issued by the authorized application gateway.
    """

    async def begin_or_resume_upload(
        self,
        command: OriginalUploadCommandInput,
        state: UploadCommandState,
    ) -> UploadCommandState: ...

    async def write_chunks(
        self,
        upload_id: UUID,
        chunks: AsyncIterable[bytes],
    ) -> ObservedContentIdentity: ...

    async def abort_upload(self, state: UploadCommandState) -> UploadCommandState: ...

    async def finalize_upload(
        self,
        state: UploadCommandState,
        observed_content: ObservedContentIdentity,
    ) -> WriteReceipt: ...

    def open_bounded_read(
        self,
        grant: AuthorizedOriginalReadGrant,
        *,
        max_bytes: int,
    ) -> AsyncIterator[bytes]: ...

    async def verify(
        self,
        reference: ObjectReference,
        *,
        max_bytes: int,
    ) -> IntegrityResult: ...

    async def reconciliation_inventory(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[tuple[ReconciliationInventoryEntry, ...], str | None]: ...

    async def prepare_deletion(
        self,
        reference: ObjectReference,
        approval: DeletionApproval,
    ) -> None: ...

    async def delete_controlled(
        self,
        reference: ObjectReference,
        approval: DeletionApproval,
    ) -> DeletionResult: ...


class AuthorizedOriginalReadService(Protocol):
    """Public application seam that resolves policy before opening storage."""

    def read_original(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID | None,
        source_revision_id: UUID,
        purpose: str,
        max_bytes: int,
    ) -> AsyncIterator[bytes]: ...


__all__ = ["AuthorizedOriginalReadService", "OriginalObjectStore"]
