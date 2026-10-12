from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.storage import (
    CanonicalOriginalReference,
    OriginalStorageReconciler,
    OriginalUploadRecord,
    ReconciliationFindingKind,
)
from spine.infrastructure.object_storage import (
    DeletionApproval,
    DeletionResult,
    IntegrityResult,
    IntegrityStatus,
    ObjectReference,
    ReconciliationInventoryEntry,
    Issue8ResultReference,
    OriginalUploadCommandInput,
    OriginalUploadCommandResult,
    PreWriteRequestDigest,
    UploadCommandState,
    UploadCommandStateName,
)
from spine.infrastructure.object_storage.errors import (
    ObjectCorrupt,
    ObjectNotFound,
    ObjectProtected,
)


NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("20000000-0000-4000-8000-000000000001")
REVISION_ID = UUID("30000000-0000-4000-8000-000000000001")
UPLOAD_ID = UUID("40000000-0000-4000-8000-000000000001")


def reference(number: int, digest: str = "a" * 64) -> ObjectReference:
    return ObjectReference(
        schema_version=1,
        object_id=UUID(f"50000000-0000-4000-8000-{number:012d}"),
        storage_generation=UUID(f"60000000-0000-4000-8000-{number:012d}"),
        digest_algorithm="sha256",
        digest_hex=digest,
        byte_length=4,
    )


class _Storage:
    def __init__(self, entries: tuple[ReconciliationInventoryEntry, ...]) -> None:
        self.entries = entries
        self.verify_errors: dict[ObjectReference, Exception] = {}
        self.deleted: list[ObjectReference] = []
        self.approvals: list[DeletionApproval] = []

    async def reconciliation_inventory(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
        workspace_id: UUID | None = None,
        environment_id: UUID | None = None,
    ) -> tuple[tuple[ReconciliationInventoryEntry, ...], str | None]:
        assert workspace_id is None or workspace_id == WORKSPACE_ID
        assert environment_id is None or environment_id == ENVIRONMENT_ID
        assert limit > 0
        if cursor is None:
            return self.entries[:limit], "page-2" if len(self.entries) > limit else None
        assert cursor == "page-2"
        return self.entries[limit:], None

    async def verify(
        self,
        object_reference: ObjectReference,
        *,
        max_bytes: int,
    ) -> IntegrityResult:
        assert max_bytes > 0
        error = self.verify_errors.get(object_reference)
        if error is not None:
            raise error
        return IntegrityResult(
            status=IntegrityStatus.VERIFIED,
            algorithm="sha256",
            expected_digest=object_reference.digest_hex,
            observed_digest=object_reference.digest_hex,
            expected_length=object_reference.byte_length,
            observed_length=object_reference.byte_length,
        )

    async def prepare_deletion(
        self,
        object_reference: ObjectReference,
        approval: DeletionApproval,
    ) -> None:
        self.approvals.append(approval)

    async def delete_controlled(
        self,
        object_reference: ObjectReference,
        approval: DeletionApproval,
    ) -> DeletionResult:
        assert self.approvals[-1] == approval
        self.deleted.append(object_reference)
        return DeletionResult(status="deleted")


def canonical(object_reference: ObjectReference) -> CanonicalOriginalReference:
    return CanonicalOriginalReference(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        source_revision_id=REVISION_ID,
        upload_id=UPLOAD_ID,
        object_reference=object_reference,
    )


def staged_record() -> OriginalUploadRecord:
    command = OriginalUploadCommandInput(
        operation_name="original.upload",
        operation_schema_version=1,
        idempotency_key="staged-reconciliation",
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        purpose="document.upload",
    )
    digest = PreWriteRequestDigest(
        algorithm="sha256",
        algorithm_version="spine.command-digest.v1",
        digest_hex="c" * 64,
    )
    result = OriginalUploadCommandResult(
        request_digest=digest,
        upload_id=UPLOAD_ID,
        object_id=reference(9).object_id,
        issue8_result_reference=Issue8ResultReference(
            receipt_id=UUID("90000000-0000-4000-8000-000000000001")
        ),
    )
    return OriginalUploadRecord(
        record_version=1,
        command=command,
        result=result,
        state=UploadCommandState(
            issue8_receipt_id=result.issue8_result_reference.receipt_id,
            request_digest=digest,
            upload_id=UPLOAD_ID,
            object_id=result.object_id,
            state=UploadCommandStateName.STAGING,
        ),
    )


@pytest.mark.asyncio
async def test_reconciliation_reports_safe_orphan_and_preserves_restart_cursor() -> None:
    known = reference(1)
    orphan = reference(2, "b" * 64)
    storage = _Storage(
        (
            ReconciliationInventoryEntry(
                object_reference=known,
                upload_id=UPLOAD_ID,
                finalized=True,
                observed_at=NOW,
            ),
            ReconciliationInventoryEntry(
                object_reference=orphan,
                upload_id=UUID("40000000-0000-4000-8000-000000000002"),
                finalized=True,
                observed_at=NOW,
            ),
        )
    )
    reconciler = OriginalStorageReconciler(object_store=storage, clock=lambda: NOW)

    report = await reconciler.reconcile_page(
        (canonical(known),),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        limit=1,
        max_verify_bytes=16,
    )

    assert report.next_cursor == "page-2"
    assert report.complete is False
    assert report.findings == ()

    resumed = await reconciler.reconcile_page(
        (canonical(known),),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        cursor=report.next_cursor,
        limit=1,
        max_verify_bytes=16,
    )

    assert resumed.complete is True
    assert [finding.kind for finding in resumed.findings] == [
        ReconciliationFindingKind.ORPHAN
    ]
    assert resumed.findings[0].object_reference == orphan


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (ObjectNotFound(), ReconciliationFindingKind.MISSING),
        (ObjectCorrupt(), ReconciliationFindingKind.CORRUPT),
    ],
)
async def test_reconciliation_classifies_reference_integrity_failures(
    error: Exception,
    kind: ReconciliationFindingKind,
) -> None:
    target = reference(3)
    storage = _Storage(())
    storage.verify_errors[target] = error
    reconciler = OriginalStorageReconciler(object_store=storage, clock=lambda: NOW)

    report = await reconciler.reconcile_page(
        (canonical(target),),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        max_verify_bytes=16,
    )

    assert [finding.kind for finding in report.findings] == [kind]
    assert report.findings[0].source_revision_id == REVISION_ID


@pytest.mark.asyncio
async def test_reconciliation_reports_staged_and_ambiguous_upload_evidence() -> None:
    target = reference(5)
    storage = _Storage(
        (
            ReconciliationInventoryEntry(
                object_reference=target,
                upload_id=UPLOAD_ID,
                finalized=True,
                observed_at=NOW,
            ),
        )
    )
    reconciler = OriginalStorageReconciler(object_store=storage, clock=lambda: NOW)
    second = canonical(target).model_copy(
        update={
            "source_revision_id": UUID("30000000-0000-4000-8000-000000000002"),
            "upload_id": UUID("40000000-0000-4000-8000-000000000002"),
        }
    )

    report = await reconciler.reconcile_page(
        (canonical(target), second),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        upload_records=(staged_record(),),
        max_verify_bytes=16,
    )

    assert [finding.kind for finding in report.findings] == [
        ReconciliationFindingKind.AMBIGUOUS,
        ReconciliationFindingKind.STAGED,
    ]


@pytest.mark.asyncio
async def test_reconciliation_fails_closed_for_same_object_id_with_new_generation() -> None:
    target = reference(6)
    stale = target.model_copy(
        update={
            "storage_generation": UUID("60000000-0000-4000-8000-000000000099"),
            "digest_hex": "d" * 64,
        }
    )
    storage = _Storage(
        (
            ReconciliationInventoryEntry(
                object_reference=target,
                upload_id=UPLOAD_ID,
                finalized=True,
                observed_at=NOW,
            ),
        )
    )
    reconciler = OriginalStorageReconciler(object_store=storage, clock=lambda: NOW)

    report = await reconciler.reconcile_page(
        (canonical(target), canonical(stale)),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        max_verify_bytes=16,
    )

    assert report.findings[0].kind is ReconciliationFindingKind.AMBIGUOUS


@pytest.mark.asyncio
async def test_cleanup_requires_orphan_finding_and_explicit_approval() -> None:
    target = reference(4)
    entry = ReconciliationInventoryEntry(
        object_reference=target,
        upload_id=UPLOAD_ID,
        finalized=True,
        observed_at=NOW,
    )
    storage = _Storage((entry,))
    reconciler = OriginalStorageReconciler(object_store=storage, clock=lambda: NOW)
    report = await reconciler.reconcile_page(
        (),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        max_verify_bytes=16,
    )
    finding = report.findings[0]
    approval = DeletionApproval(
        command_id=UUID("70000000-0000-4000-8000-000000000001"),
        approval_reference=UUID("80000000-0000-4000-8000-000000000001"),
    )

    result = await reconciler.cleanup_orphan(
        finding,
        approval,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        canonical_references=(),
    )

    assert result.status == "deleted"
    assert storage.deleted == [target]

    with pytest.raises(ObjectProtected):
        await reconciler.cleanup_orphan(
            finding.model_copy(update={"kind": ReconciliationFindingKind.MISSING}),
            approval,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            canonical_references=(),
        )

    with pytest.raises(ObjectProtected):
        await reconciler.cleanup_orphan(
            finding,
            approval,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            canonical_references=(canonical(target),),
        )

    with pytest.raises(ObjectProtected):
        await reconciler.cleanup_orphan(
            finding,
            approval,
            workspace_id=UUID("11000000-0000-4000-8000-000000000001"),
            environment_id=ENVIRONMENT_ID,
            canonical_references=(),
        )
