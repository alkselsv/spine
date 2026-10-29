"""PostgreSQL adapters for mounted-filesystem reconciliation state."""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import UUID

from sqlalchemy import insert, select, update

from spine.application.mounted_filesystem_reconciliation import (
    InMemoryMountedFilesystemReconciliationRepository,
    MountedFilesystemCanonicalSource,
    MountedFilesystemReconciliationCommand,
    MountedFilesystemReconciliationRepository,
    MountedFilesystemReconciliationRun,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.domain.sources import RevisionKind
from spine.infrastructure.persistence.postgresql_mappings import (
    mounted_fs_reconciliation_runs,
)


class PostgreSQLMountedFilesystemReconciliationRepository(
    MountedFilesystemReconciliationRepository
):
    """Persist every accepted run checkpoint inside the exact tenant scope."""

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
    ) -> MountedFilesystemReconciliationRun | None:
        async with self._uow("read", run_id, workspace_id, environment_id) as uow:
            result = await uow._execute(  # type: ignore[attr-defined]
                select(mounted_fs_reconciliation_runs).where(
                    mounted_fs_reconciliation_runs.c.run_id == run_id,
                    mounted_fs_reconciliation_runs.c.workspace_id
                    == workspace_id,
                    mounted_fs_reconciliation_runs.c.environment_id
                    == environment_id,
                )
            )
            row = result.mappings().one_or_none()
            await uow.rollback()  # type: ignore[attr-defined]
            return None if row is None else _run_from_row(row)

    async def save(
        self, run: MountedFilesystemReconciliationRun
    ) -> MountedFilesystemReconciliationRun:
        async with self._uow(
            "write", run.run_id, run.workspace_id, run.environment_id
        ) as uow:
            result = await uow._execute(  # type: ignore[attr-defined]
                select(mounted_fs_reconciliation_runs)
                .where(mounted_fs_reconciliation_runs.c.run_id == run.run_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                await uow._execute(  # type: ignore[attr-defined]
                    insert(mounted_fs_reconciliation_runs).values(
                        **_columns(run)
                    )
                )
            else:
                guard = InMemoryMountedFilesystemReconciliationRepository()
                await guard.save(_run_from_row(row))
                accepted = await guard.save(run)
                await uow._execute(  # type: ignore[attr-defined]
                    update(mounted_fs_reconciliation_runs)
                    .where(
                        mounted_fs_reconciliation_runs.c.run_id == run.run_id
                    )
                    .values(
                        state=accepted.state.value,
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
        context = self._context_factory(
            PersistenceOperation(f"mounted_filesystem_reconciliation_{operation}"),
            identity,
        )
        scope = context.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id,
            scope.environment_id,
        ) != (workspace_id, environment_id):
            raise ValueError("mounted filesystem run lookup requires exact tenant scope")
        return self._uow_factory(context)


class PostgreSQLMountedFilesystemLifecycleSourceReader:
    """Resolve current path-scoped sources inside one admitted tenant."""

    def __init__(self, uow_factory: Callable[[TrustedPersistenceContext], object]) -> None:
        self._uow_factory = uow_factory

    async def list_sources(
        self,
        command: MountedFilesystemReconciliationCommand,
        context: TrustedPersistenceContext,
    ) -> tuple[MountedFilesystemCanonicalSource, ...]:
        scope = context.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id,
            scope.environment_id,
        ) != (command.workspace_id, command.environment_id):
            raise ValueError("mounted filesystem source lookup requires exact tenant scope")
        results: list[MountedFilesystemCanonicalSource] = []
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
                        or source.external_namespace != "mounted_filesystem"
                        or source.external_generation != "mounted-path:v1"
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
                            source.source_object_id, limit=500
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
                        or external.provider_kind != "mounted_filesystem"
                        or external.identity_assurance != "path_scoped"
                        or external.resource_path is None
                    ):
                        continue
                    results.append(
                        MountedFilesystemCanonicalSource(
                            provider_object_reference=external.resource_path,
                            source_object_id=source.source_object_id,
                            current_revision_id=current.revision_id,
                            current_revision_kind=current.kind,
                        )
                    )
                last = page[-1]
                after = (last.created_at, last.source_object_id)
                if len(page) < 500:
                    break
            await uow.rollback()
        return tuple(results)


def _columns(run: MountedFilesystemReconciliationRun) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "workspace_id": run.workspace_id,
        "environment_id": run.environment_id,
        "connection_id": run.connection_id,
        "connection_version_id": run.connection_version_id,
        "resource_scope_reference": run.resource_scope_reference,
        "idempotency_key": run.idempotency_key,
        "state": run.state.value,
        "record_schema_version": 1,
        "record": _record(run),
        "created_at": run.started_at,
        "updated_at": run.updated_at,
    }


def _run_from_row(row: object) -> MountedFilesystemReconciliationRun:
    try:
        payload = row["record"]  # type: ignore[index]
    except (KeyError, TypeError):
        payload = row._mapping["record"]  # type: ignore[attr-defined]
    if payload.get("schema_version") != 1 or not isinstance(payload.get("run"), dict):
        raise ValueError("Unsupported mounted filesystem reconciliation record schema")
    return MountedFilesystemReconciliationRun.model_validate_json(
        json.dumps(payload["run"])
    )


def _record(run: MountedFilesystemReconciliationRun) -> dict[str, object]:
    return {"schema_version": 1, "run": run.model_dump(mode="json")}


__all__ = [
    "PostgreSQLMountedFilesystemLifecycleSourceReader",
    "PostgreSQLMountedFilesystemReconciliationRepository",
]
