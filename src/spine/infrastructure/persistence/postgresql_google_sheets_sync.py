"""PostgreSQL adapter for the tenant-scoped Google Sheets sync ledger."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, update

from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.persistence.google_sheets_sync import (
    GoogleSheetsSyncItem,
    GoogleSheetsSyncPage,
    GoogleSheetsSyncRepository,
    GoogleSheetsSyncRun,
    GoogleSheetsSyncRunCommand,
    GoogleSheetsSyncRunState,
    InMemoryGoogleSheetsSyncRepository,
    GoogleSheetsIdentityDisposition,
    GoogleSheetsIdentityDispositionCommand,
    GoogleSheetsIdentityFailureCode,
    GoogleSheetsIdentityQuarantine,
    identity_disposition_event,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    google_sheets_identity_dispositions,
    google_sheets_sync_runs,
)


class PostgreSQLGoogleSheetsSyncRepository(GoogleSheetsSyncRepository):
    """Persist each immutable run/page/item envelope through the tenant UoW."""

    def __init__(self, uow_factory: Callable, context_factory: Callable) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def start_run(
        self, command: GoogleSheetsSyncRunCommand
    ) -> GoogleSheetsSyncRun:
        async with self._uow("google_sheets_sync_run", command.sync_run_id) as uow:
            result = await uow._execute(
                select(google_sheets_sync_runs).where(
                    google_sheets_sync_runs.c.workspace_id == command.workspace_id,
                    google_sheets_sync_runs.c.environment_id == command.environment_id,
                    google_sheets_sync_runs.c.idempotency_key
                    == command.idempotency_key,
                )
            )
            row = result.mappings().one_or_none()
            if row is not None:
                repository = _repository_from_row(row)
                existing = await repository.start_run(command)
                await uow.rollback()
                return existing
            repository = InMemoryGoogleSheetsSyncRepository()
            run = await repository.start_run(command)
            await uow._execute(
                insert(google_sheets_sync_runs).values(**_columns(run, ()))
            )
            await uow.commit()
            return run

    async def resolve_run(self, sync_run_id: UUID) -> GoogleSheetsSyncRun | None:
        async with self._uow("google_sheets_sync_read", sync_run_id) as uow:
            row = await _load(uow, sync_run_id)
            await uow.rollback()
            return None if row is None else _run_from_row(row)

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
        kwargs = {
            "sync_run_id": sync_run_id,
            "scope_identity": scope_identity,
            "page_token": page_token,
            "next_page_token": next_page_token,
            "provider_version": provider_version,
            "identities": identities,
            "now": now,
        }
        return await self._mutate(
            "google_sheets_sync_page", kwargs["sync_run_id"], "open_page", kwargs
        )

    async def list_pages(self, sync_run_id: UUID) -> tuple[GoogleSheetsSyncPage, ...]:
        async with self._uow("google_sheets_sync_read", sync_run_id) as uow:
            row = await _load(uow, sync_run_id)
            await uow.rollback()
            if row is None:
                return ()
            return await _repository_from_row(row).list_pages(sync_run_id)

    async def mark_item_succeeded(
        self, item_id: UUID, *, revision_id: UUID, now: datetime
    ) -> GoogleSheetsSyncItem:
        return await self._mutate(
            "google_sheets_sync_item",
            item_id,
            "mark_item_succeeded",
            {"item_id": item_id, "revision_id": revision_id, "now": now},
        )

    async def complete_page(
        self, page_id: UUID, *, now: datetime
    ) -> GoogleSheetsSyncPage:
        return await self._mutate(
            "google_sheets_sync_page",
            page_id,
            "complete_page",
            {"page_id": page_id, "now": now},
        )

    async def mark_page_incomplete(
        self, page_id: UUID, *, now: datetime
    ) -> GoogleSheetsSyncPage:
        return await self._mutate(
            "google_sheets_sync_page",
            page_id,
            "mark_page_incomplete",
            {"page_id": page_id, "now": now},
        )

    async def mark_run(
        self,
        sync_run_id: UUID,
        *,
        state: GoogleSheetsSyncRunState,
        failure_code: str | None,
        now: datetime,
    ) -> GoogleSheetsSyncRun:
        return await self._mutate(
            "google_sheets_sync_run",
            sync_run_id,
            "mark_run",
            {
                "sync_run_id": sync_run_id,
                "state": state,
                "failure_code": failure_code,
                "now": now,
            },
        )

    async def record_identity_disposition(
        self, command: GoogleSheetsIdentityDispositionCommand
    ) -> GoogleSheetsIdentityQuarantine:
        event = identity_disposition_event(command)
        async with self._uow(
            "google_sheets_identity_disposition", command.sync_run_id
        ) as uow:
            row = await uow._execute(
                select(google_sheets_identity_dispositions).where(
                    google_sheets_identity_dispositions.c.sync_run_id == command.sync_run_id,
                    google_sheets_identity_dispositions.c.scope_identity == command.scope_identity,
                )
            )
            existing = row.mappings().one_or_none()
            if existing is not None:
                persisted = _identity_disposition_from_row(existing)
                if any(
                    getattr(persisted, field) != getattr(event, field)
                    for field in (
                        "event_id",
                        "sync_run_id",
                        "workspace_id",
                        "environment_id",
                        "connection_id",
                        "scope_identity",
                        "disposition",
                        "failure_code",
                    )
                ):
                    raise ValueError("Google Sheets identity disposition conflicts with durable state")
                await uow.rollback()
                return persisted
            await uow._execute(
                insert(google_sheets_identity_dispositions).values(
                    event_id=event.event_id,
                    sync_run_id=event.sync_run_id,
                    workspace_id=event.workspace_id,
                    environment_id=event.environment_id,
                    connection_id=event.connection_id,
                    scope_identity=event.scope_identity,
                    disposition=event.disposition.value,
                    failure_code=(event.failure_code.value if event.failure_code else None),
                    occurred_at=event.occurred_at,
                )
            )
            await uow.commit()
            return event

    async def list_identity_dispositions(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        connection_id: UUID,
        scope_identities: tuple[str, ...],
    ) -> tuple[GoogleSheetsIdentityQuarantine, ...]:
        if not scope_identities:
            return ()
        async with self._uow(
            "google_sheets_identity_disposition_read", connection_id
        ) as uow:
            result = await uow._execute(
                select(google_sheets_identity_dispositions)
                .where(
                    google_sheets_identity_dispositions.c.workspace_id == workspace_id,
                    google_sheets_identity_dispositions.c.environment_id == environment_id,
                    google_sheets_identity_dispositions.c.connection_id == connection_id,
                    google_sheets_identity_dispositions.c.scope_identity.in_(scope_identities),
                )
                .order_by(
                    google_sheets_identity_dispositions.c.occurred_at,
                    google_sheets_identity_dispositions.c.event_id,
                )
            )
            events = tuple(
                _identity_disposition_from_row(row)
                for row in result.mappings().all()
            )
            await uow.rollback()
            return events

    async def _mutate(
        self, operation: str, identity: UUID, method: str, kwargs: dict[str, Any]
    ) -> Any:
        async with self._uow(operation, identity) as uow:
            sync_run_id = kwargs.get("sync_run_id")
            if sync_run_id is None:
                sync_run_id = await _find_run_id(uow, identity)
            row = await _load(uow, sync_run_id)
            if row is None:
                raise KeyError("Google Sheets sync run is not durable")
            repository = _repository_from_row(row)
            result = await getattr(repository, method)(**kwargs)
            run, pages = repository.snapshot(sync_run_id)
            await uow._execute(
                update(google_sheets_sync_runs)
                .where(google_sheets_sync_runs.c.sync_run_id == sync_run_id)
                .values(
                    state=run.state.value,
                    record=_record(run, pages),
                    updated_at=run.updated_at,
                )
            )
            await uow.commit()
            return result

    def _uow(self, operation: str, identity: UUID):
        context = self._context_factory(PersistenceOperation(operation), identity)
        return self._uow_factory(context)


async def _load(uow: Any, sync_run_id: UUID) -> Any | None:
    result = await uow._execute(
        select(google_sheets_sync_runs).where(
            google_sheets_sync_runs.c.sync_run_id == sync_run_id
        )
    )
    return result.mappings().one_or_none()


async def _find_run_id(uow: Any, identity: UUID) -> UUID:
    result = await uow._execute(
        select(google_sheets_sync_runs.c.sync_run_id, google_sheets_sync_runs.c.record)
    )
    for row in result.mappings():
        repository = _repository_from_row(row)
        if identity in repository.pages:
            return row["sync_run_id"]
        if any(
            item.item_id == identity
            for page in repository.pages.values()
            for item in page.items
        ):
            return row["sync_run_id"]
    raise KeyError("Google Sheets sync page or item is not durable")


def _record(
    run: GoogleSheetsSyncRun, pages: tuple[GoogleSheetsSyncPage, ...]
) -> dict[str, Any]:
    return {
        "run": run.model_dump(mode="json"),
        "pages": [page.model_dump(mode="json") for page in pages],
    }


def _columns(
    run: GoogleSheetsSyncRun, pages: tuple[GoogleSheetsSyncPage, ...]
) -> dict[str, Any]:
    return {
        "sync_run_id": run.sync_run_id,
        "workspace_id": run.workspace_id,
        "environment_id": run.environment_id,
        "connection_id": run.connection_id,
        "resource_scope_reference": run.resource_scope_reference,
        "service_principal_id": run.service_principal_id,
        "idempotency_key": run.idempotency_key,
        "state": run.state.value,
        "prior_committed_cursor": run.prior_committed_cursor,
        "proposed_cursor": run.proposed_cursor,
        "record": _record(run, pages),
        "created_at": run.requested_at,
        "updated_at": run.updated_at,
    }


def _repository_from_row(row: Any) -> InMemoryGoogleSheetsSyncRepository:
    payload = row["record"]
    run = GoogleSheetsSyncRun.model_validate_json(json.dumps(payload["run"]))
    pages = tuple(
        GoogleSheetsSyncPage.model_validate_json(json.dumps(value))
        for value in payload["pages"]
    )
    return InMemoryGoogleSheetsSyncRepository.from_snapshot(run, pages)


def _run_from_row(row: Any) -> GoogleSheetsSyncRun:
    return GoogleSheetsSyncRun.model_validate_json(json.dumps(row["record"]["run"]))


def _identity_disposition_from_row(row: Any) -> GoogleSheetsIdentityQuarantine:
    return GoogleSheetsIdentityQuarantine(
        event_id=row["event_id"],
        sync_run_id=row["sync_run_id"],
        workspace_id=row["workspace_id"],
        environment_id=row["environment_id"],
        connection_id=row["connection_id"],
        scope_identity=row["scope_identity"],
        disposition=GoogleSheetsIdentityDisposition(row["disposition"]),
        failure_code=(
            GoogleSheetsIdentityFailureCode(row["failure_code"])
            if row["failure_code"] is not None
            else None
        ),
        occurred_at=row["occurred_at"],
    )


__all__ = ["PostgreSQLGoogleSheetsSyncRepository"]
