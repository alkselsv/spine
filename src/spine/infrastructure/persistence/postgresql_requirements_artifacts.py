"""Scoped immutable persistence for requirements artifact versions."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from spine.application.persistence.context import EnvironmentScope
from spine.application.persistence.errors import (
    IdempotencyConflictError,
    InvalidPersistenceContextError,
    OptimisticConflictError,
)
from spine.domain.workflows.requirements import RequirementsArtifact
from spine.infrastructure.persistence.postgresql_mappings import (
    requirements_artifact_versions,
    workflow_runs,
)


class PostgreSQLRequirementsArtifactRepository:
    def __init__(self, uow: object) -> None:
        self._uow = uow

    async def persist_candidate(self, artifact: RequirementsArtifact) -> RequirementsArtifact:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id != artifact.workspace_id or scope.environment_id != artifact.environment_id
        ):
            raise InvalidPersistenceContextError("Requirements artifact scope is invalid.")
        run_row = (await self._uow._execute(select(workflow_runs.c.work_item_id).where(
            workflow_runs.c.workspace_id == artifact.workspace_id,
            workflow_runs.c.environment_id == artifact.environment_id,
            workflow_runs.c.run_id == artifact.run_id,
        ))).first()
        if run_row is None or run_row[0] != artifact.work_item_id:
            raise InvalidPersistenceContextError("Requirements artifact run linkage is invalid.")
        table = requirements_artifact_versions
        lock_key = f"{artifact.workspace_id}:{artifact.environment_id}:{artifact.artifact_id}"
        await self._uow._execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )
        identity = (table.c.workspace_id == artifact.workspace_id) & \
            (table.c.environment_id == artifact.environment_id) & \
            (table.c.run_id == artifact.run_id) & \
            (table.c.production_identity == artifact.production_identity)
        existing = (await self._uow._execute(select(table).where(identity))).mappings().first()
        if existing is not None:
            if existing["production_digest"] != artifact.production_digest:
                raise IdempotencyConflictError("Requirements production identity conflicts with prior output.")
            return RequirementsArtifact.model_validate(existing["payload"])

        latest_row = (await self._uow._execute(select(table.c.artifact_version).where(
            table.c.workspace_id == artifact.workspace_id,
            table.c.environment_id == artifact.environment_id,
            table.c.artifact_id == artifact.artifact_id,
        ).order_by(table.c.artifact_version.desc()).limit(1))).first()
        latest_version = latest_row[0] if latest_row else 0
        if artifact.artifact_version != latest_version + 1 or artifact.predecessor_version != (
            latest_version or None
        ):
            raise OptimisticConflictError("Requirements artifact successor is not monotonic.")

        payload = artifact.model_dump(mode="json")
        statement = insert(table).values(
            workspace_id=artifact.workspace_id,
            environment_id=artifact.environment_id,
            artifact_id=artifact.artifact_id,
            artifact_version=artifact.artifact_version,
            run_id=artifact.run_id,
            work_item_id=artifact.work_item_id,
            production_identity=artifact.production_identity,
            production_digest=artifact.production_digest,
            predecessor_version=artifact.predecessor_version,
            payload=payload,
            created_at=datetime.now(timezone.utc),
        ).on_conflict_do_nothing()
        result = await self._uow._execute(statement)
        if result.rowcount == 1:
            return artifact
        existing = (await self._uow._execute(select(table).where(identity))).mappings().first()
        if existing is None:
            raise OptimisticConflictError("Requirements artifact version already exists.")
        if existing["production_digest"] != artifact.production_digest:
            raise IdempotencyConflictError("Requirements production identity conflicts with prior output.")
        return RequirementsArtifact.model_validate(existing["payload"])
    async def get(self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID,
                  artifact_version: int | None = None) -> RequirementsArtifact | None:
        table = requirements_artifact_versions
        query = select(table.c.payload).where(
            table.c.workspace_id == workspace_id,
            table.c.environment_id == environment_id,
            table.c.artifact_id == artifact_id,
        )
        if artifact_version is None:
            query = query.order_by(table.c.artifact_version.desc()).limit(1)
        else:
            query = query.where(table.c.artifact_version == artifact_version)
        row = (await self._uow._execute(query)).first()
        return RequirementsArtifact.model_validate(row[0]) if row else None

    async def versions(self, *, workspace_id: UUID, environment_id: UUID,
                       artifact_id: UUID) -> tuple[RequirementsArtifact, ...]:
        table = requirements_artifact_versions
        rows = (await self._uow._execute(select(table.c.payload).where(
            table.c.workspace_id == workspace_id,
            table.c.environment_id == environment_id,
            table.c.artifact_id == artifact_id,
        ).order_by(table.c.artifact_version))).all()
        return tuple(RequirementsArtifact.model_validate(row[0]) for row in rows)

    async def get_for_run(self, *, workspace_id: UUID, environment_id: UUID,
                          run_id: UUID) -> RequirementsArtifact | None:
        table = requirements_artifact_versions
        row = (await self._uow._execute(select(table.c.payload).where(
            table.c.workspace_id == workspace_id,
            table.c.environment_id == environment_id,
            table.c.run_id == run_id,
        ).order_by(table.c.artifact_version.desc()).limit(1))).first()
        return RequirementsArtifact.model_validate(row[0]) if row else None
