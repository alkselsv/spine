"""PostgreSQL adapter for the durable Google Drive change-feed ledger.

The existing Drive sync table stores the complete typed change envelope in JSONB
while retaining tenant and checkpoint columns for RLS and operations. This keeps
page effects and cursor transitions in one transaction and makes replay safe.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import insert, select, update

from spine.application.persistence.context import PersistenceOperation, TrustedPersistenceContext
from spine.application.persistence.drive_changes import (
    DriveChangePageRecord,
    DriveChangeRepository,
    DriveChangeRun,
    DriveChangeRunCommand,
    DriveChangeSyncConflictError,
    InMemoryDriveChangeRepository,
)
from spine.infrastructure.persistence.postgresql_mappings import drive_change_runs


class PostgreSQLDriveChangeRepository(DriveChangeRepository):
    """Persist change runs through the tenant-scoped UoW."""

    def __init__(
        self,
        uow_factory: Callable[[TrustedPersistenceContext], object],
        context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def start_run(self, command: DriveChangeRunCommand) -> DriveChangeRun:
        async with self._uow("drive_change_run", command.sync_run_id) as uow:
            row = await uow._execute(  # type: ignore[attr-defined]
                select(drive_change_runs).where(
                    drive_change_runs.c.workspace_id == command.workspace_id,
                    drive_change_runs.c.environment_id == command.environment_id,
                    drive_change_runs.c.idempotency_key == command.idempotency_key,
                )
            )
            existing = row.mappings().one_or_none()
            if existing is not None:
                repository = _repository_from_row(existing)
                result = await repository.start_run(command)
                await uow.rollback()  # type: ignore[attr-defined]
                return result
            repository = InMemoryDriveChangeRepository()
            result = await repository.start_run(command)
            await _insert(uow, result, repository)
            await uow.commit()  # type: ignore[attr-defined]
            return result

    async def resolve_run(self, sync_run_id: UUID):
        async with self._uow("drive_change_read", sync_run_id) as uow:
            row = await uow._execute(  # type: ignore[attr-defined]
                select(drive_change_runs).where(
                    drive_change_runs.c.sync_run_id == sync_run_id
                )
            )
            record = row.mappings().one_or_none()
            await uow.rollback()  # type: ignore[attr-defined]
            if record is None:
                return None
            return _run_from_row(record)

    async def open_page(self, **kwargs):
        return await self._mutate("drive_change_page", kwargs["sync_run_id"], "open_page", kwargs)

    async def resolve_page(self, sync_run_id: UUID, page_token: str):
        return await self._read_page(sync_run_id, page_token)

    async def list_pages(self, sync_run_id: UUID):
        async with self._uow("drive_change_read", sync_run_id) as uow:
            record = await _load(uow, sync_run_id)
            await uow.rollback()  # type: ignore[attr-defined]
            if record is None:
                return ()
            return await _repository_from_row(record).list_pages(sync_run_id)

    async def mark_item_succeeded(self, item_id: UUID, *, revision_id: UUID, now: datetime):
        return await self._mutate(
            "drive_change_item", item_id, "mark_item_succeeded",
            {"item_id": item_id, "revision_id": revision_id, "now": now},
        )

    async def mark_item_ignored(self, item_id: UUID, *, reason: str, now: datetime):
        return await self._mutate(
            "drive_change_item", item_id, "mark_item_ignored",
            {"item_id": item_id, "reason": reason, "now": now},
        )

    async def mark_item_failed(self, item_id: UUID, *, failure_code: str, now: datetime):
        return await self._mutate(
            "drive_change_item", item_id, "mark_item_failed",
            {"item_id": item_id, "failure_code": failure_code, "now": now},
        )

    async def complete_page(self, page_id: UUID, *, now: datetime):
        return await self._mutate(
            "drive_change_page", page_id, "complete_page", {"page_id": page_id, "now": now}
        )

    async def mark_page_incomplete(self, page_id: UUID, *, now: datetime):
        return await self._mutate(
            "drive_change_page", page_id, "mark_page_incomplete",
            {"page_id": page_id, "now": now},
        )

    async def advance_run(self, sync_run_id: UUID, *, next_page_token: str, now: datetime):
        return await self._mutate(
            "drive_change_run", sync_run_id, "advance_run",
            {"sync_run_id": sync_run_id, "next_page_token": next_page_token, "now": now},
        )

    async def mark_resumable(self, sync_run_id: UUID, *, now: datetime):
        return await self._mutate(
            "drive_change_run", sync_run_id, "mark_resumable",
            {"sync_run_id": sync_run_id, "now": now},
        )

    async def commit_run(self, sync_run_id: UUID, *, now: datetime):
        return await self._mutate(
            "drive_change_run", sync_run_id, "commit_run",
            {"sync_run_id": sync_run_id, "now": now},
        )

    async def mark_reconciliation_required(self, sync_run_id: UUID, *, reason: str, now: datetime):
        return await self._mutate(
            "drive_change_run", sync_run_id, "mark_reconciliation_required",
            {"sync_run_id": sync_run_id, "reason": reason, "now": now},
        )

    async def _read_page(self, sync_run_id: UUID, page_token: str):
        async with self._uow("drive_change_read", sync_run_id) as uow:
            record = await _load(uow, sync_run_id)
            await uow.rollback()  # type: ignore[attr-defined]
            if record is None:
                return None
            return await _repository_from_row(record).resolve_page(sync_run_id, page_token)

    async def _mutate(self, operation: str, identity: UUID, method: str, kwargs: dict):
        async with self._uow(operation, identity) as uow:
            run_id = kwargs.get("sync_run_id")
            if run_id is None:
                run_id = await _find_run_id(uow, identity)
            record = await _load(uow, run_id)
            if record is None:
                raise KeyError("Drive change run is not durable")
            repository = _repository_from_row(record)
            result = await getattr(repository, method)(**kwargs)
            run, pages = repository.snapshot(run_id)
            await _save(uow, run, pages)
            await uow.commit()  # type: ignore[attr-defined]
            return result

    def _uow(self, operation: str, identity: UUID):
        return self._uow_factory(self._context_factory(PersistenceOperation(operation), identity))


async def _find_run_id(uow: object, identity: UUID) -> UUID:
    rows = await uow._execute(  # type: ignore[attr-defined]
        select(drive_change_runs.c.sync_run_id, drive_change_runs.c.record)
    )
    for record in rows.mappings():
        repository = _repository_from_row(record)
        if identity in repository.pages:
            return record["sync_run_id"]
        for page in repository.pages.values():
            if any(item.item_id == identity for item in page.items):
                return record["sync_run_id"]
    raise KeyError("Drive change item or page is not durable")


async def _load(uow: object, sync_run_id: UUID):
    result = await uow._execute(  # type: ignore[attr-defined]
        select(drive_change_runs).where(drive_change_runs.c.sync_run_id == sync_run_id)
    )
    return result.mappings().one_or_none()


async def _insert(uow: object, run: DriveChangeRun, repository: InMemoryDriveChangeRepository):
    await uow._execute(insert(drive_change_runs).values(**_columns(run, repository)))  # type: ignore[attr-defined]


async def _save(uow: object, run: DriveChangeRun, pages: tuple[DriveChangePageRecord, ...]):
    payload = {
        "run": run.model_dump(mode="json"),
        "pages": [page.model_dump(mode="json") for page in pages],
    }
    await uow._execute(  # type: ignore[attr-defined]
        update(drive_change_runs)
        .where(drive_change_runs.c.sync_run_id == run.sync_run_id)
        .values(
            state=run.state.value,
            proposed_start_page_token=run.last_committed_cursor,
            committed_cursor=run.proposed_cursor or run.last_committed_cursor,
            next_page_token=run.next_page_token,
            record=payload,
            updated_at=run.updated_at,
        )
    )


def _columns(run: DriveChangeRun, repository: InMemoryDriveChangeRepository):
    _, pages = repository.snapshot(run.sync_run_id)
    return {
        "sync_run_id": run.sync_run_id,
        "workspace_id": run.workspace_id,
        "environment_id": run.environment_id,
        "connection_id": run.connection_id,
        "resource_scope_reference": run.resource_scope_reference,
        "service_principal_id": run.service_principal_id,
        "idempotency_key": run.idempotency_key,
        "state": run.state.value,
        "proposed_start_page_token": run.last_committed_cursor,
        "committed_cursor": run.proposed_cursor or run.last_committed_cursor,
        "next_page_token": run.next_page_token,
        "record": {
            "run": run.model_dump(mode="json"),
            "pages": [page.model_dump(mode="json") for page in pages],
        },
        "created_at": run.requested_at,
        "updated_at": run.updated_at,
    }


def _repository_from_row(row: object) -> InMemoryDriveChangeRepository:
    try:
        payload = row["record"]  # type: ignore[index]
    except (KeyError, TypeError):
        payload = row._mapping["record"]  # type: ignore[attr-defined]
    run = DriveChangeRun.model_validate_json(json.dumps(payload["run"]))
    pages = tuple(
        DriveChangePageRecord.model_validate_json(json.dumps(value))
        for value in payload["pages"]
    )
    return InMemoryDriveChangeRepository.from_snapshot(run, pages)


def _run_from_row(row: object) -> DriveChangeRun:
    try:
        payload = row["record"]  # type: ignore[index]
    except (KeyError, TypeError):
        payload = row._mapping["record"]  # type: ignore[attr-defined]
    return DriveChangeRun.model_validate_json(json.dumps(payload["run"]))


__all__ = ["PostgreSQLDriveChangeRepository"]
