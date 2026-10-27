"""Durable orchestration for a governed Google Drive initial enumeration."""

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
    GoogleProviderAuthorizationError,
    GoogleProviderPolicyError,
    GoogleProviderQuotaError,
)
from spine.application.google_sync_diagnostics import GoogleSyncDiagnosticSnapshot
from spine.application.persistence.context import PersistenceOperation, TrustedPersistenceContext
from spine.application.persistence.drive_sync import (
    DriveSnapshotPageRecord,
    DriveSnapshotRun,
    DriveSnapshotRunCommand,
    DriveSnapshotRunState,
    DriveSnapshotItemState,
    DriveSnapshotPageState,
    DriveSyncRepository,
)
from spine.connectors.google_drive import DriveSnapshotPage


class DriveInitialSnapshotCommand(BaseModel):
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

    @model_validator(mode="after")
    def validate_ids(self) -> "DriveInitialSnapshotCommand":
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


class DriveInitialSnapshotReader(Protocol):
    async def get_start_page_token(
        self, command: DriveInitialSnapshotCommand, context: TrustedPersistenceContext
    ) -> str: ...

    async def list_page(
        self,
        command: DriveInitialSnapshotCommand,
        page_token: str | None,
        context: TrustedPersistenceContext,
    ) -> DriveSnapshotPage: ...


class ConnectorIngestionPort(Protocol):
    async def ingest(self, command: ConnectorIngestionCommand) -> ConnectorIngestionResult: ...


@dataclass(frozen=True, slots=True)
class DriveInitialSnapshotResult:
    run: DriveSnapshotRun
    pages: tuple[DriveSnapshotPageRecord, ...]

    @property
    def diagnostic_snapshot(self) -> GoogleSyncDiagnosticSnapshot:
        return GoogleSyncDiagnosticSnapshot.from_run(self.run, self.pages)

    @property
    def state(self) -> DriveSnapshotRunState:
        return self.run.state

    @property
    def proposed_start_page_token(self) -> str:
        return self.run.proposed_start_page_token

    @property
    def committed_cursor(self) -> str | None:
        return self.run.committed_cursor


@dataclass(frozen=True, slots=True)
class DriveInitialSnapshotCoordinator:
    repository: DriveSyncRepository
    reader: DriveInitialSnapshotReader
    ingestion: ConnectorIngestionPort
    context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext]
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def run(self, command: DriveInitialSnapshotCommand) -> DriveInitialSnapshotResult:
        sync_run_id = _sync_run_id(command)
        run = await self.repository.resolve_run(sync_run_id)
        if run is None:
            start_token_is_suspended = False
            start_token_is_policy_blocked = False
            start_token_failure_code: str | None = None
            try:
                start_token = await self.reader.get_start_page_token(
                    command,
                    self.context_factory(
                        PersistenceOperation("google_drive_snapshot_read"),
                        uuid5(NAMESPACE_URL, f"{sync_run_id}:start-token"),
                    ),
                )
            except GoogleProviderAuthorizationError:
                start_token_is_suspended = True
                start_token = "suspended"
            except GoogleProviderPolicyError:
                start_token_is_policy_blocked = True
                start_token = "policy_blocked"
            except GoogleProviderQuotaError:
                start_token_failure_code = "provider_quota_retryable"
                start_token = "provider_unavailable"
            except Exception:
                start_token_failure_code = "provider_unavailable_retryable"
                start_token = "provider_unavailable"
            run = await self.repository.start_run(
                DriveSnapshotRunCommand(
                    sync_run_id=sync_run_id,
                    workspace_id=command.workspace_id,
                    environment_id=command.environment_id,
                    connection_id=command.connection_id,
                    resource_scope_reference=command.resource_scope_reference,
                    service_principal_id=command.service_principal_id,
                    authorization_generation=command.authorization_generation,
                    configuration_generation=command.configuration_generation,
                    command_identity=command.command_identity,
                    idempotency_key=command.idempotency_key,
                    proposed_start_page_token=start_token,
                    committed_cursor=command.last_committed_cursor,
                    requested_at=self._now(),
                )
            )
            if start_token_is_suspended:
                run = await self.repository.mark_suspended(
                    sync_run_id, reason="authorization_revoked", now=self._now()
                )
                return await self._result(run)
            if start_token_is_policy_blocked:
                run = await self.repository.mark_policy_blocked(
                    sync_run_id,
                    failure_code="provider_policy_denied",
                    now=self._now(),
                )
                return await self._result(run)
            if start_token_failure_code is not None:
                run = await self.repository.mark_resumable(
                    sync_run_id,
                    failure_code=start_token_failure_code,
                    now=self._now(),
                )
                return await self._result(run)

        if run.state in {
            DriveSnapshotRunState.SUCCEEDED,
            DriveSnapshotRunState.RECONCILIATION_REQUIRED,
            DriveSnapshotRunState.SUSPENDED,
            DriveSnapshotRunState.POLICY_BLOCKED,
        }:
            return await self._result(run)

        while True:
            page_token = run.next_page_token
            existing_page = await self.repository.resolve_page(sync_run_id, page_token)
            if existing_page is not None and existing_page.state is DriveSnapshotPageState.COMPLETED:
                if existing_page.next_page_token is None:
                    run = await self.repository.commit_run(sync_run_id, now=self._now())
                    return await self._result(run)
                run = await self.repository.advance_run(
                    sync_run_id,
                    next_page_token=existing_page.next_page_token,
                    now=self._now(),
                )
                continue
            if existing_page is not None and existing_page.state is DriveSnapshotPageState.INCOMPLETE:
                run = await self.repository.mark_reconciliation_required(
                    sync_run_id, now=self._now()
                )
                return await self._result(run)

            try:
                page = await self.reader.list_page(
                    command,
                    page_token,
                    self.context_factory(
                        PersistenceOperation("google_drive_snapshot_list"),
                        uuid5(NAMESPACE_URL, f"{sync_run_id}:page:{page_token or '__initial__'}"),
                    ),
                )
                if page.page_token != page_token:
                    raise ValueError("Drive page token does not match the requested checkpoint")
                stored_page = await self.repository.open_page(
                    sync_run_id=sync_run_id,
                    page_token=page.page_token,
                    next_page_token=page.next_page_token,
                    incomplete_search=page.incomplete_search,
                    file_ids=tuple(item.file_id for item in page.items),
                    now=self._now(),
                )
            except GoogleProviderAuthorizationError:
                run = await self.repository.mark_suspended(
                    sync_run_id, reason="authorization_revoked", now=self._now()
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
                run = await self.repository.mark_resumable(
                    sync_run_id,
                    failure_code="provider_quota_retryable",
                    now=self._now(),
                )
                return await self._result(run)
            except Exception:
                run = await self.repository.mark_resumable(sync_run_id, now=self._now())
                return await self._result(run)

            for stored_item in stored_page.items:
                if stored_item.state is DriveSnapshotItemState.SUCCEEDED:
                    continue
                item_command = _item_command(command, stored_item.file_id)
                try:
                    result = await self.ingestion.ingest(item_command)
                    await self.repository.mark_item_succeeded(
                        stored_item.item_id,
                        revision_id=result.revision.revision_id,
                        now=self._now(),
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
                await self.repository.mark_page_incomplete(stored_page.page_id, now=self._now())
                run = await self.repository.mark_reconciliation_required(
                    sync_run_id, now=self._now()
                )
                return await self._result(run)

            await self.repository.complete_page(stored_page.page_id, now=self._now())
            if page.next_page_token is None:
                run = await self.repository.commit_run(sync_run_id, now=self._now())
                return await self._result(run)
            run = await self.repository.advance_run(
                sync_run_id, next_page_token=page.next_page_token, now=self._now()
            )

    async def _result(self, run: DriveSnapshotRun) -> DriveInitialSnapshotResult:
        return DriveInitialSnapshotResult(run=run, pages=await self.repository.list_pages(run.sync_run_id))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Drive snapshot clock must be timezone-aware")
        return value


def _sync_run_id(command: DriveInitialSnapshotCommand) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "spine:drive-initial-snapshot:v1:"
        f"{command.workspace_id}:{command.environment_id}:{command.connection_id}:"
        f"{command.resource_scope_reference}:{command.idempotency_key}",
    )


def _item_command(
    command: DriveInitialSnapshotCommand, file_id: str
) -> ConnectorIngestionCommand:
    digest = hashlib.sha256(file_id.encode("utf-8")).hexdigest()[:32]
    item_key = f"{command.idempotency_key}:item:{digest}"
    if len(item_key) > 255:
        item_key = f"drive-snapshot-item:{hashlib.sha256(item_key.encode()).hexdigest()}"
    return ConnectorIngestionCommand(
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        command_identity=f"{command.command_identity[:45]}.i{digest}",
        idempotency_key=item_key,
        connection_id=command.connection_id,
        resource_scope_reference=command.resource_scope_reference,
        resource_object_reference=file_id,
        authorization_generation=command.authorization_generation,
        configuration_generation=command.configuration_generation,
        access_policy_version_id=command.access_policy_version_id,
    )


__all__ = [
    "ConnectorIngestionPort",
    "DriveInitialSnapshotCommand",
    "DriveInitialSnapshotCoordinator",
    "DriveInitialSnapshotReader",
    "DriveInitialSnapshotResult",
]
