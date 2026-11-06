from __future__ import annotations

import pytest

from spine.domain.synthetic_cleanup import (
    FakeOpaqueId,
    SyntheticAuthorityStatus,
    SyntheticCleanupEvent,
    SyntheticCleanupJob,
    SyntheticCleanupState,
    SyntheticEventKind,
    SyntheticStoreReceipt,
    SyntheticStoreStatus,
    transition_synthetic_cleanup,
)
from tests.qualification.test_issue1002_synthetic_recovery_vectors import (
    FakeRef,
    FakeRestoreHarness,
    FakeStoreHarness,
    FakeStoreResult,
    SyntheticAuditEntry,
    VECTORS,
)


def _job() -> SyntheticCleanupJob:
    return SyntheticCleanupJob(
        operation_key=FakeOpaqueId.OPERATION_A,
        workspace_ref=FakeOpaqueId.WORKSPACE_A,
        required_store_refs=(FakeOpaqueId.STORE_A, FakeOpaqueId.STORE_B),
    )


def _event(kind: SyntheticEventKind, **values: object) -> SyntheticCleanupEvent:
    return SyntheticCleanupEvent(kind=kind, **values)


def _pending() -> SyntheticCleanupJob:
    fenced = transition_synthetic_cleanup(
        _job(),
        _event(
            SyntheticEventKind.CHECK_AUTHORITY,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
        ),
    )
    return transition_synthetic_cleanup(
        fenced,
        _event(
            SyntheticEventKind.START,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
        ),
    )


def _receipt(store_ref: FakeOpaqueId, status: SyntheticStoreStatus) -> SyntheticStoreReceipt:
    return SyntheticStoreReceipt(store_ref=store_ref, status=status)


@pytest.mark.parametrize(
    ("vector_id", "authority", "safe_code"),
    [
        ("missing_authority", SyntheticAuthorityStatus.MISSING, "AUTHORITY_MISSING"),
        ("stale_authority", SyntheticAuthorityStatus.STALE, "AUTHORITY_STALE"),
        ("revoked_authority", SyntheticAuthorityStatus.REVOKED, "AUTHORITY_REVOKED"),
    ],
)
def test_issue1002_authority_vectors_block_at_canonical_state_machine(
    vector_id: str,
    authority: SyntheticAuthorityStatus,
    safe_code: str,
) -> None:
    vector = next(item for item in VECTORS if item.case_id == vector_id)
    blocked = transition_synthetic_cleanup(
        _job(),
        _event(SyntheticEventKind.CHECK_AUTHORITY, authority_status=authority),
    )

    assert (blocked.state.value, blocked.safe_code) == (vector.expected_state, safe_code)
    assert blocked.state is SyntheticCleanupState.BLOCKED
    assert blocked.fence_active is False
    assert blocked.state is not SyntheticCleanupState.VERIFIED_SYNTHETIC


def test_issue1002_partial_delete_vector_maps_store_failure_to_canonical_code() -> None:
    vector = next(item for item in VECTORS if item.case_id == "partial_delete")
    first = transition_synthetic_cleanup(
        _pending(),
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref=FakeOpaqueId.STORE_A,
            store_status=SyntheticStoreStatus.VERIFIED,
        ),
    )
    failed = transition_synthetic_cleanup(
        first,
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref=FakeOpaqueId.STORE_B,
            store_status=SyntheticStoreStatus.FAILED,
        ),
    )

    assert (failed.state.value, failed.safe_code) == (vector.expected_state, "STORE_FAILED")
    assert failed.fence_active
    assert len(failed.store_receipts) == 2
    assert failed.state is not SyntheticCleanupState.VERIFIED_SYNTHETIC


def test_issue1002_store_outage_maps_to_failed_and_never_verifies() -> None:
    vector = next(item for item in VECTORS if item.case_id == "store_outage")
    result = transition_synthetic_cleanup(
        _pending(),
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref=FakeOpaqueId.STORE_A,
            store_status=SyntheticStoreStatus.FAILED,
        ),
    )

    assert result.state.value == vector.expected_state
    assert result.safe_code == "STORE_FAILED"
    assert result.fence_active
    assert result.state is not SyntheticCleanupState.VERIFIED_SYNTHETIC


def test_issue1002_uncovered_store_vector_is_rejected_by_canonical_machine() -> None:
    vector = next(item for item in VECTORS if item.case_id == "missing_uncovered_store")
    result = transition_synthetic_cleanup(
        _pending(),
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref=FakeOpaqueId.STORE_UNCOVERED,
            store_status=SyntheticStoreStatus.VERIFIED,
        ),
    )

    assert result.state is SyntheticCleanupState.CLEANUP_PENDING
    assert result.state is not SyntheticCleanupState.VERIFIED_SYNTHETIC
    assert result.safe_code == "INVALID_TRANSITION"
    assert vector.expected_state == "CLEANUP_INCOMPLETE"
    assert vector.safe_code == "STORE_UNCOVERED"


def test_issue1002_retry_and_duplicate_vector_reuses_job_key_and_receipts() -> None:
    vector = next(item for item in VECTORS if item.case_id == "duplicate_retry")
    pending = _pending()
    incomplete = transition_synthetic_cleanup(
        pending,
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref=FakeOpaqueId.STORE_A,
            store_status=SyntheticStoreStatus.FAILED,
        ),
    )
    retried = transition_synthetic_cleanup(
        incomplete,
        _event(
            SyntheticEventKind.RETRY,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
        ),
    )
    retried = transition_synthetic_cleanup(
        retried,
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref=FakeOpaqueId.STORE_A,
            store_status=SyntheticStoreStatus.VERIFIED,
        ),
    )
    verified = transition_synthetic_cleanup(
        retried,
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref=FakeOpaqueId.STORE_B,
            store_status=SyntheticStoreStatus.VERIFIED,
        ),
    )
    duplicate_receipt = transition_synthetic_cleanup(
        verified,
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref=FakeOpaqueId.STORE_B,
            store_status=SyntheticStoreStatus.VERIFIED,
        ),
    )

    assert retried.operation_key == pending.operation_key
    assert retried.retry_count == 1
    assert (verified.state.value, verified.safe_code) == (
        vector.expected_state,
        vector.safe_code,
    )
    assert duplicate_receipt == verified
    assert verified.fence_active


def test_issue1002_duplicate_store_replay_has_one_effect_after_state_machine_retry() -> None:
    store = FakeStoreHarness(
        workspace_ref=FakeRef.WORKSPACE_A,
        store_refs=(FakeRef.STORE_A,),
        artifact_refs=(FakeRef.ARTIFACT_A,),
    )
    command = {
        "operation_key": FakeRef.OPERATION_A,
        "workspace_ref": FakeRef.WORKSPACE_A,
        "store_ref": FakeRef.STORE_A,
        "artifact_ref": FakeRef.ARTIFACT_A,
        "current_test_authority": True,
    }

    first = store.delete_synthetic(**command)
    replay = store.delete_synthetic(**command)

    assert first is FakeStoreResult.DELETED_SYNTHETIC
    assert replay is FakeStoreResult.DUPLICATE
    assert len(store.calls) == 1
    assert not store.records


def test_issue1002_cancelled_vector_rejects_late_store_result() -> None:
    vector = next(item for item in VECTORS if item.case_id == "cancellation")
    cancelled = transition_synthetic_cleanup(_pending(), _event(SyntheticEventKind.CANCEL))
    late_result = transition_synthetic_cleanup(
        cancelled,
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref=FakeOpaqueId.STORE_A,
            store_status=SyntheticStoreStatus.VERIFIED,
        ),
    )

    assert cancelled.state.value == vector.expected_state
    assert cancelled.safe_code == vector.safe_code
    assert late_result.state is SyntheticCleanupState.CANCELLED
    assert late_result.safe_code == "INVALID_TRANSITION"
    assert late_result.fence_active


def test_issue1002_unknown_commit_requires_complete_current_authority_receipt() -> None:
    vector = next(item for item in VECTORS if item.case_id == "unknown_commit")
    unknown = transition_synthetic_cleanup(
        _pending(), _event(SyntheticEventKind.COMMIT_UNKNOWN)
    )
    no_receipt = transition_synthetic_cleanup(
        unknown,
        _event(
            SyntheticEventKind.RECONCILE_RECEIPT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            receipts=(_receipt(FakeOpaqueId.STORE_A, SyntheticStoreStatus.VERIFIED),),
        ),
    )
    revoked = transition_synthetic_cleanup(
        no_receipt,
        _event(
            SyntheticEventKind.RECONCILE_RECEIPT,
            authority_status=SyntheticAuthorityStatus.REVOKED,
            receipts=(
                _receipt(FakeOpaqueId.STORE_A, SyntheticStoreStatus.VERIFIED),
                _receipt(FakeOpaqueId.STORE_B, SyntheticStoreStatus.VERIFIED),
            ),
        ),
    )
    reconciled = transition_synthetic_cleanup(
        revoked,
        _event(
            SyntheticEventKind.RECONCILE_RECEIPT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            receipts=(
                _receipt(FakeOpaqueId.STORE_A, SyntheticStoreStatus.VERIFIED),
                _receipt(FakeOpaqueId.STORE_B, SyntheticStoreStatus.VERIFIED),
            ),
        ),
    )

    assert (unknown.state.value, unknown.safe_code) == (vector.expected_state, vector.safe_code)
    assert no_receipt.state is SyntheticCleanupState.UNKNOWN
    assert no_receipt.safe_code == "RECONCILIATION_REQUIRED"
    assert revoked.state is SyntheticCleanupState.UNKNOWN
    assert revoked.safe_code == "AUTHORITY_REVOKED"
    assert reconciled.state is SyntheticCleanupState.VERIFIED_SYNTHETIC
    assert reconciled.fence_active


def test_issue1002_restore_vector_releases_only_after_successful_machine_replay() -> None:
    vector = next(item for item in VECTORS if item.case_id == "restore_quarantine_replay")
    pending = _pending()
    verified = transition_synthetic_cleanup(
        transition_synthetic_cleanup(
            pending,
            _event(
                SyntheticEventKind.STORE_RESULT,
                authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
                store_ref=FakeOpaqueId.STORE_A,
                store_status=SyntheticStoreStatus.VERIFIED,
            ),
        ),
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref=FakeOpaqueId.STORE_B,
            store_status=SyntheticStoreStatus.VERIFIED,
        ),
    )
    quarantined = transition_synthetic_cleanup(
        verified, _event(SyntheticEventKind.RESTORE_REQUESTED)
    )
    released = transition_synthetic_cleanup(
        quarantined, _event(SyntheticEventKind.RESTORE_REPLAY_SUCCEEDED)
    )

    assert quarantined.restore_fenced and quarantined.restore_replay_pending
    assert not quarantined.restore_replay_verified
    assert released.state.value == vector.expected_state
    assert released.safe_code == vector.safe_code
    assert released.restore_replay_verified
    assert not released.restore_fenced


def test_issue1002_cross_workspace_duplicate_stays_denied_before_replay_disclosure() -> None:
    vector = next(item for item in VECTORS if item.case_id == "cross_workspace")
    store = FakeStoreHarness(
        workspace_ref=FakeRef.WORKSPACE_B,
        store_refs=(FakeRef.STORE_B,),
        artifact_refs=(FakeRef.ARTIFACT_B,),
    )
    command = {
        "operation_key": FakeRef.OPERATION_A,
        "workspace_ref": FakeRef.WORKSPACE_B,
        "store_ref": FakeRef.STORE_B,
        "artifact_ref": FakeRef.ARTIFACT_B,
    }
    assert store.delete_synthetic(**command, current_test_authority=True) is FakeStoreResult.DELETED_SYNTHETIC

    denied = store.delete_synthetic(
        **{**command, "workspace_ref": FakeRef.WORKSPACE_A},
        current_test_authority=True,
    )

    assert vector.safe_code == "DENIED"
    assert denied is FakeStoreResult.DENIED
    assert len(store.calls) == 1


def test_issue1002_audit_redaction_vector_uses_closed_fake_schema() -> None:
    vector = next(item for item in VECTORS if item.case_id == "audit_log_redaction")

    with pytest.raises(ValueError):
        SyntheticAuditEntry(
            operation_ref=FakeRef.OPERATION_A,
            result_code="AUDIT_FIELD_REJECTED",
            body="synthetic protected payload",
        )
    assert vector.safe_code == "AUDIT_FIELD_REJECTED"


def test_issue1020_state_machine_and_denied_fake_store_emit_no_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failed = transition_synthetic_cleanup(
        _pending(),
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref=FakeOpaqueId.STORE_A,
            store_status=SyntheticStoreStatus.FAILED,
        ),
    )
    store = FakeStoreHarness(
        workspace_ref=FakeRef.WORKSPACE_B,
        store_refs=(FakeRef.STORE_B,),
        artifact_refs=(FakeRef.ARTIFACT_B,),
    )
    denied = store.delete_synthetic(
        operation_key=FakeRef.OPERATION_A,
        workspace_ref=FakeRef.WORKSPACE_A,
        store_ref=FakeRef.STORE_B,
        artifact_ref=FakeRef.ARTIFACT_B,
        current_test_authority=True,
    )

    assert failed.safe_code == "STORE_FAILED"
    assert denied is FakeStoreResult.DENIED
    assert caplog.records == []
    assert caplog.text == ""


def test_issue1002_vector_catalog_has_explicit_status_and_deny_fences() -> None:
    assert len(VECTORS) == 12
    assert all(item.qualification.value == "NOT_RUN" for item in VECTORS)
    assert all(item.reads_denied for item in VECTORS)
    assert all(item.publication_denied for item in VECTORS)
    assert all(item.checkpoints_denied for item in VECTORS)


def test_issue1002_restore_fake_stays_unreadable_after_restriction_replay() -> None:
    restored = FakeRestoreHarness(record_ref=FakeRef.RESTORED_ARTIFACT_A)

    assert not restored.can_read_or_publish(record_ref=FakeRef.RESTORED_ARTIFACT_A)
    restored.replay_current_restrictions(succeeded=True)
    assert not restored.can_read_or_publish(record_ref=FakeRef.RESTORED_ARTIFACT_A)
    assert restored.deletion_restriction_applied
