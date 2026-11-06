"""PostgreSQL persistence for immutable retention applicability authority."""

from __future__ import annotations

import json
from typing import Any, Protocol

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.engine import Result
from sqlalchemy.exc import IntegrityError

from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.errors import ConstraintConflictError
from spine.application.persistence.unit_of_work import UnitOfWork
from spine.application.retention import (
    RetentionApplicabilityAuthority,
    RetentionApplicabilityConflictError,
    RetentionApplicabilityEffectsWriter,
    RetentionApplicabilityError,
    RetentionSourceLineage,
)
from spine.domain.sources.retention import RetentionApplicability, RetentionTargetKind
from spine.infrastructure.persistence.postgresql_mappings import (
    current_retention_applicabilities,
    retention_applicabilities,
    source_objects,
    source_revisions,
)
from spine.infrastructure.persistence.postgresql_source_locks import (
    lock_retention_source_class_exclusive,
    lock_source_lifecycle_exclusive,
)


class PostgreSQLRetentionUnitOfWork(UnitOfWork, Protocol):
    async def execute_statement(
        self, statement: object, parameters: object | None = None
    ) -> Result[Any]: ...

    async def __aenter__(self) -> "PostgreSQLRetentionUnitOfWork": ...


class PostgreSQLRetentionUnitOfWorkFactory(Protocol):
    def __call__(
        self, context: TrustedPersistenceContext
    ) -> PostgreSQLRetentionUnitOfWork: ...


class PostgreSQLRetentionApplicabilityRepository:
    def __init__(
        self,
        *,
        uow_factory: PostgreSQLRetentionUnitOfWorkFactory,
        authority: RetentionApplicabilityAuthority,
        effects: RetentionApplicabilityEffectsWriter,
    ) -> None:
        self._uow_factory = uow_factory
        self._authority = authority
        self._effects = effects

    async def record_applicability(
        self,
        applicability: RetentionApplicability,
        context: TrustedPersistenceContext,
    ) -> RetentionApplicability:
        _validate_context(context, applicability.workspace_id, applicability.environment_id)
        actor_id = context.acting_subject_id or context.service_principal_id
        if actor_id != applicability.actor_id:
            raise RetentionApplicabilityError()
        try:
            async with self._uow_factory(context) as uow:
                if applicability.target_kind is RetentionTargetKind.EXACT_SOURCE:
                    assert applicability.source_object_id is not None
                    await lock_source_lifecycle_exclusive(
                        uow.execute_statement,
                        workspace_id=applicability.workspace_id,
                        environment_id=applicability.environment_id,
                        source_object_id=applicability.source_object_id,
                    )
                else:
                    assert applicability.source_class is not None
                    await lock_retention_source_class_exclusive(
                        uow.execute_statement,
                        workspace_id=applicability.workspace_id,
                        environment_id=applicability.environment_id,
                        source_class=applicability.source_class,
                    )
                existing_result = await uow.execute_statement(
                    select(retention_applicabilities).where(
                        retention_applicabilities.c.applicability_id
                        == applicability.applicability_id,
                        retention_applicabilities.c.workspace_id
                        == applicability.workspace_id,
                        retention_applicabilities.c.environment_id
                        == applicability.environment_id,
                    )
                )
                existing_row = existing_result.mappings().one_or_none()
                if existing_row is not None:
                    existing = _binding(existing_row)
                    if existing != applicability:
                        raise RetentionApplicabilityConflictError()
                    await uow.rollback()
                    return existing

                await self._authority.verify(applicability, context, uow)
                pointer_result = await uow.execute_statement(
                    select(current_retention_applicabilities)
                    .where(
                        current_retention_applicabilities.c.workspace_id
                        == applicability.workspace_id,
                        current_retention_applicabilities.c.environment_id
                        == applicability.environment_id,
                        current_retention_applicabilities.c.target_kind
                        == applicability.target_kind.value,
                        current_retention_applicabilities.c.target_reference
                        == applicability.target_reference,
                    )
                    .with_for_update()
                )
                pointer = pointer_result.mappings().one_or_none()
                current_id = pointer["current_applicability_id"] if pointer else None
                current_generation = pointer["generation"] if pointer else 0
                if (
                    current_id != applicability.expected_current_applicability_id
                    or current_generation != applicability.expected_current_generation
                    or applicability.generation != current_generation + 1
                ):
                    raise RetentionApplicabilityConflictError()
                audit_id, outbox_id = await self._effects.persist(
                    applicability, context, uow
                )
                if (audit_id, outbox_id) != (
                    applicability.audit_event_id,
                    applicability.outbox_event_id,
                ):
                    raise RetentionApplicabilityError()
                await uow.execute_statement(
                    insert(retention_applicabilities).values(
                        **_binding_columns(applicability)
                    )
                )
                pointer_values = {
                    "workspace_id": applicability.workspace_id,
                    "environment_id": applicability.environment_id,
                    "target_kind": applicability.target_kind.value,
                    "target_reference": applicability.target_reference,
                    "current_applicability_id": applicability.applicability_id,
                    "predecessor_applicability_id": applicability.expected_current_applicability_id,
                    "generation": applicability.generation,
                    "updated_at": applicability.approved_at,
                }
                if pointer is None:
                    statement = insert(current_retention_applicabilities).values(
                        **pointer_values
                    ).returning(current_retention_applicabilities.c.current_applicability_id)
                else:
                    statement = update(current_retention_applicabilities).where(
                        current_retention_applicabilities.c.workspace_id
                        == applicability.workspace_id,
                        current_retention_applicabilities.c.environment_id
                        == applicability.environment_id,
                        current_retention_applicabilities.c.target_kind
                        == applicability.target_kind.value,
                        current_retention_applicabilities.c.target_reference
                        == applicability.target_reference,
                        current_retention_applicabilities.c.current_applicability_id
                        == applicability.expected_current_applicability_id,
                        current_retention_applicabilities.c.generation
                        == applicability.expected_current_generation,
                    ).values(
                        current_applicability_id=applicability.applicability_id,
                        predecessor_applicability_id=(
                            applicability.expected_current_applicability_id
                        ),
                        generation=applicability.generation,
                        updated_at=applicability.approved_at,
                    ).returning(current_retention_applicabilities.c.current_applicability_id)
                result = await uow.execute_statement(statement)
                if result.scalar_one_or_none() != applicability.applicability_id:
                    raise RetentionApplicabilityConflictError()
                await uow.commit()
                return applicability
        except (IntegrityError, ConstraintConflictError):
            try:
                async with self._uow_factory(context) as uow:
                    current_result = await uow.execute_statement(
                        select(
                            current_retention_applicabilities.c.current_applicability_id,
                            current_retention_applicabilities.c.generation,
                        ).where(
                            current_retention_applicabilities.c.workspace_id
                            == applicability.workspace_id,
                            current_retention_applicabilities.c.environment_id
                            == applicability.environment_id,
                            current_retention_applicabilities.c.target_kind
                            == applicability.target_kind.value,
                            current_retention_applicabilities.c.target_reference
                            == applicability.target_reference,
                        )
                    )
                    current = current_result.mappings().one_or_none()
                    await uow.rollback()
                if current is not None and (
                    current["current_applicability_id"]
                    != applicability.expected_current_applicability_id
                    or current["generation"] != applicability.expected_current_generation
                ):
                    raise RetentionApplicabilityConflictError() from None
            except RetentionApplicabilityConflictError:
                raise
            except Exception:
                pass
            raise RetentionApplicabilityError() from None

    async def list_current_applicabilities(
        self,
        source: RetentionSourceLineage,
        context: TrustedPersistenceContext,
    ) -> tuple[RetentionApplicability, ...]:
        scope = context.scope
        if not isinstance(scope, EnvironmentScope):
            return ()
        _validate_context(context, scope.workspace_id, scope.environment_id)
        async with self._uow_factory(context) as uow:
            lineage_result = await uow.execute_statement(
                select(source_objects.c.source_kind)
                .select_from(
                    source_objects.join(
                        source_revisions,
                        and_(
                            source_revisions.c.workspace_id == source_objects.c.workspace_id,
                            source_revisions.c.environment_id == source_objects.c.environment_id,
                            source_revisions.c.source_object_id == source_objects.c.source_object_id,
                        ),
                    )
                )
                .where(
                    source_objects.c.workspace_id == scope.workspace_id,
                    source_objects.c.environment_id == scope.environment_id,
                    source_objects.c.source_object_id == source.source_object_id,
                    source_revisions.c.revision_id == source.source_revision_id,
                    source_objects.c.source_kind == source.source_class,
                )
            )
            if lineage_result.scalar_one_or_none() is None:
                await uow.rollback()
                return ()
            result = await uow.execute_statement(
                select(retention_applicabilities)
                .select_from(
                    current_retention_applicabilities.join(
                        retention_applicabilities,
                        and_(
                            retention_applicabilities.c.workspace_id
                            == current_retention_applicabilities.c.workspace_id,
                            retention_applicabilities.c.environment_id
                            == current_retention_applicabilities.c.environment_id,
                            retention_applicabilities.c.target_kind
                            == current_retention_applicabilities.c.target_kind,
                            retention_applicabilities.c.target_reference
                            == current_retention_applicabilities.c.target_reference,
                            retention_applicabilities.c.applicability_id
                            == current_retention_applicabilities.c.current_applicability_id,
                            retention_applicabilities.c.generation
                            == current_retention_applicabilities.c.generation,
                        ),
                    )
                )
                .where(
                    retention_applicabilities.c.workspace_id == scope.workspace_id,
                    retention_applicabilities.c.environment_id == scope.environment_id,
                    or_(
                        and_(
                            retention_applicabilities.c.target_kind
                            == RetentionTargetKind.EXACT_SOURCE.value,
                            retention_applicabilities.c.source_object_id
                            == source.source_object_id,
                        ),
                        and_(
                            retention_applicabilities.c.target_kind
                            == RetentionTargetKind.SOURCE_CLASS.value,
                            retention_applicabilities.c.source_class == source.source_class,
                        ),
                    ),
                )
            )
            rows = result.mappings().all()
            await uow.rollback()
        return tuple(_binding(row) for row in rows)


def _validate_context(
    context: TrustedPersistenceContext, workspace_id: object, environment_id: object
) -> None:
    try:
        context.validate_shape()
    except Exception:
        raise RetentionApplicabilityError() from None
    if not isinstance(context.scope, EnvironmentScope) or (
        context.scope.workspace_id,
        context.scope.environment_id,
    ) != (workspace_id, environment_id):
        raise RetentionApplicabilityError()


def _binding_columns(binding: RetentionApplicability) -> dict[str, object]:
    return {
        "applicability_id": binding.applicability_id,
        "workspace_id": binding.workspace_id,
        "environment_id": binding.environment_id,
        "target_kind": binding.target_kind.value,
        "target_reference": binding.target_reference,
        "source_object_id": binding.source_object_id,
        "source_class": binding.source_class,
        "source_class_reference": binding.source_class_reference,
        "source_class_version_id": binding.source_class_version_id,
        "source_class_digest": binding.source_class_digest,
        "retention_policy_id": binding.retention_policy_id,
        "retention_policy_version_id": binding.retention_policy_version_id,
        "retention_policy_digest": binding.retention_policy_digest,
        "generation": binding.generation,
        "audit_event_id": binding.audit_event_id,
        "outbox_event_id": binding.outbox_event_id,
        "record": binding.model_dump(mode="json"),
        "approved_at": binding.approved_at,
    }


def _binding(row: object) -> RetentionApplicability:
    try:
        record = row["record"]  # type: ignore[index]
    except (KeyError, TypeError):
        record = row._mapping["record"]  # type: ignore[attr-defined]
    return RetentionApplicability.model_validate_json(json.dumps(record))


__all__ = [
    "PostgreSQLRetentionApplicabilityRepository",
    "PostgreSQLRetentionUnitOfWork",
    "PostgreSQLRetentionUnitOfWorkFactory",
]
