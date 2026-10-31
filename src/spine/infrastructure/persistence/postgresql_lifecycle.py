"""PostgreSQL adapter for immutable lifecycle evidence and authority CAS."""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from spine.application.lifecycle import (
    LifecycleAuthorityError,
    LifecycleAuthorityRepository,
    LifecycleConflictError,
    LifecycleDecisionAuthority,
    LifecycleDecisionEffectsWriter,
    LifecycleEligibilityQuery,
    LifecycleObservationAuthority,
    lifecycle_context_actor,
    validate_lifecycle_context,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import ConstraintConflictError
from spine.domain.sources.lifecycle import (
    CurrentLifecycleEligibility,
    LifecycleDecision,
    LifecycleEligibilityStatus,
    LifecycleObservation,
)
from spine.domain.sources.models import RevisionKind
from spine.infrastructure.persistence.postgresql_mappings import (
    current_lifecycle_authorities,
    current_source_heads,
    lifecycle_decisions,
    lifecycle_observations,
    source_objects,
)


class PostgreSQLLifecycleAuthorityRepository(LifecycleAuthorityRepository):
    """Record lifecycle evidence and update the current authority pointer atomically."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[TrustedPersistenceContext], object],
        observation_authority: LifecycleObservationAuthority,
        decision_authority: LifecycleDecisionAuthority,
        decision_effects: LifecycleDecisionEffectsWriter,
    ) -> None:
        self._uow_factory = uow_factory
        self._observation_authority = observation_authority
        self._decision_authority = decision_authority
        self._decision_effects = decision_effects

    async def record_observation(
        self,
        observation: LifecycleObservation,
        context: TrustedPersistenceContext,
    ) -> LifecycleObservation:
        validate_lifecycle_context(context, observation.workspace_id, observation.environment_id)
        try:
            async with self._uow_factory(context) as uow:
                existing_result = await uow._execute(  # type: ignore[attr-defined]
                    select(lifecycle_observations).where(
                        lifecycle_observations.c.observation_id == observation.observation_id,
                        lifecycle_observations.c.workspace_id == observation.workspace_id,
                        lifecycle_observations.c.environment_id == observation.environment_id,
                    )
                )
                existing_row = existing_result.mappings().one_or_none()
                if existing_row is not None:
                    existing = _observation(existing_row)
                    if existing != observation:
                        raise LifecycleConflictError()
                    await uow.rollback()  # type: ignore[attr-defined]
                    return existing
                await self._observation_authority.verify(observation, context, uow)
                await uow._execute(  # type: ignore[attr-defined]
                    pg_insert(lifecycle_observations)
                    .values(**_observation_columns(observation))
                    .on_conflict_do_nothing()
                )
                result = await uow._execute(  # type: ignore[attr-defined]
                    select(lifecycle_observations).where(
                        lifecycle_observations.c.observation_id == observation.observation_id,
                        lifecycle_observations.c.workspace_id == observation.workspace_id,
                        lifecycle_observations.c.environment_id == observation.environment_id,
                    )
                )
                row = result.mappings().one_or_none()
                if row is None:
                    raise LifecycleAuthorityError()
                existing = _observation(row)
                if existing != observation:
                    raise LifecycleConflictError()
                await uow.commit()  # type: ignore[attr-defined]
                return existing
        except (IntegrityError, ConstraintConflictError):
            raise LifecycleAuthorityError() from None

    async def record_decision(
        self,
        decision: LifecycleDecision,
        context: TrustedPersistenceContext,
    ) -> LifecycleDecision:
        validate_lifecycle_context(context, decision.workspace_id, decision.environment_id)
        if lifecycle_context_actor(context) != decision.actor_id:
            raise LifecycleAuthorityError()
        try:
            async with self._uow_factory(context) as uow:
                existing_result = await uow._execute(  # type: ignore[attr-defined]
                    select(lifecycle_decisions).where(
                        lifecycle_decisions.c.decision_id == decision.decision_id,
                        lifecycle_decisions.c.workspace_id == decision.workspace_id,
                        lifecycle_decisions.c.environment_id == decision.environment_id,
                    )
                )
                existing_row = existing_result.mappings().one_or_none()
                if existing_row is not None:
                    existing = _decision(existing_row)
                    if existing != decision:
                        raise LifecycleConflictError()
                    await uow.rollback()  # type: ignore[attr-defined]
                    return existing

                observation = await self._verify_observation(uow, decision)
                try:
                    decision.validate_observation(observation)
                except ValueError:
                    raise LifecycleAuthorityError() from None
                await self._decision_authority.authorize(decision, context, uow)
                pointer_result = await uow._execute(  # type: ignore[attr-defined]
                    select(current_lifecycle_authorities).where(
                        current_lifecycle_authorities.c.workspace_id == decision.workspace_id,
                        current_lifecycle_authorities.c.environment_id == decision.environment_id,
                        current_lifecycle_authorities.c.source_object_id == decision.source_object_id,
                    ).with_for_update()
                )
                pointer = pointer_result.mappings().one_or_none()
                current_id = pointer["current_decision_id"] if pointer else None
                current_generation = pointer["generation"] if pointer else 0
                if (
                    current_id != decision.expected_current_decision_id
                    or current_generation != decision.expected_current_generation
                    or decision.generation != current_generation + 1
                ):
                    raise LifecycleConflictError()

                audit_event_id, outbox_event_id = await self._decision_effects.persist(
                    decision, context, uow
                )
                if (audit_event_id, outbox_event_id) != (
                    decision.audit_event_id,
                    decision.outbox_event_id,
                ):
                    raise LifecycleAuthorityError()

                await uow._execute(  # type: ignore[attr-defined]
                    insert(lifecycle_decisions).values(**_decision_columns(decision))
                )
                pointer_insert = pg_insert(current_lifecycle_authorities).values(
                    workspace_id=decision.workspace_id,
                    environment_id=decision.environment_id,
                    source_object_id=decision.source_object_id,
                    current_decision_id=decision.decision_id,
                    predecessor_decision_id=decision.expected_current_decision_id,
                    generation=decision.generation,
                    updated_at=decision.decided_at,
                )
                pointer_write = pointer_insert.on_conflict_do_update(
                    index_elements=[
                        current_lifecycle_authorities.c.workspace_id,
                        current_lifecycle_authorities.c.environment_id,
                        current_lifecycle_authorities.c.source_object_id,
                    ],
                    set_={
                        "current_decision_id": pointer_insert.excluded.current_decision_id,
                        "predecessor_decision_id": pointer_insert.excluded.predecessor_decision_id,
                        "generation": pointer_insert.excluded.generation,
                        "updated_at": pointer_insert.excluded.updated_at,
                    },
                    where=(
                        current_lifecycle_authorities.c.current_decision_id.is_not_distinct_from(
                            decision.expected_current_decision_id
                        )
                        & (
                            current_lifecycle_authorities.c.generation
                            == decision.expected_current_generation
                        )
                    ),
                ).returning(current_lifecycle_authorities.c.current_decision_id)
                write_result = await uow._execute(pointer_write)  # type: ignore[attr-defined]
                if write_result.scalar_one_or_none() != decision.decision_id:
                    raise LifecycleConflictError()
                await uow.commit()  # type: ignore[attr-defined]
                return decision
        except (IntegrityError, ConstraintConflictError):
            raise LifecycleAuthorityError() from None

    async def current_eligibility(
        self,
        query: LifecycleEligibilityQuery,
        context: TrustedPersistenceContext,
    ) -> CurrentLifecycleEligibility:
        validate_lifecycle_context(context, query.workspace_id, query.environment_id)
        async with self._uow_factory(context) as uow:
            result = await uow._execute(  # type: ignore[attr-defined]
                select(
                    source_objects.c.connection_id.label("canonical_connection_id"),
                    current_source_heads.c.current_revision_kind,
                    lifecycle_decisions,
                )
                .select_from(
                    source_objects
                    .outerjoin(
                        current_source_heads,
                        (current_source_heads.c.source_object_id
                         == source_objects.c.source_object_id)
                        & (current_source_heads.c.workspace_id == source_objects.c.workspace_id)
                        & (current_source_heads.c.environment_id == source_objects.c.environment_id),
                    )
                    .outerjoin(
                        current_lifecycle_authorities,
                        (current_lifecycle_authorities.c.workspace_id
                         == source_objects.c.workspace_id)
                        & (current_lifecycle_authorities.c.environment_id
                           == source_objects.c.environment_id)
                        & (current_lifecycle_authorities.c.source_object_id
                           == source_objects.c.source_object_id),
                    )
                    .outerjoin(
                        lifecycle_decisions,
                        (lifecycle_decisions.c.decision_id
                         == current_lifecycle_authorities.c.current_decision_id)
                        & (lifecycle_decisions.c.workspace_id
                           == current_lifecycle_authorities.c.workspace_id)
                        & (lifecycle_decisions.c.environment_id
                           == current_lifecycle_authorities.c.environment_id)
                        & (lifecycle_decisions.c.source_object_id
                           == current_lifecycle_authorities.c.source_object_id)
                        & (lifecycle_decisions.c.generation
                           == current_lifecycle_authorities.c.generation),
                    )
                )
                .where(
                    source_objects.c.workspace_id == query.workspace_id,
                    source_objects.c.environment_id == query.environment_id,
                    source_objects.c.source_object_id == query.source_object_id,
                )
            )
            row = result.mappings().one_or_none()
            await uow.rollback()  # type: ignore[attr-defined]
            if (
                row is None
                or row["canonical_connection_id"] != query.connection_id
            ):
                return _unavailable(query)
            decision = _decision(row) if row["decision_id"] is not None else None
            if decision is None or not query.matches_decision(decision):
                return _unavailable(query)
            if row["current_revision_kind"] == RevisionKind.TOMBSTONE.value:
                return CurrentLifecycleEligibility(
                    workspace_id=decision.workspace_id,
                    environment_id=decision.environment_id,
                    source_object_id=decision.source_object_id,
                    connection_id=decision.connection_id,
                    connection_version_id=decision.connection_version_id,
                    scope_reference=decision.scope_reference,
                    scope_version_id=decision.scope_version_id,
                    scope_digest=decision.scope_digest,
                    current_decision_id=decision.decision_id,
                    generation=decision.generation,
                    status=LifecycleEligibilityStatus.TOMBSTONED,
                    legal_hold_active=decision.legal_hold_active,
                )
        return CurrentLifecycleEligibility(
            workspace_id=decision.workspace_id,
            environment_id=decision.environment_id,
            source_object_id=decision.source_object_id,
            connection_id=decision.connection_id,
            connection_version_id=decision.connection_version_id,
            scope_reference=decision.scope_reference,
            scope_version_id=decision.scope_version_id,
            scope_digest=decision.scope_digest,
            current_decision_id=decision.decision_id,
            generation=decision.generation,
            status=decision.status,
            legal_hold_active=decision.legal_hold_active,
        )

    async def _verify_observation(
        self, uow: object, decision: LifecycleDecision
    ) -> LifecycleObservation:
        result = await uow._execute(  # type: ignore[attr-defined]
            select(lifecycle_observations).where(
                lifecycle_observations.c.observation_id == decision.observation_id,
                lifecycle_observations.c.workspace_id == decision.workspace_id,
                lifecycle_observations.c.environment_id == decision.environment_id,
                lifecycle_observations.c.source_object_id == decision.source_object_id,
            )
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise LifecycleAuthorityError()
        return _observation(row)


def _observation_columns(observation: LifecycleObservation) -> dict[str, object]:
    return {
        "observation_id": observation.observation_id,
        "workspace_id": observation.workspace_id,
        "environment_id": observation.environment_id,
        "source_object_id": observation.source_object_id,
        "connection_id": observation.connection_id,
        "connection_version_id": observation.connection_version_id,
        "scope_reference": observation.scope_reference,
        "scope_version_id": observation.scope_version_id,
        "scope_digest": observation.scope_digest,
        "kind": observation.kind.value,
        "evidence_reference": observation.evidence_reference,
        "record": observation.model_dump(mode="json"),
        "observed_at": observation.observed_at,
    }


def _decision_columns(decision: LifecycleDecision) -> dict[str, object]:
    return {
        "decision_id": decision.decision_id,
        "workspace_id": decision.workspace_id,
        "environment_id": decision.environment_id,
        "source_object_id": decision.source_object_id,
        "observation_id": decision.observation_id,
        "connection_id": decision.connection_id,
        "connection_version_id": decision.connection_version_id,
        "scope_reference": decision.scope_reference,
        "scope_version_id": decision.scope_version_id,
        "scope_digest": decision.scope_digest,
        "effect": decision.effect.value,
        "status": decision.status.value,
        "authority_reference": decision.authority_reference,
        "authority_version_id": decision.authority_version_id,
        "authority_kind": decision.authority_kind.value,
        "actor_id": decision.actor_id,
        "audit_event_id": decision.audit_event_id,
        "outbox_event_id": decision.outbox_event_id,
        "generation": decision.generation,
        "record": decision.model_dump(mode="json"),
        "decided_at": decision.decided_at,
    }


def _observation(row: object) -> LifecycleObservation:
    return LifecycleObservation.model_validate_json(json.dumps(_record_value(row)))


def _decision(row: object) -> LifecycleDecision:
    return LifecycleDecision.model_validate_json(json.dumps(_record_value(row)))


def _record_value(row: object) -> object:
    try:
        return row["record"]  # type: ignore[index]
    except (KeyError, TypeError):
        return row._mapping["record"]  # type: ignore[attr-defined]


def _unavailable(query: LifecycleEligibilityQuery) -> CurrentLifecycleEligibility:
    return CurrentLifecycleEligibility(
        workspace_id=query.workspace_id,
        environment_id=query.environment_id,
        source_object_id=query.source_object_id,
        connection_id=query.connection_id,
        connection_version_id=query.connection_version_id,
        scope_reference=query.scope_reference,
        scope_version_id=query.scope_version_id,
        scope_digest=query.scope_digest,
        current_decision_id=None,
        generation=0,
        status=LifecycleEligibilityStatus.AUTHORITY_UNAVAILABLE,
        legal_hold_active=False,
    )


__all__ = ["PostgreSQLLifecycleAuthorityRepository"]
