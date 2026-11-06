"""Content-free durable state for source disposition operations."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier


class DispositionStatus(str, Enum):
    BLOCKED = "blocked"
    FENCED = "fenced"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"
    VERIFIED = "verified"
    CANCELLED = "cancelled"


class StoreDispositionStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DispositionStoreState(DefinitionModel):
    store_id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    status: StoreDispositionStatus = StoreDispositionStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    result_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")

    @model_validator(mode="after")
    def validate_attempt_result(self) -> "DispositionStoreState":
        if self.status in {
            StoreDispositionStatus.PENDING,
            StoreDispositionStatus.IN_PROGRESS,
        } and self.result_code is not None:
            raise ValueError("pending or in-progress store state cannot carry a result code")
        if (
            self.status not in {
                StoreDispositionStatus.PENDING,
                StoreDispositionStatus.IN_PROGRESS,
            }
            and self.attempts == 0
            and self.result_code != "UNKNOWN_STORE"
        ):
            raise ValueError("store outcomes require at least one attempt")
        if self.status is StoreDispositionStatus.IN_PROGRESS and self.attempts == 0:
            raise ValueError("in-progress store state requires a persisted attempt")
        return self


class DispositionOperation(DefinitionModel):
    """A tenant/environment scoped operation without source-identifying data."""

    schema_version: Literal["source-disposition-operation:v1"] = "source-disposition-operation:v1"
    workspace_id: UUID
    environment_id: UUID
    operation_id: UUID
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state_version: int = Field(default=1, ge=1)
    declared_store_ids: tuple[VersionIdentifier, ...] = Field(min_length=1)
    completion_ack_expires_at: datetime | None = None
    status: DispositionStatus = DispositionStatus.FENCED
    fence_active: bool = True
    stores: tuple[DispositionStoreState, ...] = Field(min_length=1)
    safe_code: str = "DISPOSITION_PENDING"

    @model_validator(mode="after")
    def validate_scope_and_completion(self) -> "DispositionOperation":
        if any(value.int == 0 for value in (self.workspace_id, self.environment_id, self.operation_id)):
            raise ValueError("disposition scope identifiers must be non-zero")
        store_ids = tuple(store.store_id for store in self.stores)
        if len(set(store_ids)) != len(store_ids):
            raise ValueError("disposition store IDs must be unique")
        if len(set(self.declared_store_ids)) != len(self.declared_store_ids):
            raise ValueError("declared disposition store IDs must be unique")
        if self.status is DispositionStatus.VERIFIED:
            if any(store.status is not StoreDispositionStatus.VERIFIED for store in self.stores):
                raise ValueError("verified disposition requires every store to verify")
            if self.safe_code != "DISPOSITION_VERIFIED":
                raise ValueError("verified disposition requires its safe result code")
            if self.completion_ack_expires_at is None:
                raise ValueError("verified disposition requires a bounded acknowledgement deadline")
            if (
                self.completion_ack_expires_at.tzinfo is None
                or self.completion_ack_expires_at.utcoffset() is None
            ):
                raise ValueError("completion acknowledgement deadline must be timezone-aware")
        if self.status in {
            DispositionStatus.FENCED,
            DispositionStatus.INCOMPLETE,
            DispositionStatus.UNKNOWN,
            DispositionStatus.CANCELLED,
        } and not self.fence_active:
            raise ValueError("unverified disposition must retain its fence")
        if self.status is DispositionStatus.BLOCKED and self.fence_active:
            raise ValueError("blocked pre-fence disposition cannot claim a persisted fence")
        return self
