"""Shared lifecycle recorder and current-eligibility reader for source adapters."""

from __future__ import annotations

import asyncio
import re
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


class LifecycleObservationAuthority(Protocol):
    """Verify source identity and current connection/scope authority in one UoW.

    Implementations bind the Source Object to its Provider Connection, then
    verify the exact active connection version, generations, approved scope
    version/digest and provider-identity reference. Revocation must serialize
    with lifecycle decisions through the same authority store.
    """

    async def verify(
        self,
        observation: LifecycleObservation,
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


async def require_current_read_eligibility(
    reader: CurrentLifecycleEligibilityReader,
    query: "LifecycleEligibilityQuery",
    context: TrustedPersistenceContext,
) -> CurrentLifecycleEligibility:
    """Fail closed unless the exact current S3 decision permits a source read."""

    current = await reader.current_eligibility(query, context)
    if (
        current.workspace_id,
        current.environment_id,
        current.source_object_id,
        current.connection_id,
        current.connection_version_id,
        current.scope_reference,
        current.scope_version_id,
        current.scope_digest,
    ) != (
        query.workspace_id,
        query.environment_id,
        query.source_object_id,
        query.connection_id,
        query.connection_version_id,
        query.scope_reference,
        query.scope_version_id,
        query.scope_digest,
    ) or (
        current.current_decision_id is None
        or current.status is not LifecycleEligibilityStatus.ELIGIBLE
    ):
        raise LifecycleAuthorityError()
    return current


@dataclass(frozen=True, slots=True)
class ScopeWithdrawalCommand:
    """A governance withdrawal bound to one source and exact approved scope."""

    observation: LifecycleObservation
    decision: LifecycleDecision

    def __post_init__(self) -> None:
        from spine.domain.sources.lifecycle import (
            LifecycleDecisionEffect,
            LifecycleObservationConfidence,
            LifecycleObservationKind,
        )

        if (
            self.observation.kind is not LifecycleObservationKind.SCOPE_WITHDRAWN
            or self.observation.confidence is not LifecycleObservationConfidence.QUALIFIED
            or self.decision.effect is not LifecycleDecisionEffect.READ_WITHDRAWN
            or self.decision.status is not LifecycleEligibilityStatus.READ_WITHDRAWN
        ):
            raise LifecycleAuthorityError()
        try:
            self.decision.validate_observation(self.observation)
        except ValueError:
            raise LifecycleAuthorityError() from None


@dataclass(frozen=True, slots=True)
class SourceScopeWithdrawal:
    """Record evidence, then advance S3-1 current authority through its CAS."""

    authority: LifecycleAuthorityRepository

    async def execute(
        self,
        command: ScopeWithdrawalCommand,
        context: TrustedPersistenceContext,
    ) -> LifecycleDecision:
        await self.authority.record_observation(command.observation, context)
        return await self.authority.record_decision(command.decision, context)


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
    provider_object_reference: str | None = None
    source_external_namespace: str | None = None
    source_external_generation: str | None = None
    source_external_object_id: str | None = None

    def __post_init__(self) -> None:
        if any(
            value.int == 0
            for value in (
                self.workspace_id,
                self.environment_id,
                self.source_object_id,
                self.connection_id,
                self.connection_version_id,
                self.scope_reference,
                self.scope_version_id,
            )
        ) or re.fullmatch(r"[0-9a-f]{64}", self.scope_digest) is None:
            raise LifecycleAuthorityError()
        if self.provider_object_reference is not None and not (
            1 <= len(self.provider_object_reference) <= 1024
        ):
            raise LifecycleAuthorityError()
        external_identity = (
            self.source_external_namespace,
            self.source_external_generation,
            self.source_external_object_id,
        )
        if any(value is not None for value in external_identity) and any(
            value is None or not value for value in external_identity
        ):
            raise LifecycleAuthorityError()

    def digest_payload(self) -> dict[str, object]:
        """Return the stable exact binding used by command idempotency digests."""

        payload: dict[str, object] = {
            "workspace_id": self.workspace_id,
            "environment_id": self.environment_id,
            "source_object_id": self.source_object_id,
            "connection_id": self.connection_id,
            "connection_version_id": self.connection_version_id,
            "scope_reference": self.scope_reference,
            "scope_version_id": self.scope_version_id,
            "scope_digest": self.scope_digest,
        }
        if self.provider_object_reference is not None:
            payload["provider_object_reference"] = self.provider_object_reference
        if self.source_external_namespace is not None:
            payload["source_external_namespace"] = self.source_external_namespace
            payload["source_external_generation"] = self.source_external_generation
            payload["source_external_object_id"] = self.source_external_object_id
        return payload

    def matches_decision(self, decision: LifecycleDecision) -> bool:
        return (
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.connection_id,
            self.connection_version_id,
            self.scope_reference,
            self.scope_version_id,
            self.scope_digest,
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


@dataclass(frozen=True, slots=True)
class InMemoryLifecycleAuthorityRepository(LifecycleAuthorityRepository):
    """Deterministic adapter for application and contract tests."""

    observation_authority: LifecycleObservationAuthority
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
    canonical_tombstones: set[tuple[UUID, UUID, UUID]] = field(
        init=False, default_factory=set, repr=False, compare=False
    )
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock, repr=False, compare=False)

    async def record_observation(
        self,
        observation: LifecycleObservation,
        context: TrustedPersistenceContext,
    ) -> LifecycleObservation:
        validate_lifecycle_context(context, observation.workspace_id, observation.environment_id)
        key = observation.observation_id
        async with self._lock:
            existing = self.observations.get(key)
            if existing is not None and existing != observation:
                raise LifecycleConflictError()
            if existing is not None:
                return existing
            await self.observation_authority.verify(observation, context, None)
            self.observations[key] = observation
            return observation

    async def record_decision(
        self,
        decision: LifecycleDecision,
        context: TrustedPersistenceContext,
    ) -> LifecycleDecision:
        validate_lifecycle_context(context, decision.workspace_id, decision.environment_id)
        if lifecycle_context_actor(context) != decision.actor_id:
            raise LifecycleAuthorityError()
        key = (decision.workspace_id, decision.environment_id, decision.source_object_id)
        async with self._lock:
            existing = self.decisions.get(decision.decision_id)
            if existing is not None:
                if existing != decision:
                    raise LifecycleConflictError()
                return existing
            observation = self.observations.get(decision.observation_id)
            if observation is None:
                raise LifecycleAuthorityError()
            try:
                decision.validate_observation(observation)
            except ValueError:
                raise LifecycleAuthorityError() from None
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
        validate_lifecycle_context(context, query.workspace_id, query.environment_id)
        decision = self.current.get(
            (query.workspace_id, query.environment_id, query.source_object_id)
        )
        if decision is None or not query.matches_decision(decision):
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
        if (query.workspace_id, query.environment_id, query.source_object_id) in self.canonical_tombstones:
            status = LifecycleEligibilityStatus.TOMBSTONED
        else:
            status = decision.status
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
            status=status,
            legal_hold_active=decision.legal_hold_active,
        )


def validate_lifecycle_context(
    context: TrustedPersistenceContext, workspace_id: UUID, environment_id: UUID
) -> None:
    context.validate_shape()
    if not isinstance(context.scope, EnvironmentScope) or (
        context.scope.workspace_id,
        context.scope.environment_id,
    ) != (workspace_id, environment_id):
        raise LifecycleAuthorityError()


def lifecycle_context_actor(context: TrustedPersistenceContext) -> UUID | None:
    if context.origin.value == "interactive":
        return context.acting_subject_id
    return context.service_principal_id


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
    "LifecycleObservationAuthority",
    "ScopeWithdrawalCommand",
    "SourceScopeWithdrawal",
    "lifecycle_context_actor",
    "require_current_read_eligibility",
    "validate_lifecycle_context",
]
