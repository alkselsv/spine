"""Evidence-based reconciliation for original-object storage.

The adapter owns physical inspection.  This module owns the comparison with
canonical receipt/reference evidence and deliberately never infers provenance
from a digest alone.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from spine.domain.sources import SourceRevisionReceiptAssociation
from spine.application.storage.contracts import OriginalUploadRecord, UploadCleanupStatus
from spine.infrastructure.object_storage.contracts import (
    DeletionApproval,
    DeletionResult,
    IntegrityResult,
    IntegrityStatus,
    ObjectReference,
    ReconciliationInventoryEntry,
    UploadCommandStateName,
)
from spine.infrastructure.object_storage.errors import (
    ObjectCorrupt,
    ObjectNotFound,
    ObjectProtected,
    StorageError,
    StorageErrorCategory,
)
from .diagnostics import (
    NullStorageOutcomeRecorder,
    StorageOutcome,
    StorageOutcomeKind,
    StorageOutcomeRecorder,
    StorageOutcomeStatus,
    record_storage_outcome_safely,
)


class ReconciliationFindingKind(str, Enum):
    """Safe classifications emitted by a reconciliation pass."""

    ORPHAN = "orphan"
    MISSING = "missing"
    CORRUPT = "corrupt"
    AMBIGUOUS = "ambiguous"
    STAGED = "staged"
    CLEANUP_PENDING = "cleanup_pending"


class CanonicalOriginalReference(BaseModel):
    """The canonical receipt/reference facts needed for physical comparison."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: UUID
    environment_id: UUID
    source_revision_id: UUID
    upload_id: UUID
    object_reference: ObjectReference

    @classmethod
    def from_association(
        cls,
        association: SourceRevisionReceiptAssociation,
    ) -> "CanonicalOriginalReference":
        """Project one canonical receipt association into reconciliation facts."""

        return cls(
            workspace_id=association.workspace_id,
            environment_id=association.environment_id,
            source_revision_id=association.revision_id,
            upload_id=association.original_upload_id,
            object_reference=association.original_reference,
        )

    @model_validator(mode="after")
    def validate_identities(self) -> "CanonicalOriginalReference":
        for name in (
            "workspace_id",
            "environment_id",
            "source_revision_id",
            "upload_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if self.object_reference.object_id.int == 0:
            raise ValueError("object reference identity must be non-zero")
        return self


class ReconciliationFinding(BaseModel):
    """A disclosure-safe reconciliation observation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: ReconciliationFindingKind
    workspace_id: UUID | None = None
    environment_id: UUID | None = None
    object_reference: ObjectReference | None = None
    upload_id: UUID | None = None
    source_revision_id: UUID | None = None
    category: StorageErrorCategory | None = None
    observed_at: datetime

    @model_validator(mode="after")
    def validate_finding(self) -> "ReconciliationFinding":
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.object_reference is None and self.kind is ReconciliationFindingKind.ORPHAN:
            raise ValueError("an orphan finding requires an object reference")
        if self.kind is ReconciliationFindingKind.MISSING and self.object_reference is None:
            raise ValueError("a missing finding requires an object reference")
        return self


class ReconciliationReport(BaseModel):
    """One bounded, restartable reconciliation page."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    findings: tuple[ReconciliationFinding, ...]
    next_cursor: str | None
    complete: bool

    @model_validator(mode="after")
    def validate_cursor(self) -> "ReconciliationReport":
        if self.complete != (self.next_cursor is None):
            raise ValueError("report completion does not match its cursor")
        return self


class ReconciliationStorage(Protocol):
    async def reconciliation_inventory(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
        workspace_id: UUID | None = None,
        environment_id: UUID | None = None,
    ) -> tuple[tuple[ReconciliationInventoryEntry, ...], str | None]: ...

    async def verify(
        self,
        reference: ObjectReference,
        *,
        max_bytes: int,
    ) -> IntegrityResult: ...

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


class OriginalStorageReconciler:
    """Compare sanitized physical inventory with canonical receipt evidence."""

    def __init__(
        self,
        *,
        object_store: ReconciliationStorage,
        clock: Callable[[], datetime] | None = None,
        outcome_recorder: StorageOutcomeRecorder | None = None,
    ) -> None:
        self._object_store = object_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._outcome_recorder = outcome_recorder or NullStorageOutcomeRecorder()

    async def reconcile_page(
        self,
        canonical_references: Iterable[CanonicalOriginalReference],
        *,
        workspace_id: UUID,
        environment_id: UUID | None,
        trace_id: UUID | None = None,
        upload_records: Iterable[OriginalUploadRecord] = (),
        cursor: str | None = None,
        limit: int = 100,
        max_verify_bytes: int,
    ) -> ReconciliationReport:
        canonical = tuple(
            item
            for item in canonical_references
            if item.workspace_id == workspace_id
            and item.environment_id == environment_id
        )
        inventory, next_cursor = await self._object_store.reconciliation_inventory(
            cursor=cursor,
            limit=limit,
            workspace_id=workspace_id,
            environment_id=environment_id,
        )
        by_reference: dict[ObjectReference, list[CanonicalOriginalReference]] = {}
        by_object_id: dict[UUID, list[CanonicalOriginalReference]] = {}
        for item in canonical:
            by_reference.setdefault(item.object_reference, []).append(item)
            by_object_id.setdefault(item.object_reference.object_id, []).append(item)

        findings = [
            self._classify_inventory_entry(
                entry,
                by_reference,
                by_object_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
            )
            for entry in inventory
        ]
        findings.extend(
            self._classify_upload_record(record)
            for record in upload_records
            if record.command.workspace_id == workspace_id
            and record.command.environment_id == environment_id
        )
        for item in canonical:
            finding = await self._verify_canonical_reference(
                item,
                max_verify_bytes=max_verify_bytes,
            )
            if finding is not None:
                findings.append(finding)
        report = ReconciliationReport(
            findings=tuple(finding for finding in findings if finding is not None),
            next_cursor=next_cursor,
            complete=next_cursor is None,
        )
        for finding in report.findings:
            await self._record_finding_outcome(finding, trace_id=trace_id)
        return report

    async def cleanup_orphan(
        self,
        finding: ReconciliationFinding,
        approval: DeletionApproval,
        *,
        workspace_id: UUID,
        environment_id: UUID | None,
        canonical_references: Iterable[CanonicalOriginalReference],
        trace_id: UUID | None = None,
    ) -> DeletionResult:
        """Delete a reported orphan after rechecking current canonical state."""

        if (
            finding.kind is not ReconciliationFindingKind.ORPHAN
            or finding.object_reference is None
            or finding.workspace_id != workspace_id
            or finding.environment_id != environment_id
        ):
            raise ObjectProtected()
        if any(
            item.workspace_id == finding.workspace_id
            and item.environment_id == finding.environment_id
            and item.object_reference.object_id == finding.object_reference.object_id
            for item in canonical_references
        ):
            raise ObjectProtected()
        await self._object_store.prepare_deletion(finding.object_reference, approval)
        result = await self._object_store.delete_controlled(
            finding.object_reference,
            approval,
        )
        if trace_id is not None:
            await record_storage_outcome_safely(
                self._outcome_recorder,
                StorageOutcome(
                    workspace_id=workspace_id,
                    environment_id=environment_id,
                    trace_id=trace_id,
                    operation=StorageOutcomeKind.DELETION,
                    status=StorageOutcomeStatus.APPROVED_DELETION,
                    subject_id=finding.object_reference.object_id,
                    producer_deduplication_id=(
                        f"storage:deletion:{finding.object_reference.object_id}:approved"
                    ),
                ),
            )
        return result

    async def _record_finding_outcome(
        self,
        finding: ReconciliationFinding,
        *,
        trace_id: UUID | None,
    ) -> None:
        if trace_id is None or finding.workspace_id is None:
            return
        operation = {
            ReconciliationFindingKind.ORPHAN: StorageOutcomeKind.ORPHAN,
            ReconciliationFindingKind.MISSING: StorageOutcomeKind.MISSING,
            ReconciliationFindingKind.CORRUPT: StorageOutcomeKind.INTEGRITY,
        }.get(finding.kind, StorageOutcomeKind.RECONCILIATION)
        status = (
            StorageOutcomeStatus.DEGRADED
            if finding.kind is ReconciliationFindingKind.ORPHAN
            else StorageOutcomeStatus.UNAVAILABLE
            if finding.kind is ReconciliationFindingKind.MISSING
            else StorageOutcomeStatus.INDETERMINATE
        )
        subject_id = (
            finding.object_reference.object_id
            if finding.object_reference is not None
            else finding.upload_id
        )
        if subject_id is None:
            return
        await record_storage_outcome_safely(
            self._outcome_recorder,
            StorageOutcome(
                workspace_id=finding.workspace_id,
                environment_id=finding.environment_id,
                trace_id=trace_id,
                operation=operation,
                status=status,
                subject_id=subject_id,
                error_code=(finding.category.value if finding.category else None),
                producer_deduplication_id=(
                    f"storage:{operation.value}:{subject_id}:{finding.kind.value}"
                ),
            ),
        )

    def _classify_inventory_entry(
        self,
        entry: ReconciliationInventoryEntry,
        canonical: dict[ObjectReference, list[CanonicalOriginalReference]],
        canonical_by_object_id: dict[UUID, list[CanonicalOriginalReference]],
        *,
        workspace_id: UUID,
        environment_id: UUID | None,
    ) -> ReconciliationFinding | None:
        if entry.integrity_status is IntegrityStatus.UNAVAILABLE:
            return ReconciliationFinding(
                kind=ReconciliationFindingKind.MISSING,
                workspace_id=workspace_id,
                environment_id=environment_id,
                object_reference=entry.object_reference,
                upload_id=entry.upload_id,
                category=StorageErrorCategory.OBJECT_NOT_FOUND,
                observed_at=self._now(),
            )
        if entry.integrity_status is IntegrityStatus.MISMATCH:
            return ReconciliationFinding(
                kind=ReconciliationFindingKind.CORRUPT,
                workspace_id=workspace_id,
                environment_id=environment_id,
                object_reference=entry.object_reference,
                upload_id=entry.upload_id,
                category=StorageErrorCategory.OBJECT_CORRUPT,
                observed_at=self._now(),
            )
        matches = canonical.get(entry.object_reference, [])
        if len(matches) == 1:
            same_object_id = canonical_by_object_id.get(entry.object_reference.object_id, [])
            if len(same_object_id) != 1:
                return ReconciliationFinding(
                    kind=ReconciliationFindingKind.AMBIGUOUS,
                    workspace_id=workspace_id,
                    environment_id=environment_id,
                    object_reference=entry.object_reference,
                    upload_id=entry.upload_id,
                    category=StorageErrorCategory.STORAGE_INTEGRITY_INDETERMINATE,
                    observed_at=self._now(),
                )
            if matches[0].upload_id != entry.upload_id:
                return ReconciliationFinding(
                    kind=ReconciliationFindingKind.AMBIGUOUS,
                    workspace_id=workspace_id,
                    environment_id=environment_id,
                    object_reference=entry.object_reference,
                    upload_id=entry.upload_id,
                    category=StorageErrorCategory.STORAGE_INTEGRITY_INDETERMINATE,
                    observed_at=self._now(),
                )
            return None
        same_object_id = canonical_by_object_id.get(entry.object_reference.object_id, [])
        return ReconciliationFinding(
            kind=ReconciliationFindingKind.ORPHAN
            if not matches and not same_object_id
            else ReconciliationFindingKind.AMBIGUOUS,
            object_reference=entry.object_reference,
            workspace_id=workspace_id,
            environment_id=environment_id,
            upload_id=entry.upload_id,
            category=(
                StorageErrorCategory.STORAGE_INTEGRITY_INDETERMINATE
            if matches or same_object_id
            else None
            ),
            observed_at=self._now(),
        )

    def _classify_upload_record(
        self,
        record: OriginalUploadRecord,
    ) -> ReconciliationFinding:
        if record.state.state in {
            UploadCommandStateName.STAGING,
            UploadCommandStateName.INTERRUPTED,
        }:
            return ReconciliationFinding(
                kind=ReconciliationFindingKind.STAGED,
                workspace_id=record.command.workspace_id,
                environment_id=record.command.environment_id,
                upload_id=record.result.upload_id,
                observed_at=self._now(),
            )
        if record.state.state is UploadCommandStateName.RECONCILIATION_REQUIRED:
            return ReconciliationFinding(
                kind=ReconciliationFindingKind.AMBIGUOUS,
                workspace_id=record.command.workspace_id,
                environment_id=record.command.environment_id,
                upload_id=record.result.upload_id,
                category=StorageErrorCategory.RECONCILIATION_REQUIRED,
                observed_at=self._now(),
            )
        if record.cleanup_status in {
            UploadCleanupStatus.PENDING,
            UploadCleanupStatus.FAILED,
        }:
            return ReconciliationFinding(
                kind=ReconciliationFindingKind.CLEANUP_PENDING,
                workspace_id=record.command.workspace_id,
                environment_id=record.command.environment_id,
                upload_id=record.result.upload_id,
                category=(
                    StorageErrorCategory.OBJECT_DELETION_FAILED
                    if record.cleanup_status is UploadCleanupStatus.FAILED
                    else None
                ),
                observed_at=self._now(),
            )
        return ReconciliationFinding(
            kind=ReconciliationFindingKind.AMBIGUOUS,
            workspace_id=record.command.workspace_id,
            environment_id=record.command.environment_id,
            upload_id=record.result.upload_id,
            category=StorageErrorCategory.STORAGE_INTEGRITY_INDETERMINATE,
            observed_at=self._now(),
        )

    async def _verify_canonical_reference(
        self,
        item: CanonicalOriginalReference,
        *,
        max_verify_bytes: int,
    ) -> ReconciliationFinding | None:
        try:
            result = await self._object_store.verify(
                item.object_reference,
                max_bytes=max_verify_bytes,
            )
            if result.status is not IntegrityStatus.VERIFIED:
                return self._finding(
                    ReconciliationFindingKind.AMBIGUOUS,
                    item,
                    StorageErrorCategory.STORAGE_INTEGRITY_INDETERMINATE,
                )
        except ObjectNotFound:
            return self._finding(
                ReconciliationFindingKind.MISSING,
                item,
                StorageErrorCategory.OBJECT_NOT_FOUND,
            )
        except ObjectCorrupt:
            return self._finding(
                ReconciliationFindingKind.CORRUPT,
                item,
                StorageErrorCategory.OBJECT_CORRUPT,
            )
        except StorageError as error:
            return self._finding(
                ReconciliationFindingKind.AMBIGUOUS,
                item,
                error.category,
            )
        return None

    def _finding(
        self,
        kind: ReconciliationFindingKind,
        item: CanonicalOriginalReference,
        category: StorageErrorCategory,
    ) -> ReconciliationFinding:
        return ReconciliationFinding(
            kind=kind,
            workspace_id=item.workspace_id,
            environment_id=item.environment_id,
            object_reference=item.object_reference,
            upload_id=item.upload_id,
            source_revision_id=item.source_revision_id,
            category=category,
            observed_at=self._now(),
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("reconciliation clock must return timezone-aware datetime")
        return now


__all__ = [
    "CanonicalOriginalReference",
    "OriginalStorageReconciler",
    "ReconciliationFinding",
    "ReconciliationFindingKind",
    "ReconciliationReport",
    "ReconciliationStorage",
]
