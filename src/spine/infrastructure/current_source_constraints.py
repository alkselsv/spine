"""PostgreSQL current lifecycle, retention, and hold constraints."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import and_, or_, select

from spine.domain.sources.retention import (
    RetentionApplicability,
    RetentionArtifactClass,
    RetentionTargetKind,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    current_disclosure_holds,
    current_lifecycle_authorities,
    current_retention_applicabilities,
    current_source_heads,
    disclosure_hold_releases,
    disclosure_hold_targets,
    disclosure_holds,
    lifecycle_decisions,
    retention_applicabilities,
    source_objects,
    source_revisions,
)
from spine.infrastructure.persistence.postgresql_source_locks import (
    lock_retention_source_class_shared,
    lock_source_lifecycle_shared,
)


class _ScalarResult(Protocol):
    def scalar_one_or_none(self) -> object | None: ...


class CurrentConstraintUnitOfWork(Protocol):
    async def _execute(
        self, statement: object, parameters: object | None = None
    ) -> _ScalarResult: ...


async def lock_source_usage_currentness(
    uow: CurrentConstraintUnitOfWork,
    *,
    workspace_id: UUID,
    environment_id: UUID,
    source_object_id: UUID,
) -> bool:
    """Serialize canonical/lifecycle consumers with current-state writers."""

    await lock_source_lifecycle_shared(
        uow._execute,
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=source_object_id,
    )
    result = await uow._execute(
        select(current_source_heads.c.source_object_id)
        .where(
            current_source_heads.c.workspace_id == workspace_id,
            current_source_heads.c.environment_id == environment_id,
            current_source_heads.c.source_object_id == source_object_id,
        )
        .with_for_update(of=current_source_heads)
    )
    return result.scalar_one_or_none() == source_object_id


async def current_lifecycle_allows(
    uow: CurrentConstraintUnitOfWork,
    *,
    workspace_id: UUID,
    environment_id: UUID,
    source_object_id: UUID,
    expected_decision_id: UUID | None = None,
    expected_generation: int | None = None,
) -> bool:
    """Resolve one exact current lifecycle authority, never historical state."""

    if (expected_decision_id is None) != (expected_generation is None):
        return False
    filters = []
    if expected_decision_id is not None:
        filters.extend(
            (
                current_lifecycle_authorities.c.current_decision_id
                == expected_decision_id,
                current_lifecycle_authorities.c.generation == expected_generation,
            )
        )
    result = await uow._execute(
        select(lifecycle_decisions.c.status)
        .select_from(
            current_lifecycle_authorities.join(
                lifecycle_decisions,
                (
                    lifecycle_decisions.c.workspace_id
                    == current_lifecycle_authorities.c.workspace_id
                )
                & (
                    lifecycle_decisions.c.environment_id
                    == current_lifecycle_authorities.c.environment_id
                )
                & (
                    lifecycle_decisions.c.source_object_id
                    == current_lifecycle_authorities.c.source_object_id
                )
                & (
                    lifecycle_decisions.c.decision_id
                    == current_lifecycle_authorities.c.current_decision_id
                )
                & (
                    lifecycle_decisions.c.generation
                    == current_lifecycle_authorities.c.generation
                ),
            )
        )
        .where(
            current_lifecycle_authorities.c.workspace_id == workspace_id,
            current_lifecycle_authorities.c.environment_id == environment_id,
            current_lifecycle_authorities.c.source_object_id == source_object_id,
            *filters,
        )
        .with_for_update(of=current_lifecycle_authorities)
    )
    return result.scalar_one_or_none() == "eligible"


async def projection_hold_allows(
    uow: CurrentConstraintUnitOfWork,
    *,
    workspace_id: UUID,
    environment_id: UUID,
    source_object_id: UUID,
    source_revision_id: UUID,
    dependency_id: UUID,
    artifact_id: UUID,
    evaluated_at: datetime,
    prohibited_effects: tuple[str, ...],
) -> bool:
    """Deny model/projection use when an exact current hold is effective."""

    result = await uow._execute(
        select(disclosure_holds.c.hold_id)
        .select_from(
            disclosure_holds.join(
                disclosure_hold_targets,
                (
                    disclosure_hold_targets.c.workspace_id
                    == disclosure_holds.c.workspace_id
                )
                & (
                    disclosure_hold_targets.c.environment_id
                    == disclosure_holds.c.environment_id
                )
                & (
                    disclosure_hold_targets.c.source_object_id
                    == disclosure_holds.c.source_object_id
                )
                & (disclosure_hold_targets.c.hold_id == disclosure_holds.c.hold_id),
            )
            .join(
                current_disclosure_holds,
                (
                    current_disclosure_holds.c.workspace_id
                    == disclosure_holds.c.workspace_id
                )
                & (
                    current_disclosure_holds.c.environment_id
                    == disclosure_holds.c.environment_id
                )
                & (
                    current_disclosure_holds.c.source_object_id
                    == disclosure_holds.c.source_object_id
                )
                & (current_disclosure_holds.c.hold_id == disclosure_holds.c.hold_id),
            )
            .outerjoin(
                disclosure_hold_releases,
                (
                    disclosure_hold_releases.c.workspace_id
                    == current_disclosure_holds.c.workspace_id
                )
                & (
                    disclosure_hold_releases.c.environment_id
                    == current_disclosure_holds.c.environment_id
                )
                & (
                    disclosure_hold_releases.c.source_object_id
                    == current_disclosure_holds.c.source_object_id
                )
                & (
                    disclosure_hold_releases.c.release_id
                    == current_disclosure_holds.c.current_release_id
                ),
            )
        )
        .where(
            disclosure_holds.c.workspace_id == workspace_id,
            disclosure_holds.c.environment_id == environment_id,
            disclosure_holds.c.source_object_id == source_object_id,
            disclosure_hold_targets.c.source_revision_id == source_revision_id,
            disclosure_hold_targets.c.dependency_id == dependency_id,
            disclosure_hold_targets.c.artifact_id == artifact_id,
            disclosure_holds.c.prohibited_effects.overlap(list(prohibited_effects)),
            disclosure_holds.c.effective_from <= evaluated_at,
            disclosure_holds.c.effective_until > evaluated_at,
            or_(
                current_disclosure_holds.c.state == "active",
                disclosure_hold_releases.c.released_at > evaluated_at,
            ),
        )
        .with_for_update(of=current_disclosure_holds)
        .limit(1)
    )
    return result.scalar_one_or_none() is None


async def current_retention_allows(
    uow: CurrentConstraintUnitOfWork,
    *,
    workspace_id: UUID,
    environment_id: UUID,
    source_object_id: UUID,
    source_revision_id: UUID,
    artifact_class: RetentionArtifactClass,
    evaluated_at: datetime,
    expected_applicability_id: UUID | None = None,
    expected_generation: int | None = None,
) -> bool:
    """Require exactly one current, effective binding for exact lineage."""

    if (expected_applicability_id is None) != (expected_generation is None):
        return False
    lineage = await uow._execute(
        select(source_objects.c.source_kind)
        .select_from(
            source_objects.join(
                source_revisions,
                (source_revisions.c.workspace_id == source_objects.c.workspace_id)
                & (source_revisions.c.environment_id == source_objects.c.environment_id)
                & (
                    source_revisions.c.source_object_id
                    == source_objects.c.source_object_id
                ),
            )
        )
        .where(
            source_objects.c.workspace_id == workspace_id,
            source_objects.c.environment_id == environment_id,
            source_objects.c.source_object_id == source_object_id,
            source_revisions.c.revision_id == source_revision_id,
        )
    )
    source_class = lineage.scalar_one_or_none()
    if source_class is None:
        return False
    await lock_retention_source_class_shared(
        uow._execute,
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_class=source_class,
    )
    result = await uow._execute(
        select(retention_applicabilities.c.record)
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
            retention_applicabilities.c.workspace_id == workspace_id,
            retention_applicabilities.c.environment_id == environment_id,
            or_(
                and_(
                    retention_applicabilities.c.target_kind
                    == RetentionTargetKind.EXACT_SOURCE.value,
                    retention_applicabilities.c.source_object_id == source_object_id,
                ),
                and_(
                    retention_applicabilities.c.target_kind
                    == RetentionTargetKind.SOURCE_CLASS.value,
                    retention_applicabilities.c.source_class == source_class,
                ),
            ),
        )
        .with_for_update(of=current_retention_applicabilities)
    )
    try:
        rows = result.scalars().all()  # type: ignore[attr-defined]
    except AttributeError:
        value = result.scalar_one_or_none()
        rows = [] if value is None else [value]
    matches = []
    for record in rows:
        try:
            payload = json.loads(record) if isinstance(record, str) else record
            binding = RetentionApplicability.model_validate(payload)
        except Exception:
            return False
        if artifact_class in binding.artifact_classes:
            matches.append(binding)
    if len(matches) != 1:
        return False
    binding = matches[0]
    return (
        binding.effective_from <= evaluated_at
        and (
            binding.effective_until is None
            or evaluated_at < binding.effective_until
        )
        and (
            expected_applicability_id is None
            or (
                binding.applicability_id == expected_applicability_id
                and binding.generation == expected_generation
            )
        )
    )


__all__ = [
    "current_lifecycle_allows",
    "current_retention_allows",
    "lock_source_usage_currentness",
    "projection_hold_allows",
]
