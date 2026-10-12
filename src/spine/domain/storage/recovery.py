"""Immutable, disclosure-safe contracts for backup coordination.

These types deliberately contain coordination evidence only.  They do not
contain object bytes, paths, credentials, provider errors, tenant content or
live storage authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, model_validator


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_LOWERCASE_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @staticmethod
    def _validate_safe_identifier(value: str, field_name: str) -> str:
        if not _SAFE_IDENTIFIER.fullmatch(value):
            raise ValueError(f"{field_name} must be a bounded safe identifier")
        return value

    @staticmethod
    def _validate_timestamp(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value


class FenceState(str, Enum):
    INACTIVE = "inactive"
    REQUESTED = "requested"
    DRAINING = "draining"
    QUIESCENT = "quiescent"
    SNAPSHOTTING = "snapshotting"
    VALIDATING = "validating"
    RELEASING = "releasing"
    RELEASED = "released"
    RELEASE_DEGRADED = "release_degraded"


class TicketAdmission(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    BLOCKED = "blocked"


class FenceOperationKind(str, Enum):
    FINALIZATION = "finalization"
    ASSOCIATION = "association"
    REFERENCE_GENERATION_MUTATION = "reference_generation_mutation"
    DELETION_CLEANUP = "deletion_cleanup"
    REPAIR_REASSOCIATION = "repair_reassociation"
    RETENTION_MUTATION = "retention_mutation"


class FenceOperationState(str, Enum):
    ADMITTED = "admitted"
    EXECUTING = "executing"
    TERMINAL_SUCCEEDED = "terminal_succeeded"
    TERMINAL_FAILED = "terminal_failed"
    RECONCILED_ORPHAN = "reconciled_orphan"
    INDETERMINATE = "indeterminate"
    CANCELLED_BEFORE_IRREVERSIBLE_BOUNDARY = "cancelled_before_irreversible_boundary"


class RecoveryFailureCategory(str, Enum):
    TIMEOUT = "timeout"
    COORDINATOR_CRASH = "coordinator_crash"
    COMPONENT_FAILURE = "component_failure"
    COMPONENT_IDENTITY_MISMATCH = "component_identity_mismatch"
    POSTGRES_RECOVERY_POSITION_MISMATCH = "postgres_recovery_position_mismatch"
    REFERENCED_OBJECT_MISSING = "referenced_object_missing"
    REFERENCED_OBJECT_CORRUPT = "referenced_object_corrupt"
    STALE_FENCE_EPOCH = "stale_fence_epoch"
    INDETERMINATE_OPERATION = "indeterminate_operation"
    INTEGRITY_FAILURE = "integrity_failure"


class RecoveryManifestProfile(str, Enum):
    COORDINATED = "coordinated"
    BEST_EFFORT = "best_effort"


class RecoveryManifestStatus(str, Enum):
    BUILDING = "building"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    COHERENT = "coherent"


class ComponentState(str, Enum):
    ABSENT = "absent"
    IDENTIFIED = "identified"
    DURABLE = "durable"
    VERIFIED = "verified"
    FAILED = "failed"


class RecoveryComponentEvidence(_Contract):
    """Sanitized verification result for one recovery component identity."""

    component: Literal["postgres", "object_volume"]
    expected_identity: str = Field(min_length=1, max_length=256)
    observed_identity: str = Field(min_length=1, max_length=256)
    durable: bool
    verified: bool
    verification_reference: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_evidence(self) -> "RecoveryComponentEvidence":
        for name in (
            "expected_identity",
            "observed_identity",
            "verification_reference",
        ):
            self._validate_safe_identifier(getattr(self, name), name)
        if self.verified and (
            not self.durable or self.expected_identity != self.observed_identity
        ):
            raise ValueError(
                "verified component evidence must be durable and identity-matched"
            )
        return self


def _digest_model(model: BaseModel, *, exclude: set[str] | None = None) -> str:
    payload = model.model_dump(mode="json", exclude=exclude or set())
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class BackupFenceLease(_Contract):
    """The mutable live lease; it is never copied into a manifest."""

    schema_version: PositiveInt = 1
    deployment_scope_id: str = Field(min_length=1, max_length=256)
    active_fence_epoch: PositiveInt | None = None
    fence_state: FenceState = FenceState.INACTIVE
    cutoff_ticket_sequence: NonNegativeInt | None = None
    lease_owner: str | None = Field(default=None, min_length=1, max_length=256)
    lease_expires_at: datetime | None = None
    lease_renewal: datetime | None = None
    takeover_reference: str | None = Field(default=None, min_length=1, max_length=256)
    ticket_admission: TicketAdmission = TicketAdmission.OPEN
    cleanup_release_progress: str = Field(default="none", min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_lease(self) -> "BackupFenceLease":
        self._validate_safe_identifier(self.deployment_scope_id, "deployment_scope_id")
        if self.lease_owner is not None:
            self._validate_safe_identifier(self.lease_owner, "lease_owner")
        if self.takeover_reference is not None:
            self._validate_safe_identifier(self.takeover_reference, "takeover_reference")
        self._validate_safe_identifier(
            self.cleanup_release_progress, "cleanup_release_progress"
        )
        for name in ("lease_expires_at", "lease_renewal"):
            value = getattr(self, name)
            if value is not None:
                self._validate_timestamp(value, name)
        active = self.fence_state not in {FenceState.INACTIVE, FenceState.RELEASED}
        if active and (
            self.active_fence_epoch is None
            or self.lease_owner is None
            or self.lease_expires_at is None
            or self.cutoff_ticket_sequence is None
        ):
            raise ValueError("active fence state requires complete lease evidence")
        if self.fence_state in {FenceState.INACTIVE, FenceState.RELEASED}:
            if self.ticket_admission is not TicketAdmission.OPEN:
                raise ValueError("inactive or released fence must admit tickets")
            if self.lease_owner is not None or self.lease_expires_at is not None:
                raise ValueError("released fence cannot retain a live lease owner")
        elif self.ticket_admission is TicketAdmission.OPEN:
            raise ValueError("active fence cannot leave ticket admission open")
        return self


class FenceOperationTicket(_Contract):
    """One durable, ordered operation admission record."""

    schema_version: PositiveInt = 1
    ticket_id: str = Field(min_length=1, max_length=256)
    deployment_scope_id: str = Field(min_length=1, max_length=256)
    ticket_sequence: PositiveInt
    fence_epoch: PositiveInt | None = None
    operation_kind: FenceOperationKind
    target_reference: str = Field(min_length=1, max_length=256)
    admitted_at: datetime
    state: FenceOperationState = FenceOperationState.ADMITTED
    terminal_at: datetime | None = None
    safe_failure_category: RecoveryFailureCategory | None = None
    correlation_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_ticket(self) -> "FenceOperationTicket":
        for name in ("ticket_id", "deployment_scope_id", "target_reference"):
            self._validate_safe_identifier(getattr(self, name), name)
        if self.correlation_id is not None:
            self._validate_safe_identifier(self.correlation_id, "correlation_id")
        self._validate_timestamp(self.admitted_at, "admitted_at")
        if self.terminal_at is not None:
            self._validate_timestamp(self.terminal_at, "terminal_at")
            if self.terminal_at < self.admitted_at:
                raise ValueError("terminal_at must follow admitted_at")
        terminal = self.state in {
            FenceOperationState.TERMINAL_SUCCEEDED,
            FenceOperationState.TERMINAL_FAILED,
            FenceOperationState.RECONCILED_ORPHAN,
            FenceOperationState.INDETERMINATE,
            FenceOperationState.CANCELLED_BEFORE_IRREVERSIBLE_BOUNDARY,
        }
        if terminal != (self.terminal_at is not None):
            raise ValueError("terminal ticket state and terminal_at disagree")
        if self.state is FenceOperationState.TERMINAL_FAILED:
            if self.safe_failure_category is None:
                raise ValueError("terminal failure requires a safe failure category")
        elif self.safe_failure_category is not None:
            raise ValueError("safe failure category only applies to terminal failure")
        return self


class QuiescentEvidence(_Contract):
    """Proof that the ticket barrier was closed at one durable watermark."""

    schema_version: PositiveInt = 1
    fence_epoch: PositiveInt
    cutoff_ticket_sequence: NonNegativeInt
    watermark: str = Field(min_length=1, max_length=256)
    pre_fence_terminal_ticket_count: NonNegativeInt
    indeterminate_ticket_count: int = 0
    captured_at: datetime
    verification_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_evidence(self) -> "QuiescentEvidence":
        self._validate_safe_identifier(self.watermark, "watermark")
        self._validate_timestamp(self.captured_at, "captured_at")
        if self.indeterminate_ticket_count != 0:
            raise ValueError("quiescent evidence cannot contain indeterminate tickets")
        if not _LOWERCASE_SHA256.fullmatch(self.verification_digest):
            raise ValueError("verification_digest must be lowercase sha256")
        if self.verification_digest != _digest_model(self, exclude={"verification_digest"}):
            raise ValueError("verification_digest does not match quiescent evidence")
        return self

    @classmethod
    def build(
        cls,
        *,
        fence_epoch: int,
        cutoff_ticket_sequence: int,
        watermark: str,
        pre_fence_terminal_ticket_count: int,
        captured_at: datetime,
    ) -> "QuiescentEvidence":
        draft = cls.model_construct(
            schema_version=1,
            fence_epoch=fence_epoch,
            cutoff_ticket_sequence=cutoff_ticket_sequence,
            watermark=watermark,
            pre_fence_terminal_ticket_count=pre_fence_terminal_ticket_count,
            indeterminate_ticket_count=0,
            captured_at=captured_at,
            verification_digest="0" * 64,
        )
        return cls.model_validate(
            draft.model_copy(
                update={
                    "verification_digest": _digest_model(
                        draft, exclude={"verification_digest"}
                    )
                }
            )
        )


class StorageRecoveryManifest(_Contract):
    """Immutable recovery evidence; live lease state is intentionally absent."""

    schema_version: PositiveInt = 1
    recovery_set_id: str = Field(min_length=1, max_length=256)
    deployment_scope_id: str = Field(min_length=1, max_length=256)
    profile: RecoveryManifestProfile
    manifest_status: RecoveryManifestStatus
    fence_epoch: PositiveInt
    cutoff_ticket_sequence: NonNegativeInt
    quiescent_evidence: QuiescentEvidence | None
    postgres_boundary: str = Field(min_length=1, max_length=256)
    postgres_recovery_watermark: str | None = Field(default=None, min_length=1, max_length=256)
    object_volume_generation: str = Field(min_length=1, max_length=256)
    postgres_component_state: ComponentState
    object_component_state: ComponentState
    postgres_backup_identity: str | None = Field(default=None, min_length=1, max_length=256)
    postgres_verification_reference: str | None = Field(
        default=None, min_length=1, max_length=256
    )
    object_verification_reference: str | None = Field(
        default=None, min_length=1, max_length=256
    )
    drained_operation_count: NonNegativeInt
    orphaned_operation_count: NonNegativeInt
    indeterminate_operation_count: NonNegativeInt
    integrity_algorithm: str = "sha256"
    manifest_digest: str = Field(min_length=64, max_length=64)
    scope: str = Field(min_length=1, max_length=256)
    start_watermark: str | None = Field(default=None, min_length=1, max_length=256)
    end_watermark: str | None = Field(default=None, min_length=1, max_length=256)
    created_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_manifest(self) -> "StorageRecoveryManifest":
        for name in (
            "recovery_set_id",
            "deployment_scope_id",
            "postgres_boundary",
            "object_volume_generation",
            "scope",
        ):
            self._validate_safe_identifier(getattr(self, name), name)
        if self.postgres_recovery_watermark is not None:
            self._validate_safe_identifier(
                self.postgres_recovery_watermark, "postgres_recovery_watermark"
            )
        if self.postgres_backup_identity is not None:
            self._validate_safe_identifier(
                self.postgres_backup_identity, "postgres_backup_identity"
            )
        for name in (
            "postgres_verification_reference",
            "object_verification_reference",
        ):
            value = getattr(self, name)
            if value is not None:
                self._validate_safe_identifier(value, name)
        for name in ("start_watermark", "end_watermark"):
            value = getattr(self, name)
            if value is not None:
                self._validate_safe_identifier(value, name)
        self._validate_timestamp(self.created_at, "created_at")
        if self.completed_at is not None:
            self._validate_timestamp(self.completed_at, "completed_at")
            if self.completed_at < self.created_at:
                raise ValueError("completed_at must follow created_at")
        if self.integrity_algorithm != "sha256":
            raise ValueError("only sha256 manifest integrity is supported")
        if not _LOWERCASE_SHA256.fullmatch(self.manifest_digest):
            raise ValueError("manifest_digest must be lowercase sha256")
        if self.profile is RecoveryManifestProfile.BEST_EFFORT and self.manifest_status is RecoveryManifestStatus.COHERENT:
            raise ValueError("best-effort recovery cannot be coherent")
        if self.manifest_status is RecoveryManifestStatus.COHERENT:
            if self.quiescent_evidence is None:
                raise ValueError("coherent manifest requires quiescent evidence")
            if self.quiescent_evidence.fence_epoch != self.fence_epoch:
                raise ValueError("quiescent evidence fence epoch must match manifest")
            if (
                self.quiescent_evidence.cutoff_ticket_sequence
                != self.cutoff_ticket_sequence
            ):
                raise ValueError(
                    "quiescent evidence cutoff must match manifest cutoff"
                )
            if self.postgres_recovery_watermark is None:
                raise ValueError("coherent manifest requires a recovery watermark")
            if self.quiescent_evidence.watermark != self.postgres_recovery_watermark:
                raise ValueError(
                    "quiescent evidence watermark must match manifest watermark"
                )
            if self.postgres_component_state is not ComponentState.VERIFIED:
                raise ValueError("coherent manifest requires verified PostgreSQL evidence")
            if self.object_component_state is not ComponentState.VERIFIED:
                raise ValueError("coherent manifest requires verified object evidence")
            if (
                self.postgres_verification_reference is None
                or self.object_verification_reference is None
            ):
                raise ValueError("coherent manifest requires verification references")
            if self.indeterminate_operation_count != 0:
                raise ValueError("coherent manifest cannot contain indeterminate operations")
            if self.completed_at is None:
                raise ValueError("coherent manifest requires completed_at")
        if self.manifest_digest != _digest_model(self, exclude={"manifest_digest"}):
            raise ValueError("manifest_digest does not match immutable manifest fields")
        return self

    @classmethod
    def build(cls, **values: Any) -> "StorageRecoveryManifest":
        values.setdefault("schema_version", 1)
        values.setdefault("integrity_algorithm", "sha256")
        values["manifest_digest"] = "0" * 64
        draft = cls.model_construct(**values)
        values["manifest_digest"] = _digest_model(draft, exclude={"manifest_digest"})
        return cls.model_validate(values)


_ALLOWED_TICKET_TRANSITIONS: dict[FenceOperationState, frozenset[FenceOperationState]] = {
    FenceOperationState.ADMITTED: frozenset(
        {
            FenceOperationState.EXECUTING,
            FenceOperationState.CANCELLED_BEFORE_IRREVERSIBLE_BOUNDARY,
        }
    ),
    FenceOperationState.EXECUTING: frozenset(
        {
            FenceOperationState.TERMINAL_SUCCEEDED,
            FenceOperationState.TERMINAL_FAILED,
            FenceOperationState.RECONCILED_ORPHAN,
            FenceOperationState.INDETERMINATE,
        }
    ),
}


def validate_ticket_transition(
    previous: FenceOperationState,
    current: FenceOperationState,
) -> None:
    """Reject non-monotonic or replayed ticket state transitions."""

    if previous is current:
        raise ValueError("ticket state transition must advance")
    if current not in _ALLOWED_TICKET_TRANSITIONS.get(previous, frozenset()):
        raise ValueError(f"invalid ticket transition: {previous.value} -> {current.value}")


__all__ = [
    "BackupFenceLease",
    "ComponentState",
    "FenceOperationKind",
    "FenceOperationState",
    "FenceOperationTicket",
    "FenceState",
    "QuiescentEvidence",
    "RecoveryFailureCategory",
    "RecoveryComponentEvidence",
    "RecoveryManifestProfile",
    "RecoveryManifestStatus",
    "StorageRecoveryManifest",
    "TicketAdmission",
    "validate_ticket_transition",
]
