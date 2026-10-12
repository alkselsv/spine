"""Application contracts for trusted original-upload orchestration."""

from __future__ import annotations

from enum import Enum
from types import TracebackType
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, PositiveInt, model_validator

from spine.application.persistence.idempotency import IdempotencyRepository
from spine.application.persistence.context import TrustedPersistenceContext
from spine.infrastructure.object_storage.contracts import (
    ObservedContentIdentity,
    OriginalUploadCommandInput,
    OriginalUploadCommandResult,
    UploadCommandState,
    UploadCommandStateName,
    WriteReceipt,
)
from spine.infrastructure.object_storage.errors import StorageErrorCategory


class UploadCleanupStatus(str, Enum):
    """Physical cleanup progress, separate from terminal logical abort."""

    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class OriginalUploadRecord(BaseModel):
    """Durable application state for one idempotent upload command."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: PositiveInt = 1
    record_version: PositiveInt
    command: OriginalUploadCommandInput
    result: OriginalUploadCommandResult
    state: UploadCommandState
    observed_content: ObservedContentIdentity | None = None
    cleanup_status: UploadCleanupStatus = UploadCleanupStatus.NOT_APPLICABLE
    cleanup_error_category: StorageErrorCategory | None = None

    @model_validator(mode="after")
    def validate_consistency(self) -> "OriginalUploadRecord":
        if self.result.request_digest != self.state.request_digest:
            raise ValueError("upload request digest is inconsistent")
        if self.result.upload_id != self.state.upload_id:
            raise ValueError("upload identity is inconsistent")
        if self.result.object_id != self.state.object_id:
            raise ValueError("object identity is inconsistent")
        if self.result.issue8_result_reference.receipt_id != self.state.issue8_receipt_id:
            raise ValueError("idempotency receipt identity is inconsistent")
        if self.state.state is UploadCommandStateName.FINALIZED:
            receipt = self.state.provisional_receipt
            if receipt is None or self.observed_content != receipt.observed_content:
                raise ValueError("finalized upload evidence is inconsistent")
        if self.state.state is UploadCommandStateName.ABORTED:
            if self.cleanup_status is UploadCleanupStatus.NOT_APPLICABLE:
                raise ValueError("aborted upload requires separate cleanup state")
        elif self.cleanup_status is not UploadCleanupStatus.NOT_APPLICABLE:
            raise ValueError("cleanup state only applies to an aborted upload")
        if (
            self.cleanup_status is UploadCleanupStatus.FAILED
        ) != (self.cleanup_error_category is not None):
            raise ValueError("cleanup failure category is inconsistent")
        return self


class OriginalUploadOutcome(BaseModel):
    """Verified physical result exposed only after durable finalization state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    result: OriginalUploadCommandResult
    write_receipt: WriteReceipt
    replay: bool

    @model_validator(mode="after")
    def validate_result(self) -> "OriginalUploadOutcome":
        if self.write_receipt.upload_id != self.result.upload_id:
            raise ValueError("write receipt upload identity is inconsistent")
        if self.write_receipt.object_reference.object_id != self.result.object_id:
            raise ValueError("write receipt object identity is inconsistent")
        return self


class OriginalUploadAbortOutcome(BaseModel):
    """Terminal abort outcome with independently observable cleanup progress."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    result: OriginalUploadCommandResult
    state: UploadCommandStateName
    cleanup_status: UploadCleanupStatus
    cleanup_error_category: StorageErrorCategory | None = None
    replay: bool

    @model_validator(mode="after")
    def validate_abort(self) -> "OriginalUploadAbortOutcome":
        if self.state is not UploadCommandStateName.ABORTED:
            raise ValueError("abort outcome must be terminally aborted")
        if self.cleanup_status is UploadCleanupStatus.NOT_APPLICABLE:
            raise ValueError("abort outcome requires cleanup state")
        if (
            self.cleanup_status is UploadCleanupStatus.FAILED
        ) != (self.cleanup_error_category is not None):
            raise ValueError("cleanup failure category is inconsistent")
        return self


class OriginalUploadReconciliationOutcome(BaseModel):
    """Evidence-based result of a trusted upload reconciliation attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    result: OriginalUploadCommandResult
    state: UploadCommandStateName
    write_receipt: WriteReceipt | None = None
    resolved: bool
    changed: bool

    @model_validator(mode="after")
    def validate_reconciliation(self) -> "OriginalUploadReconciliationOutcome":
        if self.state is UploadCommandStateName.FINALIZED and self.write_receipt is None:
            raise ValueError("finalized reconciliation requires a write receipt")
        if self.write_receipt is not None:
            if self.write_receipt.upload_id != self.result.upload_id:
                raise ValueError("reconciled receipt upload identity is inconsistent")
            if self.write_receipt.object_reference.object_id != self.result.object_id:
                raise ValueError("reconciled receipt object identity is inconsistent")
        if self.resolved != (self.state is not UploadCommandStateName.RECONCILIATION_REQUIRED):
            raise ValueError("reconciliation resolution flag is inconsistent")
        return self


class OriginalUploadRepository(Protocol):
    async def add(self, record: OriginalUploadRecord) -> None: ...

    async def resolve(self, upload_id: UUID) -> OriginalUploadRecord | None: ...

    async def replace(
        self,
        record: OriginalUploadRecord,
        *,
        expected_version: int,
    ) -> None: ...


class OriginalUploadUnitOfWork(Protocol):
    @property
    def idempotency(self) -> IdempotencyRepository: ...

    @property
    def original_uploads(self) -> OriginalUploadRepository: ...

    async def __aenter__(self) -> "OriginalUploadUnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class OriginalUploadUnitOfWorkFactory(Protocol):
    def __call__(
        self,
        context: TrustedPersistenceContext,
    ) -> OriginalUploadUnitOfWork: ...


__all__ = [
    "OriginalUploadAbortOutcome",
    "OriginalUploadOutcome",
    "OriginalUploadReconciliationOutcome",
    "OriginalUploadRecord",
    "OriginalUploadRepository",
    "OriginalUploadUnitOfWork",
    "OriginalUploadUnitOfWorkFactory",
    "UploadCleanupStatus",
]
