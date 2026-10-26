"""PostgreSQL adapter for the governed Drive snapshot ledger.

The record is JSONB because page/item state is an immutable replay envelope;
the tenant and checkpoint columns remain relational for RLS and operational
inspection. A write replaces the complete envelope in one transaction.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import insert, select, update

from spine.application.persistence.context import PersistenceOperation, TrustedPersistenceContext
from spine.application.persistence.drive_sync import (
    DriveSnapshotPageRecord,
    DriveSnapshotRun,
    DriveSnapshotRunCommand,
    DriveSyncRepository,
    InMemoryDriveSyncRepository,
)
from spine.infrastructure.persistence.postgresql_mappings import drive_snapshot_runs


class PostgreSQLDriveSyncRepository(DriveSyncRepository):
    """Persist one complete run envelope through the existing tenant UoW."""

    def __init__(
        self,
        uow_factory: Callable[[TrustedPersistenceContext], object],
        context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def start_run(self, command: DriveSnapshotRunCommand) -> DriveSnapshotRun:
        async with self._uow("drive_snapshot_run", command.sync_run_id) as uow:
            row = await uow._execute(  # type: ignore[attr-defined]
                select(drive_snapshot_runs).where(
                    drive_snapshot_runs.c.workspace_id == command.workspace_id,
                    drive_snapshot_runs.c.environment_id == command.environment_id,
                    drive_snapshot_runs.c.idempotency_key == command.idempotency_key,
                )
            )
            existing = row.mappings().one_or_none()
            if existing is not None:
                repository = _repository_from_row(existing)
                result = await repository.start_run(command)
                await uow.rollback()  # type: ignore[attr-defined]
                return result
            repository = InMemoryDriveSyncRepository()
            result = await repository.start_run(command)
            await _insert(uow, result, repository)
            await uow.commit()  # type: ignore[attr-defined]
            return result

    async def resolve_run(self, sync_run_id: UUID) -> DriveSnapshotRun | None:
        async with self._uow("drive_snapshot_read", sync_run_id) as uow:
            row = await uow._execute(  # type: ignore[attr-defined]
                select(drive_snapshot_runs).where(
                    drive_snapshot_runs.c.sync_run_id == sync_run_id
                )
            )
            record = row.mappings().one_or_none()
            await uow.rollback()  # type: ignore[attr-defined]
            return None if record is None else _run_from_row(record)

    async def open_page(self, **kwargs):
        return await self._mutate("drive_snapshot_page", kwargs["sync_run_id"], "open_page", kwargs)

    async def resolve_page(self, sync_run_id: UUID, page_token: str | None):
        return await self._read_page(sync_run_id, page_token)

    async def list_pages(self, sync_run_id: UUID):
        async with self._uow("drive_snapshot_read", sync_run_id) as uow:
            record = await _load(uow, sync_run_id)
            await uow.rollback()  # type: ignore[attr-defined]
            if record is None:
                return ()
            repository = _repository_from_row(record)
            return await repository.list_pages(sync_run_id)

    async def mark_item_succeeded(self, item_id: UUID, *, revision_id: UUID, now: datetime):
        return await self._mutate("drive_snapshot_item", item_id, "mark_item_succeeded", {"item_id": item_id, "revision_id": revision_id, "now": now})

    async def mark_item_failed(self, item_id: UUID, *, failure_code: str, now: datetime):
        return await self._mutate("drive_snapshot_item", item_id, "mark_item_failed", {"item_id": item_id, "failure_code": failure_code, "now": now})

    async def complete_page(self, page_id: UUID, *, now: datetime):
        return await self._mutate("drive_snapshot_page", page_id, "complete_page", {"page_id": page_id, "now": now})

    async def mark_page_incomplete(self, page_id: UUID, *, now: datetime):
        return await self._mutate("drive_snapshot_page", page_id, "mark_page_incomplete", {"page_id": page_id, "now": now})

    async def advance_run(self, sync_run_id: UUID, *, next_page_token: str, now: datetime):
        return await self._mutate("drive_snapshot_run", sync_run_id, "advance_run", {"sync_run_id": sync_run_id, "next_page_token": next_page_token, "now": now})

    async def mark_resumable(self, sync_run_id: UUID, *, now: datetime):
        return await self._mutate("drive_snapshot_run", sync_run_id, "mark_resumable", {"sync_run_id": sync_run_id, "now": now})

    async def commit_run(self, sync_run_id: UUID, *, now: datetime):
        return await self._mutate("drive_snapshot_run", sync_run_id, "commit_run", {"sync_run_id": sync_run_id, "now": now})

    async def mark_reconciliation_required(self, sync_run_id: UUID, *, now: datetime):
        return await self._mutate("drive_snapshot_run", sync_run_id, "mark_reconciliation_required", {"sync_run_id": sync_run_id, "now": now})

    async def _read_page(self, sync_run_id: UUID, page_token: str | None):
        async with self._uow("drive_snapshot_read", sync_run_id) as uow:
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
                raise KeyError("Drive snapshot run is not durable")
            repository = _repository_from_row(record)
            result = await getattr(repository, method)(**kwargs)
            run, pages = repository.snapshot(run_id)
            await _save(uow, run, pages)
            await uow.commit()  # type: ignore[attr-defined]
            return result

    def _uow(self, operation: str, identity: UUID):
        return self._uow_factory(
            self._context_factory(PersistenceOperation(operation), identity)
        )


async def _find_run_id(uow: object, identity: UUID) -> UUID:
    row = await uow._execute(  # type: ignore[attr-defined]
        select(drive_snapshot_runs.c.sync_run_id, drive_snapshot_runs.c.record)
    )
    for record in row.mappings():
        repository = _repository_from_row(record)
        if identity in repository.pages:
            return record["sync_run_id"]
        for page in repository.pages.values():
            if any(item.item_id == identity for item in page.items):
                return record["sync_run_id"]
    raise KeyError("Drive snapshot item or page is not durable")


async def _load(uow: object, sync_run_id: UUID):
    result = await uow._execute(  # type: ignore[attr-defined]
        select(drive_snapshot_runs).where(
            drive_snapshot_runs.c.sync_run_id == sync_run_id
        )
    )
    return result.mappings().one_or_none()


async def _insert(uow: object, run: DriveSnapshotRun, repository: InMemoryDriveSyncRepository) -> None:
    await uow._execute(  # type: ignore[attr-defined]
        insert(drive_snapshot_runs).values(**_columns(run, repository))
    )


async def _save(uow: object, run: DriveSnapshotRun, pages: tuple[DriveSnapshotPageRecord, ...]) -> None:
    payload = {
        "run": run.model_dump(mode="json"),
        "pages": [page.model_dump(mode="json") for page in pages],
    }
    await uow._execute(  # type: ignore[attr-defined]
        update(drive_snapshot_runs)
        .where(drive_snapshot_runs.c.sync_run_id == run.sync_run_id)
        .values(
            state=run.state.value,
            proposed_start_page_token=run.proposed_start_page_token,
            committed_cursor=run.committed_cursor,
            next_page_token=run.next_page_token,
            record=payload,
            updated_at=run.updated_at,
        )
    )


def _columns(run: DriveSnapshotRun, repository: InMemoryDriveSyncRepository) -> dict[str, object]:
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
        "proposed_start_page_token": run.proposed_start_page_token,
        "committed_cursor": run.committed_cursor,
        "next_page_token": run.next_page_token,
        "record": {
            "run": run.model_dump(mode="json"),
            "pages": [page.model_dump(mode="json") for page in pages],
        },
        "created_at": run.requested_at,
        "updated_at": run.updated_at,
    }


def _repository_from_row(row: object) -> InMemoryDriveSyncRepository:
    try:
        payload = row["record"]  # type: ignore[index]
    except (KeyError, TypeError):
        payload = row._mapping["record"]  # type: ignore[attr-defined]
    run = DriveSnapshotRun.model_validate_json(json.dumps(payload["run"]))
    pages = tuple(
        DriveSnapshotPageRecord.model_validate_json(json.dumps(value))
        for value in payload["pages"]
    )
    return InMemoryDriveSyncRepository.from_snapshot(run, pages)


def _run_from_row(row: object) -> DriveSnapshotRun:
    try:
        payload = row["record"]  # type: ignore[index]
    except (KeyError, TypeError):
        payload = row._mapping["record"]  # type: ignore[attr-defined]
    return DriveSnapshotRun.model_validate_json(json.dumps(payload["run"]))


__all__ = ["PostgreSQLDriveSyncRepository"]
