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
    DELETION_PENDING = "deletion_pending"
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


class StoreEffectPermitStatus(str, Enum):
    ALLOWED = "allowed"
    AUTHORITY_NOT_CURRENT = "authority_not_current"
    OPERATION_NOT_CURRENT = "operation_not_current"
    UNAVAILABLE = "unavailable"


class DispositionAuthorityBinding(DefinitionModel):
    """Exact deletion-only authority and source-generation scope for one operation."""

    schema_version: Literal["source-disposition-authority-binding:v1"] = (
        "source-disposition-authority-binding:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    operation_id: UUID
    source_object_id: UUID
    expected_revision_id: UUID
    expected_acceptance_id: UUID
    expected_generation: int = Field(ge=1)
    tombstone_authorization_id: UUID
    authorization_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    deletion_policy_approval_id: UUID
    deletion_policy_version_id: UUID
    deletion_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    deletion_policy_generation: int = Field(ge=1)
    retention_eligibility_reference: UUID
    retention_eligibility_generation: int = Field(ge=1)
    issuer_authority_id: UUID
    issuer_generation: int = Field(ge=1)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_binding(self) -> "DispositionAuthorityBinding":
        if any(
            value.int == 0
            for value in (
                self.workspace_id,
                self.environment_id,
                self.operation_id,
                self.source_object_id,
                self.expected_revision_id,
                self.expected_acceptance_id,
                self.tombstone_authorization_id,
                self.deletion_policy_approval_id,
                self.deletion_policy_version_id,
                self.retention_eligibility_reference,
                self.issuer_authority_id,
            )
        ):
            raise ValueError("disposition authority identifiers must be non-zero")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("disposition authority expiry must be timezone-aware")
        return self


class DispositionAuthoritySnapshot(DefinitionModel):
    """Current pointer state read under the admission/revalidation boundary."""

    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    current_revision_id: UUID
    current_acceptance_id: UUID
    current_generation: int = Field(ge=1)
    tombstone_authorization_id: UUID
    authorization_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_active: bool
    deletion_policy_approval_id: UUID
    deletion_policy_version_id: UUID
    deletion_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    deletion_policy_generation: int = Field(ge=1)
    deletion_policy_active: bool
    retention_eligibility_reference: UUID
    retention_eligibility_generation: int = Field(ge=1)
    retention_eligibility_active: bool
    issuer_authority_id: UUID
    issuer_generation: int = Field(ge=1)
    issuer_active: bool
    read_processing_revoked: bool = False


def disposition_authority_is_current(
    binding: DispositionAuthorityBinding,
    snapshot: DispositionAuthoritySnapshot,
    *,
    evaluated_at: datetime,
) -> bool:
    """Match exact deletion authority while keeping read revocation independent."""

    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        return False
    return (
        evaluated_at < binding.expires_at
        and (
            snapshot.workspace_id,
            snapshot.environment_id,
            snapshot.source_object_id,
            snapshot.current_revision_id,
            snapshot.current_acceptance_id,
            snapshot.current_generation,
            snapshot.tombstone_authorization_id,
            snapshot.authorization_digest,
            snapshot.deletion_policy_approval_id,
            snapshot.deletion_policy_version_id,
            snapshot.deletion_policy_digest,
            snapshot.deletion_policy_generation,
            snapshot.retention_eligibility_reference,
            snapshot.retention_eligibility_generation,
            snapshot.issuer_authority_id,
            snapshot.issuer_generation,
        )
        == (
            binding.workspace_id,
            binding.environment_id,
            binding.source_object_id,
            binding.expected_revision_id,
            binding.expected_acceptance_id,
            binding.expected_generation,
            binding.tombstone_authorization_id,
            binding.authorization_digest,
            binding.deletion_policy_approval_id,
            binding.deletion_policy_version_id,
            binding.deletion_policy_digest,
            binding.deletion_policy_generation,
            binding.retention_eligibility_reference,
            binding.retention_eligibility_generation,
            binding.issuer_authority_id,
            binding.issuer_generation,
        )
        and snapshot.authorization_active
        and snapshot.deletion_policy_active
        and snapshot.retention_eligibility_active
        and snapshot.issuer_active
    )


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
    authority_binding: DispositionAuthorityBinding | None = None
    completion_ack_expires_at: datetime | None = None
    status: DispositionStatus = DispositionStatus.DELETION_PENDING
    fence_active: bool = True
    stores: tuple[DispositionStoreState, ...] = Field(min_length=1)
    safe_code: str = "DISPOSITION_PENDING"

    @model_validator(mode="after")
    def validate_scope_and_completion(self) -> "DispositionOperation":
        if any(value.int == 0 for value in (self.workspace_id, self.environment_id, self.operation_id)):
            raise ValueError("disposition scope identifiers must be non-zero")
        if self.authority_binding is not None and (
            self.authority_binding.workspace_id != self.workspace_id
            or self.authority_binding.environment_id != self.environment_id
            or self.authority_binding.operation_id != self.operation_id
        ):
            raise ValueError("disposition authority binding scope does not match operation")
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
            DispositionStatus.DELETION_PENDING,
            DispositionStatus.INCOMPLETE,
            DispositionStatus.UNKNOWN,
            DispositionStatus.CANCELLED,
        } and not self.fence_active:
            raise ValueError("unverified disposition must retain its fence")
        if self.status is DispositionStatus.BLOCKED and self.fence_active:
            raise ValueError("blocked pre-fence disposition cannot claim a persisted fence")
        return self
