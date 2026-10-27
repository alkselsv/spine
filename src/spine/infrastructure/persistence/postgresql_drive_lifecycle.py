"""PostgreSQL storage for tenant-scoped Drive lifecycle decision records."""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import UUID

from sqlalchemy import insert, select, update

from spine.application.google_drive_lifecycle import (
    DriveLifecycleDecisionRepository,
    DriveLifecycleSource,
    DriveLifecycleSourceReader,
    DriveReconciliationEvidence,
    DriveRemovalDecision,
    InMemoryDriveLifecycleDecisionRepository,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.infrastructure.persistence.postgresql_mappings import drive_lifecycle_decisions


class PostgreSQLDriveLifecycleDecisionRepository(DriveLifecycleDecisionRepository):
    """Persist removal policy decisions under the active workspace RLS context."""

    def __init__(
        self,
        uow_factory: Callable[[TrustedPersistenceContext], object],
        context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def resolve(
        self, decision_id: UUID, *, workspace_id: UUID, environment_id: UUID
    ) -> DriveRemovalDecision | None:
        async with self._uow("drive_lifecycle_decision_read", decision_id) as uow:
            row = await uow._execute(  # type: ignore[attr-defined]
                select(drive_lifecycle_decisions).where(
                    drive_lifecycle_decisions.c.decision_id == decision_id,
                    drive_lifecycle_decisions.c.workspace_id == workspace_id,
                    drive_lifecycle_decisions.c.environment_id == environment_id,
                )
            )
            value = row.mappings().one_or_none()
            await uow.rollback()  # type: ignore[attr-defined]
            return _decision(value) if value is not None else None

    async def record(self, decision: DriveRemovalDecision) -> DriveRemovalDecision:
        async with self._uow("drive_lifecycle_decision", decision.decision_id) as uow:
            row = await uow._execute(  # type: ignore[attr-defined]
                select(drive_lifecycle_decisions).where(
                    drive_lifecycle_decisions.c.decision_id == decision.decision_id,
                    drive_lifecycle_decisions.c.workspace_id == decision.workspace_id,
                    drive_lifecycle_decisions.c.environment_id == decision.environment_id,
                ).with_for_update()
            )
            existing_row = row.mappings().one_or_none()
            if existing_row is None:
                await uow._execute(  # type: ignore[attr-defined]
                    insert(drive_lifecycle_decisions).values(**_columns(decision))
                )
                await uow.commit()  # type: ignore[attr-defined]
                return decision

            existing = _decision(existing_row)
            memory = InMemoryDriveLifecycleDecisionRepository()
            memory.records[existing.decision_id] = existing
            result = await memory.record(decision)
            if result != existing:
                await uow._execute(  # type: ignore[attr-defined]
                    update(drive_lifecycle_decisions)
                    .where(
                        drive_lifecycle_decisions.c.decision_id == decision.decision_id,
                        drive_lifecycle_decisions.c.workspace_id == decision.workspace_id,
                        drive_lifecycle_decisions.c.environment_id == decision.environment_id,
                    )
                    .values(state=result.state.value, record=result.model_dump(mode="json"))
                )
                await uow.commit()  # type: ignore[attr-defined]
                return result
            await uow.rollback()  # type: ignore[attr-defined]
            return result

    def _uow(self, operation: str, identity: UUID):
        return self._uow_factory(self._context_factory(PersistenceOperation(operation), identity))


class PostgreSQLGoogleDriveLifecycleSourceReader(DriveLifecycleSourceReader):
    """Resolve current Google Drive heads and file IDs inside the evidence tenant."""

    def __init__(self, uow_factory: Callable[[TrustedPersistenceContext], object]) -> None:
        self._uow_factory = uow_factory

    async def list_current_sources(
        self, evidence: DriveReconciliationEvidence, context: TrustedPersistenceContext
    ) -> tuple[DriveLifecycleSource, ...]:
        scope = context.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id, scope.environment_id
        ) != (evidence.workspace_id, evidence.environment_id):
            raise ValueError("Drive lifecycle source lookup requires its reconciliation tenant")

        results: list[DriveLifecycleSource] = []
        seen_file_ids: set[str] = set()
        async with self._uow_factory(context) as uow:
            after = None
            while True:
                page = await uow.sources.list_sources(limit=500, after=after)
                if not page:
                    break
                for source in page:
                    if (
                        source.identity_mode.value != "connector"
                        or source.connection_id != evidence.connection_id
                        or source.external_namespace != "google"
                        or source.external_generation != "google-source-key:v1"
                    ):
                        continue
                    head = await uow.canonical.resolve_current(source.source_object_id)
                    if head is None:
                        continue
                    revision = await uow.sources.resolve_revision(head.current_revision_id)
                    if revision is None:
                        continue
                    proven_file_ids = {
                        item.origin_locator_value
                        for item in await uow.sources.list_provenance(revision.revision_id)
                        if item.connection_id == evidence.connection_id
                        and item.origin_locator_kind == "google-drive-file"
                        and item.origin_locator_schema == "locator:google-drive-file:v1"
                    }
                    if len(proven_file_ids) != 1:
                        continue
                    file_id = proven_file_ids.pop()
                    if file_id in seen_file_ids:
                        raise ValueError("Drive lifecycle source identity is ambiguous")
                    seen_file_ids.add(file_id)
                    results.append(
                        DriveLifecycleSource(
                            file_id=file_id,
                            source_object_id=source.source_object_id,
                            current_revision_id=head.current_revision_id,
                            is_tombstoned=head.current_revision_kind.value == "tombstone",
                        )
                    )
                last = page[-1]
                after = (last.created_at, last.source_object_id)
                if len(page) < 500:
                    break
            await uow.rollback()
        return tuple(results)


def _columns(decision: DriveRemovalDecision) -> dict[str, object]:
    return {
        "decision_id": decision.decision_id,
        "workspace_id": decision.workspace_id,
        "environment_id": decision.environment_id,
        "reconciliation_id": decision.reconciliation_id,
        "snapshot_run_id": decision.snapshot_run_id,
        "change_sync_run_id": decision.change_sync_run_id,
        "committed_cursor": decision.committed_cursor,
        "connection_id": decision.connection_id,
        "resource_scope_reference": decision.resource_scope_reference,
        "policy_version_id": decision.policy_version_id,
        "file_id": decision.file_id,
        "source_object_id": decision.source_object_id,
        "policy_authorized": decision.policy_authorized,
        "state": decision.state.value,
        "record": decision.model_dump(mode="json"),
        "decided_at": decision.decided_at,
    }


def _decision(row: object) -> DriveRemovalDecision:
    try:
        record = row["record"]  # type: ignore[index]
    except (KeyError, TypeError):
        record = row._mapping["record"]  # type: ignore[attr-defined]
    return DriveRemovalDecision.model_validate_json(json.dumps(record))


__all__ = [
    "PostgreSQLDriveLifecycleDecisionRepository",
    "PostgreSQLGoogleDriveLifecycleSourceReader",
]
