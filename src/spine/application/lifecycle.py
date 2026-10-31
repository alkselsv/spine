"""Shared lifecycle recorder and current-eligibility reader for source adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.domain.sources.lifecycle import (
    CurrentLifecycleEligibility,
    LifecycleDecision,
    LifecycleEligibilityStatus,
    LifecycleObservation,
)


class LifecycleAuthorityError(RuntimeError):
    """Safe failure for unavailable, denied, stale, or mismatched lifecycle state."""

    def __init__(self) -> None:
        super().__init__("Lifecycle authority is unavailable.")


class LifecycleConflictError(LifecycleAuthorityError):
    """Expected-current lifecycle authority no longer matches."""


class LifecycleDecisionAuthority(Protocol):
    """Verify current governance in the supplied transaction and hold its fence.

    Implementations must use serialization compatible with governance revocation
    so authority cannot change between verification and the lifecycle CAS commit.
    """

    async def authorize(
        self,
        decision: LifecycleDecision,
        context: TrustedPersistenceContext,
        transaction: object | None,
    ) -> None: ...


class LifecycleDecisionEffectsWriter(Protocol):
    """Append the decision's safe Audit Event and outbox intent in the same UoW."""

    async def persist(
        self,
        decision: LifecycleDecision,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> tuple[UUID, UUID]: ...


class LifecycleObservationRecorder(Protocol):
    """Producer seam for immutable provider-neutral observation evidence."""

    async def record_observation(
        self,
        observation: LifecycleObservation,
        context: TrustedPersistenceContext,
    ) -> LifecycleObservation: ...


class CurrentLifecycleEligibilityReader(Protocol):
    """Consumer seam for the current governed source-eligibility authority."""

    async def current_eligibility(
        self,
        query: "LifecycleEligibilityQuery",
        context: TrustedPersistenceContext,
    ) -> CurrentLifecycleEligibility: ...


class LifecycleAuthorityRepository(
    LifecycleObservationRecorder, CurrentLifecycleEligibilityReader, Protocol
):
    """Append evidence/decisions and resolve current state through one authority."""

    async def record_decision(
        self,
        decision: LifecycleDecision,
        context: TrustedPersistenceContext,
    ) -> LifecycleDecision: ...


@dataclass(frozen=True, slots=True)
class LifecycleEligibilityQuery:
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    scope_reference: UUID
    scope_version_id: UUID
    scope_digest: str


@dataclass(frozen=True, slots=True)
class InMemoryLifecycleAuthorityRepository(LifecycleAuthorityRepository):
    """Deterministic adapter for application and contract tests."""

    decision_authority: LifecycleDecisionAuthority
    observations: dict[UUID, LifecycleObservation] = field(
        init=False, default_factory=dict, repr=False, compare=False
    )
    decisions: dict[UUID, LifecycleDecision] = field(
        init=False, default_factory=dict, repr=False, compare=False
    )
    current: dict[tuple[UUID, UUID, UUID], LifecycleDecision] = field(
        init=False, default_factory=dict, repr=False, compare=False
    )
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock, repr=False, compare=False)

    async def record_observation(
        self,
        observation: LifecycleObservation,
        context: TrustedPersistenceContext,
    ) -> LifecycleObservation:
        _validate_context(context, observation.workspace_id, observation.environment_id)
        key = observation.observation_id
        async with self._lock:
            existing = self.observations.get(key)
            if existing is not None and existing != observation:
                raise LifecycleConflictError()
            self.observations[key] = observation
            return existing or observation

    async def record_decision(
        self,
        decision: LifecycleDecision,
        context: TrustedPersistenceContext,
    ) -> LifecycleDecision:
        _validate_context(context, decision.workspace_id, decision.environment_id)
        if _context_actor(context) != decision.actor_id:
            raise LifecycleAuthorityError()
        key = (decision.workspace_id, decision.environment_id, decision.source_object_id)
        async with self._lock:
            existing = self.decisions.get(decision.decision_id)
            if existing is not None:
                if existing != decision:
                    raise LifecycleConflictError()
                return existing
            observation = self.observations.get(decision.observation_id)
            if observation is None or not _same_binding(decision, observation):
                raise LifecycleAuthorityError()
            current = self.current.get(key)
            current_id = current.decision_id if current is not None else None
            current_generation = current.generation if current is not None else 0
            if (
                decision.expected_current_decision_id != current_id
                or decision.expected_current_generation != current_generation
                or decision.generation != current_generation + 1
            ):
                raise LifecycleConflictError()
            await self.decision_authority.authorize(decision, context, None)
            self.decisions[decision.decision_id] = decision
            self.current[key] = decision
            return decision

    async def current_eligibility(
        self,
        query: LifecycleEligibilityQuery,
        context: TrustedPersistenceContext,
    ) -> CurrentLifecycleEligibility:
        _validate_context(context, query.workspace_id, query.environment_id)
        decision = self.current.get(
            (query.workspace_id, query.environment_id, query.source_object_id)
        )
        if decision is None or not _query_matches_decision(query, decision):
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


def _validate_context(
    context: TrustedPersistenceContext, workspace_id: UUID, environment_id: UUID
) -> None:
    context.validate_shape()
    if not isinstance(context.scope, EnvironmentScope) or (
        context.scope.workspace_id,
        context.scope.environment_id,
    ) != (workspace_id, environment_id):
        raise LifecycleAuthorityError()


def _context_actor(context: TrustedPersistenceContext) -> UUID | None:
    if context.origin.value == "interactive":
        return context.acting_subject_id
    return context.service_principal_id


def _same_binding(decision: LifecycleDecision, observation: LifecycleObservation) -> bool:
    return (
        decision.workspace_id,
        decision.environment_id,
        decision.source_object_id,
        decision.connection_id,
        decision.connection_version_id,
        decision.scope_reference,
        decision.scope_version_id,
        decision.scope_digest,
    ) == (
        observation.workspace_id,
        observation.environment_id,
        observation.source_object_id,
        observation.connection_id,
        observation.connection_version_id,
        observation.scope_reference,
        observation.scope_version_id,
        observation.scope_digest,
    ) and decision.observation_id == observation.observation_id


def _query_matches_decision(
    query: LifecycleEligibilityQuery, decision: LifecycleDecision
) -> bool:
    return (
        query.workspace_id,
        query.environment_id,
        query.source_object_id,
        query.connection_id,
        query.connection_version_id,
        query.scope_reference,
        query.scope_version_id,
        query.scope_digest,
    ) == (
        decision.workspace_id,
        decision.environment_id,
        decision.source_object_id,
        decision.connection_id,
        decision.connection_version_id,
        decision.scope_reference,
        decision.scope_version_id,
        decision.scope_digest,
    )


__all__ = [
    "CurrentLifecycleEligibilityReader",
    "InMemoryLifecycleAuthorityRepository",
    "LifecycleAuthorityError",
    "LifecycleAuthorityRepository",
    "LifecycleConflictError",
    "LifecycleDecisionAuthority",
    "LifecycleDecisionEffectsWriter",
    "LifecycleEligibilityQuery",
    "LifecycleObservationRecorder",
]
