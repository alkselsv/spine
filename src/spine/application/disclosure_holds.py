"""Authorized disclosure-hold commands and deny-only current-state guard."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from spine.application.lifecycle import (
    CurrentLifecycleEligibilityReader,
    LifecycleEligibilityQuery,
    lifecycle_context_actor,
    validate_lifecycle_context,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.domain.common import ContextOrigin
from spine.domain.sources.disclosure_holds import (
    DisclosureHold,
    DisclosureHoldEffect,
    DisclosureHoldRelease,
)
from spine.domain.sources.lifecycle import (
    LifecycleDecision,
    LifecycleDecisionEffect,
    LifecycleEligibilityStatus,
    LifecycleObservation,
)


class DisclosureHoldAuthorityError(RuntimeError):
    """Disclosure-safe failure for missing or mismatched hold authority."""

    def __init__(self) -> None:
        super().__init__("Disclosure hold authority is unavailable.")


class DisclosureHoldConflictError(DisclosureHoldAuthorityError):
    """An immutable identity or current release state conflicts."""


class DisclosureConstraintError(RuntimeError):
    """Safe deny returned by retrieval and final-disclosure consumers."""

    def __init__(self) -> None:
        super().__init__("Disclosure is unavailable.")


@dataclass(frozen=True, slots=True)
class CreateDisclosureHoldCommand:
    hold: DisclosureHold
    observation: LifecycleObservation
    review_decision: LifecycleDecision
    lifecycle_query: LifecycleEligibilityQuery

    def __post_init__(self) -> None:
        if (
            self.hold.workspace_id,
            self.hold.environment_id,
            self.hold.source_object_id,
            self.hold.review_decision_id,
            self.hold.evidence_observation_id,
        ) != (
            self.review_decision.workspace_id,
            self.review_decision.environment_id,
            self.review_decision.source_object_id,
            self.review_decision.decision_id,
            self.observation.observation_id,
        ) or not self.lifecycle_query.matches_decision(
            self.review_decision
        ):
            raise DisclosureHoldAuthorityError()
        if (
            self.review_decision.effect
            is not LifecycleDecisionEffect.DISCLOSURE_REVIEW_REQUIRED
            or self.review_decision.status
            is not LifecycleEligibilityStatus.DISCLOSURE_REVIEW_REQUIRED
        ):
            raise DisclosureHoldAuthorityError()
        try:
            self.review_decision.validate_observation(self.observation)
        except ValueError:
            raise DisclosureHoldAuthorityError() from None


@dataclass(frozen=True, slots=True)
class ReleaseDisclosureHoldCommand:
    release: DisclosureHoldRelease


@dataclass(frozen=True, slots=True)
class DisclosureConstraintQuery:
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    dependency_id: UUID
    artifact_id: UUID
    effect: DisclosureHoldEffect
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if (
            any(
                value.int == 0
                for value in (
                    self.workspace_id,
                    self.environment_id,
                    self.source_object_id,
                    self.source_revision_id,
                    self.dependency_id,
                    self.artifact_id,
                )
            )
            or self.evaluated_at.tzinfo is None
            or self.evaluated_at.utcoffset() is None
        ):
            raise DisclosureHoldAuthorityError()


@dataclass(frozen=True, slots=True)
class CurrentDisclosureHolds:
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    hold_ids: tuple[UUID, ...]

    @property
    def denied(self) -> bool:
        return bool(self.hold_ids)


class DisclosureHoldAuthority(Protocol):
    """Verify delegated source-governance authority in the persistence UoW."""

    async def authorize_create(
        self,
        hold: DisclosureHold,
        context: TrustedPersistenceContext,
        transaction: object | None,
    ) -> None: ...

    async def authorize_release(
        self,
        release: DisclosureHoldRelease,
        hold: DisclosureHold,
        context: TrustedPersistenceContext,
        transaction: object | None,
    ) -> None: ...


class DisclosureHoldEffectsWriter(Protocol):
    """Persist the required safe Audit Event inside the hold transaction."""

    async def persist_hold(
        self,
        hold: DisclosureHold,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> tuple[UUID, UUID]: ...

    async def persist_release(
        self,
        release: DisclosureHoldRelease,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> tuple[UUID, UUID]: ...


class DisclosureHoldReader(Protocol):
    async def active_holds(
        self,
        query: DisclosureConstraintQuery,
        context: TrustedPersistenceContext,
    ) -> CurrentDisclosureHolds: ...


class DisclosureHoldRepository(DisclosureHoldReader, Protocol):
    async def create_hold(
        self, hold: DisclosureHold, context: TrustedPersistenceContext
    ) -> DisclosureHold: ...

    async def release_hold(
        self, release: DisclosureHoldRelease, context: TrustedPersistenceContext
    ) -> DisclosureHoldRelease: ...


@dataclass(frozen=True, slots=True)
class DisclosureHoldService:
    repository: DisclosureHoldRepository
    lifecycle: CurrentLifecycleEligibilityReader

    async def create(
        self,
        command: CreateDisclosureHoldCommand,
        context: TrustedPersistenceContext,
    ) -> DisclosureHold:
        _require_interactive_actor(context, command.hold.actor_id)
        current = await self.lifecycle.current_eligibility(
            command.lifecycle_query, context
        )
        if (
            current.current_decision_id != command.hold.review_decision_id
            or current.status
            is not LifecycleEligibilityStatus.DISCLOSURE_REVIEW_REQUIRED
        ):
            raise DisclosureHoldAuthorityError()
        return await self.repository.create_hold(command.hold, context)

    async def release(
        self,
        command: ReleaseDisclosureHoldCommand,
        context: TrustedPersistenceContext,
    ) -> DisclosureHoldRelease:
        _require_interactive_actor(context, command.release.actor_id)
        return await self.repository.release_hold(command.release, context)


async def require_disclosure_unheld(
    lifecycle: CurrentLifecycleEligibilityReader,
    holds: DisclosureHoldReader,
    lifecycle_query: LifecycleEligibilityQuery,
    constraint_query: DisclosureConstraintQuery,
    context: TrustedPersistenceContext,
) -> None:
    """Deny unless lifecycle is eligible and no exact active hold applies."""

    if (
        lifecycle_query.workspace_id,
        lifecycle_query.environment_id,
        lifecycle_query.source_object_id,
    ) != (
        constraint_query.workspace_id,
        constraint_query.environment_id,
        constraint_query.source_object_id,
    ):
        raise DisclosureConstraintError()
    try:
        current = await lifecycle.current_eligibility(lifecycle_query, context)
        active = await holds.active_holds(constraint_query, context)
    except Exception:
        raise DisclosureConstraintError() from None
    if current.status is not LifecycleEligibilityStatus.ELIGIBLE or active.denied:
        raise DisclosureConstraintError()


@dataclass(frozen=True, slots=True)
class InMemoryDisclosureHoldRepository(DisclosureHoldRepository):
    authority: DisclosureHoldAuthority
    holds: dict[UUID, DisclosureHold] = field(
        init=False, default_factory=dict, repr=False, compare=False
    )
    releases: dict[UUID, DisclosureHoldRelease] = field(
        init=False, default_factory=dict, repr=False, compare=False
    )
    release_by_hold: dict[UUID, UUID] = field(
        init=False, default_factory=dict, repr=False, compare=False
    )
    _lock: asyncio.Lock = field(
        init=False, default_factory=asyncio.Lock, repr=False, compare=False
    )

    async def create_hold(
        self, hold: DisclosureHold, context: TrustedPersistenceContext
    ) -> DisclosureHold:
        _validate_context(
            context, hold.workspace_id, hold.environment_id, hold.actor_id
        )
        async with self._lock:
            existing = self.holds.get(hold.hold_id)
            if existing is not None:
                if existing != hold:
                    raise DisclosureHoldConflictError()
                return existing
            if hold.supersedes_hold_id is not None:
                superseded = self.holds.get(hold.supersedes_hold_id)
                if superseded is None or (
                    superseded.workspace_id,
                    superseded.environment_id,
                    superseded.source_object_id,
                ) != (hold.workspace_id, hold.environment_id, hold.source_object_id):
                    raise DisclosureHoldAuthorityError()
                if hold.supersedes_hold_id in self.release_by_hold:
                    raise DisclosureHoldConflictError()
                if any(
                    candidate.supersedes_hold_id == hold.supersedes_hold_id
                    for candidate in self.holds.values()
                ):
                    raise DisclosureHoldConflictError()
            await self.authority.authorize_create(hold, context, None)
            self.holds[hold.hold_id] = hold
            return hold

    async def release_hold(
        self, release: DisclosureHoldRelease, context: TrustedPersistenceContext
    ) -> DisclosureHoldRelease:
        _validate_context(
            context,
            release.workspace_id,
            release.environment_id,
            release.actor_id,
        )
        async with self._lock:
            existing = self.releases.get(release.release_id)
            if existing is not None:
                if existing != release:
                    raise DisclosureHoldConflictError()
                return existing
            current_release_id = self.release_by_hold.get(release.hold_id)
            if current_release_id is not None:
                prior = self.releases[current_release_id]
                if prior != release:
                    raise DisclosureHoldConflictError()
                return prior
            hold = self.holds.get(release.hold_id)
            if hold is None:
                raise DisclosureHoldAuthorityError()
            try:
                release.validate_hold(hold)
            except ValueError:
                raise DisclosureHoldAuthorityError() from None
            successors = tuple(
                candidate
                for candidate in self.holds.values()
                if candidate.supersedes_hold_id == hold.hold_id
            )
            if successors and (
                len(successors) != 1
                or release.superseded_by_hold_id != successors[0].hold_id
            ):
                raise DisclosureHoldAuthorityError()
            if release.superseded_by_hold_id is not None:
                successor = self.holds.get(release.superseded_by_hold_id)
                if (
                    successor is None
                    or successor.supersedes_hold_id != hold.hold_id
                    or (
                        successor.workspace_id,
                        successor.environment_id,
                        successor.source_object_id,
                    )
                    != (
                        hold.workspace_id,
                        hold.environment_id,
                        hold.source_object_id,
                    )
                ):
                    raise DisclosureHoldAuthorityError()
            await self.authority.authorize_release(release, hold, context, None)
            self.releases[release.release_id] = release
            self.release_by_hold[release.hold_id] = release.release_id
            return release

    async def active_holds(
        self,
        query: DisclosureConstraintQuery,
        context: TrustedPersistenceContext,
    ) -> CurrentDisclosureHolds:
        _validate_context(
            context, query.workspace_id, query.environment_id, actor_id=None
        )
        async with self._lock:
            matching = tuple(
                sorted(
                    (
                        hold.hold_id
                        for hold in self.holds.values()
                        if _matches(hold, query)
                        and not _released_at_or_before(
                            self, hold.hold_id, query.evaluated_at
                        )
                    ),
                    key=lambda value: value.hex,
                )
            )
        return CurrentDisclosureHolds(
            workspace_id=query.workspace_id,
            environment_id=query.environment_id,
            source_object_id=query.source_object_id,
            hold_ids=matching,
        )


def _validate_context(
    context: TrustedPersistenceContext,
    workspace_id: UUID,
    environment_id: UUID,
    actor_id: UUID | None,
) -> None:
    try:
        validate_lifecycle_context(context, workspace_id, environment_id)
    except Exception:
        raise DisclosureHoldAuthorityError() from None
    if actor_id is not None and lifecycle_context_actor(context) != actor_id:
        raise DisclosureHoldAuthorityError()


def _require_interactive_actor(
    context: TrustedPersistenceContext, actor_id: UUID
) -> None:
    if (
        context.origin is not ContextOrigin.INTERACTIVE
        or context.acting_subject_id != actor_id
    ):
        raise DisclosureHoldAuthorityError()


def _matches(hold: DisclosureHold, query: DisclosureConstraintQuery) -> bool:
    return (
        hold.workspace_id,
        hold.environment_id,
        hold.source_object_id,
    ) == (
        query.workspace_id,
        query.environment_id,
        query.source_object_id,
    ) and (
        query.source_revision_id in hold.source_revision_ids
        and query.dependency_id in hold.dependency_ids
        and query.artifact_id in hold.artifact_ids
        and query.effect in hold.prohibited_effects
        and hold.effective_from <= query.evaluated_at < hold.effective_until
    )


def _released_at_or_before(
    repository: InMemoryDisclosureHoldRepository,
    hold_id: UUID,
    evaluated_at: datetime,
) -> bool:
    release_id = repository.release_by_hold.get(hold_id)
    return (
        release_id is not None
        and repository.releases[release_id].released_at <= evaluated_at
    )


__all__ = [
    "CreateDisclosureHoldCommand",
    "CurrentDisclosureHolds",
    "DisclosureConstraintError",
    "DisclosureConstraintQuery",
    "DisclosureHoldAuthority",
    "DisclosureHoldAuthorityError",
    "DisclosureHoldConflictError",
    "DisclosureHoldEffectsWriter",
    "DisclosureHoldReader",
    "DisclosureHoldRepository",
    "DisclosureHoldService",
    "InMemoryDisclosureHoldRepository",
    "ReleaseDisclosureHoldCommand",
    "require_disclosure_unheld",
]
