"""Compose a complete Drive snapshot and change-feed drain into removal evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from spine.application.lifecycle import LifecycleEligibilityQuery
from spine.application.google_drive_changes import (
    DriveChangeCommand,
    DriveChangeResult,
    DriveChangeRunState,
)
from spine.application.google_drive_lifecycle import (
    DriveLifecycleResult,
    DriveLifecycleSignal,
    DriveReconciliationEvidence,
    DriveReconciliationState,
    DriveRemovalPolicy,
    GoogleDriveLifecycleCoordinator,
)
from spine.application.google_drive_snapshot import (
    DriveInitialSnapshotCommand,
    DriveInitialSnapshotResult,
    DriveSnapshotRunState,
)
from spine.domain.common import DefinitionModel


class DriveInitialSnapshotPort(Protocol):
    async def run(
        self, command: DriveInitialSnapshotCommand
    ) -> DriveInitialSnapshotResult: ...


class DriveIncrementalChangesPort(Protocol):
    async def run(self, command: DriveChangeCommand) -> DriveChangeResult: ...


class DriveFullReconciliationCommand(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    service_principal_id: UUID
    authorization_generation: int = Field(gt=0)
    configuration_generation: int = Field(gt=0)
    access_policy_version_id: UUID
    command_identity: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)
    last_committed_cursor: str | None = Field(default=None, max_length=4096)
    reconciliation_max_pages: int = Field(default=100, gt=0, le=10_000)
    lifecycle_query: LifecycleEligibilityQuery | None = None

    @model_validator(mode="after")
    def validate_ids(self) -> "DriveFullReconciliationCommand":
        for name in (
            "workspace_id", "environment_id", "connection_id", "resource_scope_reference",
            "service_principal_id", "access_policy_version_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        query = self.lifecycle_query
        if query is not None and (
            query.workspace_id != self.workspace_id
            or query.environment_id != self.environment_id
            or query.connection_id != self.connection_id
            or query.scope_reference != self.resource_scope_reference
            or query.provider_object_reference != str(self.resource_scope_reference)
        ):
            raise ValueError("lifecycle query must bind the exact Drive root scope")
        return self


@dataclass(frozen=True, slots=True)
class DriveFullReconciliationResult:
    snapshot: DriveInitialSnapshotResult
    changes: DriveChangeResult | None
    lifecycle: DriveLifecycleResult | None
    state: DriveReconciliationState


@dataclass(frozen=True, slots=True)
class DriveFullReconciliationCoordinator:
    """Run a full scoped snapshot, drain its captured feed, then evaluate lifecycle."""

    snapshots: DriveInitialSnapshotPort
    changes: DriveIncrementalChangesPort
    lifecycle: GoogleDriveLifecycleCoordinator
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def run(
        self,
        command: DriveFullReconciliationCommand,
        policy: DriveRemovalPolicy,
    ) -> DriveFullReconciliationResult:
        snapshot_command = DriveInitialSnapshotCommand(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            connection_id=command.connection_id,
            resource_scope_reference=command.resource_scope_reference,
            service_principal_id=command.service_principal_id,
            authorization_generation=command.authorization_generation,
            configuration_generation=command.configuration_generation,
            access_policy_version_id=command.access_policy_version_id,
            command_identity=_subcommand(command.command_identity, "snapshot", 128),
            idempotency_key=_subcommand(command.idempotency_key, "snapshot"),
            last_committed_cursor=command.last_committed_cursor,
            lifecycle_query=command.lifecycle_query,
        )
        snapshot = await self.snapshots.run(snapshot_command)
        if snapshot.state is not DriveSnapshotRunState.SUCCEEDED:
            return DriveFullReconciliationResult(
                snapshot, None, None,
                _snapshot_state(snapshot.state),
            )

        change_command = DriveChangeCommand(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            connection_id=command.connection_id,
            resource_scope_reference=command.resource_scope_reference,
            service_principal_id=command.service_principal_id,
            authorization_generation=command.authorization_generation,
            configuration_generation=command.configuration_generation,
            access_policy_version_id=command.access_policy_version_id,
            command_identity=_subcommand(command.command_identity, "changes", 128),
            idempotency_key=_subcommand(command.idempotency_key, "changes"),
            last_committed_cursor=snapshot.proposed_start_page_token,
            reconciliation_max_pages=command.reconciliation_max_pages,
            lifecycle_query=command.lifecycle_query,
        )
        changes = await self.changes.run(change_command)
        if changes.state is not DriveChangeRunState.SUCCEEDED:
            state = _change_state(changes.run.reconciliation_reason)
            return DriveFullReconciliationResult(snapshot, changes, None, state)

        file_ids = {item.file_id for page in snapshot.pages for item in page.items}
        signals: dict[str, DriveLifecycleSignal] = {}
        for page in changes.pages:
            for item in page.items:
                signals[item.file_id] = item.signal
                if item.signal in {
                    DriveLifecycleSignal.REMOVED,
                    DriveLifecycleSignal.TRASHED,
                    DriveLifecycleSignal.MOVED_OUT_OF_SCOPE,
                }:
                    file_ids.discard(item.file_id)
                elif item.signal is DriveLifecycleSignal.PRESENT:
                    file_ids.add(item.file_id)
                else:
                    # An unknown membership signal makes this full scan unsuitable
                    # as deletion evidence, even if its cursor advanced safely.
                    return DriveFullReconciliationResult(
                        snapshot, changes, None, DriveReconciliationState.RECONCILIATION_REQUIRED
                    )

        completed_at = self.clock()
        if completed_at.tzinfo is None or completed_at.utcoffset() is None:
            raise ValueError("Drive reconciliation clock must be timezone-aware")
        evidence = DriveReconciliationEvidence(
            reconciliation_id=_reconciliation_id(command),
            snapshot_run_id=snapshot.run.sync_run_id,
            change_sync_run_id=changes.run.sync_run_id,
            committed_cursor=changes.committed_cursor,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            connection_id=command.connection_id,
            resource_scope_reference=command.resource_scope_reference,
            service_principal_id=command.service_principal_id,
            access_policy_version_id=command.access_policy_version_id,
            authorization_generation=command.authorization_generation,
            configuration_generation=command.configuration_generation,
            state=DriveReconciliationState.COMPLETE,
            file_ids=frozenset(file_ids),
            signals=tuple(sorted(signals.items())),
            started_at=snapshot.run.requested_at,
            completed_at=completed_at,
        )
        lifecycle_result = await self.lifecycle.reconcile(evidence, policy)
        state = (
            DriveReconciliationState.COMPLETE
            if lifecycle_result.retained_reason is None
            else DriveReconciliationState.RECONCILIATION_REQUIRED
        )
        return DriveFullReconciliationResult(snapshot, changes, lifecycle_result, state)


def _subcommand(value: str, suffix: str, max_length: int = 255) -> str:
    candidate = f"{value}.{suffix}"
    if len(candidate) <= max_length:
        return candidate
    digest = uuid5(NAMESPACE_URL, candidate).hex
    return f"drive-reconciliation:{digest}"


def _reconciliation_id(command: DriveFullReconciliationCommand) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "spine:google-drive-full-reconciliation:v1:"
        f"{command.workspace_id}:{command.environment_id}:{command.connection_id}:"
        f"{command.resource_scope_reference}:{command.idempotency_key}",
    )


def _snapshot_state(state: DriveSnapshotRunState) -> DriveReconciliationState:
    if state is DriveSnapshotRunState.RECONCILIATION_REQUIRED:
        return DriveReconciliationState.RECONCILIATION_REQUIRED
    if state is DriveSnapshotRunState.SUCCEEDED:
        return DriveReconciliationState.COMPLETE
    return DriveReconciliationState.INCOMPLETE


def _change_state(reason: str | None) -> DriveReconciliationState:
    if reason in {"provider_http_401", "provider_http_403"}:
        return DriveReconciliationState.AUTHORIZATION_REVOKED
    if reason == "incomplete_change_page":
        return DriveReconciliationState.INCOMPLETE
    return DriveReconciliationState.RECONCILIATION_REQUIRED


__all__ = [
    "DriveFullReconciliationCommand",
    "DriveFullReconciliationCoordinator",
    "DriveFullReconciliationResult",
    "DriveIncrementalChangesPort",
    "DriveInitialSnapshotPort",
]
