from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.google_drive_lifecycle import (
    DriveLifecycleDecisionState,
    DriveLifecycleSignal,
    DriveLifecycleSource,
    DriveRemovalPolicy,
    GoogleDriveLifecycleCoordinator,
    InMemoryDriveLifecycleDecisionRepository,
)
from spine.application.google_drive_reconciliation import (
    DriveFullReconciliationCommand,
    DriveFullReconciliationCoordinator,
)
from spine.application.google_drive_changes import DriveChangeRunState
from spine.application.google_drive_snapshot import DriveSnapshotRunState


NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
ENVIRONMENT = UUID("22222222-2222-4222-8222-222222222222")
CONNECTION = UUID("33333333-3333-4333-8333-333333333333")
SCOPE = UUID("44444444-4444-4444-8444-444444444444")
PRINCIPAL = UUID("55555555-5555-4555-8555-555555555555")
ACCESS_POLICY = UUID("66666666-6666-4666-8666-666666666666")
SOURCE_ID = UUID("77777777-7777-4777-8777-777777777777")
REVISION_ID = UUID("88888888-8888-4888-8888-888888888888")
TOMBSTONE_ID = UUID("99999999-9999-4999-8999-999999999999")


def command():
    return DriveFullReconciliationCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE,
        service_principal_id=PRINCIPAL,
        authorization_generation=3,
        configuration_generation=4,
        access_policy_version_id=ACCESS_POLICY,
        command_identity="drive.reconcile",
        idempotency_key="reconcile-1",
        last_committed_cursor="cursor-old",
    )


def removal_policy():
    return DriveRemovalPolicy(
        policy_version_id=ACCESS_POLICY,
        version="drive-removal-policy:v1",
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE,
        authorized_signals=frozenset({DriveLifecycleSignal.REMOVED}),
    )


class SnapshotRunner:
    def __init__(self, state=DriveSnapshotRunState.SUCCEEDED):
        self.state = state
        self.command = None

    async def run(self, request):
        self.command = request
        return SimpleNamespace(
            state=self.state,
            run=SimpleNamespace(
                requested_at=NOW,
                sync_run_id=UUID("abababab-abab-4bab-8bab-abababababab"),
            ),
            proposed_start_page_token="cursor-at-snapshot-start",
            pages=(
                SimpleNamespace(
                    items=(SimpleNamespace(file_id="file-1"),)
                ),
            ),
        )


class ChangesRunner:
    def __init__(self, *, state=DriveChangeRunState.SUCCEEDED, signal=DriveLifecycleSignal.REMOVED):
        self.state = state
        self.signal = signal
        self.command = None

    async def run(self, request):
        self.command = request
        return SimpleNamespace(
            state=self.state,
            run=SimpleNamespace(
                reconciliation_reason=(
                    "provider_http_403"
                    if self.state is DriveChangeRunState.RECONCILIATION_REQUIRED
                    else None
                ),
                sync_run_id=UUID("acacacac-acac-4cac-8cac-acacacacacac"),
            ),
            committed_cursor="cursor-committed",
            pages=(
                SimpleNamespace(
                    items=(SimpleNamespace(file_id="file-1", signal=self.signal),)
                ),
            ),
        )


class Sources:
    async def list_current_sources(self, evidence, context):
        del evidence, context
        return (
            DriveLifecycleSource(
                file_id="file-1",
                source_object_id=SOURCE_ID,
                current_revision_id=REVISION_ID,
                is_tombstoned=False,
            ),
        )


class Tombstones:
    async def compose(self, **kwargs):
        from spine.application.source_lifecycle import SourceLifecycleKind

        key = kwargs["idempotency_key"]
        return SimpleNamespace(
            kind=SourceLifecycleKind.DELETION,
            source=SimpleNamespace(source_object_id=SOURCE_ID),
            revision=SimpleNamespace(revision_id=TOMBSTONE_ID),
            observation_idempotency_key=SimpleNamespace(value=key),
            canonical_acceptance=SimpleNamespace(
                idempotency_key=SimpleNamespace(value=key),
                expected_current_revision_id=REVISION_ID,
            ),
        )


class Lifecycle:
    def __init__(self):
        self.commands = []

    async def apply(self, request):
        self.commands.append(request)
        return SimpleNamespace(
            acceptance=SimpleNamespace(revision=SimpleNamespace(revision_id=TOMBSTONE_ID))
        )


def coordinator(snapshot_state=DriveSnapshotRunState.SUCCEEDED, change_state=DriveChangeRunState.SUCCEEDED, signal=DriveLifecycleSignal.REMOVED):
    snapshot = SnapshotRunner(snapshot_state)
    changes = ChangesRunner(state=change_state, signal=signal)
    lifecycle_effects = Lifecycle()
    lifecycle = GoogleDriveLifecycleCoordinator(
        decisions=InMemoryDriveLifecycleDecisionRepository(),
        sources=Sources(),
        tombstones=Tombstones(),
        source_lifecycle=lifecycle_effects,
        context_factory=lambda identity: SimpleNamespace(trace_id=identity),
        clock=lambda: NOW,
    )
    return (
        DriveFullReconciliationCoordinator(
            snapshots=snapshot,
            changes=changes,
            lifecycle=lifecycle,
            clock=lambda: NOW,
        ),
        snapshot,
        changes,
        lifecycle_effects,
    )


@pytest.mark.asyncio
async def test_full_reconciliation_drains_feed_before_authorized_removal():
    service, snapshot, changes, lifecycle = coordinator()

    result = await service.run(command(), removal_policy())

    assert result.state.value == "complete"
    assert changes.command.last_committed_cursor == "cursor-at-snapshot-start"
    assert result.lifecycle.decisions[0].signal is DriveLifecycleSignal.REMOVED
    assert result.lifecycle.decisions[0].state is DriveLifecycleDecisionState.TOMBSTONED
    assert len(lifecycle.commands) == 1


@pytest.mark.asyncio
async def test_incomplete_snapshot_or_revoked_access_never_runs_lifecycle():
    incomplete, _, _, lifecycle = coordinator(snapshot_state=DriveSnapshotRunState.RECONCILIATION_REQUIRED)
    incomplete_result = await incomplete.run(command(), removal_policy())
    assert incomplete_result.lifecycle is None
    assert lifecycle.commands == []

    revoked, _, _, lifecycle = coordinator(change_state=DriveChangeRunState.RECONCILIATION_REQUIRED)
    revoked_result = await revoked.run(command(), removal_policy())
    assert revoked_result.state.value == "authorization_revoked"
    assert revoked_result.lifecycle is None
    assert lifecycle.commands == []


@pytest.mark.asyncio
async def test_ambiguous_change_signal_withholds_deletion_evidence():
    service, _, _, lifecycle = coordinator(signal=DriveLifecycleSignal.AMBIGUOUS)

    result = await service.run(command(), removal_policy())

    assert result.state.value == "reconciliation_required"
    assert result.lifecycle is None
    assert lifecycle.commands == []
