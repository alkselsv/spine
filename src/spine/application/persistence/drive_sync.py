"""Durable contracts for the governed Google Drive initial snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel


class DriveSnapshotRunState(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RESUMABLE = "resumable"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    SUSPENDED = "suspended"
    POLICY_BLOCKED = "policy_blocked"


class DriveSnapshotPageState(str, Enum):
    OPEN = "open"
    COMPLETED = "completed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


class DriveSnapshotItemState(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DriveSnapshotRun(DefinitionModel):
    """Immutable identity plus mutable, explicitly versioned sync state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sync_run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    service_principal_id: UUID
    authorization_generation: int = Field(gt=0)
    configuration_generation: int = Field(gt=0)
    command_identity: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)
    proposed_start_page_token: str = Field(min_length=1, max_length=4096)
    committed_cursor: str | None = Field(default=None, max_length=4096)
    next_page_token: str | None = Field(default=None, max_length=4096)
    state: DriveSnapshotRunState = DriveSnapshotRunState.RUNNING
    suspension_reason: str | None = Field(default=None, max_length=64)
    failure_code: str | None = Field(default=None, max_length=64)
    requested_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_scope(self) -> "DriveSnapshotRun":
        for name in (
            "sync_run_id",
            "workspace_id",
            "environment_id",
            "connection_id",
            "resource_scope_reference",
            "service_principal_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        for value in (self.requested_at, self.updated_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Drive snapshot timestamps must be timezone-aware")
        return self


class DriveSnapshotItemRecord(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    item_id: UUID
    page_id: UUID
    file_id: str = Field(min_length=1, max_length=256)
    input_ordinal: int = Field(gt=0)
    state: DriveSnapshotItemState = DriveSnapshotItemState.PENDING
    outcome_revision_id: UUID | None = None
    failure_code: str | None = Field(default=None, max_length=64)
    updated_at: datetime

    @model_validator(mode="after")
    def validate_outcome(self) -> "DriveSnapshotItemRecord":
        if self.outcome_revision_id is not None and self.outcome_revision_id.int == 0:
            raise ValueError("outcome_revision_id must be non-zero")
        if self.state is DriveSnapshotItemState.SUCCEEDED:
            if self.outcome_revision_id is None or self.failure_code is not None:
                raise ValueError("successful items require a revision and no failure")
        if self.state is DriveSnapshotItemState.FAILED:
            if self.failure_code is None or self.outcome_revision_id is not None:
                raise ValueError("failed items require a safe failure code")
        return self


class DriveSnapshotPageRecord(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    page_id: UUID
    sync_run_id: UUID
    page_token: str | None = Field(default=None, max_length=4096)
    next_page_token: str | None = Field(default=None, max_length=4096)
    incomplete_search: bool
    state: DriveSnapshotPageState = DriveSnapshotPageState.OPEN
    items: tuple[DriveSnapshotItemRecord, ...] = ()
    created_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "DriveSnapshotPageRecord":
        if self.page_id.int == 0 or self.sync_run_id.int == 0:
            raise ValueError("Drive snapshot page IDs must be non-zero")
        if self.completed_at is not None and (
            self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None
        ):
            raise ValueError("completed_at must be timezone-aware")
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("Drive snapshot page item IDs must be unique")
        if len({item.file_id for item in self.items}) != len(self.items):
            raise ValueError("Drive snapshot page file IDs must be unique")
        return self


@dataclass(frozen=True, slots=True)
class DriveSnapshotRunCommand:
    sync_run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    service_principal_id: UUID
    authorization_generation: int
    configuration_generation: int
    command_identity: str
    idempotency_key: str
    proposed_start_page_token: str
    committed_cursor: str | None
    requested_at: datetime


class DriveSyncRepository(Protocol):
    async def start_run(self, command: DriveSnapshotRunCommand) -> DriveSnapshotRun: ...

    async def resolve_run(self, sync_run_id: UUID) -> DriveSnapshotRun | None: ...

    async def open_page(
        self,
        *,
        sync_run_id: UUID,
        page_token: str | None,
        next_page_token: str | None,
        incomplete_search: bool,
        file_ids: tuple[str, ...],
        now: datetime,
    ) -> DriveSnapshotPageRecord: ...

    async def resolve_page(
        self, sync_run_id: UUID, page_token: str | None
    ) -> DriveSnapshotPageRecord | None: ...

    async def list_pages(self, sync_run_id: UUID) -> tuple[DriveSnapshotPageRecord, ...]: ...

    async def mark_item_succeeded(
        self, item_id: UUID, *, revision_id: UUID, now: datetime
    ) -> DriveSnapshotItemRecord: ...

    async def mark_item_failed(
        self, item_id: UUID, *, failure_code: str, now: datetime
    ) -> DriveSnapshotItemRecord: ...

    async def complete_page(self, page_id: UUID, *, now: datetime) -> DriveSnapshotPageRecord: ...

    async def mark_page_incomplete(
        self, page_id: UUID, *, now: datetime
    ) -> DriveSnapshotPageRecord: ...

    async def advance_run(
        self, sync_run_id: UUID, *, next_page_token: str, now: datetime
    ) -> DriveSnapshotRun: ...

    async def mark_resumable(
        self,
        sync_run_id: UUID,
        *,
        now: datetime,
        failure_code: str = "provider_unavailable_retryable",
    ) -> DriveSnapshotRun: ...

    async def commit_run(self, sync_run_id: UUID, *, now: datetime) -> DriveSnapshotRun: ...

    async def mark_reconciliation_required(
        self, sync_run_id: UUID, *, now: datetime
    ) -> DriveSnapshotRun: ...

    async def mark_suspended(
        self, sync_run_id: UUID, *, now: datetime, reason: str
    ) -> DriveSnapshotRun: ...

    async def mark_policy_blocked(
        self, sync_run_id: UUID, *, now: datetime, failure_code: str
    ) -> DriveSnapshotRun: ...


class DriveSyncConflictError(RuntimeError):
    """The durable snapshot ledger received a conflicting replay."""


class InMemoryDriveSyncRepository:
    """Deterministic repository used by offline/application contract tests."""

    def __init__(self) -> None:
        self.runs: dict[UUID, DriveSnapshotRun] = {}
        self.pages: dict[UUID, DriveSnapshotPageRecord] = {}
        self._run_keys: dict[tuple[UUID, UUID, str], UUID] = {}
        self._page_keys: dict[tuple[UUID, str], UUID] = {}
        self._item_to_page: dict[UUID, UUID] = {}

    @classmethod
    def from_snapshot(
        cls,
        run: DriveSnapshotRun,
        pages: tuple[DriveSnapshotPageRecord, ...],
    ) -> "InMemoryDriveSyncRepository":
        repository = cls()
        repository.runs[run.sync_run_id] = run
        repository._run_keys[(run.workspace_id, run.environment_id, run.idempotency_key)] = run.sync_run_id
        for page in pages:
            repository.pages[page.page_id] = page
            repository._page_keys[(run.sync_run_id, page.page_token or "__initial__")] = page.page_id
            for item in page.items:
                repository._item_to_page[item.item_id] = page.page_id
        return repository

    def snapshot(
        self, sync_run_id: UUID
    ) -> tuple[DriveSnapshotRun, tuple[DriveSnapshotPageRecord, ...]]:
        return (
            self.runs[sync_run_id],
            tuple(page for page in self.pages.values() if page.sync_run_id == sync_run_id),
        )

    async def start_run(self, command: DriveSnapshotRunCommand) -> DriveSnapshotRun:
        key = (command.workspace_id, command.environment_id, command.idempotency_key)
        existing_id = self._run_keys.get(key)
        if existing_id is not None:
            existing = self.runs[existing_id]
            if (
                existing.proposed_start_page_token != command.proposed_start_page_token
                or existing.connection_id != command.connection_id
                or existing.resource_scope_reference != command.resource_scope_reference
                or existing.authorization_generation != command.authorization_generation
                or existing.configuration_generation != command.configuration_generation
            ):
                raise DriveSyncConflictError("Drive snapshot command conflicts with durable state.")
            return existing
        run = DriveSnapshotRun(
            sync_run_id=command.sync_run_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            connection_id=command.connection_id,
            resource_scope_reference=command.resource_scope_reference,
            service_principal_id=command.service_principal_id,
            authorization_generation=command.authorization_generation,
            configuration_generation=command.configuration_generation,
            command_identity=command.command_identity,
            idempotency_key=command.idempotency_key,
            proposed_start_page_token=command.proposed_start_page_token,
            committed_cursor=command.committed_cursor,
            requested_at=command.requested_at,
            updated_at=command.requested_at,
        )
        self.runs[run.sync_run_id] = run
        self._run_keys[key] = run.sync_run_id
        return run

    async def resolve_run(self, sync_run_id: UUID) -> DriveSnapshotRun | None:
        return self.runs.get(sync_run_id)

    async def open_page(
        self,
        *,
        sync_run_id: UUID,
        page_token: str | None,
        next_page_token: str | None,
        incomplete_search: bool,
        file_ids: tuple[str, ...],
        now: datetime,
    ) -> DriveSnapshotPageRecord:
        key = (sync_run_id, page_token or "__initial__")
        existing_id = self._page_keys.get(key)
        if existing_id is not None:
            existing = self.pages[existing_id]
            if (
                existing.next_page_token != next_page_token
                or existing.incomplete_search != incomplete_search
                or tuple(item.file_id for item in existing.items) != file_ids
            ):
                raise DriveSyncConflictError("Drive snapshot page conflicts with durable state.")
            return existing
        page_id = uuid5(
            NAMESPACE_URL,
            f"spine:drive-snapshot-page:v1:{sync_run_id}:{page_token or '__initial__'}",
        )
        items = tuple(
            DriveSnapshotItemRecord(
                item_id=uuid5(NAMESPACE_URL, f"spine:drive-snapshot-item:v1:{page_id}:{file_id}"),
                page_id=page_id,
                file_id=file_id,
                input_ordinal=ordinal,
                updated_at=now,
            )
            for ordinal, file_id in enumerate(file_ids, start=1)
        )
        page = DriveSnapshotPageRecord(
            page_id=page_id,
            sync_run_id=sync_run_id,
            page_token=page_token,
            next_page_token=next_page_token,
            incomplete_search=incomplete_search,
            items=items,
            created_at=now,
        )
        self.pages[page_id] = page
        self._page_keys[key] = page_id
        for item in items:
            self._item_to_page[item.item_id] = page_id
        return page

    async def resolve_page(
        self, sync_run_id: UUID, page_token: str | None
    ) -> DriveSnapshotPageRecord | None:
        page_id = self._page_keys.get((sync_run_id, page_token or "__initial__"))
        return self.pages.get(page_id) if page_id is not None else None

    async def list_pages(self, sync_run_id: UUID) -> tuple[DriveSnapshotPageRecord, ...]:
        return tuple(
            page
            for page in self.pages.values()
            if page.sync_run_id == sync_run_id
        )

    async def mark_item_succeeded(
        self, item_id: UUID, *, revision_id: UUID, now: datetime
    ) -> DriveSnapshotItemRecord:
        page = self.pages[self._item_to_page[item_id]]
        current = next(item for item in page.items if item.item_id == item_id)
        if current.state is DriveSnapshotItemState.SUCCEEDED:
            if current.outcome_revision_id != revision_id:
                raise DriveSyncConflictError("Drive snapshot item outcome conflicts with durable state.")
            return current
        updated = current.model_copy(
            update={
                "state": DriveSnapshotItemState.SUCCEEDED,
                "outcome_revision_id": revision_id,
                "failure_code": None,
                "updated_at": now,
            }
        )
        self._replace_item(page, updated)
        return updated

    async def mark_item_failed(
        self, item_id: UUID, *, failure_code: str, now: datetime
    ) -> DriveSnapshotItemRecord:
        if not failure_code or len(failure_code) > 64:
            raise ValueError("failure_code must be a bounded safe identifier")
        page = self.pages[self._item_to_page[item_id]]
        current = next(item for item in page.items if item.item_id == item_id)
        if current.state is DriveSnapshotItemState.SUCCEEDED:
            return current
        updated = current.model_copy(
            update={
                "state": DriveSnapshotItemState.FAILED,
                "outcome_revision_id": None,
                "failure_code": failure_code,
                "updated_at": now,
            }
        )
        self._replace_item(page, updated)
        return updated

    async def complete_page(self, page_id: UUID, *, now: datetime) -> DriveSnapshotPageRecord:
        page = self.pages[page_id]
        if page.incomplete_search or any(
            item.state is not DriveSnapshotItemState.SUCCEEDED for item in page.items
        ):
            raise DriveSyncConflictError("Drive snapshot page is not durably complete.")
        updated = page.model_copy(
            update={"state": DriveSnapshotPageState.COMPLETED, "completed_at": now}
        )
        self.pages[page_id] = updated
        return updated

    async def mark_page_incomplete(
        self, page_id: UUID, *, now: datetime
    ) -> DriveSnapshotPageRecord:
        page = self.pages[page_id]
        updated = page.model_copy(
            update={"state": DriveSnapshotPageState.INCOMPLETE, "completed_at": now}
        )
        self.pages[page_id] = updated
        return updated

    async def advance_run(
        self, sync_run_id: UUID, *, next_page_token: str, now: datetime
    ) -> DriveSnapshotRun:
        run = self.runs[sync_run_id]
        updated = run.model_copy(
            update={
                "next_page_token": next_page_token,
                "state": DriveSnapshotRunState.RUNNING,
                "updated_at": now,
            }
        )
        self.runs[sync_run_id] = updated
        return updated

    async def mark_resumable(
        self,
        sync_run_id: UUID,
        *,
        now: datetime,
        failure_code: str = "provider_unavailable_retryable",
    ) -> DriveSnapshotRun:
        run = self.runs[sync_run_id]
        updated = run.model_copy(
            update={
                "state": DriveSnapshotRunState.RESUMABLE,
                "failure_code": failure_code,
                "updated_at": now,
            }
        )
        self.runs[sync_run_id] = updated
        return updated

    async def commit_run(self, sync_run_id: UUID, *, now: datetime) -> DriveSnapshotRun:
        run = self.runs[sync_run_id]
        page = await self.resolve_page(sync_run_id, run.next_page_token)
        if page is None or page.state is not DriveSnapshotPageState.COMPLETED or page.next_page_token is not None:
            raise DriveSyncConflictError("Drive snapshot cannot commit before the final page is complete.")
        updated = run.model_copy(
            update={
                "committed_cursor": run.proposed_start_page_token,
                "state": DriveSnapshotRunState.SUCCEEDED,
                "updated_at": now,
            }
        )
        self.runs[sync_run_id] = updated
        return updated

    async def mark_reconciliation_required(
        self, sync_run_id: UUID, *, now: datetime
    ) -> DriveSnapshotRun:
        run = self.runs[sync_run_id]
        updated = run.model_copy(
            update={"state": DriveSnapshotRunState.RECONCILIATION_REQUIRED, "updated_at": now}
        )
        self.runs[sync_run_id] = updated
        return updated

    async def mark_suspended(
        self, sync_run_id: UUID, *, now: datetime, reason: str
    ) -> DriveSnapshotRun:
        updated = self.runs[sync_run_id].model_copy(
            update={
                "state": DriveSnapshotRunState.SUSPENDED,
                "suspension_reason": reason,
                "updated_at": now,
            }
        )
        self.runs[sync_run_id] = updated
        return updated

    async def mark_policy_blocked(
        self, sync_run_id: UUID, *, now: datetime, failure_code: str
    ) -> DriveSnapshotRun:
        updated = self.runs[sync_run_id].model_copy(
            update={
                "state": DriveSnapshotRunState.POLICY_BLOCKED,
                "failure_code": failure_code,
                "updated_at": now,
            }
        )
        self.runs[sync_run_id] = updated
        return updated

    def _replace_item(self, page: DriveSnapshotPageRecord, replacement: DriveSnapshotItemRecord) -> None:
        self.pages[page.page_id] = page.model_copy(
            update={
                "items": tuple(
                    replacement if item.item_id == replacement.item_id else item
                    for item in page.items
                )
            }
        )


__all__ = [
    "DriveSnapshotItemRecord",
    "DriveSnapshotItemState",
    "DriveSnapshotPageRecord",
    "DriveSnapshotPageState",
    "DriveSnapshotRun",
    "DriveSnapshotRunCommand",
    "DriveSnapshotRunState",
    "DriveSyncConflictError",
    "DriveSyncRepository",
    "InMemoryDriveSyncRepository",
]
