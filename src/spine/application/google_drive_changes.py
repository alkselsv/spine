"""Durable orchestration for the governed Google Drive change feed."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spine.application.connector_ingestion import (
    ConnectorIngestionCommand,
    ConnectorIngestionResult,
)
from spine.application.connections.google import (
    GoogleConnectionSuspensionError,
    GoogleProviderAuthorizationError,
    GoogleProviderPolicyError,
    GoogleProviderQuotaError,
    GoogleProviderReadError,
)
from spine.application.google_sync_diagnostics import GoogleSyncDiagnosticSnapshot
from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.persistence.drive_changes import (
    DriveChangeInput,
    DriveChangeItemRecord,
    DriveChangeItemState,
    DriveChangePageRecord,
    DriveChangePageState,
    DriveChangeRepository,
    DriveChangeRun,
    DriveChangeRunCommand,
    DriveChangeRunState,
    DriveChangeFenceError,
)
from spine.connectors.google_drive import (
    DriveChangePage,
    GoogleDriveAccessOutcomeError,
    GoogleDriveChangeGapError,
    GoogleDriveRetryExhaustedError,
    GoogleDriveTokenExpiredError,
)
from spine.domain.sources.google import GoogleDriveLifecycleSignal


class DriveChangeCommand(BaseModel):
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
    last_committed_cursor: str = Field(min_length=1, max_length=4096)
    reconciliation_max_pages: int = Field(default=100, gt=0, le=10_000)

    @model_validator(mode="after")
    def validate_ids(self) -> "DriveChangeCommand":
        for name in (
            "workspace_id",
            "environment_id",
            "connection_id",
            "resource_scope_reference",
            "service_principal_id",
            "access_policy_version_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        return self


class DriveChangeReader(Protocol):
    async def list_changes(
        self,
        command: DriveChangeCommand,
        page_token: str,
        context: TrustedPersistenceContext,
    ) -> DriveChangePage: ...


class ConnectorIngestionPort(Protocol):
    async def ingest(
        self, command: ConnectorIngestionCommand
    ) -> ConnectorIngestionResult: ...


class DriveReconciliationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sync_run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    service_principal_id: UUID
    authorization_generation: int = Field(gt=0)
    configuration_generation: int = Field(gt=0)
    prior_committed_cursor: str = Field(min_length=1, max_length=4096)
    reason: str = Field(min_length=1, max_length=64)
    command_identity: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)
    max_pages: int = Field(gt=0, le=10_000)


class DriveReconciliationPort(Protocol):
    async def initiate(self, command: DriveReconciliationCommand) -> None: ...


class DriveSheetsWakeupPort(Protocol):
    async def reconcile_change(
        self, command: DriveChangeCommand, item: DriveChangeItemRecord
    ) -> UUID | None: ...


@dataclass(frozen=True, slots=True)
class DriveChangeResult:
    run: DriveChangeRun
    pages: tuple[DriveChangePageRecord, ...]

    @property
    def diagnostic_snapshot(self) -> GoogleSyncDiagnosticSnapshot:
        return GoogleSyncDiagnosticSnapshot.from_run(self.run, self.pages)

    @property
    def state(self) -> DriveChangeRunState:
        return self.run.state

    @property
    def committed_cursor(self) -> str:
        return self.run.proposed_cursor or self.run.last_committed_cursor


@dataclass(frozen=True, slots=True)
class DriveChangeCoordinator:
    repository: DriveChangeRepository
    reader: DriveChangeReader
    ingestion: ConnectorIngestionPort
    context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext]
    reconciliation: DriveReconciliationPort | None = None
    sheets_wakeup: DriveSheetsWakeupPort | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def run(self, command: DriveChangeCommand) -> DriveChangeResult:
        sync_run_id = _sync_run_id(command)
        run = await self.repository.resolve_run(sync_run_id)
        if run is None:
            run = await self.repository.start_run(
                DriveChangeRunCommand(
                    sync_run_id=sync_run_id,
                    workspace_id=command.workspace_id,
                    environment_id=command.environment_id,
                    connection_id=command.connection_id,
                    resource_scope_reference=command.resource_scope_reference,
                    service_principal_id=command.service_principal_id,
                    access_policy_version_id=command.access_policy_version_id,
                    authorization_generation=command.authorization_generation,
                    configuration_generation=command.configuration_generation,
                    command_identity=command.command_identity,
                    idempotency_key=command.idempotency_key,
                    last_committed_cursor=command.last_committed_cursor,
                    requested_at=self._now(),
                )
            )
        else:
            _assert_fence(run, command)

        if run.state in {
            DriveChangeRunState.SUCCEEDED,
            DriveChangeRunState.RECONCILIATION_REQUIRED,
            DriveChangeRunState.SUSPENDED,
            DriveChangeRunState.POLICY_BLOCKED,
        }:
            return await self._result(run)

        while True:
            page_token = run.next_page_token
            existing_page = await self.repository.resolve_page(sync_run_id, page_token)
            if (
                existing_page is not None
                and existing_page.state is DriveChangePageState.COMPLETED
            ):
                if existing_page.next_page_token is not None:
                    run = await self.repository.advance_run(
                        sync_run_id,
                        next_page_token=existing_page.next_page_token,
                        now=self._now(),
                    )
                    continue
                if existing_page.new_start_page_token is None:
                    return await self._reconcile(
                        run, command, "missing_terminal_cursor"
                    )
                run = await self.repository.commit_run(sync_run_id, now=self._now())
                return await self._result(run)
            if (
                existing_page is not None
                and existing_page.state is DriveChangePageState.INCOMPLETE
            ):
                return await self._reconcile(run, command, "incomplete_change_page")

            try:
                page = await self.reader.list_changes(
                    command,
                    page_token,
                    self.context_factory(
                        PersistenceOperation("google_drive_changes_list"),
                        uuid5(NAMESPACE_URL, f"{sync_run_id}:page:{page_token}"),
                    ),
                )
                if page.page_token != page_token:
                    raise GoogleDriveChangeGapError(
                        "Drive change page token does not match checkpoint"
                    )
                stored_page = await self.repository.open_page(
                    sync_run_id=sync_run_id,
                    page_token=page.page_token,
                    next_page_token=page.next_page_token,
                    new_start_page_token=page.new_start_page_token,
                    incomplete_search=page.incomplete_search,
                    changes=tuple(
                        DriveChangeInput(
                            change_key=change.change_key,
                            file_id=change.file_id,
                            provider_version=change.provider_version,
                            removed=change.removed,
                            signal=change.lifecycle_signal(),
                        )
                        for change in page.changes
                    ),
                    now=self._now(),
                )
            except GoogleProviderAuthorizationError:
                run = await self.repository.mark_suspended(
                    sync_run_id, reason="authorization_revoked", now=self._now()
                )
                return await self._result(run)
            except GoogleConnectionSuspensionError:
                run = await self.repository.mark_suspended(
                    sync_run_id,
                    reason="connection_suspension_failed",
                    now=self._now(),
                )
                return await self._result(run)
            except GoogleProviderPolicyError:
                run = await self.repository.mark_policy_blocked(
                    sync_run_id,
                    failure_code="provider_policy_denied",
                    now=self._now(),
                )
                return await self._result(run)
            except GoogleProviderQuotaError:
                run = await self.repository.record_provider_failure(
                    sync_run_id,
                    failure_code="provider_quota_retryable",
                    now=self._now(),
                )
                return await self._result(run)
            except GoogleProviderReadError:
                run = await self.repository.record_provider_failure(
                    sync_run_id,
                    failure_code="provider_unavailable_retryable",
                    now=self._now(),
                )
                return await self._result(run)
            except GoogleDriveTokenExpiredError:
                return await self._reconcile(run, command, "provider_token_expired")
            except GoogleDriveAccessOutcomeError as error:
                return await self._reconcile(run, command, f"provider_{error.signal.value}")
            except GoogleDriveRetryExhaustedError:
                return await self._reconcile(run, command, "provider_retry_exhausted")
            except GoogleDriveChangeGapError:
                return await self._reconcile(run, command, "unrecoverable_token_gap")
            except Exception:
                run = await self.repository.mark_resumable(sync_run_id, now=self._now())
                return await self._result(run)

            for stored_item in stored_page.items:
                if stored_item.state in {
                    DriveChangeItemState.SUCCEEDED,
                    DriveChangeItemState.RECONCILED,
                    DriveChangeItemState.IGNORED,
                }:
                    continue
                if stored_item.signal in {
                    GoogleDriveLifecycleSignal.REMOVED,
                    GoogleDriveLifecycleSignal.TRASHED,
                    GoogleDriveLifecycleSignal.MOVED_OUT_OF_SCOPE,
                }:
                    await self.repository.mark_item_ignored(
                        stored_item.item_id,
                        reason=stored_item.signal.value,
                        now=self._now(),
                    )
                    continue
                if self.sheets_wakeup is not None:
                    try:
                        reconciliation_id = await self.sheets_wakeup.reconcile_change(
                            command, stored_item
                        )
                    except Exception:
                        await self.repository.mark_item_failed(
                            stored_item.item_id,
                            failure_code="sheets_reconciliation_failed",
                            now=self._now(),
                        )
                        run = await self.repository.mark_resumable(
                            sync_run_id, now=self._now()
                        )
                        return await self._result(run)
                    if reconciliation_id is not None:
                        await self.repository.mark_item_reconciled(
                            stored_item.item_id,
                            reconciliation_id=reconciliation_id,
                            now=self._now(),
                        )
                        continue
                try:
                    result = await self.ingestion.ingest(
                        _item_command(command, stored_item)
                    )
                    await self.repository.mark_item_succeeded(
                        stored_item.item_id,
                        revision_id=result.revision.revision_id,
                        now=self._now(),
                    )
                except GoogleDriveAccessOutcomeError as error:
                    await self.repository.mark_item_failed(
                        stored_item.item_id,
                        failure_code=error.signal.value,
                        now=self._now(),
                    )
                    run = await self.repository.resolve_run(sync_run_id)
                    assert run is not None
                    return await self._reconcile(
                        run, command, f"provider_{error.signal.value}"
                    )
                except Exception:
                    await self.repository.mark_item_failed(
                        stored_item.item_id,
                        failure_code="item_ingestion_failed",
                        now=self._now(),
                    )
                    run = await self.repository.mark_resumable(
                        sync_run_id, now=self._now()
                    )
                    return await self._result(run)

            if page.incomplete_search:
                await self.repository.mark_page_incomplete(
                    stored_page.page_id, now=self._now()
                )
                run = await self.repository.resolve_run(sync_run_id)
                assert run is not None
                return await self._reconcile(run, command, "incomplete_change_page")

            await self.repository.complete_page(stored_page.page_id, now=self._now())
            if page.next_page_token is None:
                if page.new_start_page_token is None:
                    run = await self.repository.resolve_run(sync_run_id)
                    assert run is not None
                    return await self._reconcile(
                        run, command, "missing_terminal_cursor"
                    )
                run = await self.repository.commit_run(sync_run_id, now=self._now())
                return await self._result(run)
            run = await self.repository.advance_run(
                sync_run_id, next_page_token=page.next_page_token, now=self._now()
            )

    async def _reconcile(
        self,
        run: DriveChangeRun,
        command: DriveChangeCommand,
        reason: str,
    ) -> DriveChangeResult:
        if self.reconciliation is not None:
            await self.reconciliation.initiate(
                DriveReconciliationCommand(
                    sync_run_id=run.sync_run_id,
                    workspace_id=run.workspace_id,
                    environment_id=run.environment_id,
                    connection_id=run.connection_id,
                    resource_scope_reference=run.resource_scope_reference,
                    service_principal_id=run.service_principal_id,
                    authorization_generation=run.authorization_generation,
                    configuration_generation=run.configuration_generation,
                    prior_committed_cursor=run.last_committed_cursor,
                    reason=reason,
                    command_identity=f"{command.command_identity}.reconcile"[:128],
                    idempotency_key=f"{command.idempotency_key}:reconcile",
                    max_pages=command.reconciliation_max_pages,
                )
            )
        run = await self.repository.mark_reconciliation_required(
            run.sync_run_id, reason=reason, now=self._now()
        )
        return await self._result(run)

    async def _result(self, run: DriveChangeRun) -> DriveChangeResult:
        return DriveChangeResult(
            run=run,
            pages=await self.repository.list_pages(run.sync_run_id),
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Drive change clock must be timezone-aware")
        return value


def _assert_fence(run: DriveChangeRun, command: DriveChangeCommand) -> None:
    if (
        run.workspace_id != command.workspace_id
        or run.environment_id != command.environment_id
        or run.connection_id != command.connection_id
        or run.resource_scope_reference != command.resource_scope_reference
        or run.service_principal_id != command.service_principal_id
        or run.access_policy_version_id != command.access_policy_version_id
        or run.authorization_generation != command.authorization_generation
        or run.configuration_generation != command.configuration_generation
        or run.last_committed_cursor != command.last_committed_cursor
    ):
        raise DriveChangeFenceError(
            "Drive change run is fenced by a changed authority or scope"
        )


def _sync_run_id(command: DriveChangeCommand) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "spine:drive-change-run:v1:"
        f"{command.workspace_id}:{command.environment_id}:{command.connection_id}:"
        f"{command.resource_scope_reference}:{command.idempotency_key}",
    )


def _item_command(
    command: DriveChangeCommand, item: DriveChangeItemRecord
) -> ConnectorIngestionCommand:
    digest = hashlib.sha256(item.change_key.encode("utf-8")).hexdigest()[:32]
    item_key = f"{command.idempotency_key}:change:{digest}"
    if len(item_key) > 255:
        item_key = f"drive-change-item:{hashlib.sha256(item_key.encode()).hexdigest()}"
    return ConnectorIngestionCommand(
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        command_identity=f"{command.command_identity[:25]}.c{digest}",
        idempotency_key=item_key,
        connection_id=command.connection_id,
        resource_scope_reference=command.resource_scope_reference,
        resource_object_reference=item.file_id,
        authorization_generation=command.authorization_generation,
        configuration_generation=command.configuration_generation,
        access_policy_version_id=command.access_policy_version_id,
    )


__all__ = [
    "ConnectorIngestionPort",
    "DriveChangeCommand",
    "DriveChangeCoordinator",
    "DriveChangeReader",
    "DriveChangeResult",
    "DriveReconciliationCommand",
    "DriveReconciliationPort",
]
