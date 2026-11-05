"""Versioned application port for external commit-fence providers.

The port describes the provider behavior a consumer needs. A matching Python
implementation or a passing fake does not qualify a real provider for F2; that
requires written, provider-specific evidence for revocation/commit ordering.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, StrictStr, model_validator

from spine.application.commit_fence import CommitFenceBinding
from spine.domain.common import ValidatedDefinitionModel


_CONTRACT_VERSION = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$"


def _require_utc(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be a UTC timestamp")


class CommitFenceOperation(str, Enum):
    ACQUIRE = "acquire"
    RENEW = "renew"
    PRE_COMMIT = "pre_commit"
    FINISH = "finish"
    ABORT = "abort"


class CommitFenceOperationState(str, Enum):
    ACQUIRED = "acquired"
    RENEWED = "renewed"
    VALID = "valid"
    DENIED = "denied"
    STALE_HOLDER = "stale_holder"
    EXPIRED = "expired"
    AMBIGUOUS = "ambiguous"
    RELEASED = "released"
    INDETERMINATE = "indeterminate"
    UNSUPPORTED = "unsupported"


class CommitFenceFailureReason(str, Enum):
    LOST_RESPONSE = "lost_response"
    CLOCK_UNCERTAIN = "clock_uncertain"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    REVOCATION_ACKNOWLEDGEMENT_PENDING = "revocation_acknowledgement_pending"


class CommitFencePermit(ValidatedDefinitionModel):
    """Opaque provider permit bound to one exact EF-1 authority binding."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["external-commit-fence-permit:v1"] = (
        "external-commit-fence-permit:v1"
    )
    binding: CommitFenceBinding
    provider_contract_version: StrictStr = Field(pattern=_CONTRACT_VERSION)
    permit_reference: UUID
    acquired_at: datetime
    valid_until: datetime

    @model_validator(mode="after")
    def validate_permit(self) -> "CommitFencePermit":
        if self.permit_reference.int == 0:
            raise ValueError("commit-fence permit reference must be non-zero")
        _require_utc(self.acquired_at, field_name="acquired_at")
        _require_utc(self.valid_until, field_name="valid_until")
        if not self.binding.is_valid_at(self.acquired_at):
            raise ValueError("commit-fence permit must be acquired within binding validity")
        if not self.acquired_at < self.valid_until <= self.binding.valid_until:
            raise ValueError("commit-fence permit validity must be bounded by its binding")
        return self


class CommitFenceOperationResult(ValidatedDefinitionModel):
    """Exact, versioned result of one provider-port operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["external-commit-fence-operation-result:v1"] = (
        "external-commit-fence-operation-result:v1"
    )
    binding: CommitFenceBinding
    provider_contract_version: StrictStr = Field(pattern=_CONTRACT_VERSION)
    operation: CommitFenceOperation
    state: CommitFenceOperationState
    evaluated_at: datetime
    permit: CommitFencePermit | None = None
    released_permit_reference: UUID | None = None
    failure_reason: CommitFenceFailureReason | None = None

    @model_validator(mode="after")
    def validate_operation_result(self) -> "CommitFenceOperationResult":
        _require_utc(self.evaluated_at, field_name="evaluated_at")
        expected_success = {
            CommitFenceOperation.ACQUIRE: CommitFenceOperationState.ACQUIRED,
            CommitFenceOperation.RENEW: CommitFenceOperationState.RENEWED,
            CommitFenceOperation.PRE_COMMIT: CommitFenceOperationState.VALID,
            CommitFenceOperation.FINISH: CommitFenceOperationState.RELEASED,
            CommitFenceOperation.ABORT: CommitFenceOperationState.RELEASED,
        }[self.operation]
        if self.state is expected_success:
            if self.operation in {CommitFenceOperation.FINISH, CommitFenceOperation.ABORT}:
                if self.permit is not None or self.released_permit_reference is None:
                    raise ValueError("release success requires only the released permit reference")
                if self.released_permit_reference.int == 0:
                    raise ValueError("released permit reference must be non-zero")
            else:
                if self.permit is None or self.released_permit_reference is not None:
                    raise ValueError("successful permit operation requires a live permit")
                if self.permit.binding != self.binding:
                    raise ValueError("provider result permit must retain the exact requested binding")
                if self.permit.provider_contract_version != self.provider_contract_version:
                    raise ValueError("provider result and permit contract versions must match")
                if not self.evaluated_at < self.permit.valid_until:
                    raise ValueError("permit expiry equality or past expiry denies the operation")
        elif self.permit is not None or self.released_permit_reference is not None:
            raise ValueError("unsuccessful provider result cannot carry a permit")

        if self.state in {
            CommitFenceOperationState.AMBIGUOUS,
            CommitFenceOperationState.INDETERMINATE,
        } and self.failure_reason is None:
            raise ValueError("ambiguous or indeterminate result requires a failure reason")
        if self.state not in {
            CommitFenceOperationState.AMBIGUOUS,
            CommitFenceOperationState.INDETERMINATE,
        } and self.failure_reason is not None:
            raise ValueError("failure reason is reserved for ambiguous or indeterminate results")
        return self

    @property
    def binding_digest(self) -> str:
        return self.binding.calculate_digest()

    @property
    def permits_commit(self) -> bool:
        """Only an unexpired VALID pre-commit result permits proceeding."""

        return (
            self.operation is CommitFenceOperation.PRE_COMMIT
            and self.state is CommitFenceOperationState.VALID
            and self.permit is not None
            and self.evaluated_at < self.permit.valid_until
        )


class ExternalCommitFencePort(Protocol):
    """Required F2 operations; adapters must fail closed on unknown outcomes.

    A provider may issue a permit only if its authority contract guarantees
    that revocation acknowledgement waits until every conflicting permit is
    finished or expired. Local expiry, a second epoch read, and mock tests are
    not evidence of that guarantee.
    """

    provider_contract_version: str

    async def acquire(self, binding: CommitFenceBinding) -> CommitFenceOperationResult: ...

    async def validate_before_commit(
        self, permit: CommitFencePermit, *, evaluated_at: datetime
    ) -> CommitFenceOperationResult: ...

    async def finish(self, permit: CommitFencePermit) -> CommitFenceOperationResult: ...

    async def abort(self, permit: CommitFencePermit) -> CommitFenceOperationResult: ...


class RenewableExternalCommitFencePort(ExternalCommitFencePort, Protocol):
    """Optional renewal capability for a named provider contract."""

    async def renew(
        self, permit: CommitFencePermit, *, evaluated_at: datetime
    ) -> CommitFenceOperationResult: ...


__all__ = [
    "CommitFenceFailureReason",
    "CommitFenceOperation",
    "CommitFenceOperationResult",
    "CommitFenceOperationState",
    "CommitFencePermit",
    "ExternalCommitFencePort",
    "RenewableExternalCommitFencePort",
]
