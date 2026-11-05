from __future__ import annotations

import pytest
from pydantic import ValidationError

from spine.domain.synthetic_cleanup import (
    SyntheticAuthorityStatus,
    SyntheticCleanupEvent,
    SyntheticCleanupJob,
    SyntheticCleanupState,
    SyntheticEventKind,
    SyntheticStoreReceipt,
    SyntheticStoreStatus,
    transition_synthetic_cleanup,
)


def _job(*, store_refs: tuple[str, ...] = ("fake-store-a", "fake-store-b")) -> SyntheticCleanupJob:
    return SyntheticCleanupJob(
        operation_key="fake-operation-a",
        workspace_ref="fake-workspace-a",
        required_store_refs=store_refs,
    )


def _event(kind: SyntheticEventKind, **values: object) -> SyntheticCleanupEvent:
    return SyntheticCleanupEvent(kind=kind, **values)


def _fenced_pending(job: SyntheticCleanupJob | None = None) -> SyntheticCleanupJob:
    proposed = job or _job()
    fenced = transition_synthetic_cleanup(
        proposed,
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


@pytest.mark.parametrize(
    ("authority", "expected_code"),
    [
        (SyntheticAuthorityStatus.MISSING, "AUTHORITY_MISSING"),
        (SyntheticAuthorityStatus.STALE, "AUTHORITY_STALE"),
        (SyntheticAuthorityStatus.REVOKED, "AUTHORITY_REVOKED"),
    ],
)
def test_unavailable_authority_blocks_and_cannot_be_upgraded(
    authority: SyntheticAuthorityStatus,
    expected_code: str,
) -> None:
    blocked = transition_synthetic_cleanup(
        _job(),
        _event(SyntheticEventKind.CHECK_AUTHORITY, authority_status=authority),
    )
    later_check = transition_synthetic_cleanup(
        blocked,
        _event(
            SyntheticEventKind.CHECK_AUTHORITY,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
        ),
    )

    assert blocked.state is SyntheticCleanupState.BLOCKED
    assert blocked.safe_code == expected_code
    assert later_check.state is SyntheticCleanupState.BLOCKED
    assert later_check.state is not SyntheticCleanupState.VERIFIED_SYNTHETIC


def test_partial_failure_stays_fenced_and_retry_reuses_operation_key() -> None:
    pending = _fenced_pending()
    failed = transition_synthetic_cleanup(
        pending,
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref="fake-store-a",
            store_status=SyntheticStoreStatus.FAILED,
        ),
    )
    retry_event = _event(
        SyntheticEventKind.RETRY,
        authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
    )
    retrying = transition_synthetic_cleanup(failed, retry_event)
    retry_replay = transition_synthetic_cleanup(retrying, retry_event)
    first_success = transition_synthetic_cleanup(
        retrying,
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref="fake-store-a",
            store_status=SyntheticStoreStatus.VERIFIED,
        ),
    )

    assert failed.state is SyntheticCleanupState.CLEANUP_INCOMPLETE
    assert failed.fence_active
    assert retrying.state is SyntheticCleanupState.CLEANUP_PENDING
    assert retrying.retry_count == 1
    assert retrying.operation_key == pending.operation_key
    assert retry_replay == retrying
    assert first_success.state is SyntheticCleanupState.CLEANUP_INCOMPLETE
    assert first_success.state is not SyntheticCleanupState.VERIFIED_SYNTHETIC

    complete = transition_synthetic_cleanup(
        first_success,
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref="fake-store-b",
            store_status=SyntheticStoreStatus.VERIFIED,
        ),
    )
    duplicate = transition_synthetic_cleanup(
        complete,
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref="fake-store-b",
            store_status=SyntheticStoreStatus.VERIFIED,
        ),
    )
    assert complete.state is SyntheticCleanupState.VERIFIED_SYNTHETIC
    assert duplicate == complete
    assert transition_synthetic_cleanup(
        complete,
        _event(
            SyntheticEventKind.START,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
        ),
    ) == complete


@pytest.mark.parametrize(
    ("authority", "expected_code"),
    [
        (SyntheticAuthorityStatus.MISSING, "AUTHORITY_MISSING"),
        (SyntheticAuthorityStatus.STALE, "AUTHORITY_STALE"),
        (SyntheticAuthorityStatus.REVOKED, "AUTHORITY_REVOKED"),
    ],
)
def test_start_rechecks_authority_and_blocks_before_dispatch(
    authority: SyntheticAuthorityStatus,
    expected_code: str,
) -> None:
    fenced = transition_synthetic_cleanup(
        _job(),
        _event(
            SyntheticEventKind.CHECK_AUTHORITY,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
        ),
    )
    blocked = transition_synthetic_cleanup(
        fenced,
        _event(SyntheticEventKind.START, authority_status=authority),
    )

    assert blocked.state is SyntheticCleanupState.BLOCKED
    assert blocked.authority_status is authority
    assert blocked.safe_code == expected_code
    assert blocked.state is not SyntheticCleanupState.CLEANUP_PENDING


@pytest.mark.parametrize(
    ("authority", "expected_code"),
    [
        (SyntheticAuthorityStatus.MISSING, "AUTHORITY_MISSING"),
        (SyntheticAuthorityStatus.STALE, "AUTHORITY_STALE"),
        (SyntheticAuthorityStatus.REVOKED, "AUTHORITY_REVOKED"),
    ],
)
def test_authority_loss_during_pending_blocks_late_store_receipts(
    authority: SyntheticAuthorityStatus,
    expected_code: str,
) -> None:
    pending = _fenced_pending()
    blocked = transition_synthetic_cleanup(
        pending,
        _event(SyntheticEventKind.CHECK_AUTHORITY, authority_status=authority),
    )
    late_receipt = transition_synthetic_cleanup(
        blocked,
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref="fake-store-a",
            store_status=SyntheticStoreStatus.VERIFIED,
        ),
    )

    assert blocked.state is SyntheticCleanupState.BLOCKED
    assert blocked.safe_code == expected_code
    assert blocked.fence_active
    assert late_receipt.state is SyntheticCleanupState.BLOCKED
    assert late_receipt.state is not SyntheticCleanupState.VERIFIED_SYNTHETIC


@pytest.mark.parametrize(
    ("authority", "expected_code"),
    [
        (SyntheticAuthorityStatus.MISSING, "AUTHORITY_MISSING"),
        (SyntheticAuthorityStatus.STALE, "AUTHORITY_STALE"),
        (SyntheticAuthorityStatus.REVOKED, "AUTHORITY_REVOKED"),
    ],
)
def test_store_receipt_requires_fresh_current_authority(
    authority: SyntheticAuthorityStatus,
    expected_code: str,
) -> None:
    blocked = transition_synthetic_cleanup(
        _fenced_pending(),
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=authority,
            store_ref="fake-store-a",
            store_status=SyntheticStoreStatus.VERIFIED,
        ),
    )

    assert blocked.state is SyntheticCleanupState.BLOCKED
    assert blocked.authority_status is authority
    assert blocked.safe_code == expected_code
    assert blocked.store_receipts == ()
    assert blocked.fence_active


def test_contract_rejects_unregistered_identifier_with_fake_prefix() -> None:
    with pytest.raises(ValidationError):
        SyntheticCleanupJob(
            operation_key="fake-provider-message-id",
            workspace_ref="fake-workspace-a",
            required_store_refs=("fake-store-a",),
        )


@pytest.mark.parametrize(
    ("authority", "expected_code"),
    [
        (SyntheticAuthorityStatus.MISSING, "AUTHORITY_MISSING"),
        (SyntheticAuthorityStatus.STALE, "AUTHORITY_STALE"),
        (SyntheticAuthorityStatus.REVOKED, "AUTHORITY_REVOKED"),
    ],
)
def test_retry_rechecks_authority_and_blocks_when_not_current(
    authority: SyntheticAuthorityStatus,
    expected_code: str,
) -> None:
    incomplete = transition_synthetic_cleanup(
        _fenced_pending(),
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref="fake-store-a",
            store_status=SyntheticStoreStatus.FAILED,
        ),
    )
    blocked = transition_synthetic_cleanup(
        incomplete,
        _event(SyntheticEventKind.RETRY, authority_status=authority),
    )

    assert blocked.state is SyntheticCleanupState.BLOCKED
    assert blocked.authority_status is authority
    assert blocked.safe_code == expected_code
    assert blocked.fence_active
    assert blocked.state is not SyntheticCleanupState.VERIFIED_SYNTHETIC


def test_retry_requires_explicit_authority_result() -> None:
    with pytest.raises(ValidationError):
        _event(SyntheticEventKind.RETRY)


def test_start_requires_explicit_authority_result() -> None:
    with pytest.raises(ValidationError):
        _event(SyntheticEventKind.START)


def test_store_result_and_reconciliation_require_explicit_authority() -> None:
    with pytest.raises(ValidationError):
        _event(
            SyntheticEventKind.STORE_RESULT,
            store_ref="fake-store-a",
            store_status=SyntheticStoreStatus.VERIFIED,
        )
    with pytest.raises(ValidationError):
        _event(
            SyntheticEventKind.RECONCILE_RECEIPT,
            receipts=(
                SyntheticStoreReceipt(
                    store_ref="fake-store-a",
                    status=SyntheticStoreStatus.VERIFIED,
                ),
            ),
        )


def test_unknown_commit_requires_explicit_complete_receipt_reconciliation() -> None:
    unknown = transition_synthetic_cleanup(
        _fenced_pending(),
        _event(SyntheticEventKind.COMMIT_UNKNOWN),
    )
    incomplete_reconciliation = transition_synthetic_cleanup(
        unknown,
        _event(
            SyntheticEventKind.RECONCILE_RECEIPT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            receipts=(
                SyntheticStoreReceipt(
                    store_ref="fake-store-a",
                    status=SyntheticStoreStatus.VERIFIED,
                ),
            ),
        ),
    )

    assert unknown.state is SyntheticCleanupState.UNKNOWN
    assert incomplete_reconciliation.state is SyntheticCleanupState.UNKNOWN
    assert incomplete_reconciliation.safe_code == "RECONCILIATION_REQUIRED"

    reconciled = transition_synthetic_cleanup(
        incomplete_reconciliation,
        _event(
            SyntheticEventKind.RECONCILE_RECEIPT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            receipts=(
                SyntheticStoreReceipt(
                    store_ref="fake-store-a",
                    status=SyntheticStoreStatus.VERIFIED,
                ),
                SyntheticStoreReceipt(
                    store_ref="fake-store-b",
                    status=SyntheticStoreStatus.VERIFIED,
                ),
            ),
        ),
    )
    assert reconciled.state is SyntheticCleanupState.VERIFIED_SYNTHETIC
    assert reconciled.fence_active


def test_unknown_commit_reconciliation_needs_fresh_authority() -> None:
    unknown = transition_synthetic_cleanup(
        _fenced_pending(),
        _event(SyntheticEventKind.COMMIT_UNKNOWN),
    )
    rejected = transition_synthetic_cleanup(
        unknown,
        _event(
            SyntheticEventKind.RECONCILE_RECEIPT,
            authority_status=SyntheticAuthorityStatus.REVOKED,
            receipts=(
                SyntheticStoreReceipt(
                    store_ref="fake-store-a",
                    status=SyntheticStoreStatus.VERIFIED,
                ),
                SyntheticStoreReceipt(
                    store_ref="fake-store-b",
                    status=SyntheticStoreStatus.VERIFIED,
                ),
            ),
        ),
    )

    assert rejected.state is SyntheticCleanupState.UNKNOWN
    assert rejected.authority_status is SyntheticAuthorityStatus.REVOKED
    assert rejected.safe_code == "AUTHORITY_REVOKED"
    assert rejected.fence_active


def test_unknown_receipt_remains_unresolved_after_reconciliation() -> None:
    unknown = transition_synthetic_cleanup(
        _fenced_pending(),
        _event(SyntheticEventKind.COMMIT_UNKNOWN),
    )
    reconciled = transition_synthetic_cleanup(
        unknown,
        _event(
            SyntheticEventKind.RECONCILE_RECEIPT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            receipts=(
                SyntheticStoreReceipt(
                    store_ref="fake-store-a",
                    status=SyntheticStoreStatus.UNKNOWN,
                ),
                SyntheticStoreReceipt(
                    store_ref="fake-store-b",
                    status=SyntheticStoreStatus.VERIFIED,
                ),
            ),
        ),
    )
    assert reconciled.state is SyntheticCleanupState.UNKNOWN
    assert reconciled.safe_code == "RECONCILIATION_REQUIRED"


def test_cancelled_work_remains_fenced_and_cannot_restart() -> None:
    cancelled = transition_synthetic_cleanup(
        _fenced_pending(),
        _event(SyntheticEventKind.CANCEL),
    )
    restarted = transition_synthetic_cleanup(
        cancelled,
        _event(
            SyntheticEventKind.START,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
        ),
    )

    assert cancelled.state is SyntheticCleanupState.CANCELLED
    assert cancelled.fence_active
    assert restarted.state is SyntheticCleanupState.CANCELLED


def test_restore_stays_quarantined_until_verified_synthetic_replay() -> None:
    pending = _fenced_pending(_job(store_refs=("fake-store-a",)))
    verified = transition_synthetic_cleanup(
        pending,
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref="fake-store-a",
            store_status=SyntheticStoreStatus.VERIFIED,
        ),
    )
    quarantined = transition_synthetic_cleanup(
        verified,
        _event(SyntheticEventKind.RESTORE_REQUESTED),
    )
    released = transition_synthetic_cleanup(
        quarantined,
        _event(SyntheticEventKind.RESTORE_REPLAY_SUCCEEDED),
    )

    assert quarantined.restore_fenced
    assert quarantined.restore_replay_pending
    assert not quarantined.restore_replay_verified
    assert released.restore_replay_verified
    assert not released.restore_fenced


def test_restore_cannot_release_without_a_pending_synthetic_replay() -> None:
    pending = _fenced_pending(_job(store_refs=("fake-store-a",)))
    verified = transition_synthetic_cleanup(
        pending,
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref="fake-store-a",
            store_status=SyntheticStoreStatus.VERIFIED,
        ),
    )

    result = transition_synthetic_cleanup(
        verified,
        _event(SyntheticEventKind.RESTORE_REPLAY_SUCCEEDED),
    )

    assert result.restore_fenced
    assert not result.restore_replay_verified
    assert result.safe_code == "INVALID_TRANSITION"


def test_uncovered_store_cannot_be_reported_as_verified() -> None:
    pending = _fenced_pending(_job(store_refs=("fake-store-a",)))
    result = transition_synthetic_cleanup(
        pending,
        _event(
            SyntheticEventKind.STORE_RESULT,
            authority_status=SyntheticAuthorityStatus.CURRENT_TEST_ONLY,
            store_ref="fake-store-uncovered",
            store_status=SyntheticStoreStatus.VERIFIED,
        ),
    )

    assert result.state is SyntheticCleanupState.CLEANUP_PENDING
    assert result.state is not SyntheticCleanupState.VERIFIED_SYNTHETIC
    assert result.safe_code == "INVALID_TRANSITION"


def test_contract_rejects_unbounded_or_extra_identifier_fields() -> None:
    with pytest.raises(ValidationError):
        SyntheticCleanupJob(
            operation_key="provider-message-id",
            workspace_ref="fake-workspace-a",
            required_store_refs=("fake-store-a",),
        )

    with pytest.raises(ValidationError):
        SyntheticCleanupJob(
            operation_key="fake-operation-a",
            workspace_ref="fake-workspace-a",
            required_store_refs=("fake-store-a",),
            gmail_message_id="synthetic-is-not-a-schema-field",
        )


def test_event_contract_forbids_malformed_or_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SyntheticCleanupEvent(kind=SyntheticEventKind.CHECK_AUTHORITY)
    with pytest.raises(ValidationError):
        SyntheticCleanupEvent(
            kind=SyntheticEventKind.START,
            payload="body-like-content",
        )
