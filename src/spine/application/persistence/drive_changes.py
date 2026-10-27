"""Durable contracts for governed Google Drive change-feed processing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel


class DriveChangeRunState(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RESUMABLE = "resumable"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class DriveChangePageState(str, Enum):
    OPEN = "open"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


class DriveChangeItemState(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    RECONCILED = "reconciled"
    IGNORED = "ignored"
    FAILED = "failed"


class DriveChangeInput(DefinitionModel):
    """The safe, provider-neutral identity needed to process one change."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    change_key: str = Field(min_length=1, max_length=1024)
    file_id: str = Field(min_length=1, max_length=256)
    provider_version: str | None = Field(default=None, max_length=128)
    removed: bool = False


class DriveChangeRun(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sync_run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    service_principal_id: UUID
    access_policy_version_id: UUID
    authorization_generation: int = Field(gt=0)
    configuration_generation: int = Field(gt=0)
    command_identity: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)
    last_committed_cursor: str = Field(min_length=1, max_length=4096)
    proposed_cursor: str | None = Field(default=None, max_length=4096)
    next_page_token: str = Field(min_length=1, max_length=4096)
    state: DriveChangeRunState = DriveChangeRunState.RUNNING
    reconciliation_reason: str | None = Field(default=None, max_length=64)
    requested_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_scope(self) -> "DriveChangeRun":
        for name in (
            "sync_run_id",
            "workspace_id",
            "environment_id",
            "connection_id",
            "resource_scope_reference",
            "service_principal_id",
            "access_policy_version_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        for value in (self.requested_at, self.updated_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Drive change timestamps must be timezone-aware")
        if self.state is DriveChangeRunState.SUCCEEDED and not self.proposed_cursor:
            raise ValueError("successful Drive change runs require a proposed cursor")
        if (
            self.state is DriveChangeRunState.RECONCILIATION_REQUIRED
            and not self.reconciliation_reason
        ):
            raise ValueError("reconciliation-required runs require a safe reason")
        return self


class DriveChangeItemRecord(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    item_id: UUID
    page_id: UUID
    change_key: str = Field(min_length=1, max_length=1024)
    file_id: str = Field(min_length=1, max_length=256)
    provider_version: str | None = Field(default=None, max_length=128)
    removed: bool = False
    input_ordinal: int = Field(gt=0)
    state: DriveChangeItemState = DriveChangeItemState.PENDING
    outcome_revision_id: UUID | None = None
    outcome_reconciliation_id: UUID | None = None
    failure_code: str | None = Field(default=None, max_length=64)
    updated_at: datetime

    @model_validator(mode="after")
    def validate_outcome(self) -> "DriveChangeItemRecord":
        if self.outcome_revision_id is not None and self.outcome_revision_id.int == 0:
            raise ValueError("outcome_revision_id must be non-zero")
        if (
            self.outcome_reconciliation_id is not None
            and self.outcome_reconciliation_id.int == 0
        ):
            raise ValueError("outcome_reconciliation_id must be non-zero")
        if self.state is DriveChangeItemState.SUCCEEDED:
            if (
                self.outcome_revision_id is None
                or self.outcome_reconciliation_id is not None
                or self.failure_code is not None
            ):
                raise ValueError("successful items require a revision and no failure")
        if self.state is DriveChangeItemState.RECONCILED:
            if (
                self.outcome_reconciliation_id is None
                or self.outcome_revision_id is not None
                or self.failure_code is not None
            ):
                raise ValueError(
                    "reconciled items require a reconciliation and no failure"
                )
        if self.state in {DriveChangeItemState.IGNORED, DriveChangeItemState.FAILED}:
            if (
                self.failure_code is None
                or self.outcome_revision_id is not None
                or self.outcome_reconciliation_id is not None
            ):
                raise ValueError("non-processed items require a safe failure code")
        return self


class DriveChangePageRecord(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    page_id: UUID
    sync_run_id: UUID
    page_token: str = Field(min_length=1, max_length=4096)
    next_page_token: str | None = Field(default=None, max_length=4096)
    new_start_page_token: str | None = Field(default=None, max_length=4096)
    incomplete_search: bool = False
    state: DriveChangePageState = DriveChangePageState.OPEN
    items: tuple[DriveChangeItemRecord, ...] = ()
    created_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "DriveChangePageRecord":
        if self.page_id.int == 0 or self.sync_run_id.int == 0:
            raise ValueError("Drive change page IDs must be non-zero")
        for value in (self.created_at, self.completed_at):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError("Drive change page timestamps must be timezone-aware")
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("Drive change item IDs must be unique")
        return self


@dataclass(frozen=True, slots=True)
class DriveChangeRunCommand:
    sync_run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    service_principal_id: UUID
    access_policy_version_id: UUID
    authorization_generation: int
    configuration_generation: int
    command_identity: str
    idempotency_key: str
    last_committed_cursor: str
    requested_at: datetime


class DriveChangeRepository(Protocol):
    async def start_run(self, command: DriveChangeRunCommand) -> DriveChangeRun: ...

    async def resolve_run(self, sync_run_id: UUID) -> DriveChangeRun | None: ...

    async def open_page(
        self,
        *,
        sync_run_id: UUID,
        page_token: str,
        next_page_token: str | None,
        new_start_page_token: str | None,
        incomplete_search: bool,
        changes: tuple[DriveChangeInput, ...],
        now: datetime,
    ) -> DriveChangePageRecord: ...

    async def resolve_page(
        self, sync_run_id: UUID, page_token: str
    ) -> DriveChangePageRecord | None: ...

    async def list_pages(
        self, sync_run_id: UUID
    ) -> tuple[DriveChangePageRecord, ...]: ...

    async def mark_item_succeeded(
        self, item_id: UUID, *, revision_id: UUID, now: datetime
    ) -> DriveChangeItemRecord: ...

    async def mark_item_reconciled(
        self, item_id: UUID, *, reconciliation_id: UUID, now: datetime
    ) -> DriveChangeItemRecord: ...

    async def mark_item_ignored(
        self, item_id: UUID, *, reason: str, now: datetime
    ) -> DriveChangeItemRecord: ...

    async def mark_item_failed(
        self, item_id: UUID, *, failure_code: str, now: datetime
    ) -> DriveChangeItemRecord: ...

    async def complete_page(
        self, page_id: UUID, *, now: datetime
    ) -> DriveChangePageRecord: ...

    async def mark_page_incomplete(
        self, page_id: UUID, *, now: datetime
    ) -> DriveChangePageRecord: ...

    async def advance_run(
        self, sync_run_id: UUID, *, next_page_token: str, now: datetime
    ) -> DriveChangeRun: ...

    async def mark_resumable(
        self, sync_run_id: UUID, *, now: datetime
    ) -> DriveChangeRun: ...

    async def commit_run(
        self, sync_run_id: UUID, *, now: datetime
    ) -> DriveChangeRun: ...

    async def mark_reconciliation_required(
        self, sync_run_id: UUID, *, reason: str, now: datetime
    ) -> DriveChangeRun: ...


class DriveChangeSyncConflictError(RuntimeError):
    """The durable change ledger received a conflicting replay."""


class DriveChangeFenceError(RuntimeError):
    """A resumed change run no longer matches its captured authority."""


class InMemoryDriveChangeRepository:
    """Deterministic durable-ledger fake used by application and contract tests."""

    def __init__(self, *, fail_next_item_checkpoint: bool = False) -> None:
        self.runs: dict[UUID, DriveChangeRun] = {}
        self.pages: dict[UUID, DriveChangePageRecord] = {}
        self._run_keys: dict[tuple[UUID, UUID, str], UUID] = {}
        self._page_keys: dict[tuple[UUID, str], UUID] = {}
        self._item_to_page: dict[UUID, UUID] = {}
        self._fail_next_item_checkpoint = fail_next_item_checkpoint

    @classmethod
    def from_snapshot(
        cls,
        run: DriveChangeRun,
        pages: tuple[DriveChangePageRecord, ...],
    ) -> "InMemoryDriveChangeRepository":
        repository = cls()
        repository.runs[run.sync_run_id] = run
        repository._run_keys[
            (run.workspace_id, run.environment_id, run.idempotency_key)
        ] = run.sync_run_id
        for page in pages:
            repository.pages[page.page_id] = page
            repository._page_keys[(run.sync_run_id, page.page_token)] = page.page_id
            for item in page.items:
                repository._item_to_page[item.item_id] = page.page_id
        return repository

    def snapshot(
        self, sync_run_id: UUID
    ) -> tuple[DriveChangeRun, tuple[DriveChangePageRecord, ...]]:
        return (
            self.runs[sync_run_id],
            tuple(
                page for page in self.pages.values() if page.sync_run_id == sync_run_id
            ),
        )

    async def start_run(self, command: DriveChangeRunCommand) -> DriveChangeRun:
        key = (command.workspace_id, command.environment_id, command.idempotency_key)
        existing_id = self._run_keys.get(key)
        if existing_id is not None:
            existing = self.runs[existing_id]
            if any(
                (
                    existing.connection_id != command.connection_id,
                    existing.resource_scope_reference
                    != command.resource_scope_reference,
                    existing.service_principal_id != command.service_principal_id,
                    existing.authorization_generation
                    != command.authorization_generation,
                    existing.configuration_generation
                    != command.configuration_generation,
                    existing.last_committed_cursor != command.last_committed_cursor,
                )
            ):
                raise DriveChangeSyncConflictError(
                    "Drive change command conflicts with durable state"
                )
            return existing
        run = DriveChangeRun(
            sync_run_id=command.sync_run_id,
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
            next_page_token=command.last_committed_cursor,
            requested_at=command.requested_at,
            updated_at=command.requested_at,
        )
        self.runs[run.sync_run_id] = run
        self._run_keys[key] = run.sync_run_id
        return run

    async def resolve_run(self, sync_run_id: UUID) -> DriveChangeRun | None:
        return self.runs.get(sync_run_id)

    async def open_page(self, **kwargs) -> DriveChangePageRecord:
        sync_run_id = kwargs["sync_run_id"]
        page_token = kwargs["page_token"]
        key = (sync_run_id, page_token)
        existing_id = self._page_keys.get(key)
        if existing_id is not None:
            existing = self.pages[existing_id]
            if (
                existing.next_page_token != kwargs["next_page_token"]
                or existing.new_start_page_token != kwargs["new_start_page_token"]
                or existing.incomplete_search != kwargs["incomplete_search"]
                or tuple(
                    (item.change_key, item.file_id, item.provider_version, item.removed)
                    for item in existing.items
                )
                != tuple(
                    (
                        change.change_key,
                        change.file_id,
                        change.provider_version,
                        change.removed,
                    )
                    for change in kwargs["changes"]
                )
            ):
                raise DriveChangeSyncConflictError(
                    "Drive change page conflicts with durable state"
                )
            return existing
        page_id = uuid5(
            NAMESPACE_URL,
            f"spine:drive-change-page:v1:{sync_run_id}:{page_token}",
        )
        items = tuple(
            DriveChangeItemRecord(
                item_id=uuid5(
                    NAMESPACE_URL,
                    f"spine:drive-change-item:v1:{page_id}:{ordinal}:{change.change_key}",
                ),
                page_id=page_id,
                change_key=change.change_key,
                file_id=change.file_id,
                provider_version=change.provider_version,
                removed=change.removed,
                input_ordinal=ordinal,
                updated_at=kwargs["now"],
            )
            for ordinal, change in enumerate(kwargs["changes"], start=1)
        )
        page = DriveChangePageRecord(
            page_id=page_id,
            sync_run_id=sync_run_id,
            page_token=page_token,
            next_page_token=kwargs["next_page_token"],
            new_start_page_token=kwargs["new_start_page_token"],
            incomplete_search=kwargs["incomplete_search"],
            items=items,
            created_at=kwargs["now"],
        )
        self.pages[page_id] = page
        self._page_keys[key] = page_id
        for item in items:
            self._item_to_page[item.item_id] = page_id
        return page

    async def resolve_page(self, sync_run_id: UUID, page_token: str):
        page_id = self._page_keys.get((sync_run_id, page_token))
        return self.pages.get(page_id) if page_id is not None else None

    async def list_pages(self, sync_run_id: UUID):
        return tuple(
            page for page in self.pages.values() if page.sync_run_id == sync_run_id
        )

    async def mark_item_succeeded(
        self, item_id: UUID, *, revision_id: UUID, now: datetime
    ):
        if self._fail_next_item_checkpoint:
            self._fail_next_item_checkpoint = False
            raise RuntimeError("change item checkpoint acknowledgement lost")
        page = self.pages[self._item_to_page[item_id]]
        current = next(item for item in page.items if item.item_id == item_id)
        if current.state is DriveChangeItemState.SUCCEEDED:
            if current.outcome_revision_id != revision_id:
                raise DriveChangeSyncConflictError(
                    "change outcome conflicts with durable state"
                )
            return current
        updated = current.model_copy(
            update={
                "state": DriveChangeItemState.SUCCEEDED,
                "outcome_revision_id": revision_id,
                "failure_code": None,
                "updated_at": now,
            }
        )
        self._replace_item(page, updated)
        return updated

    async def mark_item_reconciled(
        self, item_id: UUID, *, reconciliation_id: UUID, now: datetime
    ) -> DriveChangeItemRecord:
        if reconciliation_id.int == 0:
            raise ValueError("reconciliation_id must be a non-zero UUID")
        page = self.pages[self._item_to_page[item_id]]
        current = next(item for item in page.items if item.item_id == item_id)
        if current.state is DriveChangeItemState.RECONCILED:
            if current.outcome_reconciliation_id != reconciliation_id:
                raise DriveChangeSyncConflictError(
                    "reconciliation outcome conflicts with durable state"
                )
            return current
        updated = current.model_copy(
            update={
                "state": DriveChangeItemState.RECONCILED,
                "outcome_revision_id": None,
                "outcome_reconciliation_id": reconciliation_id,
                "failure_code": None,
                "updated_at": now,
            }
        )
        self._replace_item(page, updated)
        return updated

    async def mark_item_ignored(self, item_id: UUID, *, reason: str, now: datetime):
        return self._mark_item_terminal(
            item_id, DriveChangeItemState.IGNORED, reason, now
        )

    async def mark_item_failed(
        self, item_id: UUID, *, failure_code: str, now: datetime
    ):
        return self._mark_item_terminal(
            item_id, DriveChangeItemState.FAILED, failure_code, now
        )

    def _mark_item_terminal(self, item_id, state, code, now):
        if not code or len(code) > 64:
            raise ValueError("change failure code must be a bounded safe identifier")
        page = self.pages[self._item_to_page[item_id]]
        current = next(item for item in page.items if item.item_id == item_id)
        if current.state is DriveChangeItemState.SUCCEEDED:
            return current
        if current.state is state and current.failure_code == code:
            return current
        updated = current.model_copy(
            update={
                "state": state,
                "outcome_revision_id": None,
                "outcome_reconciliation_id": None,
                "failure_code": code,
                "updated_at": now,
            }
        )
        self._replace_item(page, updated)
        return updated

    async def complete_page(self, page_id: UUID, *, now: datetime):
        page = self.pages[page_id]
        if page.incomplete_search or any(
            item.state
            not in {
                DriveChangeItemState.SUCCEEDED,
                DriveChangeItemState.RECONCILED,
                DriveChangeItemState.IGNORED,
            }
            for item in page.items
        ):
            raise DriveChangeSyncConflictError(
                "Drive change page is not durably complete"
            )
        updated = page.model_copy(
            update={"state": DriveChangePageState.COMPLETED, "completed_at": now}
        )
        self.pages[page_id] = updated
        return updated

    async def mark_page_incomplete(self, page_id: UUID, *, now: datetime):
        updated = self.pages[page_id].model_copy(
            update={"state": DriveChangePageState.INCOMPLETE, "completed_at": now}
        )
        self.pages[page_id] = updated
        return updated

    async def advance_run(
        self, sync_run_id: UUID, *, next_page_token: str, now: datetime
    ):
        run = self.runs[sync_run_id]
        updated = run.model_copy(
            update={
                "next_page_token": next_page_token,
                "state": DriveChangeRunState.RUNNING,
                "updated_at": now,
            }
        )
        self.runs[sync_run_id] = updated
        return updated

    async def mark_resumable(self, sync_run_id: UUID, *, now: datetime):
        updated = self.runs[sync_run_id].model_copy(
            update={"state": DriveChangeRunState.RESUMABLE, "updated_at": now}
        )
        self.runs[sync_run_id] = updated
        return updated

    async def commit_run(self, sync_run_id: UUID, *, now: datetime):
        run = self.runs[sync_run_id]
        page = await self.resolve_page(sync_run_id, run.next_page_token)
        if (
            page is None
            or page.state is not DriveChangePageState.COMPLETED
            or page.next_page_token is not None
            or not page.new_start_page_token
        ):
            raise DriveChangeSyncConflictError(
                "Drive change run cannot commit before terminal effects"
            )
        updated = run.model_copy(
            update={
                "proposed_cursor": page.new_start_page_token,
                "state": DriveChangeRunState.SUCCEEDED,
                "updated_at": now,
            }
        )
        self.runs[sync_run_id] = updated
        return updated

    async def mark_reconciliation_required(
        self, sync_run_id: UUID, *, reason: str, now: datetime
    ):
        if not reason or len(reason) > 64:
            raise ValueError("reconciliation reason must be a bounded safe identifier")
        updated = self.runs[sync_run_id].model_copy(
            update={
                "state": DriveChangeRunState.RECONCILIATION_REQUIRED,
                "reconciliation_reason": reason,
                "updated_at": now,
            }
        )
        self.runs[sync_run_id] = updated
        return updated

    def _replace_item(
        self, page: DriveChangePageRecord, replacement: DriveChangeItemRecord
    ):
        self.pages[page.page_id] = page.model_copy(
            update={
                "items": tuple(
                    replacement if item.item_id == replacement.item_id else item
                    for item in page.items
                )
            }
        )


__all__ = [
    "DriveChangeInput",
    "DriveChangeItemRecord",
    "DriveChangeItemState",
    "DriveChangePageRecord",
    "DriveChangePageState",
    "DriveChangeRepository",
    "DriveChangeRun",
    "DriveChangeRunCommand",
    "DriveChangeRunState",
    "DriveChangeSyncConflictError",
    "DriveChangeFenceError",
    "InMemoryDriveChangeRepository",
]
