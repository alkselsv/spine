"""Append-only PostgreSQL requirements acceptance decisions."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from spine.application.persistence.context import EnvironmentScope
from spine.application.persistence.errors import (
    ConstraintConflictError,
    InvalidPersistenceContextError,
)
from spine.application.requirements_acceptance import RequirementsAcceptanceReceipt
from spine.infrastructure.persistence.postgresql_mappings import (
    requirements_acceptance_decisions,
)


class PostgreSQLRequirementsAcceptanceRepository:
    def __init__(self, uow: object) -> None:
        self._uow = uow

    async def get(
        self, *, command_id: UUID
    ) -> RequirementsAcceptanceReceipt | None:
        self._uow._guard_active()
        scope = self._scope()
        row = (
            await self._uow._execute(
                select(requirements_acceptance_decisions.c.receipt).where(
                    requirements_acceptance_decisions.c.workspace_id
                    == scope.workspace_id,
                    requirements_acceptance_decisions.c.environment_id
                    == scope.environment_id,
                    requirements_acceptance_decisions.c.command_id == command_id,
                )
            )
        ).first()
        return (
            RequirementsAcceptanceReceipt.model_validate(row[0], strict=False)
            if row
            else None
        )

    async def create(
        self, receipt: RequirementsAcceptanceReceipt
    ) -> RequirementsAcceptanceReceipt:
        self._uow._guard_active()
        scope = self._scope()
        command = receipt.command_receipt
        if command.command_id.int == 0:
            raise InvalidPersistenceContextError(
                "Requirements acceptance decision is invalid."
            )
        statement = (
            insert(requirements_acceptance_decisions)
            .values(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                command_id=command.command_id,
                run_id=command.run_id,
                artifact_id=receipt.accepted_artifact_ref.artifact_id,
                artifact_version=receipt.accepted_artifact_ref.artifact_version,
                artifact_digest=receipt.accepted_artifact_digest,
                actor_id=receipt.actor.id,
                policy_version=receipt.policy_version,
                receipt=receipt.model_dump(mode="json"),
                created_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_nothing()
        )
        result = await self._uow._execute(statement)
        stored = await self.get(command_id=command.command_id)
        if stored is None:
            raise ConstraintConflictError(
                "Requirements acceptance decision was not persisted."
            )
        if result.rowcount != 1 and stored != receipt:
            raise ConstraintConflictError(
                "Requirements acceptance decision conflicts with prior outcome."
            )
        return stored

    def _scope(self) -> EnvironmentScope:
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            raise InvalidPersistenceContextError(
                "Requirements acceptance scope is invalid."
            )
        return scope


__all__ = ["PostgreSQLRequirementsAcceptanceRepository"]
