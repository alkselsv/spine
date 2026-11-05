"""Trusted issuance of exact, one-transition tombstone authorizations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from uuid import UUID

from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.domain.common import ContextOrigin
from spine.domain.sources.canonical import CurrentSourceHead
from spine.domain.sources.deletion_authorization import (
    QualifiedDeletionEvidence,
    SourceDeletionPolicyApproval,
    TombstoneAuthorization,
    TombstoneAuthorizationDecision,
    TombstoneAuthorizationDisposition,
    TombstoneAuthorizationIssuer,
)
from spine.domain.sources.models import RevisionKind


class TombstoneAuthorizationOutcome(str, Enum):
    ISSUED = "issued"
    REPLAYED = "replayed"
    TOMBSTONE_NOT_AUTHORIZED = "tombstone_not_authorized"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


@dataclass(frozen=True, slots=True)
class TombstoneAuthorizationResult:
    outcome: TombstoneAuthorizationOutcome
    authorization: TombstoneAuthorization | None = None

    def __post_init__(self) -> None:
        carries_authorization = self.outcome in {
            TombstoneAuthorizationOutcome.ISSUED,
            TombstoneAuthorizationOutcome.REPLAYED,
        }
        if carries_authorization != (self.authorization is not None):
            raise ValueError("tombstone authorization result shape is invalid")


@dataclass(frozen=True, slots=True)
class IssueTombstoneAuthorizationCommand:
    authorization: TombstoneAuthorization


class TombstoneAuthorizationAuthority(Protocol):
    """Resolve server-selected current authority; callers cannot supply it."""

    async def resolve_evidence(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
    ) -> QualifiedDeletionEvidence | None: ...

    async def resolve_current_policy_approval(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
    ) -> SourceDeletionPolicyApproval | None: ...

    async def resolve_current_issuer(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
    ) -> TombstoneAuthorizationIssuer | None: ...

    async def resolve_current_head(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
    ) -> CurrentSourceHead | None: ...

    async def retention_and_legal_eligible(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
    ) -> bool: ...


class TombstoneAuthorizationRepository(Protocol):
    async def replay(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
    ) -> TombstoneAuthorizationResult | None: ...

    async def issue(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
    ) -> TombstoneAuthorizationResult: ...

    async def record_decision(
        self,
        decision: TombstoneAuthorizationDecision,
        context: TrustedPersistenceContext,
    ) -> TombstoneAuthorizationDecision: ...

    async def is_current(
        self, authorization_id: UUID, context: TrustedPersistenceContext
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class TombstoneAuthorizationService:
    repository: TombstoneAuthorizationRepository
    authority: TombstoneAuthorizationAuthority
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def issue(
        self,
        command: IssueTombstoneAuthorizationCommand,
        context: TrustedPersistenceContext,
    ) -> TombstoneAuthorizationResult:
        authorization = command.authorization
        if not _trusted_issuer_context(context, authorization):
            return _denied()
        try:
            replay = await self.repository.replay(authorization, context)
        except Exception:
            return _denied()
        if replay is not None:
            return replay

        evaluated_at = self.clock()
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            return _denied()
        try:
            evidence = await self.authority.resolve_evidence(authorization, context)
            approval = await self.authority.resolve_current_policy_approval(
                authorization, context
            )
            issuer = await self.authority.resolve_current_issuer(authorization, context)
            head = await self.authority.resolve_current_head(authorization, context)
            eligible = await self.authority.retention_and_legal_eligible(
                authorization, context
            )
            if evidence is None or approval is None or issuer is None or head is None:
                return _denied()
            authorization.validate_bindings(
                evidence=evidence,
                approval=approval,
                issuer=issuer,
                evaluated_at=evaluated_at,
            )
            authorization.validate_expected_head(
                revision_id=head.current_revision_id,
                acceptance_id=head.acceptance_id,
                cas_version=head.cas_version,
            )
            if (
                not eligible
                or head.current_revision_kind is RevisionKind.TOMBSTONE
                or (head.workspace_id, head.environment_id, head.source_object_id)
                != (
                    authorization.workspace_id,
                    authorization.environment_id,
                    authorization.source_object_id,
                )
                or not authorization.issued_at
                <= evaluated_at
                < authorization.expires_at
            ):
                return _denied()
        except Exception:
            return _denied()
        try:
            return await self.repository.issue(authorization, context)
        except Exception:
            try:
                reconciled = await self.repository.replay(authorization, context)
            except Exception:
                return _denied()
            return reconciled if reconciled is not None else _denied()


@dataclass(frozen=True, slots=True)
class InMemoryTombstoneAuthorizationRepository(TombstoneAuthorizationRepository):
    """Deterministic contract fake; it makes no SQL atomicity claim."""

    authorizations: dict[UUID, TombstoneAuthorization] = field(
        init=False, default_factory=dict, repr=False, compare=False
    )
    authorization_by_command: dict[tuple[UUID, UUID, str], UUID] = field(
        init=False, default_factory=dict, repr=False, compare=False
    )
    authorization_by_head: dict[tuple[UUID, UUID, UUID, UUID, UUID, int], UUID] = field(
        init=False, default_factory=dict, repr=False, compare=False
    )
    current_decisions: dict[UUID, TombstoneAuthorizationDecision] = field(
        init=False, default_factory=dict, repr=False, compare=False
    )
    decisions: dict[UUID, TombstoneAuthorizationDecision] = field(
        init=False, default_factory=dict, repr=False, compare=False
    )
    _lock: asyncio.Lock = field(
        init=False, default_factory=asyncio.Lock, repr=False, compare=False
    )

    async def replay(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
    ) -> TombstoneAuthorizationResult | None:
        if not _scope_matches(
            context, authorization.workspace_id, authorization.environment_id
        ):
            return _denied()
        async with self._lock:
            existing_id = self.authorization_by_command.get(_command_key(authorization))
            if existing_id is None:
                return None
            existing = self.authorizations[existing_id]
            if (
                existing.command_digest != authorization.command_digest
                or existing != authorization
            ):
                return TombstoneAuthorizationResult(
                    TombstoneAuthorizationOutcome.IDEMPOTENCY_CONFLICT
                )
            return TombstoneAuthorizationResult(
                TombstoneAuthorizationOutcome.REPLAYED, existing
            )

    async def issue(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
    ) -> TombstoneAuthorizationResult:
        if not _trusted_issuer_context(context, authorization):
            return _denied()
        async with self._lock:
            command_key = _command_key(authorization)
            existing_id = self.authorization_by_command.get(command_key)
            if existing_id is not None:
                existing = self.authorizations[existing_id]
                if existing == authorization:
                    return TombstoneAuthorizationResult(
                        TombstoneAuthorizationOutcome.REPLAYED, existing
                    )
                return TombstoneAuthorizationResult(
                    TombstoneAuthorizationOutcome.IDEMPOTENCY_CONFLICT
                )
            if authorization.authorization_id in self.authorizations:
                return TombstoneAuthorizationResult(
                    TombstoneAuthorizationOutcome.IDEMPOTENCY_CONFLICT
                )
            head_key = _head_key(authorization)
            if head_key in self.authorization_by_head:
                return _denied()
            baseline = TombstoneAuthorizationDecision.issued(authorization)
            self.authorizations[authorization.authorization_id] = authorization
            self.authorization_by_command[command_key] = authorization.authorization_id
            self.authorization_by_head[head_key] = authorization.authorization_id
            self.decisions[baseline.decision_id] = baseline
            self.current_decisions[authorization.authorization_id] = baseline
            return TombstoneAuthorizationResult(
                TombstoneAuthorizationOutcome.ISSUED, authorization
            )

    async def record_decision(
        self,
        decision: TombstoneAuthorizationDecision,
        context: TrustedPersistenceContext,
    ) -> TombstoneAuthorizationDecision:
        if not _scope_matches(context, decision.workspace_id, decision.environment_id):
            raise ValueError("tombstone authorization authority is unavailable")
        async with self._lock:
            existing = self.decisions.get(decision.decision_id)
            if existing is not None:
                if existing != decision:
                    raise ValueError("tombstone authorization decision conflicts")
                return existing
            authorization = self.authorizations.get(decision.authorization_id)
            current = self.current_decisions.get(decision.authorization_id)
            if (
                authorization is None
                or current is None
                or not _trusted_issuer_context(context, authorization)
                or (
                    decision.authority_reference,
                    decision.authority_generation,
                )
                != (
                    authorization.issuer_authority_id,
                    authorization.issuer_authority_generation,
                )
                or (
                    decision.workspace_id,
                    decision.environment_id,
                    decision.source_object_id,
                    decision.expected_current_decision_id,
                    decision.expected_current_generation,
                )
                != (
                    authorization.workspace_id,
                    authorization.environment_id,
                    authorization.source_object_id,
                    current.decision_id,
                    current.generation,
                )
            ):
                raise ValueError("tombstone authorization decision head conflicts")
            if current.disposition is not TombstoneAuthorizationDisposition.ISSUED:
                raise ValueError("tombstone authorization is already inactive")
            self.decisions[decision.decision_id] = decision
            self.current_decisions[decision.authorization_id] = decision
            return decision

    async def is_current(
        self, authorization_id: UUID, context: TrustedPersistenceContext
    ) -> bool:
        async with self._lock:
            authorization = self.authorizations.get(authorization_id)
            decision = self.current_decisions.get(authorization_id)
            return bool(
                authorization is not None
                and decision is not None
                and _scope_matches(
                    context, authorization.workspace_id, authorization.environment_id
                )
                and decision.disposition is TombstoneAuthorizationDisposition.ISSUED
            )


def _trusted_issuer_context(
    context: TrustedPersistenceContext, authorization: TombstoneAuthorization
) -> bool:
    try:
        context.validate_shape()
    except Exception:
        return False
    return (
        context.origin is ContextOrigin.WORKER
        and context.service_principal_id == authorization.issuer_service_principal_id
        and _scope_matches(
            context, authorization.workspace_id, authorization.environment_id
        )
    )


def _scope_matches(
    context: TrustedPersistenceContext, workspace_id: UUID, environment_id: UUID
) -> bool:
    return (
        isinstance(context.scope, EnvironmentScope)
        and context.scope.workspace_id == workspace_id
        and context.scope.environment_id == environment_id
    )


def _command_key(authorization: TombstoneAuthorization) -> tuple[UUID, UUID, str]:
    return (
        authorization.workspace_id,
        authorization.environment_id,
        authorization.idempotency_key,
    )


def _head_key(
    authorization: TombstoneAuthorization,
) -> tuple[UUID, UUID, UUID, UUID, UUID, int]:
    return (
        authorization.workspace_id,
        authorization.environment_id,
        authorization.source_object_id,
        authorization.expected_current_revision_id,
        authorization.expected_current_acceptance_id,
        authorization.expected_current_cas_version,
    )


def _denied() -> TombstoneAuthorizationResult:
    return TombstoneAuthorizationResult(
        TombstoneAuthorizationOutcome.TOMBSTONE_NOT_AUTHORIZED
    )


__all__ = [
    "InMemoryTombstoneAuthorizationRepository",
    "IssueTombstoneAuthorizationCommand",
    "TombstoneAuthorizationAuthority",
    "TombstoneAuthorizationOutcome",
    "TombstoneAuthorizationRepository",
    "TombstoneAuthorizationResult",
    "TombstoneAuthorizationService",
]
