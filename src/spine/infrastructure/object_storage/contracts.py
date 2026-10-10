"""Provider-neutral contracts for immutable original-object storage."""

from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from spine.application.persistence.command_digest import COMMAND_DIGEST_ALGORITHM_VERSION
from spine.domain.sources.original_reference import ObjectReference, StorageModel

from .errors import InvalidReadGrant


_DIGEST = r"[0-9a-f]{64}"
_IDENTIFIER = r"[a-z][a-z0-9_.:-]{0,63}"
_IDENTIFIER_PATTERN = re.compile(_IDENTIFIER)
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
DigestHex = Annotated[str, Field(pattern=_DIGEST)]
BoundedIdentifier = Annotated[str, Field(min_length=1, max_length=64, pattern=_IDENTIFIER)]
OpaqueText = Annotated[str, Field(min_length=1, max_length=256)]


class DigestAlgorithm(str, Enum):
    SHA256 = "sha256"


class StorageStatus(str, Enum):
    FINALIZED_VERIFIED = "finalized_verified"


class UploadCommandStateName(str, Enum):
    STAGING = "staging"
    INTERRUPTED = "interrupted"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    FINALIZED = "finalized"
    ABORTED = "aborted"
    INTEGRITY_CONFLICT = "integrity_conflict"


class IntegrityStatus(str, Enum):
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"
    INDETERMINATE = "indeterminate"


class OriginalUploadCommandInput(StorageModel):
    """Trusted semantic inputs supplied before a byte stream is consumed."""

    operation_name: BoundedIdentifier
    operation_schema_version: PositiveInt
    idempotency_key: OpaqueText
    workspace_id: UUID
    environment_id: UUID | None = None
    expected_sha256: DigestHex | None = None
    expected_byte_length: NonNegativeInt | None = None
    declared_media_type: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    declared_filename: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    purpose: BoundedIdentifier

    @model_validator(mode="after")
    def validate_scope(self) -> OriginalUploadCommandInput:
        if self.workspace_id.int == 0:
            raise ValueError("workspace_id must be non-zero")
        if self.environment_id is not None and self.environment_id.int == 0:
            raise ValueError("environment_id must be non-zero")
        return self


class OriginalUploadCommandPayload(StorageModel):
    """Canonical semantic payload included in the pre-write digest."""

    workspace_id: UUID
    environment_id: UUID | None = None
    purpose: BoundedIdentifier
    expected_sha256: DigestHex | None = None
    expected_byte_length: NonNegativeInt | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> OriginalUploadCommandPayload:
        if self.workspace_id.int == 0:
            raise ValueError("workspace_id must be non-zero")
        if self.environment_id is not None and self.environment_id.int == 0:
            raise ValueError("environment_id must be non-zero")
        return self


class PreWriteRequestDigest(StorageModel):
    """Issue #8 canonical digest identifying semantic command meaning."""

    algorithm: Literal["sha256"]
    algorithm_version: BoundedIdentifier
    digest_hex: DigestHex

    @model_validator(mode="after")
    def validate_algorithm_version(self) -> PreWriteRequestDigest:
        if self.algorithm_version != COMMAND_DIGEST_ALGORITHM_VERSION:
            raise ValueError("unsupported Issue #8 command digest algorithm version")
        return self


class ObservedContentIdentity(StorageModel):
    """Digest and length observed from the actual streamed bytes."""

    observed_sha256: DigestHex
    observed_byte_length: NonNegativeInt


class IntegrityResult(StorageModel):
    status: IntegrityStatus
    algorithm: Literal["sha256"]
    expected_digest: DigestHex | None = None
    observed_digest: DigestHex | None = None
    expected_length: NonNegativeInt | None = None
    observed_length: NonNegativeInt | None = None

    @model_validator(mode="after")
    def validate_verified_evidence(self) -> IntegrityResult:
        if self.status is IntegrityStatus.VERIFIED:
            if self.observed_digest is None or self.observed_length is None:
                raise ValueError("verified integrity requires observed evidence")
            if self.expected_digest is not None and self.expected_digest != self.observed_digest:
                raise ValueError("verified integrity digest does not match expectation")
            if self.expected_length is not None and self.expected_length != self.observed_length:
                raise ValueError("verified integrity length does not match expectation")
        return self


class WriteReceipt(StorageModel):
    """Verified physical evidence; it does not assert admission or authorization."""

    schema_version: PositiveInt
    upload_id: UUID
    object_reference: ObjectReference
    observed_content: ObservedContentIdentity
    verification: IntegrityResult
    storage_status: Literal["finalized_verified"]
    finalized_at: datetime

    @model_validator(mode="after")
    def validate_receipt(self) -> WriteReceipt:
        if self.upload_id.int == 0:
            raise ValueError("upload_id must be non-zero")
        if self.verification.status is not IntegrityStatus.VERIFIED:
            raise ValueError("a write receipt requires verified integrity")
        if self.verification.observed_digest != self.observed_content.observed_sha256:
            raise ValueError("receipt digest evidence is inconsistent")
        if self.verification.observed_length != self.observed_content.observed_byte_length:
            raise ValueError("receipt length evidence is inconsistent")
        if self.object_reference.digest_hex != self.observed_content.observed_sha256:
            raise ValueError("reference digest evidence is inconsistent")
        if self.object_reference.byte_length != self.observed_content.observed_byte_length:
            raise ValueError("reference length evidence is inconsistent")
        if self.finalized_at.tzinfo is None:
            raise ValueError("finalized_at must be timezone-aware")
        return self


class Issue8ResultReference(StorageModel):
    """Opaque typed reference to the canonical Issue #8 receipt."""

    receipt_id: UUID

    @model_validator(mode="after")
    def validate_id(self) -> Issue8ResultReference:
        if self.receipt_id.int == 0:
            raise ValueError("receipt_id must be non-zero")
        return self


class OriginalUploadCommandResult(StorageModel):
    """Server-issued identities returned after the Issue #8 receipt claim."""

    request_digest: PreWriteRequestDigest
    upload_id: UUID
    object_id: UUID
    issue8_result_reference: Issue8ResultReference

    @model_validator(mode="after")
    def validate_ids(self) -> OriginalUploadCommandResult:
        if self.upload_id.int == 0 or self.object_id.int == 0:
            raise ValueError("upload identities must be non-zero")
        return self


class UploadCommandState(StorageModel):
    issue8_receipt_id: UUID
    request_digest: PreWriteRequestDigest
    upload_id: UUID
    object_id: UUID
    state: UploadCommandStateName
    provisional_receipt: WriteReceipt | None = None

    @model_validator(mode="after")
    def validate_ids_and_state(self) -> UploadCommandState:
        if self.issue8_receipt_id.int == 0 or self.upload_id.int == 0 or self.object_id.int == 0:
            raise ValueError("upload identities must be non-zero")
        if self.state is UploadCommandStateName.FINALIZED and self.provisional_receipt is None:
            raise ValueError("finalized state requires a provisional receipt")
        if self.provisional_receipt is not None and self.state is not UploadCommandStateName.FINALIZED:
            raise ValueError("provisional receipt is only valid for finalized state")
        if self.provisional_receipt is not None:
            if self.provisional_receipt.upload_id != self.upload_id:
                raise ValueError("receipt upload identity does not match state")
            if self.provisional_receipt.object_reference.object_id != self.object_id:
                raise ValueError("receipt object identity does not match state")
        return self

    def transitioned(
        self,
        state: UploadCommandStateName,
        *,
        provisional_receipt: WriteReceipt | None = None,
    ) -> UploadCommandState:
        """Return the next state when the lifecycle transition is legal."""

        transitions = {
            UploadCommandStateName.STAGING: {
                UploadCommandStateName.INTERRUPTED,
                UploadCommandStateName.FINALIZED,
                UploadCommandStateName.ABORTED,
                UploadCommandStateName.INTEGRITY_CONFLICT,
                UploadCommandStateName.RECONCILIATION_REQUIRED,
            },
            UploadCommandStateName.INTERRUPTED: {
                UploadCommandStateName.STAGING,
                UploadCommandStateName.FINALIZED,
                UploadCommandStateName.ABORTED,
                UploadCommandStateName.INTEGRITY_CONFLICT,
                UploadCommandStateName.RECONCILIATION_REQUIRED,
            },
            UploadCommandStateName.RECONCILIATION_REQUIRED: {
                UploadCommandStateName.STAGING,
                UploadCommandStateName.FINALIZED,
                UploadCommandStateName.ABORTED,
                UploadCommandStateName.INTEGRITY_CONFLICT,
            },
            UploadCommandStateName.FINALIZED: set(),
            UploadCommandStateName.ABORTED: set(),
            UploadCommandStateName.INTEGRITY_CONFLICT: set(),
        }
        if state not in transitions[self.state]:
            raise ValueError(f"invalid upload state transition: {self.state} -> {state}")
        if provisional_receipt is not None and state is not UploadCommandStateName.FINALIZED:
            raise ValueError("a provisional receipt only applies to finalization")
        return type(self).model_validate(
            self.model_dump() | {
                "state": state,
                "provisional_receipt": provisional_receipt,
            }
        )


@dataclass(frozen=True, slots=True)
class _ReadGrantBinding:
    workspace_id: UUID
    environment_id: UUID | None
    source_revision_id: UUID
    object_reference: ObjectReference
    purpose: str
    authorization_decision_version: str
    expires_at: datetime


_GRANT_CREATION_TOKEN = object()


class AuthorizedOriginalReadGrant:
    """Opaque process-local capability for one authorized bounded read."""

    __slots__ = ("__registry", "__token")

    def __init__(
        self,
        *,
        registry: AuthorizedReadGrantRegistry,
        token: bytes,
        _creation_token: object,
    ) -> None:
        if _creation_token is not _GRANT_CREATION_TOKEN:
            raise TypeError("read grants must be issued by AuthorizedReadGrantRegistry")
        self.__registry = registry
        self.__token = token

    def __repr__(self) -> str:
        return "<AuthorizedOriginalReadGrant opaque one-shot capability>"

    def __copy__(self) -> AuthorizedOriginalReadGrant:
        raise TypeError("read grants cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> AuthorizedOriginalReadGrant:
        raise TypeError("read grants cannot be copied")

    def __getstate__(self) -> None:
        raise TypeError("read grants cannot be serialized")

    def __reduce__(self) -> None:
        raise TypeError("read grants cannot be serialized")

    def _belongs_to(self, registry: AuthorizedReadGrantRegistry) -> bool:
        return self.__registry is registry

    def _token(self) -> bytes:
        return self.__token


class ConsumedOriginalReadLease:
    """Opaque process-local capability created only after grant consumption."""

    __slots__ = ("__registry", "__token")

    def __init__(
        self,
        *,
        registry: AuthorizedReadGrantRegistry,
        token: bytes,
        _creation_token: object,
    ) -> None:
        if _creation_token is not _GRANT_CREATION_TOKEN:
            raise TypeError("read leases must be created by AuthorizedReadGrantRegistry")
        self.__registry = registry
        self.__token = token

    def __repr__(self) -> str:
        return "<ConsumedOriginalReadLease opaque one-shot capability>"

    def __copy__(self) -> ConsumedOriginalReadLease:
        raise TypeError("read leases cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> ConsumedOriginalReadLease:
        raise TypeError("read leases cannot be copied")

    def __getstate__(self) -> None:
        raise TypeError("read leases cannot be serialized")

    def __reduce__(self) -> None:
        raise TypeError("read leases cannot be serialized")

    def _belongs_to(self, registry: AuthorizedReadGrantRegistry) -> bool:
        return self.__registry is registry

    def _token(self) -> bytes:
        return self.__token


class AuthorizedReadGrantRegistry:
    """Process-local issuer and one-shot consumer for read capabilities."""

    def __init__(self) -> None:
        self.__records: dict[bytes, _ReadGrantBinding] = {}
        self.__lease_records: dict[bytes, _ReadGrantBinding] = {}
        self.__lock = threading.RLock()

    def issue(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID | None,
        source_revision_id: UUID,
        object_reference: ObjectReference,
        purpose: str,
        authorization_decision_version: str,
        expires_at: datetime,
    ) -> AuthorizedOriginalReadGrant:
        _validate_grant_binding(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_revision_id=source_revision_id,
            object_reference=object_reference,
            purpose=purpose,
            authorization_decision_version=authorization_decision_version,
            expires_at=expires_at,
        )
        token = secrets.token_bytes(32)
        binding = _ReadGrantBinding(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_revision_id=source_revision_id,
            object_reference=object_reference,
            purpose=purpose,
            authorization_decision_version=authorization_decision_version,
            expires_at=expires_at,
        )
        with self.__lock:
            self.__records[token] = binding
        return AuthorizedOriginalReadGrant(
            registry=self,
            token=token,
            _creation_token=_GRANT_CREATION_TOKEN,
        )

    def consume(
        self,
        grant: AuthorizedOriginalReadGrant,
        *,
        workspace_id: UUID,
        environment_id: UUID | None,
        source_revision_id: UUID,
        object_reference: ObjectReference,
        now: datetime | None = None,
    ) -> ConsumedOriginalReadLease:
        if not isinstance(grant, AuthorizedOriginalReadGrant) or not grant._belongs_to(self):
            raise InvalidReadGrant()
        with self.__lock:
            binding = self.__records.pop(grant._token(), None)
        if binding is None:
            raise InvalidReadGrant()
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None or current_time >= binding.expires_at:
            raise InvalidReadGrant()
        if (
            binding.workspace_id != workspace_id
            or binding.environment_id != environment_id
            or binding.source_revision_id != source_revision_id
            or binding.object_reference != object_reference
        ):
            raise InvalidReadGrant()
        lease_token = secrets.token_bytes(32)
        with self.__lock:
            self.__lease_records[lease_token] = binding
        return ConsumedOriginalReadLease(
            registry=self,
            token=lease_token,
            _creation_token=_GRANT_CREATION_TOKEN,
        )

    def consume_read_lease(
        self,
        lease: ConsumedOriginalReadLease,
        *,
        now: datetime | None = None,
    ) -> _ReadGrantBinding:
        """Consume a lease at the internal raw-read adapter boundary."""

        if not isinstance(lease, ConsumedOriginalReadLease) or not lease._belongs_to(self):
            raise InvalidReadGrant()
        with self.__lock:
            binding = self.__lease_records.pop(lease._token(), None)
        if binding is None:
            raise InvalidReadGrant()
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None or current_time >= binding.expires_at:
            raise InvalidReadGrant()
        return binding


def _validate_grant_binding(
    *,
    workspace_id: UUID,
    environment_id: UUID | None,
    source_revision_id: UUID,
    object_reference: ObjectReference,
    purpose: str,
    authorization_decision_version: str,
    expires_at: datetime,
) -> None:
    if (
        not isinstance(workspace_id, UUID)
        or not isinstance(source_revision_id, UUID)
        or workspace_id.int == 0
        or source_revision_id.int == 0
    ):
        raise ValueError("grant identities must be non-zero")
    if environment_id is not None and (
        not isinstance(environment_id, UUID) or environment_id.int == 0
    ):
        raise ValueError("environment_id must be non-zero")
    if not isinstance(object_reference, ObjectReference):
        raise ValueError("object_reference must be an ObjectReference")
    if not isinstance(purpose, str):
        raise ValueError("grant purpose must be a bounded identifier")
    if _IDENTIFIER_PATTERN.fullmatch(purpose) is None:
        raise ValueError("grant purpose must be a bounded identifier")
    if (
        not isinstance(authorization_decision_version, str)
        or not authorization_decision_version
        or len(authorization_decision_version) > 256
        or _IDENTIFIER_PATTERN.fullmatch(authorization_decision_version) is None
    ):
        raise ValueError("authorization decision version is invalid")
    if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware")


class ReconciliationInventoryEntry(StorageModel):
    """Sanitized inventory metadata; never a physical path or provider locator."""

    object_reference: ObjectReference
    upload_id: UUID | None
    finalized: bool
    observed_at: datetime

    @model_validator(mode="after")
    def validate_entry(self) -> ReconciliationInventoryEntry:
        if self.upload_id is not None and self.upload_id.int == 0:
            raise ValueError("upload_id must be non-zero")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return self


class DeletionApproval(StorageModel):
    """Application-issued proof that a physical deletion may be attempted."""

    command_id: UUID
    approval_reference: UUID

    @model_validator(mode="after")
    def validate_id(self) -> DeletionApproval:
        if self.command_id.int == 0 or self.approval_reference.int == 0:
            raise ValueError("deletion approval identities must be non-zero")
        return self


class DeletionResult(StorageModel):
    status: Literal["deleted", "already_absent", "protected", "failed"]


__all__ = [
    "AuthorizedOriginalReadGrant",
    "AuthorizedReadGrantRegistry",
    "ConsumedOriginalReadLease",
    "DeletionApproval",
    "DeletionResult",
    "DigestAlgorithm",
    "IntegrityResult",
    "IntegrityStatus",
    "Issue8ResultReference",
    "ObjectReference",
    "ObservedContentIdentity",
    "OriginalUploadCommandInput",
    "OriginalUploadCommandPayload",
    "OriginalUploadCommandResult",
    "PreWriteRequestDigest",
    "ReconciliationInventoryEntry",
    "StorageStatus",
    "StorageModel",
    "UploadCommandState",
    "UploadCommandStateName",
    "WriteReceipt",
]
