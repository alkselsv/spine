"""Durable contracts for bounded Google Sheets full reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel
from spine.application.google_sync_diagnostics import GOOGLE_PROVIDER_RETRY_LIMIT


class GoogleSheetsSyncRunState(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RESUMABLE = "resumable"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    SUSPENDED = "suspended"
    POLICY_BLOCKED = "policy_blocked"


class GoogleSheetsSyncPageState(str, Enum):
    OPEN = "open"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


class GoogleSheetsSyncItemState(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"


class GoogleSheetsIdentityDisposition(str, Enum):
    QUARANTINED = "quarantined"
    CLEARED = "cleared"


class GoogleSheetsIdentityFailureCode(str, Enum):
    MISSING_BUSINESS_ID = "missing_business_id"
    DUPLICATE_BUSINESS_ID = "duplicate_business_id"
    CONFLICTING_BUSINESS_ID = "conflicting_business_id"
    DEVELOPER_METADATA_MISMATCH = "developer_metadata_mismatch"
    INVALID_IDENTITY = "invalid_identity"
    SCOPE_DUPLICATE_BUSINESS_ID = "scope_duplicate_business_id"


class GoogleSheetsIdentityQuarantine(DefinitionModel):
    """Content-free tenant-scoped identity disposition audit event."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_id: UUID
    sync_run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    scope_identity: str = Field(min_length=1, max_length=1024)
    disposition: GoogleSheetsIdentityDisposition
    failure_code: GoogleSheetsIdentityFailureCode | None = None
    occurred_at: datetime

    @model_validator(mode="after")
    def validate_event(self) -> "GoogleSheetsIdentityQuarantine":
        if self.event_id.int == 0 or self.sync_run_id.int == 0:
            raise ValueError("Google Sheets identity disposition IDs must be non-zero")
        if any(
            value.int == 0
            for value in (self.workspace_id, self.environment_id, self.connection_id)
        ):
            raise ValueError("Google Sheets identity disposition scope must be non-zero")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("Google Sheets identity disposition time must be aware")
        if (self.disposition is GoogleSheetsIdentityDisposition.QUARANTINED) != (
            self.failure_code is not None
        ):
            raise ValueError("quarantined dispositions require a safe failure code")
        return self


@dataclass(frozen=True, slots=True)
class GoogleSheetsIdentityDispositionCommand:
    sync_run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    scope_identity: str
    disposition: GoogleSheetsIdentityDisposition
    failure_code: GoogleSheetsIdentityFailureCode | None
    now: datetime


def identity_disposition_event(
    command: GoogleSheetsIdentityDispositionCommand,
) -> GoogleSheetsIdentityQuarantine:
    """Build one replay-stable event shared by every ledger adapter."""

    return GoogleSheetsIdentityQuarantine(
        event_id=uuid5(
            NAMESPACE_URL,
            "spine:google-sheets-sync:v1:identity-disposition:"
            f"{command.sync_run_id}:{command.scope_identity}",
        ),
        sync_run_id=command.sync_run_id,
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        connection_id=command.connection_id,
        scope_identity=command.scope_identity,
        disposition=command.disposition,
        failure_code=command.failure_code,
        occurred_at=command.now,
    )


class GoogleSheetsSyncRun(DefinitionModel):
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
    scope_set_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    command_identity: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)
    prior_committed_cursor: str | None = Field(default=None, max_length=4096)
    proposed_cursor: str | None = Field(default=None, max_length=4096)
    state: GoogleSheetsSyncRunState = GoogleSheetsSyncRunState.RUNNING
    failure_code: str | None = Field(default=None, max_length=64)
    provider_retry_attempts: int = Field(
        default=0, ge=0, le=GOOGLE_PROVIDER_RETRY_LIMIT
    )
    requested_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_run(self) -> "GoogleSheetsSyncRun":
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
                raise ValueError("Google Sheets sync timestamps must be timezone-aware")
        if (
            self.state is GoogleSheetsSyncRunState.SUCCEEDED
            and self.failure_code is not None
        ):
            raise ValueError("successful Google Sheets runs cannot have a failure code")
        return self


class GoogleSheetsSyncItem(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    item_id: UUID
    page_id: UUID
    identity: str = Field(min_length=1, max_length=1024)
    input_ordinal: int = Field(gt=0)
    state: GoogleSheetsSyncItemState = GoogleSheetsSyncItemState.PENDING
    revision_id: UUID | None = None
    updated_at: datetime

    @model_validator(mode="after")
    def validate_item(self) -> "GoogleSheetsSyncItem":
        if self.item_id.int == 0 or self.page_id.int == 0:
            raise ValueError("Google Sheets sync item IDs must be non-zero")
        if (
            self.state is GoogleSheetsSyncItemState.SUCCEEDED
            and self.revision_id is None
        ):
            raise ValueError("successful Google Sheets items require a revision")
        if self.revision_id is not None and self.revision_id.int == 0:
            raise ValueError("revision_id must be non-zero")
        return self


class GoogleSheetsSyncPage(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    page_id: UUID
    sync_run_id: UUID
    scope_identity: str = Field(min_length=1, max_length=1024)
    page_token: str | None = Field(default=None, max_length=4096)
    next_page_token: str | None = Field(default=None, max_length=4096)
    provider_version: str = Field(min_length=1, max_length=128)
    state: GoogleSheetsSyncPageState = GoogleSheetsSyncPageState.OPEN
    items: tuple[GoogleSheetsSyncItem, ...] = ()
    created_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_page(self) -> "GoogleSheetsSyncPage":
        if self.page_id.int == 0 or self.sync_run_id.int == 0:
            raise ValueError("Google Sheets sync page IDs must be non-zero")
        for value in (self.created_at, self.completed_at):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError(
                    "Google Sheets sync page timestamps must be timezone-aware"
                )
        if len({item.identity for item in self.items}) != len(self.items):
            raise ValueError("Google Sheets page identities must be unique")
        return self


@dataclass(frozen=True, slots=True)
class GoogleSheetsSyncRunCommand:
    sync_run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    service_principal_id: UUID
    access_policy_version_id: UUID
    authorization_generation: int
    configuration_generation: int
    scope_set_digest: str
    command_identity: str
    idempotency_key: str
    prior_committed_cursor: str | None
    proposed_cursor: str | None
    requested_at: datetime


class GoogleSheetsSyncRepository(Protocol):
    async def start_run(
        self, command: GoogleSheetsSyncRunCommand
    ) -> GoogleSheetsSyncRun: ...
    async def resolve_run(self, sync_run_id: UUID) -> GoogleSheetsSyncRun | None: ...
    async def open_page(
        self,
        *,
        sync_run_id: UUID,
        scope_identity: str,
        page_token: str | None,
        next_page_token: str | None,
        provider_version: str,
        identities: tuple[str, ...],
        now: datetime,
    ) -> GoogleSheetsSyncPage: ...
    async def list_pages(
        self, sync_run_id: UUID
    ) -> tuple[GoogleSheetsSyncPage, ...]: ...
    async def mark_item_succeeded(
        self, item_id: UUID, *, revision_id: UUID, now: datetime
    ) -> GoogleSheetsSyncItem: ...
    async def complete_page(
        self, page_id: UUID, *, now: datetime
    ) -> GoogleSheetsSyncPage: ...
    async def mark_page_incomplete(
        self, page_id: UUID, *, now: datetime
    ) -> GoogleSheetsSyncPage: ...
    async def mark_run(
        self,
        sync_run_id: UUID,
        *,
        state: GoogleSheetsSyncRunState,
        failure_code: str | None,
        now: datetime,
    ) -> GoogleSheetsSyncRun: ...
    async def record_provider_failure(
        self, sync_run_id: UUID, *, failure_code: str, now: datetime
    ) -> GoogleSheetsSyncRun: ...
    async def record_identity_disposition(
        self, command: GoogleSheetsIdentityDispositionCommand
    ) -> GoogleSheetsIdentityQuarantine: ...
    async def list_identity_dispositions(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        connection_id: UUID,
        scope_identities: tuple[str, ...],
    ) -> tuple[GoogleSheetsIdentityQuarantine, ...]: ...


class GoogleSheetsSyncConflictError(RuntimeError):
    """A duplicate Sheets command or page conflicts with its durable record."""


class GoogleSheetsSyncFenceError(RuntimeError):
    """A resumed Sheets run no longer matches its captured authority or scope."""


class InMemoryGoogleSheetsSyncRepository:
    """Deterministic durable-ledger fake used by application contract tests."""

    def __init__(self) -> None:
        self.runs: dict[UUID, GoogleSheetsSyncRun] = {}
        self.pages: dict[UUID, GoogleSheetsSyncPage] = {}
        self._run_keys: dict[tuple[UUID, UUID, str], UUID] = {}
        self._page_keys: dict[tuple[UUID, str, str], UUID] = {}
        self._item_ids: dict[UUID, tuple[UUID, UUID]] = {}
        self.identity_dispositions: dict[tuple[UUID, str], GoogleSheetsIdentityQuarantine] = {}

    @classmethod
    def from_snapshot(
        cls, run: GoogleSheetsSyncRun, pages: tuple[GoogleSheetsSyncPage, ...]
    ) -> "InMemoryGoogleSheetsSyncRepository":
        repository = cls()
        repository.runs[run.sync_run_id] = run
        repository._run_keys[
            (run.workspace_id, run.environment_id, run.idempotency_key)
        ] = run.sync_run_id
        for page in pages:
            repository.pages[page.page_id] = page
            repository._page_keys[
                (run.sync_run_id, page.scope_identity, page.page_token or "__initial__")
            ] = page.page_id
            for item in page.items:
                repository._item_ids[item.item_id] = (run.sync_run_id, page.page_id)
        return repository

    def snapshot(
        self, sync_run_id: UUID
    ) -> tuple[GoogleSheetsSyncRun, tuple[GoogleSheetsSyncPage, ...]]:
        return self.runs[sync_run_id], tuple(
            page for page in self.pages.values() if page.sync_run_id == sync_run_id
        )

    async def start_run(
        self, command: GoogleSheetsSyncRunCommand
    ) -> GoogleSheetsSyncRun:
        key = (command.workspace_id, command.environment_id, command.idempotency_key)
        existing_id = self._run_keys.get(key)
        if existing_id is not None:
            existing = self.runs[existing_id]
            if any(
                getattr(existing, name) != getattr(command, name)
                for name in (
                    "sync_run_id",
                    "connection_id",
                    "resource_scope_reference",
                    "service_principal_id",
                    "access_policy_version_id",
                    "authorization_generation",
                    "configuration_generation",
                    "scope_set_digest",
                    "prior_committed_cursor",
                    "proposed_cursor",
                )
            ):
                raise GoogleSheetsSyncConflictError(
                    "Google Sheets sync command conflicts with durable state"
                )
            return existing
        run = GoogleSheetsSyncRun(
            sync_run_id=command.sync_run_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            connection_id=command.connection_id,
            resource_scope_reference=command.resource_scope_reference,
            service_principal_id=command.service_principal_id,
            access_policy_version_id=command.access_policy_version_id,
            authorization_generation=command.authorization_generation,
            configuration_generation=command.configuration_generation,
            scope_set_digest=command.scope_set_digest,
            command_identity=command.command_identity,
            idempotency_key=command.idempotency_key,
            prior_committed_cursor=command.prior_committed_cursor,
            proposed_cursor=command.proposed_cursor,
            requested_at=command.requested_at,
            updated_at=command.requested_at,
        )
        self.runs[run.sync_run_id] = run
        self._run_keys[key] = run.sync_run_id
        return run

    async def resolve_run(self, sync_run_id: UUID) -> GoogleSheetsSyncRun | None:
        return self.runs.get(sync_run_id)

    async def open_page(
        self,
        *,
        sync_run_id: UUID,
        scope_identity: str,
        page_token: str | None,
        next_page_token: str | None,
        provider_version: str,
        identities: tuple[str, ...],
        now: datetime,
    ) -> GoogleSheetsSyncPage:
        token_key = page_token or "__initial__"
        key = (sync_run_id, scope_identity, token_key)
        existing_id = self._page_keys.get(key)
        if existing_id is not None:
            existing = self.pages[existing_id]
            if (
                existing.next_page_token,
                existing.provider_version,
                tuple(i.identity for i in existing.items),
            ) != (next_page_token, provider_version, identities):
                raise GoogleSheetsSyncConflictError(
                    "Google Sheets page conflicts with durable state"
                )
            return existing
        page_id = _stable_id(f"page:{sync_run_id}:{scope_identity}:{token_key}")
        items = tuple(
            GoogleSheetsSyncItem(
                item_id=_stable_id(f"item:{page_id}:{identity}"),
                page_id=page_id,
                identity=identity,
                input_ordinal=index,
                updated_at=now,
            )
            for index, identity in enumerate(identities, 1)
        )
        page = GoogleSheetsSyncPage(
            page_id=page_id,
            sync_run_id=sync_run_id,
            scope_identity=scope_identity,
            page_token=page_token,
            next_page_token=next_page_token,
            provider_version=provider_version,
            items=items,
            created_at=now,
        )
        self.pages[page_id] = page
        self._page_keys[key] = page_id
        for item in items:
            self._item_ids[item.item_id] = (sync_run_id, page_id)
        return page

    async def list_pages(self, sync_run_id: UUID) -> tuple[GoogleSheetsSyncPage, ...]:
        return tuple(
            sorted(
                (p for p in self.pages.values() if p.sync_run_id == sync_run_id),
                key=lambda p: (p.scope_identity, p.page_token or ""),
            )
        )

    async def mark_item_succeeded(
        self, item_id: UUID, *, revision_id: UUID, now: datetime
    ) -> GoogleSheetsSyncItem:
        _, page_id = self._item_ids[item_id]
        page = self.pages[page_id]
        item = next(i for i in page.items if i.item_id == item_id)
        updated = item.model_copy(
            update={
                "state": GoogleSheetsSyncItemState.SUCCEEDED,
                "revision_id": revision_id,
                "updated_at": now,
            }
        )
        self.pages[page_id] = page.model_copy(
            update={
                "items": tuple(
                    updated if i.item_id == item_id else i for i in page.items
                )
            }
        )
        return updated

    async def complete_page(
        self, page_id: UUID, *, now: datetime
    ) -> GoogleSheetsSyncPage:
        page = self.pages[page_id]
        if any(
            item.state is not GoogleSheetsSyncItemState.SUCCEEDED for item in page.items
        ):
            raise GoogleSheetsSyncConflictError(
                "cannot complete a page with pending rows"
            )
        updated = page.model_copy(
            update={"state": GoogleSheetsSyncPageState.COMPLETED, "completed_at": now}
        )
        self.pages[page_id] = updated
        return updated

    async def mark_page_incomplete(
        self, page_id: UUID, *, now: datetime
    ) -> GoogleSheetsSyncPage:
        page = self.pages[page_id]
        updated = page.model_copy(
            update={"state": GoogleSheetsSyncPageState.INCOMPLETE, "completed_at": now}
        )
        self.pages[page_id] = updated
        return updated

    async def mark_run(
        self,
        sync_run_id: UUID,
        *,
        state: GoogleSheetsSyncRunState,
        failure_code: str | None,
        now: datetime,
    ) -> GoogleSheetsSyncRun:
        run = self.runs[sync_run_id]
        updated = run.model_copy(
            update={"state": state, "failure_code": failure_code, "updated_at": now}
        )
        self.runs[sync_run_id] = updated
        return updated

    async def record_provider_failure(
        self, sync_run_id: UUID, *, failure_code: str, now: datetime
    ) -> GoogleSheetsSyncRun:
        run = self.runs[sync_run_id]
        attempts = run.provider_retry_attempts + 1
        exhausted = attempts >= GOOGLE_PROVIDER_RETRY_LIMIT
        updated = run.model_copy(
            update={
                "provider_retry_attempts": attempts,
                "state": (
                    GoogleSheetsSyncRunState.RECONCILIATION_REQUIRED
                    if exhausted
                    else GoogleSheetsSyncRunState.RESUMABLE
                ),
                "failure_code": "provider_retry_exhausted" if exhausted else failure_code,
                "updated_at": now,
            }
        )
        self.runs[sync_run_id] = updated
        return updated

    async def record_identity_disposition(
        self, command: GoogleSheetsIdentityDispositionCommand
    ) -> GoogleSheetsIdentityQuarantine:
        key = (command.sync_run_id, command.scope_identity)
        existing = self.identity_dispositions.get(key)
        if existing is not None:
            if (existing.disposition, existing.failure_code) != (
                command.disposition,
                command.failure_code,
            ):
                raise GoogleSheetsSyncConflictError(
                    "Google Sheets identity disposition conflicts with durable state"
                )
            return existing
        event = identity_disposition_event(command)
        self.identity_dispositions[key] = event
        return event

    async def list_identity_dispositions(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        connection_id: UUID,
        scope_identities: tuple[str, ...],
    ) -> tuple[GoogleSheetsIdentityQuarantine, ...]:
        requested = set(scope_identities)
        return tuple(
            sorted(
                (
                    event
                    for event in self.identity_dispositions.values()
                    if event.workspace_id == workspace_id
                    and event.environment_id == environment_id
                    and event.connection_id == connection_id
                    and event.scope_identity in requested
                ),
                key=lambda event: (event.occurred_at, event.event_id.int),
            )
        )


def _stable_id(value: str) -> UUID:
    from uuid import NAMESPACE_URL, uuid5

    return uuid5(NAMESPACE_URL, f"spine:google-sheets-sync:v1:{value}")


__all__ = [
    "GoogleSheetsSyncConflictError",
    "GoogleSheetsSyncFenceError",
    "GoogleSheetsSyncItem",
    "GoogleSheetsSyncItemState",
    "GoogleSheetsSyncPage",
    "GoogleSheetsSyncPageState",
    "GoogleSheetsSyncRepository",
    "GoogleSheetsSyncRun",
    "GoogleSheetsSyncRunCommand",
    "GoogleSheetsSyncRunState",
    "InMemoryGoogleSheetsSyncRepository",
]
