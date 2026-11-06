from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from typing import Literal


class FakeRef(str, Enum):
    """Closed fixture vocabulary so arbitrary IDs cannot enter fake stores."""

    WORKSPACE_A = "fake-workspace-a"
    WORKSPACE_B = "fake-workspace-b"
    STORE = "fake-store"
    STORE_A = "fake-store-a"
    STORE_B = "fake-store-b"
    STORE_UNCOVERED = "fake-store-uncovered"
    ARTIFACT_A = "fake-artifact-a"
    ARTIFACT_B = "fake-artifact-b"
    OPERATION_A = "fake-operation-a"
    OPERATION_B = "fake-operation-b"
    RESTORED_ARTIFACT_A = "fake-restored-artifact-a"


def _fake_ref(value: str | FakeRef) -> FakeRef:
    return FakeRef(value)


class VectorStatus(str, Enum):
    NOT_RUN = "NOT_RUN"


@dataclass(frozen=True)
class RecoveryVector:
    case_id: str
    initial_state: str
    events: tuple[str, ...]
    expected_state: str
    safe_code: str
    reads_denied: bool
    publication_denied: bool
    checkpoints_denied: bool
    qualification: VectorStatus = VectorStatus.NOT_RUN


VECTORS = (
    RecoveryVector("partial_delete", "CLEANUP_PENDING", ("store_a_verified", "store_b_failed"), "CLEANUP_INCOMPLETE", "STORE_FAILED", True, True, True),
    RecoveryVector("store_outage", "CLEANUP_PENDING", ("store_unavailable",), "CLEANUP_INCOMPLETE", "STORE_UNAVAILABLE", True, True, True),
    RecoveryVector("missing_authority", "PROPOSED", ("authority_missing",), "BLOCKED", "AUTHORITY_MISSING", True, True, True),
    RecoveryVector("stale_authority", "PROPOSED", ("authority_stale",), "BLOCKED", "AUTHORITY_STALE", True, True, True),
    RecoveryVector("revoked_authority", "PROPOSED", ("authority_revoked",), "BLOCKED", "AUTHORITY_REVOKED", True, True, True),
    RecoveryVector("cross_workspace", "FENCED_TEST_ONLY", ("workspace_mismatch",), "BLOCKED", "DENIED", True, True, True),
    RecoveryVector("duplicate_retry", "CLEANUP_INCOMPLETE", ("retry_same_operation_key", "replay_same_command"), "VERIFIED_SYNTHETIC", "VERIFIED_SYNTHETIC", True, True, True),
    RecoveryVector("cancellation", "CLEANUP_PENDING", ("cancel", "late_store_result"), "CANCELLED", "CANCELLED", True, True, True),
    RecoveryVector("unknown_commit", "CLEANUP_PENDING", ("commit_ack_lost", "no_receipt"), "UNKNOWN", "COMMIT_UNKNOWN", True, True, True),
    RecoveryVector("restore_quarantine_replay", "VERIFIED_SYNTHETIC", ("restore_requested", "restriction_replay_pending", "restriction_replay_verified"), "VERIFIED_SYNTHETIC", "VERIFIED_SYNTHETIC", True, True, True),
    RecoveryVector("audit_log_redaction", "CLEANUP_PENDING", ("emit_forbidden_field",), "CLEANUP_INCOMPLETE", "AUDIT_FIELD_REJECTED", True, True, True),
    RecoveryVector("missing_uncovered_store", "CLEANUP_PENDING", ("uncovered_store_detected",), "CLEANUP_INCOMPLETE", "STORE_UNCOVERED", True, True, True),
)


class FakeStoreResult(str, Enum):
    DELETED_SYNTHETIC = "DELETED_SYNTHETIC"
    DUPLICATE = "DUPLICATE"
    DENIED = "DENIED"
    UNAVAILABLE = "UNAVAILABLE"
    UNCOVERED = "UNCOVERED"


class FakeStoreHarness:
    """Store-effect fake only; it does not implement the cleanup state machine."""

    def __init__(
        self,
        *,
        workspace_ref: FakeRef,
        store_refs: tuple[FakeRef, ...],
        artifact_refs: tuple[FakeRef, ...],
        unavailable: frozenset[FakeRef] = frozenset(),
    ) -> None:
        self.workspace_ref = _fake_ref(workspace_ref)
        self.store_refs = frozenset(_fake_ref(store_ref) for store_ref in store_refs)
        self.unavailable = frozenset(_fake_ref(store_ref) for store_ref in unavailable)
        validated_artifacts = tuple(_fake_ref(artifact_ref) for artifact_ref in artifact_refs)
        self.records = {
            (self.workspace_ref, store_ref, artifact_ref)
            for store_ref in self.store_refs
            for artifact_ref in validated_artifacts
        }
        self.receipts: dict[tuple[FakeRef, FakeRef, FakeRef, FakeRef], FakeStoreResult] = {}
        self.calls: list[tuple[FakeRef, FakeRef, FakeRef, FakeRef]] = []

    def set_store_available(self, *, store_ref: FakeRef) -> None:
        validated_store_ref = _fake_ref(store_ref)
        if validated_store_ref not in self.store_refs:
            raise ValueError("only declared fake stores can change availability")
        self.unavailable = self.unavailable - {validated_store_ref}

    def delete_synthetic(
        self,
        *,
        operation_key: FakeRef,
        workspace_ref: FakeRef,
        store_ref: FakeRef,
        artifact_ref: FakeRef,
        current_test_authority: bool,
    ) -> FakeStoreResult:
        operation_key = _fake_ref(operation_key)
        workspace_ref = _fake_ref(workspace_ref)
        store_ref = _fake_ref(store_ref)
        artifact_ref = _fake_ref(artifact_ref)
        if current_test_authority is not True or workspace_ref != self.workspace_ref:
            return FakeStoreResult.DENIED
        identity = (operation_key, workspace_ref, store_ref, artifact_ref)
        if identity in self.receipts:
            return FakeStoreResult.DUPLICATE
        if store_ref not in self.store_refs:
            return FakeStoreResult.UNCOVERED
        if store_ref in self.unavailable:
            return FakeStoreResult.UNAVAILABLE
        self.calls.append(identity)
        self.records.discard((workspace_ref, store_ref, artifact_ref))
        self.receipts[identity] = FakeStoreResult.DELETED_SYNTHETIC
        return FakeStoreResult.DELETED_SYNTHETIC


class FakeRestoreHarness:
    """Quarantine fake for a synthetic restored record; no restore I/O occurs."""

    def __init__(self, *, record_ref: FakeRef) -> None:
        self.record_ref = _fake_ref(record_ref)
        self.quarantined = True
        self.restrictions_replayed = False
        self.deletion_restriction_applied = False

    def replay_current_restrictions(self, *, succeeded: bool) -> None:
        self.restrictions_replayed = succeeded is True
        if succeeded is True:
            self.deletion_restriction_applied = True
            self.quarantined = False

    def can_read_or_publish(self, *, record_ref: FakeRef) -> bool:
        record_ref = _fake_ref(record_ref)
        return (
            record_ref == self.record_ref
            and not self.quarantined
            and self.restrictions_replayed
            and not self.deletion_restriction_applied
        )


class SyntheticAuditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_ref: FakeRef
    result_code: Literal[
        "DELETED_SYNTHETIC",
        "DENIED",
        "UNAVAILABLE",
        "UNCOVERED",
        "AUDIT_FIELD_REJECTED",
    ]


def test_fake_harness_rejects_unregistered_identifiers() -> None:
    with pytest.raises(ValueError):
        FakeStoreHarness(
            workspace_ref="fake-provider-message-id",
            store_refs=("fake-store-a",),
            artifact_refs=("fake-artifact-a",),
        )

    store = FakeStoreHarness(
        workspace_ref="fake-workspace-a",
        store_refs=("fake-store-a",),
        artifact_refs=("fake-artifact-a",),
    )
    with pytest.raises(ValueError):
        store.delete_synthetic(
            operation_key="fake-operation-a",
            workspace_ref="fake-workspace-a",
            store_ref="real-provider-store-id",
            artifact_ref="fake-artifact-a",
            current_test_authority=True,
        )


def test_recovery_vector_catalog_covers_required_failure_modes() -> None:
    assert {item.case_id for item in VECTORS} == {
        "partial_delete",
        "store_outage",
        "missing_authority",
        "stale_authority",
        "revoked_authority",
        "cross_workspace",
        "duplicate_retry",
        "cancellation",
        "unknown_commit",
        "restore_quarantine_replay",
        "audit_log_redaction",
        "missing_uncovered_store",
    }
    assert all(item.qualification is VectorStatus.NOT_RUN for item in VECTORS)
    assert all(item.reads_denied and item.publication_denied and item.checkpoints_denied for item in VECTORS)


def test_recovery_vector_catalog_pins_state_events_and_safe_codes() -> None:
    expected = {
        "partial_delete": ("CLEANUP_PENDING", ("store_a_verified", "store_b_failed"), "CLEANUP_INCOMPLETE", "STORE_FAILED"),
        "store_outage": ("CLEANUP_PENDING", ("store_unavailable",), "CLEANUP_INCOMPLETE", "STORE_UNAVAILABLE"),
        "missing_authority": ("PROPOSED", ("authority_missing",), "BLOCKED", "AUTHORITY_MISSING"),
        "stale_authority": ("PROPOSED", ("authority_stale",), "BLOCKED", "AUTHORITY_STALE"),
        "revoked_authority": ("PROPOSED", ("authority_revoked",), "BLOCKED", "AUTHORITY_REVOKED"),
        "cross_workspace": ("FENCED_TEST_ONLY", ("workspace_mismatch",), "BLOCKED", "DENIED"),
        "duplicate_retry": (
            "CLEANUP_INCOMPLETE",
            ("retry_same_operation_key", "replay_same_command"),
            "VERIFIED_SYNTHETIC",
            "VERIFIED_SYNTHETIC",
        ),
        "cancellation": ("CLEANUP_PENDING", ("cancel", "late_store_result"), "CANCELLED", "CANCELLED"),
        "unknown_commit": ("CLEANUP_PENDING", ("commit_ack_lost", "no_receipt"), "UNKNOWN", "COMMIT_UNKNOWN"),
        "restore_quarantine_replay": (
            "VERIFIED_SYNTHETIC",
            ("restore_requested", "restriction_replay_pending", "restriction_replay_verified"),
            "VERIFIED_SYNTHETIC",
            "VERIFIED_SYNTHETIC",
        ),
        "audit_log_redaction": ("CLEANUP_PENDING", ("emit_forbidden_field",), "CLEANUP_INCOMPLETE", "AUDIT_FIELD_REJECTED"),
        "missing_uncovered_store": ("CLEANUP_PENDING", ("uncovered_store_detected",), "CLEANUP_INCOMPLETE", "STORE_UNCOVERED"),
    }

    assert {
        item.case_id: (item.initial_state, item.events, item.expected_state, item.safe_code)
        for item in VECTORS
    } == expected
    assert all(item.qualification is VectorStatus.NOT_RUN for item in VECTORS)


def test_fake_store_duplicate_command_has_one_synthetic_effect() -> None:
    store = FakeStoreHarness(
        workspace_ref="fake-workspace-a",
        store_refs=("fake-store-a",),
        artifact_refs=("fake-artifact-a",),
    )
    command = {
        "operation_key": "fake-operation-a",
        "workspace_ref": "fake-workspace-a",
        "store_ref": "fake-store-a",
        "artifact_ref": "fake-artifact-a",
        "current_test_authority": True,
    }

    first = store.delete_synthetic(**command)
    replay = store.delete_synthetic(**command)

    assert first is FakeStoreResult.DELETED_SYNTHETIC
    assert replay is FakeStoreResult.DUPLICATE
    assert len(store.calls) == 1
    assert not store.records


def test_fake_store_requires_authority_before_duplicate_result() -> None:
    store = FakeStoreHarness(
        workspace_ref="fake-workspace-a",
        store_refs=("fake-store-a",),
        artifact_refs=("fake-artifact-a",),
    )
    command = {
        "operation_key": "fake-operation-a",
        "workspace_ref": "fake-workspace-a",
        "store_ref": "fake-store-a",
        "artifact_ref": "fake-artifact-a",
    }
    first = store.delete_synthetic(**command, current_test_authority=True)
    denied_replay = store.delete_synthetic(**command, current_test_authority=False)

    assert first is FakeStoreResult.DELETED_SYNTHETIC
    assert denied_replay is FakeStoreResult.DENIED
    assert len(store.calls) == 1


def test_fake_store_partial_failure_preserves_unavailable_store_record() -> None:
    store = FakeStoreHarness(
        workspace_ref="fake-workspace-a",
        store_refs=("fake-store-a", "fake-store-b"),
        artifact_refs=("fake-artifact-a",),
        unavailable=frozenset({"fake-store-b"}),
    )
    success = store.delete_synthetic(
        operation_key="fake-operation-a",
        workspace_ref="fake-workspace-a",
        store_ref="fake-store-a",
        artifact_ref="fake-artifact-a",
        current_test_authority=True,
    )
    unavailable = store.delete_synthetic(
        operation_key="fake-operation-a",
        workspace_ref="fake-workspace-a",
        store_ref="fake-store-b",
        artifact_ref="fake-artifact-a",
        current_test_authority=True,
    )

    assert success is FakeStoreResult.DELETED_SYNTHETIC
    assert unavailable is FakeStoreResult.UNAVAILABLE
    assert ("fake-workspace-a", "fake-store-a", "fake-artifact-a") not in store.records
    assert ("fake-workspace-a", "fake-store-b", "fake-artifact-a") in store.records
    assert len(store.calls) == 1


def test_fake_store_retry_after_outage_reuses_one_logical_effect() -> None:
    store = FakeStoreHarness(
        workspace_ref="fake-workspace-a",
        store_refs=("fake-store-a",),
        artifact_refs=("fake-artifact-a",),
        unavailable=frozenset({"fake-store-a"}),
    )
    command = {
        "operation_key": "fake-operation-a",
        "workspace_ref": "fake-workspace-a",
        "store_ref": "fake-store-a",
        "artifact_ref": "fake-artifact-a",
        "current_test_authority": True,
    }

    assert store.delete_synthetic(**command) is FakeStoreResult.UNAVAILABLE
    assert store.records
    store.set_store_available(store_ref="fake-store-a")
    assert store.delete_synthetic(**command) is FakeStoreResult.DELETED_SYNTHETIC
    assert store.delete_synthetic(**command) is FakeStoreResult.DUPLICATE
    assert len(store.calls) == 1
    assert not store.records


def test_fake_store_denies_other_workspace_without_leaking_record_state() -> None:
    store = FakeStoreHarness(
        workspace_ref="fake-workspace-b",
        store_refs=("fake-store-b",),
        artifact_refs=("fake-artifact-b",),
    )

    outcome = store.delete_synthetic(
        operation_key="fake-operation-a",
        workspace_ref="fake-workspace-a",
        store_ref="fake-store-b",
        artifact_ref="fake-artifact-b",
        current_test_authority=True,
    )

    assert outcome is FakeStoreResult.DENIED
    assert not store.calls
    assert len(store.records) == 1


@pytest.mark.parametrize("authority", [False, None, "true"])
def test_fake_store_denies_missing_or_stale_or_revoked_authority(authority: object) -> None:
    store = FakeStoreHarness(
        workspace_ref="fake-workspace-a",
        store_refs=("fake-store-a",),
        artifact_refs=("fake-artifact-a",),
    )

    outcome = store.delete_synthetic(
        operation_key="fake-operation-a",
        workspace_ref="fake-workspace-a",
        store_ref="fake-store-a",
        artifact_ref="fake-artifact-a",
        current_test_authority=authority,
    )

    assert outcome is FakeStoreResult.DENIED
    assert not store.calls
    assert len(store.records) == 1


def test_fake_store_outage_and_uncovered_store_do_not_complete() -> None:
    store = FakeStoreHarness(
        workspace_ref="fake-workspace-a",
        store_refs=("fake-store-a",),
        artifact_refs=("fake-artifact-a",),
        unavailable=frozenset({"fake-store-a"}),
    )
    outage = store.delete_synthetic(
        operation_key="fake-operation-a",
        workspace_ref="fake-workspace-a",
        store_ref="fake-store-a",
        artifact_ref="fake-artifact-a",
        current_test_authority=True,
    )
    uncovered = store.delete_synthetic(
        operation_key="fake-operation-b",
        workspace_ref="fake-workspace-a",
        store_ref="fake-store-uncovered",
        artifact_ref="fake-artifact-a",
        current_test_authority=True,
    )

    assert outage is FakeStoreResult.UNAVAILABLE
    assert uncovered is FakeStoreResult.UNCOVERED
    assert not store.calls
    assert len(store.records) == 1


@pytest.mark.parametrize(
    "forbidden",
    [
        {"body": "synthetic body"},
        {"address": "synthetic@example.invalid"},
        {"provider_id": "fake-provider-id"},
        {"attachment_name": "synthetic.bin"},
        {"digest": "a" * 64},
    ],
)
def test_audit_schema_rejects_unapproved_fields(forbidden: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        SyntheticAuditEntry(
            operation_ref="fake-operation-a",
            result_code="AUDIT_FIELD_REJECTED",
            **forbidden,
        )


def test_unknown_commit_vector_requires_receipt_before_state_resolution() -> None:
    vector = next(item for item in VECTORS if item.case_id == "unknown_commit")
    assert vector.events == ("commit_ack_lost", "no_receipt")
    assert vector.expected_state == "UNKNOWN"
    assert vector.qualification is VectorStatus.NOT_RUN


def test_restore_vector_keeps_all_access_paths_denied_until_replay() -> None:
    vector = next(item for item in VECTORS if item.case_id == "restore_quarantine_replay")
    assert vector.events[-1] == "restriction_replay_verified"
    assert vector.reads_denied
    assert vector.publication_denied
    assert vector.checkpoints_denied
    assert vector.qualification is VectorStatus.NOT_RUN


def test_fake_restore_remains_quarantined_until_restriction_replay_succeeds() -> None:
    restored = FakeRestoreHarness(record_ref="fake-restored-artifact-a")

    assert not restored.can_read_or_publish(record_ref="fake-restored-artifact-a")
    restored.replay_current_restrictions(succeeded=False)
    assert not restored.can_read_or_publish(record_ref="fake-restored-artifact-a")
    restored.replay_current_restrictions(succeeded="true")
    assert restored.quarantined
    restored.replay_current_restrictions(succeeded=True)
    assert not restored.quarantined
    assert restored.deletion_restriction_applied
    assert not restored.can_read_or_publish(record_ref="fake-restored-artifact-a")
