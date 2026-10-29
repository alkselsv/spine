"""PostgreSQL adapter for the Yandex Disk reconciliation replay envelope."""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import UUID

from sqlalchemy import insert, select, update

from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.yandex_disk_reconciliation import (
    InMemoryYandexDiskReconciliationRepository,
    YandexDiskReconciliationRepository,
    YandexDiskReconciliationRun,
    YandexDiskLifecycleSource,
    YandexDiskReconciliationCommand,
)
from spine.application.persistence.context import EnvironmentScope
from spine.domain.sources import RevisionKind
from spine.infrastructure.persistence.postgresql_mappings import (
    yandex_disk_reconciliation_runs,
)


class PostgreSQLYandexDiskReconciliationRepository(
    YandexDiskReconciliationRepository
):
    """Atomically replace one tenant-scoped run after each accepted checkpoint."""

    def __init__(
        self,
        uow_factory: Callable[[TrustedPersistenceContext], object],
        context_factory: Callable[
            [PersistenceOperation, UUID], TrustedPersistenceContext
        ],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def resolve(
        self, run_id: UUID, workspace_id: UUID, environment_id: UUID
    ) -> YandexDiskReconciliationRun | None:
        async with self._uow(
            "yandex_disk_reconciliation_read", run_id, workspace_id, environment_id
        ) as uow:
            result = await uow._execute(  # type: ignore[attr-defined]
                select(yandex_disk_reconciliation_runs).where(
                    yandex_disk_reconciliation_runs.c.run_id == run_id,
                    yandex_disk_reconciliation_runs.c.workspace_id == workspace_id,
                    yandex_disk_reconciliation_runs.c.environment_id == environment_id,
                )
            )
            row = result.mappings().one_or_none()
            await uow.rollback()  # type: ignore[attr-defined]
            return None if row is None else _run_from_row(row)

    async def save(
        self, run: YandexDiskReconciliationRun
    ) -> YandexDiskReconciliationRun:
        async with self._uow(
            "yandex_disk_reconciliation_write",
            run.run_id,
            run.workspace_id,
            run.environment_id,
        ) as uow:
            result = await uow._execute(  # type: ignore[attr-defined]
                select(yandex_disk_reconciliation_runs).where(
                    yandex_disk_reconciliation_runs.c.run_id == run.run_id
                ).with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                await uow._execute(  # type: ignore[attr-defined]
                    insert(yandex_disk_reconciliation_runs).values(**_columns(run))
                )
            else:
                repository = InMemoryYandexDiskReconciliationRepository()
                current = _run_from_row(row)
                await repository.save(current)
                accepted = await repository.save(run)
                await uow._execute(  # type: ignore[attr-defined]
                    update(yandex_disk_reconciliation_runs)
                    .where(yandex_disk_reconciliation_runs.c.run_id == run.run_id)
                    .values(
                        state=accepted.state.value,
                        committed_checkpoint=accepted.committed_checkpoint,
                        next_checkpoint=accepted.next_checkpoint,
                        record=_record(accepted),
                        updated_at=accepted.updated_at,
                    )
                )
            await uow.commit()  # type: ignore[attr-defined]
            return run

    def _uow(
        self,
        operation: str,
        identity: UUID,
        workspace_id: UUID,
        environment_id: UUID,
    ):
        context = self._context_factory(PersistenceOperation(operation), identity)
        scope = context.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id,
            scope.environment_id,
        ) != (workspace_id, environment_id):
            raise ValueError("Yandex Disk reconciliation lookup requires exact tenant scope")
        return self._uow_factory(
            context
        )


class PostgreSQLYandexDiskLifecycleSourceReader:
    """Resolve current path-scoped Yandex sources inside one admitted tenant."""

    def __init__(
        self, uow_factory: Callable[[TrustedPersistenceContext], object]
    ) -> None:
        self._uow_factory = uow_factory

    async def list_sources(
        self,
        command: YandexDiskReconciliationCommand,
        context: TrustedPersistenceContext,
    ) -> tuple[YandexDiskLifecycleSource, ...]:
        scope = context.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id,
            scope.environment_id,
        ) != (command.workspace_id, command.environment_id):
            raise ValueError("Yandex Disk source lookup requires reconciliation tenant")

        results: list[YandexDiskLifecycleSource] = []
        async with self._uow_factory(context) as uow:
            after = None
            while True:
                page = await uow.sources.list_sources(limit=500, after=after)
                if not page:
                    break
                for source in page:
                    if (
                        source.identity_mode.value != "connector"
                        or source.connection_id != command.connection_id
                        or source.external_namespace != "yandex-disk"
                        or source.external_generation != "yandex-disk-path:v1"
                    ):
                        continue
                    head = await uow.canonical.resolve_current(source.source_object_id)
                    if head is None:
                        continue
                    current = await uow.sources.resolve_revision(
                        head.current_revision_id
                    )
                    if current is None:
                        continue
                    evidence_revision = current
                    if current.kind is RevisionKind.TOMBSTONE:
                        revisions = await uow.sources.list_revisions(
                            source.source_object_id,
                            limit=500,
                        )
                        evidence_revision = next(
                            (
                                revision
                                for revision in revisions
                                if revision.kind is RevisionKind.CONTENT
                            ),
                            None,
                        )
                        if evidence_revision is None:
                            continue
                    external = evidence_revision.revision_metadata.external_file_revision
                    if (
                        external is None
                        or external.provider_kind != "yandex_disk"
                        or external.identity_assurance != "path_scoped"
                        or external.resource_path is None
                    ):
                        continue
                    results.append(
                        YandexDiskLifecycleSource(
                            provider_object_reference=external.resource_path,
                            source_object_id=source.source_object_id,
                            current_revision_id=current.revision_id,
                            current_revision_kind=current.kind,
                            current_provider_revision=(
                                external.provider_revision
                                if current.kind is RevisionKind.CONTENT
                                else None
                            ),
                        )
                    )
                last = page[-1]
                after = (last.created_at, last.source_object_id)
                if len(page) < 500:
                    break
            await uow.rollback()
        return tuple(results)


def _columns(run: YandexDiskReconciliationRun) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "workspace_id": run.workspace_id,
        "environment_id": run.environment_id,
        "connection_id": run.connection_id,
        "connection_version_id": run.connection_version_id,
        "resource_scope_reference": run.resource_scope_reference,
        "idempotency_key": run.idempotency_key,
        "state": run.state.value,
        "committed_checkpoint": run.committed_checkpoint,
        "next_checkpoint": run.next_checkpoint,
        "record_schema_version": 1,
        "record": _record(run),
        "created_at": run.started_at,
        "updated_at": run.updated_at,
    }


def _run_from_row(row: object) -> YandexDiskReconciliationRun:
    try:
        payload = row["record"]  # type: ignore[index]
    except (KeyError, TypeError):
        payload = row._mapping["record"]  # type: ignore[attr-defined]
    if payload.get("schema_version") != 1 or not isinstance(payload.get("run"), dict):
        raise ValueError("Unsupported Yandex Disk reconciliation record schema")
    return YandexDiskReconciliationRun.model_validate_json(json.dumps(payload["run"]))


def _record(run: YandexDiskReconciliationRun) -> dict[str, object]:
    return {"schema_version": 1, "run": run.model_dump(mode="json")}


__all__ = [
    "PostgreSQLYandexDiskLifecycleSourceReader",
    "PostgreSQLYandexDiskReconciliationRepository",
]
