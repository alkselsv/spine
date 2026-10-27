from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.google_drive_lifecycle import (
    DriveLifecycleDecisionState,
    DriveLifecycleSignal,
    DriveLifecycleSource,
    DriveReappearanceEvidence,
    DriveReconciliationEvidence,
    DriveReconciliationState,
    DriveRemovalPolicy,
    GoogleDriveLifecycleCoordinator,
    InMemoryDriveLifecycleDecisionRepository,
)
from spine.application.source_lifecycle import SourceLifecycleKind
from spine.connectors.google_drive import DriveChange, drive_access_signal
from spine.domain.sources import IdentityMode, RevisionMetadata, SourceObject
from spine.domain.sources.canonicalization import content_revision
from spine.domain.sources.google import GoogleDriveFileKey, GoogleDriveLifecycleSignal, google_source_identity
from spine.infrastructure.object_storage.contracts import ObjectReference


NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
ENVIRONMENT = UUID("22222222-2222-4222-8222-222222222222")
CONNECTION = UUID("33333333-3333-4333-8333-333333333333")
SCOPE = UUID("44444444-4444-4444-8444-444444444444")
PRINCIPAL = UUID("55555555-5555-4555-8555-555555555555")
POLICY_VERSION_ID = UUID("66666666-6666-4666-8666-666666666666")
SOURCE_ID = UUID("77777777-7777-4777-8777-777777777777")
REVISION_ID = UUID("88888888-8888-4888-8888-888888888888")
TOMBSTONE_ID = UUID("99999999-9999-4999-8999-999999999999")


def evidence(
    *,
    state: DriveReconciliationState = DriveReconciliationState.COMPLETE,
    file_ids: frozenset[str] = frozenset(),
    signals: tuple[tuple[str, DriveLifecycleSignal], ...] = (),
) -> DriveReconciliationEvidence:
    return DriveReconciliationEvidence(
        reconciliation_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        snapshot_run_id=UUID("abababab-abab-4bab-8bab-abababababab"),
        change_sync_run_id=UUID("acacacac-acac-4cac-8cac-acacacacacac"),
        committed_cursor="cursor-complete",
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE,
        service_principal_id=PRINCIPAL,
        access_policy_version_id=POLICY_VERSION_ID,
        authorization_generation=3,
        configuration_generation=4,
        state=state,
        file_ids=file_ids,
        signals=signals,
        started_at=NOW,
        completed_at=NOW if state is DriveReconciliationState.COMPLETE else None,
    )


def policy(*signals: DriveLifecycleSignal) -> DriveRemovalPolicy:
    return DriveRemovalPolicy(
        policy_version_id=POLICY_VERSION_ID,
        version="drive-removal-policy:v1",
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE,
        authorized_signals=frozenset(signals),
    )


class Sources:
    def __init__(self, sources: tuple[DriveLifecycleSource, ...]) -> None:
        self.values = sources
        self.calls = 0

    async def list_current_sources(self, evidence, context):
        del evidence, context
        self.calls += 1
        return self.values


class Tombstones:
    def __init__(self) -> None:
        self.calls = []

    async def compose(self, **kwargs):
        self.calls.append(kwargs)
        key = kwargs["idempotency_key"]
        return SimpleNamespace(
            kind=SourceLifecycleKind.DELETION,
            revision=SimpleNamespace(revision_id=TOMBSTONE_ID),
            observation_idempotency_key=SimpleNamespace(value=key),
            canonical_acceptance=SimpleNamespace(
                idempotency_key=SimpleNamespace(value=key),
                expected_current_revision_id=REVISION_ID,
            ),
            source=SimpleNamespace(source_object_id=SOURCE_ID),
        )


class Lifecycle:
    def __init__(self) -> None:
        self.commands = []

    async def apply(self, command):
        self.commands.append(command)
        return SimpleNamespace(
            acceptance=SimpleNamespace(
                revision=SimpleNamespace(revision_id=TOMBSTONE_ID)
            )
        )


def source(*, file_id: str = "file-1", tombstoned: bool = False):
    return DriveLifecycleSource(
        file_id=file_id,
        source_object_id=SOURCE_ID,
        current_revision_id=REVISION_ID,
        is_tombstoned=tombstoned,
    )


def reappearance_evidence(
    *, file_id: str = "file-1", canonical_file_id: str | None = None,
    continuity: bool = True,
):
    identity = google_source_identity(
        GoogleDriveFileKey(connection_id=CONNECTION, file_id=canonical_file_id or file_id)
    )
    source_id = uuid5(
        NAMESPACE_URL,
        ":".join((
            "spine:connector-ingestion:source:v1",
            str(WORKSPACE), str(ENVIRONMENT), str(CONNECTION),
            identity.external_namespace, identity.external_generation,
            identity.external_object_id,
        )),
    )
    source_object = SourceObject(
        source_object_id=source_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_kind="document",
        identity_mode=IdentityMode.CONNECTOR,
        connection_id=CONNECTION,
        external_namespace=identity.external_namespace,
        external_generation=identity.external_generation,
        external_object_id=identity.external_object_id,
        created_at=NOW,
    )
    revision = content_revision(
        revision_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        source_object_id=source_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        kind="content",
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata=RevisionMetadata(embedded_title="Reappeared"),
        original_reference=ObjectReference(
            schema_version=1,
            object_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            storage_generation=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            digest_algorithm="sha256",
            digest_hex="a" * 64,
            byte_length=4,
        ),
        original_sha256="a" * 64,
        byte_length=4,
        media_type="text/plain",
        reappearance_after_tombstone_revision_id=TOMBSTONE_ID,
        observed_at=NOW,
    )
    return DriveReappearanceEvidence(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE,
        service_principal_id=PRINCIPAL,
        authorization_generation=3,
        configuration_generation=4,
        access_policy_version_id=POLICY_VERSION_ID,
        file_id=file_id,
        source=source_object,
        revision=revision,
        tombstone_predecessor_revision_id=TOMBSTONE_ID,
        identity_continuity_proven=continuity,
        observed_at=NOW,
    )


def coordinator(sources=None):
    store = InMemoryDriveLifecycleDecisionRepository()
    source_reader = Sources((source(),) if sources is None else sources)
    tombstones = Tombstones()
    lifecycle = Lifecycle()
    service = GoogleDriveLifecycleCoordinator(
        decisions=store,
        sources=source_reader,
        tombstones=tombstones,
        source_lifecycle=lifecycle,
        context_factory=lambda identity: SimpleNamespace(trace_id=identity),
        clock=lambda: NOW,
    )
    return service, store, source_reader, tombstones, lifecycle


@pytest.mark.asyncio
async def test_confirmed_absence_retains_canonical_head_without_explicit_policy():
    service, store, _, tombstones, lifecycle = coordinator()

    result = await service.reconcile(evidence(), policy())

    assert result.retained_reason is None
    assert result.decisions[0].signal is DriveLifecycleSignal.CONFIRMED_ABSENT
    assert result.decisions[0].state is DriveLifecycleDecisionState.RETAINED
    assert result.decisions[0].policy_authorized is False
    assert len(store.records) == 1
    assert tombstones.calls == []
    assert lifecycle.commands == []


@pytest.mark.asyncio
async def test_complete_authorized_scoped_absence_tombstones_once_and_replays():
    service, _, _, tombstones, lifecycle = coordinator()
    scan = evidence()
    removal_policy = policy(DriveLifecycleSignal.CONFIRMED_ABSENT)

    first = await service.reconcile(scan, removal_policy)
    replay = await service.reconcile(scan, removal_policy)

    assert first.decisions[0].state is DriveLifecycleDecisionState.TOMBSTONED
    assert replay.decisions[0].decision_id == first.decisions[0].decision_id
    assert replay.lifecycle_results == ()
    assert len(tombstones.calls) == 1
    assert len(lifecycle.commands) == 1
    assert lifecycle.commands[0].observation_idempotency_key.value == tombstones.calls[0]["idempotency_key"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        DriveReconciliationState.INCOMPLETE,
        DriveReconciliationState.AUTHORIZATION_REVOKED,
        DriveReconciliationState.RECONCILIATION_REQUIRED,
    ],
)
async def test_unusable_reconciliation_retains_head_and_emits_no_tombstone(state):
    service, store, sources, tombstones, lifecycle = coordinator()

    result = await service.reconcile(evidence(state=state), policy(DriveLifecycleSignal.CONFIRMED_ABSENT))

    assert result.retained_reason == state.value
    assert result.decisions == ()
    assert sources.calls == 0
    assert store.records == {}
    assert tombstones.calls == []
    assert lifecycle.commands == []


@pytest.mark.parametrize(
    "signal",
    [DriveLifecycleSignal.HTTP_401, DriveLifecycleSignal.HTTP_403, DriveLifecycleSignal.HTTP_404],
)
@pytest.mark.asyncio
async def test_file_access_error_never_proves_absence(signal):
    service, store, _, tombstones, lifecycle = coordinator()

    result = await service.reconcile(
        evidence(signals=(("file-1", signal),)),
        policy(DriveLifecycleSignal.CONFIRMED_ABSENT),
    )

    assert result.decisions == ()
    assert store.records == {}
    assert tombstones.calls == []
    assert lifecycle.commands == []


@pytest.mark.parametrize(
    "signal",
    [DriveLifecycleSignal.TRASHED, DriveLifecycleSignal.MOVED_OUT_OF_SCOPE],
)
@pytest.mark.asyncio
async def test_trash_and_scope_exit_are_non_authoritative_by_default(signal):
    service, _, _, tombstones, lifecycle = coordinator()

    result = await service.reconcile(
        evidence(file_ids=frozenset({"file-1"}), signals=(("file-1", signal),)),
        policy(),
    )

    assert result.decisions[0].signal is signal
    assert result.decisions[0].state is DriveLifecycleDecisionState.RETAINED
    assert tombstones.calls == []
    assert lifecycle.commands == []


def test_drive_change_classifies_provider_signals_without_authorizing_deletion():
    assert DriveChange(file_id="file-1", removed=True).lifecycle_signal() is GoogleDriveLifecycleSignal.REMOVED
    assert DriveChange(file_id="file-1", trashed=True).lifecycle_signal() is GoogleDriveLifecycleSignal.TRASHED
    assert DriveChange(file_id="file-1").lifecycle_signal(in_scope=False) is GoogleDriveLifecycleSignal.MOVED_OUT_OF_SCOPE
    assert DriveChange(file_id="file-1").lifecycle_signal() is GoogleDriveLifecycleSignal.AMBIGUOUS


@pytest.mark.parametrize(
    ("status_code", "signal"),
    [
        (401, GoogleDriveLifecycleSignal.HTTP_401),
        (403, GoogleDriveLifecycleSignal.HTTP_403),
        (404, GoogleDriveLifecycleSignal.HTTP_404),
    ],
)
def test_drive_access_statuses_remain_distinct_typed_outcomes(status_code, signal):
    assert drive_access_signal(status_code) is signal


def test_drive_reappearance_requires_same_google_identity_and_tombstone_predecessor():
    evidence = reappearance_evidence()

    assert evidence.source.source_object_id == evidence.revision.source_object_id
    assert evidence.revision.reappearance_after_tombstone_revision_id == TOMBSTONE_ID

    with pytest.raises(ValueError, match="identity continuity"):
        reappearance_evidence(continuity=False)

    with pytest.raises(ValueError, match="canonical identity"):
        reappearance_evidence(canonical_file_id="different-file")


@pytest.mark.asyncio
async def test_drive_reappearance_uses_source_lifecycle_with_tombstone_lineage():
    service, _, _, _, lifecycle = coordinator(sources=())
    evidence = reappearance_evidence()
    command = SimpleNamespace(
        kind=SourceLifecycleKind.REAPPEARANCE,
        source=evidence.source,
        revision=evidence.revision,
        canonical_acceptance=SimpleNamespace(
            access_policy_version_id=POLICY_VERSION_ID,
            admission_decision_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
            authorization_generation=3,
            configuration_generation=4,
            connection_id=CONNECTION,
            resource_scope_reference=SCOPE,
            resource_object_reference="file-1",
        ),
    )

    await service.reappear(evidence, command)

    assert lifecycle.commands == [command]

    with pytest.raises(ValueError, match="identity and admission evidence"):
        await service.reappear(
            evidence,
            SimpleNamespace(
                kind=command.kind,
                source=command.source,
                revision=command.revision.model_copy(
                    update={"reappearance_after_tombstone_revision_id": REVISION_ID}
                ),
                canonical_acceptance=command.canonical_acceptance,
            ),
        )
