"""Deterministic test doubles for the original-object storage port.

This module is test support, not a production storage adapter. The fake models
only observable port behavior and deliberately performs no filesystem, network,
database, or provider I/O.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from .contracts import (
    AuthorizedReadGrantRegistry,
    ConsumedOriginalReadLease,
    DeletionApproval,
    DeletionResult,
    IntegrityResult,
    IntegrityStatus,
    ObjectReference,
    ObservedContentIdentity,
    OriginalUploadCommandInput,
    ReconciliationInventoryEntry,
    UploadCommandState,
    UploadCommandStateName,
    WriteReceipt,
)
from .errors import (
    IntegrityMismatch,
    InvalidReadGrant,
    ObjectCorrupt,
    ObjectNotFound,
    ObjectProtected,
    ObjectUnavailable,
    ReconciliationRequired,
    StorageError,
    UploadConflict,
    UploadLimitExceeded,
    UploadNotFound,
)


@dataclass(slots=True)
class _StagedUpload:
    command: OriginalUploadCommandInput
    state: UploadCommandState
    content: bytearray = field(default_factory=bytearray)
    receipt: WriteReceipt | None = None
    integrity_mismatch: bool = False


class DeterministicOriginalObjectStoreFake:
    """Fast async test double with injected identities, time, and hard limits."""

    def __init__(
        self,
        *,
        grant_registry: AuthorizedReadGrantRegistry,
        generation_factory: Callable[[], UUID],
        clock: Callable[[], datetime],
        max_chunk_bytes: int,
        max_object_bytes: int,
        read_chunk_bytes: int,
    ) -> None:
        self._grant_registry = grant_registry
        self._generation_factory = generation_factory
        self._clock = clock
        self._max_chunk_bytes = max_chunk_bytes
        self._max_object_bytes = max_object_bytes
        self._read_chunk_bytes = read_chunk_bytes
        self._uploads: dict[UUID, _StagedUpload] = {}
        self._objects: dict[ObjectReference, bytes] = {}
        self._objects_by_id: dict[UUID, ObjectReference] = {}
        self._object_scopes: dict[ObjectReference, tuple[UUID, UUID | None]] = {}
        self._deletion_approvals: dict[ObjectReference, DeletionApproval] = {}
        self._deleted: set[ObjectReference] = set()

    async def begin_or_resume_upload(
        self,
        command: OriginalUploadCommandInput,
        state: UploadCommandState,
    ) -> UploadCommandState:
        if state.state is UploadCommandStateName.RECONCILIATION_REQUIRED:
            raise ReconciliationRequired()
        existing = self._uploads.get(state.upload_id)
        if existing is None:
            self._uploads[state.upload_id] = _StagedUpload(command=command, state=state)
            return state
        if (
            existing.command.workspace_id != command.workspace_id
            or existing.command.environment_id != command.environment_id
        ):
            raise UploadNotFound()
        if not self._same_upload_identity(existing.state, state):
            raise UploadConflict()
        if not self._same_command_semantics(existing.command, command):
            raise UploadConflict()
        if existing.receipt is not None:
            return existing.state
        if existing.state.state in {
            UploadCommandStateName.ABORTED,
            UploadCommandStateName.INTEGRITY_CONFLICT,
        }:
            return existing.state
        if state.state not in {
            UploadCommandStateName.STAGING,
            UploadCommandStateName.INTERRUPTED,
        }:
            raise UploadConflict()
        existing.state = (
            state.transitioned(UploadCommandStateName.STAGING)
            if state.state is UploadCommandStateName.INTERRUPTED
            else state
        )
        return existing.state

    async def write_chunks(
        self,
        upload_id: UUID,
        chunks: AsyncIterable[bytes],
    ) -> ObservedContentIdentity:
        upload = self._uploads.get(upload_id)
        if upload is None:
            raise UploadNotFound()
        if upload.state.state not in {
            UploadCommandStateName.STAGING,
            UploadCommandStateName.INTERRUPTED,
        }:
            raise UploadConflict()
        try:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise UploadLimitExceeded()
                if len(chunk) > self._max_chunk_bytes:
                    raise UploadLimitExceeded()
                if len(upload.content) + len(chunk) > self._max_object_bytes:
                    raise UploadLimitExceeded()
                upload.content.extend(chunk)
        except StorageError:
            raise
        except Exception:
            raise ObjectUnavailable() from None
        content = bytes(upload.content)
        return ObservedContentIdentity(
            observed_sha256=hashlib.sha256(content).hexdigest(),
            observed_byte_length=len(content),
        )

    async def finalize_upload(
        self,
        state: UploadCommandState,
        observed_content: ObservedContentIdentity,
    ) -> WriteReceipt:
        if state.state is UploadCommandStateName.RECONCILIATION_REQUIRED:
            raise ReconciliationRequired()
        upload = self._uploads.get(state.upload_id)
        if upload is None:
            raise UploadNotFound()
        if not self._same_upload_identity(upload.state, state):
            raise UploadConflict()
        if upload.integrity_mismatch:
            raise IntegrityMismatch()
        if upload.receipt is not None:
            if upload.receipt.observed_content != observed_content:
                raise UploadConflict()
            return upload.receipt
        if upload.state.state is UploadCommandStateName.ABORTED:
            raise ObjectProtected()
        content = bytes(upload.content)
        actual = ObservedContentIdentity(
            observed_sha256=hashlib.sha256(content).hexdigest(),
            observed_byte_length=len(content),
        )
        if actual != observed_content:
            raise UploadConflict()
        if (
            upload.command.expected_sha256 is not None
            and upload.command.expected_sha256 != actual.observed_sha256
        ) or (
            upload.command.expected_byte_length is not None
            and upload.command.expected_byte_length != actual.observed_byte_length
        ):
            upload.integrity_mismatch = True
            upload.state = upload.state.transitioned(
                UploadCommandStateName.INTEGRITY_CONFLICT
            )
            raise IntegrityMismatch()
        existing_reference = self._objects_by_id.get(state.object_id)
        if existing_reference is not None:
            raise UploadConflict()
        reference = ObjectReference(
            schema_version=1,
            object_id=state.object_id,
            storage_generation=self._generation_factory(),
            digest_algorithm="sha256",
            digest_hex=actual.observed_sha256,
            byte_length=actual.observed_byte_length,
        )
        receipt = WriteReceipt(
            schema_version=1,
            upload_id=state.upload_id,
            object_reference=reference,
            observed_content=actual,
            verification=IntegrityResult(
                status=IntegrityStatus.VERIFIED,
                algorithm="sha256",
                expected_digest=upload.command.expected_sha256,
                observed_digest=actual.observed_sha256,
                expected_length=upload.command.expected_byte_length,
                observed_length=actual.observed_byte_length,
            ),
            storage_status="finalized_verified",
            finalized_at=self._clock(),
        )
        upload.receipt = receipt
        upload.state = state.transitioned(
            UploadCommandStateName.FINALIZED,
            provisional_receipt=receipt,
        )
        self._objects[reference] = content
        self._objects_by_id[state.object_id] = reference
        self._object_scopes[reference] = (
            upload.command.workspace_id,
            upload.command.environment_id,
        )
        return receipt

    async def abort_upload(self, state: UploadCommandState) -> UploadCommandState:
        if state.state is UploadCommandStateName.RECONCILIATION_REQUIRED:
            raise ReconciliationRequired()
        upload = self._uploads.get(state.upload_id)
        if upload is None:
            raise UploadNotFound()
        if not self._same_upload_identity(upload.state, state):
            raise UploadConflict()
        if upload.state.state is UploadCommandStateName.ABORTED:
            return upload.state
        if upload.receipt is not None or upload.state.state in {
            UploadCommandStateName.FINALIZED,
            UploadCommandStateName.INTEGRITY_CONFLICT,
        }:
            raise ObjectProtected()
        upload.content.clear()
        upload.state = upload.state.transitioned(UploadCommandStateName.ABORTED)
        return upload.state

    async def open_bounded_read(
        self,
        lease: ConsumedOriginalReadLease,
        *,
        max_bytes: int,
    ) -> AsyncIterator[bytes]:
        if max_bytes <= 0:
            raise UploadLimitExceeded()
        binding = self._grant_registry.consume_read_lease(lease, now=self._clock())
        expected_scope = self._object_scopes.get(binding.object_reference)
        if expected_scope is None:
            raise ObjectNotFound()
        if expected_scope != (binding.workspace_id, binding.environment_id):
            raise InvalidReadGrant()
        content = self._objects.get(binding.object_reference)
        if content is None:
            raise ObjectNotFound()
        bounded = content[:max_bytes]
        for offset in range(0, len(bounded), self._read_chunk_bytes):
            yield bounded[offset : offset + self._read_chunk_bytes]

    async def verify(
        self,
        reference: ObjectReference,
        *,
        max_bytes: int,
    ) -> IntegrityResult:
        content = self._objects.get(reference)
        if content is None:
            raise ObjectNotFound()
        if max_bytes < len(content):
            raise UploadLimitExceeded()
        digest = hashlib.sha256(content).hexdigest()
        if digest != reference.digest_hex or len(content) != reference.byte_length:
            raise ObjectCorrupt()
        return IntegrityResult(
            status=IntegrityStatus.VERIFIED,
            algorithm="sha256",
            expected_digest=reference.digest_hex,
            observed_digest=digest,
            expected_length=reference.byte_length,
            observed_length=len(content),
        )

    async def prepare_deletion(
        self,
        reference: ObjectReference,
        approval: DeletionApproval,
    ) -> None:
        if reference not in self._objects:
            raise ObjectNotFound()
        self._deletion_approvals[reference] = approval

    async def delete_controlled(
        self,
        reference: ObjectReference,
        approval: DeletionApproval,
    ) -> DeletionResult:
        if self._deletion_approvals.get(reference) != approval:
            raise ObjectProtected()
        if reference in self._deleted:
            return DeletionResult(status="already_absent")
        if reference not in self._objects:
            raise ObjectNotFound()
        del self._objects[reference]
        self._deleted.add(reference)
        return DeletionResult(status="deleted")

    async def reconciliation_inventory(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[tuple[ReconciliationInventoryEntry, ...], str | None]:
        del cursor, limit
        entries = tuple(
            ReconciliationInventoryEntry(
                object_reference=reference,
                upload_id=self._upload_id_for(reference),
                finalized=True,
                observed_at=self._clock(),
            )
            for reference in sorted(self._objects, key=lambda item: item.object_id.int)
        )
        return entries, None

    def _upload_id_for(self, reference: ObjectReference) -> UUID:
        return next(
            upload.state.upload_id
            for upload in self._uploads.values()
            if upload.receipt is not None
            and upload.receipt.object_reference == reference
        )

    @staticmethod
    def _same_upload_identity(
        first: UploadCommandState,
        second: UploadCommandState,
    ) -> bool:
        return (
            first.issue8_receipt_id == second.issue8_receipt_id
            and first.request_digest == second.request_digest
            and first.upload_id == second.upload_id
            and first.object_id == second.object_id
        )

    @staticmethod
    def _same_command_semantics(
        first: OriginalUploadCommandInput,
        second: OriginalUploadCommandInput,
    ) -> bool:
        return (
            first.operation_name == second.operation_name
            and first.operation_schema_version == second.operation_schema_version
            and first.idempotency_key == second.idempotency_key
            and first.workspace_id == second.workspace_id
            and first.environment_id == second.environment_id
            and first.expected_sha256 == second.expected_sha256
            and first.expected_byte_length == second.expected_byte_length
            and first.purpose == second.purpose
        )


__all__ = ["DeterministicOriginalObjectStoreFake"]
