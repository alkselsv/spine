"""Canonical lifecycle and disclosure-hold reads for disclosure checkpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select

from spine.domain.disclosure import DisclosureDependency
from spine.infrastructure.persistence.postgresql_mappings import (
    current_disclosure_holds,
    current_lifecycle_authorities,
    disclosure_hold_releases,
    disclosure_holds,
    lifecycle_decisions,
)


async def resolve_final_disclosure_constraints(
    uow: object,
    dependency: DisclosureDependency,
    *,
    artifact_id: UUID,
    evaluated_at: datetime,
) -> tuple[bool, bool]:
    """Return current lifecycle eligibility and an exact active-hold match.

    The caller already owns a tenant-scoped UoW. Any unavailable or malformed
    authority read raises so the enclosing disclosure revalidation fails closed.
    """

    lifecycle_result = await uow._execute(  # type: ignore[attr-defined]
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
            current_lifecycle_authorities.c.workspace_id == dependency.workspace_id,
            current_lifecycle_authorities.c.environment_id == dependency.environment_id,
            current_lifecycle_authorities.c.source_object_id
            == dependency.source_object_id,
        )
    )
    lifecycle_status = lifecycle_result.scalar_one_or_none()

    hold_result = await uow._execute(  # type: ignore[attr-defined]
        select(disclosure_holds.c.hold_id)
        .select_from(
            disclosure_holds.join(
                current_disclosure_holds,
                (
                    disclosure_holds.c.workspace_id
                    == current_disclosure_holds.c.workspace_id
                )
                & (
                    disclosure_holds.c.environment_id
                    == current_disclosure_holds.c.environment_id
                )
                & (
                    disclosure_holds.c.source_object_id
                    == current_disclosure_holds.c.source_object_id
                )
                & (disclosure_holds.c.hold_id == current_disclosure_holds.c.hold_id),
            ).outerjoin(
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
            disclosure_holds.c.workspace_id == dependency.workspace_id,
            disclosure_holds.c.environment_id == dependency.environment_id,
            disclosure_holds.c.source_object_id == dependency.source_object_id,
            disclosure_holds.c.source_revision_ids.any(dependency.source_revision_id),
            disclosure_holds.c.dependency_ids.any(dependency.dependency_id),
            disclosure_holds.c.artifact_ids.any(artifact_id),
            disclosure_holds.c.prohibited_effects.overlap(
                ["final_disclosure", "derived_artifact_disclosure"]
            ),
            disclosure_holds.c.effective_from <= evaluated_at,
            disclosure_holds.c.effective_until > evaluated_at,
            or_(
                current_disclosure_holds.c.state == "active",
                disclosure_hold_releases.c.released_at > evaluated_at,
            ),
        )
        .limit(1)
    )
    return lifecycle_status == "eligible", hold_result.scalar_one_or_none() is not None


async def retrieval_disclosure_allowed(
    uow: object,
    *,
    workspace_id: UUID,
    environment_id: UUID,
    source_object_id: UUID,
    source_revision_id: UUID,
    dependency_id: UUID,
    artifact_id: UUID,
    evaluated_at: datetime,
) -> bool:
    """Fail closed unless lifecycle is eligible and no exact retrieval hold applies."""

    lifecycle_result = await uow._execute(  # type: ignore[attr-defined]
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
        )
    )
    if lifecycle_result.scalar_one_or_none() != "eligible":
        return False
    hold_result = await uow._execute(  # type: ignore[attr-defined]
        select(disclosure_holds.c.hold_id)
        .select_from(
            disclosure_holds.join(
                current_disclosure_holds,
                (
                    disclosure_holds.c.workspace_id
                    == current_disclosure_holds.c.workspace_id
                )
                & (
                    disclosure_holds.c.environment_id
                    == current_disclosure_holds.c.environment_id
                )
                & (
                    disclosure_holds.c.source_object_id
                    == current_disclosure_holds.c.source_object_id
                )
                & (disclosure_holds.c.hold_id == current_disclosure_holds.c.hold_id),
            ).outerjoin(
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
            disclosure_holds.c.source_revision_ids.any(source_revision_id),
            disclosure_holds.c.dependency_ids.any(dependency_id),
            disclosure_holds.c.artifact_ids.any(artifact_id),
            disclosure_holds.c.prohibited_effects.overlap(["retrieval", "model_use"]),
            disclosure_holds.c.effective_from <= evaluated_at,
            disclosure_holds.c.effective_until > evaluated_at,
            or_(
                current_disclosure_holds.c.state == "active",
                disclosure_hold_releases.c.released_at > evaluated_at,
            ),
        )
        .limit(1)
    )
    return hold_result.scalar_one_or_none() is None


__all__ = ["resolve_final_disclosure_constraints", "retrieval_disclosure_allowed"]
