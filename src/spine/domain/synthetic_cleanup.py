"""Non-authorizing cleanup state machine for synthetic qualification only.

This contract has no source selector, provider identifier, retention term,
deletion permission, adapter, or production composition. `VERIFIED_SYNTHETIC`
is a test observation and is not a production authorization type.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel


class FakeOpaqueId(str, Enum):
    """Closed vocabulary for the synthetic fixture; arbitrary IDs are rejected."""

    OPERATION_A = "fake-operation-a"
    WORKSPACE_A = "fake-workspace-a"
    STORE_A = "fake-store-a"
    STORE_B = "fake-store-b"
    STORE_UNCOVERED = "fake-store-uncovered"


class SyntheticCleanupState(str, Enum):
    PROPOSED = "PROPOSED"
    BLOCKED = "BLOCKED"
    FENCED_TEST_ONLY = "FENCED_TEST_ONLY"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    CLEANUP_INCOMPLETE = "CLEANUP_INCOMPLETE"
    VERIFIED_SYNTHETIC = "VERIFIED_SYNTHETIC"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class SyntheticAuthorityStatus(str, Enum):
    CURRENT_TEST_ONLY = "CURRENT_TEST_ONLY"
    MISSING = "MISSING"
    STALE = "STALE"
    REVOKED = "REVOKED"


class SyntheticStoreStatus(str, Enum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class SyntheticEventKind(str, Enum):
    CHECK_AUTHORITY = "CHECK_AUTHORITY"
    START = "START"
    STORE_RESULT = "STORE_RESULT"
    COMMIT_UNKNOWN = "COMMIT_UNKNOWN"
    RETRY = "RETRY"
    RECONCILE_RECEIPT = "RECONCILE_RECEIPT"
    CANCEL = "CANCEL"
    RESTORE_REQUESTED = "RESTORE_REQUESTED"
    RESTORE_REPLAY_SUCCEEDED = "RESTORE_REPLAY_SUCCEEDED"


class SyntheticStoreReceipt(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store_ref: FakeOpaqueId
    status: SyntheticStoreStatus


class SyntheticCleanupJob(DefinitionModel):
    """Synthetic-only state; its opaque references never identify real sources."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["synthetic-cleanup-job:v1"] = "synthetic-cleanup-job:v1"
    operation_key: FakeOpaqueId
    workspace_ref: FakeOpaqueId
    required_store_refs: tuple[FakeOpaqueId, ...] = Field(min_length=1)
    state: SyntheticCleanupState = SyntheticCleanupState.PROPOSED
    authority_status: SyntheticAuthorityStatus = SyntheticAuthorityStatus.MISSING
    store_receipts: tuple[SyntheticStoreReceipt, ...] = ()
    retry_count: int = Field(default=0, ge=0)
    fence_active: bool = False
    restore_fenced: bool = True
    restore_replay_pending: bool = False
    restore_replay_verified: bool = False
    safe_code: Literal[
        "NONE",
        "AUTHORITY_MISSING",
        "AUTHORITY_STALE",
        "AUTHORITY_REVOKED",
        "STORE_FAILED",
        "STORE_UNKNOWN",
        "COMMIT_UNKNOWN",
        "RECONCILIATION_REQUIRED",
        "CANCELLED",
        "VERIFIED_SYNTHETIC",
        "INVALID_TRANSITION",
    ] = "NONE"

    @model_validator(mode="after")
    def validate_synthetic_scope(self) -> "SyntheticCleanupJob":
        if len(set(self.required_store_refs)) != len(self.required_store_refs):
            raise ValueError("synthetic required store references must be unique")
        receipt_refs = tuple(item.store_ref for item in self.store_receipts)
        if len(set(receipt_refs)) != len(receipt_refs):
            raise ValueError("synthetic store receipts must be unique")
        if not set(receipt_refs).issubset(self.required_store_refs):
            raise ValueError("synthetic receipt names an uncovered store")
        if self.state in {
            SyntheticCleanupState.CLEANUP_PENDING,
            SyntheticCleanupState.CLEANUP_INCOMPLETE,
            SyntheticCleanupState.VERIFIED_SYNTHETIC,
            SyntheticCleanupState.UNKNOWN,
            SyntheticCleanupState.CANCELLED,
        } and not self.fence_active:
            raise ValueError("active, unresolved, or cancelled work must remain fenced")
        if self.state is SyntheticCleanupState.VERIFIED_SYNTHETIC:
            if (
                set(receipt_refs) != set(self.required_store_refs)
                or any(item.status is not SyntheticStoreStatus.VERIFIED for item in self.store_receipts)
                or self.safe_code != "VERIFIED_SYNTHETIC"
            ):
                raise ValueError("synthetic verification requires verified receipts for every store")
        if self.restore_replay_verified and self.state is not SyntheticCleanupState.VERIFIED_SYNTHETIC:
            raise ValueError("restore replay requires synthetic cleanup verification")
        if self.restore_replay_pending and not self.restore_fenced:
            raise ValueError("pending restore replay must remain quarantined")
        if self.restore_replay_verified and (
            self.restore_fenced or self.restore_replay_pending
        ):
            raise ValueError("verified restore replay must follow quarantine and release it")
        return self


class SyntheticCleanupEvent(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SyntheticEventKind
    authority_status: SyntheticAuthorityStatus | None = None
    store_ref: FakeOpaqueId | None = None
    store_status: SyntheticStoreStatus | None = None
    receipts: tuple[SyntheticStoreReceipt, ...] = ()

    @model_validator(mode="after")
    def validate_event_shape(self) -> "SyntheticCleanupEvent":
        if self.kind in {
            SyntheticEventKind.CHECK_AUTHORITY,
            SyntheticEventKind.START,
            SyntheticEventKind.RETRY,
            SyntheticEventKind.STORE_RESULT,
            SyntheticEventKind.RECONCILE_RECEIPT,
        }:
            if self.authority_status is None:
                raise ValueError("authority-sensitive events require a synthetic authority status")
        elif self.authority_status is not None:
            raise ValueError("only authority-sensitive events may carry authority status")
        if self.kind is SyntheticEventKind.STORE_RESULT:
            if self.store_ref is None or self.store_status is None:
                raise ValueError("store result requires an opaque store reference, status and authority result")
        elif self.store_ref is not None or self.store_status is not None:
            raise ValueError("only store results may carry one store outcome")
        if self.kind is SyntheticEventKind.RECONCILE_RECEIPT:
            if not self.receipts:
                raise ValueError("receipt reconciliation requires explicit receipts")
        elif self.receipts:
            raise ValueError("only reconciliation may carry store receipts")
        return self


_AUTHORITY_CODES = {
    SyntheticAuthorityStatus.MISSING: "AUTHORITY_MISSING",
    SyntheticAuthorityStatus.STALE: "AUTHORITY_STALE",
    SyntheticAuthorityStatus.REVOKED: "AUTHORITY_REVOKED",
}


def transition_synthetic_cleanup(
    job: SyntheticCleanupJob,
    event: SyntheticCleanupEvent,
) -> SyntheticCleanupJob:
    """Apply one deterministic fake-only event; never invokes I/O or adapters."""

    if event.kind is SyntheticEventKind.CANCEL:
        if job.state is SyntheticCleanupState.UNKNOWN:
            return _invalid(job)
        return job.model_copy(
            update={
                "state": SyntheticCleanupState.CANCELLED,
                "fence_active": True,
                "safe_code": "CANCELLED",
            }
        )
    if event.kind is SyntheticEventKind.RESTORE_REQUESTED:
        return job.model_copy(
            update={
                "restore_fenced": True,
                "restore_replay_pending": True,
                "restore_replay_verified": False,
            }
        )
    if event.kind is SyntheticEventKind.RESTORE_REPLAY_SUCCEEDED:
        if (
            job.state is SyntheticCleanupState.VERIFIED_SYNTHETIC
            and job.restore_fenced
            and job.restore_replay_pending
        ):
            return job.model_copy(
                update={
                    "restore_fenced": False,
                    "restore_replay_pending": False,
                    "restore_replay_verified": True,
                }
            )
        return _invalid(job)

    if event.kind is SyntheticEventKind.CHECK_AUTHORITY:
        assert event.authority_status is not None
        if (
            job.state is SyntheticCleanupState.BLOCKED
            and job.authority_status is event.authority_status
        ) or (
            job.state is SyntheticCleanupState.FENCED_TEST_ONLY
            and event.authority_status is SyntheticAuthorityStatus.CURRENT_TEST_ONLY
        ):
            return job
        if job.state in {
            SyntheticCleanupState.CLEANUP_PENDING,
            SyntheticCleanupState.CLEANUP_INCOMPLETE,
        }:
            if event.authority_status is SyntheticAuthorityStatus.CURRENT_TEST_ONLY:
                return job
            return job.model_copy(
                update={
                    "state": SyntheticCleanupState.BLOCKED,
                    "authority_status": event.authority_status,
                    "safe_code": _AUTHORITY_CODES[event.authority_status],
                }
            )
        if job.state is not SyntheticCleanupState.PROPOSED:
            return _invalid(job)
        if event.authority_status is SyntheticAuthorityStatus.CURRENT_TEST_ONLY:
            return job.model_copy(
                update={
                    "state": SyntheticCleanupState.FENCED_TEST_ONLY,
                    "authority_status": event.authority_status,
                    "fence_active": True,
                    "safe_code": "NONE",
                }
            )
        return job.model_copy(
            update={
                "state": SyntheticCleanupState.BLOCKED,
                "authority_status": event.authority_status,
                "fence_active": job.fence_active,
                "safe_code": _AUTHORITY_CODES[event.authority_status],
            }
        )

    if event.kind is SyntheticEventKind.START:
        if job.state in {
            SyntheticCleanupState.CLEANUP_PENDING,
            SyntheticCleanupState.CLEANUP_INCOMPLETE,
            SyntheticCleanupState.VERIFIED_SYNTHETIC,
            SyntheticCleanupState.CANCELLED,
            SyntheticCleanupState.UNKNOWN,
        }:
            return job
        if job.state is SyntheticCleanupState.FENCED_TEST_ONLY:
            assert event.authority_status is not None
            if event.authority_status is SyntheticAuthorityStatus.CURRENT_TEST_ONLY:
                return job.model_copy(update={"state": SyntheticCleanupState.CLEANUP_PENDING})
            return job.model_copy(
                update={
                    "state": SyntheticCleanupState.BLOCKED,
                    "authority_status": event.authority_status,
                    "safe_code": _AUTHORITY_CODES[event.authority_status],
                }
            )
        return _invalid(job)

    if event.kind is SyntheticEventKind.STORE_RESULT:
        assert event.authority_status is not None
        if event.authority_status is not SyntheticAuthorityStatus.CURRENT_TEST_ONLY:
            if job.state in {
                SyntheticCleanupState.CLEANUP_PENDING,
                SyntheticCleanupState.CLEANUP_INCOMPLETE,
            }:
                return job.model_copy(
                    update={
                        "state": SyntheticCleanupState.BLOCKED,
                        "authority_status": event.authority_status,
                        "safe_code": _AUTHORITY_CODES[event.authority_status],
                    }
                )
            return _invalid(job)
        if job.state is SyntheticCleanupState.VERIFIED_SYNTHETIC:
            existing = next(
                (item for item in job.store_receipts if item.store_ref == event.store_ref),
                None,
            )
            if existing is not None and existing.status is event.store_status:
                return job
        if job.state not in {
            SyntheticCleanupState.CLEANUP_PENDING,
            SyntheticCleanupState.CLEANUP_INCOMPLETE,
        } or event.store_ref not in job.required_store_refs:
            return _invalid(job)
        assert event.store_ref is not None and event.store_status is not None
        receipts = {receipt.store_ref: receipt for receipt in job.store_receipts}
        receipts[event.store_ref] = SyntheticStoreReceipt(
            store_ref=event.store_ref,
            status=event.store_status,
        )
        ordered = tuple(receipts[key] for key in job.required_store_refs if key in receipts)
        if all(item.status is SyntheticStoreStatus.VERIFIED for item in ordered) and len(ordered) == len(job.required_store_refs):
            return job.model_copy(
                update={
                    "state": SyntheticCleanupState.VERIFIED_SYNTHETIC,
                    "store_receipts": ordered,
                    "safe_code": "VERIFIED_SYNTHETIC",
                }
            )
        failed = any(item.status is SyntheticStoreStatus.FAILED for item in ordered)
        unknown = any(item.status is SyntheticStoreStatus.UNKNOWN for item in ordered)
        return job.model_copy(
            update={
                "state": (
                    SyntheticCleanupState.UNKNOWN
                    if unknown
                    else SyntheticCleanupState.CLEANUP_INCOMPLETE
                ),
                "store_receipts": ordered,
                "safe_code": "STORE_UNKNOWN" if unknown else "STORE_FAILED" if failed else "NONE",
            }
        )

    if event.kind is SyntheticEventKind.COMMIT_UNKNOWN:
        if (
            job.state is SyntheticCleanupState.UNKNOWN
            and job.safe_code == "COMMIT_UNKNOWN"
        ):
            return job
        if job.state in {
            SyntheticCleanupState.CLEANUP_PENDING,
            SyntheticCleanupState.CLEANUP_INCOMPLETE,
        }:
            return job.model_copy(
                update={
                    "state": SyntheticCleanupState.UNKNOWN,
                    "fence_active": True,
                    "safe_code": "COMMIT_UNKNOWN",
                }
            )
        return _invalid(job)

    if event.kind is SyntheticEventKind.RETRY:
        assert event.authority_status is not None
        if job.state is SyntheticCleanupState.CLEANUP_PENDING:
            if event.authority_status is SyntheticAuthorityStatus.CURRENT_TEST_ONLY:
                return job
            return _invalid(job)
        if job.state is SyntheticCleanupState.CLEANUP_INCOMPLETE:
            if event.authority_status is not SyntheticAuthorityStatus.CURRENT_TEST_ONLY:
                return job.model_copy(
                    update={
                        "state": SyntheticCleanupState.BLOCKED,
                        "authority_status": event.authority_status,
                        "safe_code": _AUTHORITY_CODES[event.authority_status],
                    }
                )
            return job.model_copy(
                update={
                    "state": SyntheticCleanupState.CLEANUP_PENDING,
                    "authority_status": event.authority_status,
                    "retry_count": job.retry_count + 1,
                    "safe_code": "NONE",
                }
            )
        return _invalid(job)

    if event.kind is SyntheticEventKind.RECONCILE_RECEIPT:
        if job.state is not SyntheticCleanupState.UNKNOWN:
            return _invalid(job)
        assert event.authority_status is not None
        if event.authority_status is not SyntheticAuthorityStatus.CURRENT_TEST_ONLY:
            return job.model_copy(
                update={
                    "authority_status": event.authority_status,
                    "safe_code": _AUTHORITY_CODES[event.authority_status],
                }
            )
        receipt_refs = tuple(item.store_ref for item in event.receipts)
        if (
            len(set(receipt_refs)) != len(receipt_refs)
            or set(receipt_refs) != set(job.required_store_refs)
        ):
            return job.model_copy(
                update={"state": SyntheticCleanupState.UNKNOWN, "safe_code": "RECONCILIATION_REQUIRED"}
            )
        all_verified = all(item.status is SyntheticStoreStatus.VERIFIED for item in event.receipts)
        unresolved = any(item.status is SyntheticStoreStatus.UNKNOWN for item in event.receipts)
        return job.model_copy(
            update={
                "state": (
                    SyntheticCleanupState.UNKNOWN
                    if unresolved
                    else SyntheticCleanupState.VERIFIED_SYNTHETIC
                    if all_verified
                    else SyntheticCleanupState.CLEANUP_INCOMPLETE
                ),
                "store_receipts": tuple(
                    next(item for item in event.receipts if item.store_ref == ref)
                    for ref in job.required_store_refs
                ),
                "safe_code": (
                    "RECONCILIATION_REQUIRED"
                    if unresolved
                    else "VERIFIED_SYNTHETIC"
                    if all_verified
                    else "STORE_FAILED"
                ),
            }
        )

    return _invalid(job)


def _invalid(job: SyntheticCleanupJob) -> SyntheticCleanupJob:
    return job.model_copy(update={"safe_code": "INVALID_TRANSITION"})


__all__ = [
    "FakeOpaqueId",
    "SyntheticAuthorityStatus",
    "SyntheticCleanupEvent",
    "SyntheticCleanupJob",
    "SyntheticCleanupState",
    "SyntheticEventKind",
    "SyntheticStoreReceipt",
    "SyntheticStoreStatus",
    "transition_synthetic_cleanup",
]
