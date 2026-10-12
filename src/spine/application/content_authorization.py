"""Fail-closed decisions for protected source content.

This module is deliberately independent of authentication providers and
persistence implementations.  A caller must present a trusted context issued
by the composition root; the decision service then revalidates that context
and resolves the current source policy immediately before returning a result.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.sources import (
    AccessPolicyVersion,
    CurrentAccessPolicy,
    RevisionKind,
)


class AuthorizationContextMode(str, Enum):
    """The only protected-use modes supported by the R1 decision port."""

    HUMAN_DISCLOSURE = "human_disclosure"
    INTERACTIVE_PROCESSING = "interactive_processing"
    NONINTERACTIVE_PROCESSING = "noninteractive_processing"


class AuthorizationDecisionKind(str, Enum):
    """A decision kind cannot be substituted for another protected use."""

    HUMAN_DISCLOSURE = "human_disclosure"
    INTERACTIVE_PROCESSING = "interactive_processing"
    NONINTERACTIVE_PROCESSING = "noninteractive_processing"


class AuthorizationReasonCode(str, Enum):
    """Disclosure-safe, bounded decision reasons.

    These codes intentionally do not distinguish a missing source, policy or
    grant.  Such distinctions would disclose protected authorization state.
    """

    ALLOWED = "allowed"
    AUTHORIZATION_DENIED = "authorization_denied"
    CONTEXT_INVALID = "context_invalid"
    AUTHORITY_STALE = "authority_stale"
    SOURCE_UNAVAILABLE = "source_unavailable"
    DECISION_UNAVAILABLE = "decision_unavailable"
    AUDIT_UNAVAILABLE = "audit_unavailable"


class AuthorizationAuditStatus(str, Enum):
    """Outcome of the optional separate denied-attempt audit transaction."""

    NOT_REQUESTED = "not_requested"
    RECORDED = "recorded"
    UNAVAILABLE = "audit_unavailable"


def _nonzero(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")
    return value


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class TrustedAuthorizationContext(DefinitionModel):
    """Server-issued identity and operation snapshot used by one decision.

    This is not constructible from a request payload in production.  The
    application contract is intentionally detached so Issue #5 can supply a
    trusted issuer without making this module depend on OIDC or FastAPI.
    """

    mode: AuthorizationContextMode
    workspace_id: UUID
    environment_id: UUID
    canonical_human_id: UUID | None = None
    effective_team_ids: frozenset[UUID] = frozenset()
    service_principal_id: UUID | None = None
    purpose: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    operation: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    membership_generation: int = Field(ge=1)
    authority_generation: int = Field(ge=1)
    service_authority_generation: int | None = Field(default=None, ge=1)
    authority_snapshot_reference: UUID
    trace_id: UUID
    issued_at: datetime
    noninteractive_authority_reference: UUID | None = None

    @field_validator("workspace_id", "environment_id", "authority_snapshot_reference", "trace_id")
    @classmethod
    def validate_ids(cls, value: UUID, info: object) -> UUID:
        return _nonzero(value, getattr(info, "field_name", "identifier"))

    @field_validator("canonical_human_id", "service_principal_id", "noninteractive_authority_reference")
    @classmethod
    def validate_optional_ids(cls, value: UUID | None, info: object) -> UUID | None:
        if value is not None:
            return _nonzero(value, getattr(info, "field_name", "identifier"))
        return value

    @field_validator("effective_team_ids")
    @classmethod
    def validate_team_ids(cls, value: frozenset[UUID]) -> frozenset[UUID]:
        if any(item.int == 0 for item in value):
            raise ValueError("effective_team_ids must contain non-zero UUIDs")
        return value

    @field_validator("issued_at")
    @classmethod
    def validate_issued_at(cls, value: datetime) -> datetime:
        return _aware(value, "issued_at")

    @model_validator(mode="after")
    def validate_mode_shape(self) -> "TrustedAuthorizationContext":
        if self.mode is AuthorizationContextMode.HUMAN_DISCLOSURE:
            if self.canonical_human_id is None or self.service_principal_id is not None:
                raise ValueError("human disclosure requires only a human principal")
            if self.service_authority_generation is not None:
                raise ValueError("human disclosure cannot carry service authority")
            if self.noninteractive_authority_reference is not None:
                raise ValueError("human disclosure cannot carry workflow authority")
        elif self.mode is AuthorizationContextMode.INTERACTIVE_PROCESSING:
            if self.canonical_human_id is None or self.service_principal_id is None:
                raise ValueError("interactive processing requires human and service principals")
            if self.service_authority_generation is None:
                raise ValueError("interactive processing requires service authority generation")
            if self.noninteractive_authority_reference is not None:
                raise ValueError("interactive processing cannot carry workflow authority")
        elif self.mode is AuthorizationContextMode.NONINTERACTIVE_PROCESSING:
            if self.canonical_human_id is not None or self.service_principal_id is None:
                raise ValueError("noninteractive processing requires only a service principal")
            if self.service_authority_generation is None:
                raise ValueError("noninteractive processing requires service authority generation")
            if self.noninteractive_authority_reference is None:
                raise ValueError("noninteractive processing requires workflow authority")
            if self.effective_team_ids:
                raise ValueError("noninteractive processing cannot carry human teams")
        return self


class CurrentContentAuthorizationSnapshot(DefinitionModel):
    """Current, non-content source state needed for an authorization decision."""

    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    current_revision_id: UUID | None = None
    current_revision_kind: RevisionKind | None = None
    provenance_complete: bool = False
    current_policy: CurrentAccessPolicy | None = None
    policy: AccessPolicyVersion | None = None

    @field_validator("workspace_id", "environment_id", "source_object_id", "current_revision_id")
    @classmethod
    def validate_snapshot_ids(cls, value: UUID | None, info: object) -> UUID | None:
        if value is not None:
            return _nonzero(value, getattr(info, "field_name", "identifier"))
        return value

    @model_validator(mode="after")
    def validate_policy_scope(self) -> "CurrentContentAuthorizationSnapshot":
        if self.current_policy is not None:
            if (
                self.current_policy.workspace_id != self.workspace_id
                or self.current_policy.environment_id != self.environment_id
                or self.current_policy.source_object_id != self.source_object_id
            ):
                raise ValueError("current policy scope does not match source scope")
        if self.policy is not None:
            if (
                self.policy.workspace_id != self.workspace_id
                or self.policy.environment_id != self.environment_id
                or self.policy.source_object_id != self.source_object_id
            ):
                raise ValueError("policy scope does not match source scope")
        if self.current_policy is not None and self.policy is not None:
            if self.current_policy.access_policy_version_id != self.policy.access_policy_version_id:
                raise ValueError("policy is not the current policy version")
        return self


class AuthorizationEvidence(DefinitionModel):
    """Opaque evidence retained on an allowed decision, never content."""

    source_object_id: UUID
    policy_version_id: UUID
    policy_generation: int = Field(ge=1)

    @field_validator("source_object_id", "policy_version_id")
    @classmethod
    def validate_evidence_ids(cls, value: UUID, info: object) -> UUID:
        return _nonzero(value, getattr(info, "field_name", "identifier"))


class ContentAuthorizationDecision(DefinitionModel):
    """Typed allow/deny outcome shared by the three distinct operations."""

    allowed: bool
    decision_kind: AuthorizationDecisionKind
    workspace_id: UUID
    environment_id: UUID
    source_object_ids: tuple[UUID, ...]
    policy_evidence: tuple[AuthorizationEvidence, ...] = ()
    membership_generation: int = Field(ge=1)
    authority_generation: int = Field(ge=1)
    service_authority_generation: int | None = Field(default=None, ge=1)
    authority_snapshot_reference: UUID
    purpose: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    operation: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    reason_code: AuthorizationReasonCode
    decision_reference: UUID
    audit_reference: UUID | None = None
    audit_status: AuthorizationAuditStatus = AuthorizationAuditStatus.NOT_REQUESTED
    trace_id: UUID
    evaluated_at: datetime

    @field_validator("workspace_id", "environment_id", "authority_snapshot_reference", "decision_reference", "trace_id")
    @classmethod
    def validate_decision_ids(cls, value: UUID, info: object) -> UUID:
        return _nonzero(value, getattr(info, "field_name", "identifier"))

    @field_validator("source_object_ids")
    @classmethod
    def validate_source_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if any(item.int == 0 for item in value):
            raise ValueError("source_object_ids must contain non-zero UUIDs")
        if len(set(value)) != len(value):
            raise ValueError("source_object_ids must be unique")
        return value

    @field_validator("trace_id", "evaluated_at")
    @classmethod
    def validate_trace_or_time(cls, value: UUID | datetime, info: object) -> UUID | datetime:
        if getattr(info, "field_name", "") == "evaluated_at":
            return _aware(value, "evaluated_at")  # type: ignore[arg-type]
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> "ContentAuthorizationDecision":
        if self.allowed:
            if self.reason_code is not AuthorizationReasonCode.ALLOWED:
                raise ValueError("allowed decisions require the allowed reason")
            if not self.source_object_ids or len(self.policy_evidence) != len(self.source_object_ids):
                raise ValueError("allowed decisions require evidence for every source")
        elif self.policy_evidence:
            raise ValueError("denied decisions cannot carry policy evidence")
        if self.reason_code is AuthorizationReasonCode.ALLOWED and not self.allowed:
            raise ValueError("denied decisions cannot use the allowed reason")
        return self


class TrustedIdentityAuthority(Protocol):
    """Re-resolve the current identity and authority generations."""

    async def revalidate(
        self, context: TrustedAuthorizationContext
    ) -> TrustedAuthorizationContext: ...


class ContentAuthorizationRepository(Protocol):
    """Resolve current source state and current policy by explicit tenant scope."""

    async def resolve_current_content_authorization(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
    ) -> CurrentContentAuthorizationSnapshot | None: ...


class NonInteractiveProcessingAuthority(Protocol):
    """Prove that a service operation is server-owned and disclosure-safe."""

    async def authorize(
        self, context: TrustedAuthorizationContext
    ) -> bool: ...


class DeniedAuthorizationAttemptAudit(Protocol):
    """Separate, allowlisted audit seam for denied decisions."""

    async def record_denied(self, decision: ContentAuthorizationDecision) -> UUID: ...


class ContentAuthorizationDecisionService:
    """Revalidate and evaluate current policy for one protected use."""

    def __init__(
        self,
        *,
        repository: ContentAuthorizationRepository,
        identity_authority: TrustedIdentityAuthority,
        noninteractive_authority: NonInteractiveProcessingAuthority | None = None,
        denied_attempt_audit: DeniedAuthorizationAttemptAudit | None = None,
        clock: Callable[[], datetime] | None = None,
        decision_reference_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._repository = repository
        self._identity_authority = identity_authority
        self._noninteractive_authority = noninteractive_authority
        self._denied_attempt_audit = denied_attempt_audit
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._decision_reference_factory = decision_reference_factory or uuid4

    async def decide_human_disclosure(
        self,
        context: TrustedAuthorizationContext,
        source_ids: Iterable[UUID],
    ) -> ContentAuthorizationDecision:
        return await self._decide(
            context=context,
            source_ids=source_ids,
            kind=AuthorizationDecisionKind.HUMAN_DISCLOSURE,
        )

    async def decide_interactive_processing(
        self,
        context: TrustedAuthorizationContext,
        source_ids: Iterable[UUID],
    ) -> ContentAuthorizationDecision:
        return await self._decide(
            context=context,
            source_ids=source_ids,
            kind=AuthorizationDecisionKind.INTERACTIVE_PROCESSING,
        )

    async def decide_noninteractive_processing(
        self,
        context: TrustedAuthorizationContext,
        source_ids: Iterable[UUID],
    ) -> ContentAuthorizationDecision:
        return await self._decide(
            context=context,
            source_ids=source_ids,
            kind=AuthorizationDecisionKind.NONINTERACTIVE_PROCESSING,
        )

    async def _decide(
        self,
        *,
        context: TrustedAuthorizationContext,
        source_ids: Iterable[UUID],
        kind: AuthorizationDecisionKind,
    ) -> ContentAuthorizationDecision:
        now = self._now()
        try:
            normalized_source_ids = _normalize_source_ids(source_ids)
            if not _context_matches_kind(context, kind) or not normalized_source_ids:
                return await self._deny(
                    context=context,
                    source_ids=normalized_source_ids,
                    kind=kind,
                    reason=AuthorizationReasonCode.CONTEXT_INVALID,
                    evaluated_at=now,
                )
            current_context = await self._identity_authority.revalidate(context)
            if not _same_request_context(context, current_context, kind):
                return await self._deny(
                    context=context,
                    source_ids=normalized_source_ids,
                    kind=kind,
                    reason=AuthorizationReasonCode.AUTHORITY_STALE,
                    evaluated_at=now,
                )
            if kind is AuthorizationDecisionKind.NONINTERACTIVE_PROCESSING:
                if self._noninteractive_authority is None or not await self._noninteractive_authority.authorize(current_context):
                    return await self._deny(
                        context=current_context,
                        source_ids=normalized_source_ids,
                        kind=kind,
                        reason=AuthorizationReasonCode.AUTHORIZATION_DENIED,
                        evaluated_at=now,
                    )
            evidence: list[AuthorizationEvidence] = []
            for source_id in normalized_source_ids:
                snapshot = await self._repository.resolve_current_content_authorization(
                    workspace_id=current_context.workspace_id,
                    environment_id=current_context.environment_id,
                    source_object_id=source_id,
                )
                if snapshot is None or not _snapshot_is_current_content(snapshot, current_context):
                    return await self._deny(
                        context=current_context,
                        source_ids=normalized_source_ids,
                        kind=kind,
                        reason=AuthorizationReasonCode.SOURCE_UNAVAILABLE,
                        evaluated_at=now,
                    )
                assert snapshot.policy is not None
                assert snapshot.current_policy is not None
                if kind is AuthorizationDecisionKind.HUMAN_DISCLOSURE:
                    allowed = snapshot.policy.allows_human_read(
                        current_context.canonical_human_id, current_context.effective_team_ids  # type: ignore[arg-type]
                    )
                elif kind is AuthorizationDecisionKind.INTERACTIVE_PROCESSING:
                    allowed = snapshot.policy.allows_human_read(
                        current_context.canonical_human_id, current_context.effective_team_ids  # type: ignore[arg-type]
                    ) and snapshot.policy.allows_service_processing(
                        current_context.service_principal_id,  # type: ignore[arg-type]
                        current_context.purpose,
                        current_context.operation,
                        now,
                    )
                else:
                    allowed = snapshot.policy.allows_service_processing(
                        current_context.service_principal_id,  # type: ignore[arg-type]
                        current_context.purpose,
                        current_context.operation,
                        now,
                    )
                if not allowed:
                    return await self._deny(
                        context=current_context,
                        source_ids=normalized_source_ids,
                        kind=kind,
                        reason=AuthorizationReasonCode.AUTHORIZATION_DENIED,
                        evaluated_at=now,
                    )
                evidence.append(
                    AuthorizationEvidence(
                        source_object_id=source_id,
                        policy_version_id=snapshot.current_policy.access_policy_version_id,
                        policy_generation=snapshot.current_policy.policy_generation,
                    )
                )
            return ContentAuthorizationDecision(
                allowed=True,
                decision_kind=kind,
                workspace_id=current_context.workspace_id,
                environment_id=current_context.environment_id,
                source_object_ids=normalized_source_ids,
                policy_evidence=tuple(evidence),
                membership_generation=current_context.membership_generation,
                authority_generation=current_context.authority_generation,
                service_authority_generation=current_context.service_authority_generation,
                authority_snapshot_reference=current_context.authority_snapshot_reference,
                purpose=current_context.purpose,
                operation=current_context.operation,
                reason_code=AuthorizationReasonCode.ALLOWED,
                decision_reference=self._decision_reference_factory(),
                trace_id=current_context.trace_id,
                evaluated_at=now,
            )
        except Exception:
            return await self._deny(
                context=context,
                source_ids=_safe_source_ids(source_ids),
                kind=kind,
                reason=AuthorizationReasonCode.DECISION_UNAVAILABLE,
                evaluated_at=now,
            )

    async def _deny(
        self,
        *,
        context: TrustedAuthorizationContext,
        source_ids: tuple[UUID, ...],
        kind: AuthorizationDecisionKind,
        reason: AuthorizationReasonCode,
        evaluated_at: datetime,
    ) -> ContentAuthorizationDecision:
        decision = ContentAuthorizationDecision(
            allowed=False,
            decision_kind=kind,
            workspace_id=context.workspace_id,
            environment_id=context.environment_id,
            source_object_ids=source_ids,
            membership_generation=context.membership_generation,
            authority_generation=context.authority_generation,
            service_authority_generation=context.service_authority_generation,
            authority_snapshot_reference=context.authority_snapshot_reference,
            purpose=context.purpose,
            operation=context.operation,
            reason_code=reason,
            decision_reference=self._decision_reference_factory(),
            trace_id=context.trace_id,
            evaluated_at=evaluated_at,
        )
        if self._denied_attempt_audit is None:
            return decision
        try:
            audit_reference = await self._denied_attempt_audit.record_denied(decision)
        except Exception:
            return decision.model_copy(
                update={"audit_status": AuthorizationAuditStatus.UNAVAILABLE}
            )
        return decision.model_copy(
            update={
                "audit_reference": audit_reference,
                "audit_status": AuthorizationAuditStatus.RECORDED,
            }
        )

    def _now(self) -> datetime:
        return _aware(self._clock(), "decision clock")


def _normalize_source_ids(source_ids: Iterable[UUID]) -> tuple[UUID, ...]:
    values = tuple(source_ids)
    if any(not isinstance(value, UUID) or value.int == 0 for value in values):
        raise ValueError("source_ids must contain non-zero UUIDs")
    if len(set(values)) != len(values):
        raise ValueError("source_ids must be unique")
    return values


def _safe_source_ids(source_ids: Iterable[UUID]) -> tuple[UUID, ...]:
    try:
        return _normalize_source_ids(source_ids)
    except Exception:
        return ()


def _context_matches_kind(
    context: TrustedAuthorizationContext,
    kind: AuthorizationDecisionKind,
) -> bool:
    return context.mode.value == kind.value


def _same_request_context(
    original: TrustedAuthorizationContext,
    current: TrustedAuthorizationContext,
    kind: AuthorizationDecisionKind,
) -> bool:
    return (
        type(current) is TrustedAuthorizationContext
        and _context_matches_kind(current, kind)
        and current.workspace_id == original.workspace_id
        and current.environment_id == original.environment_id
        and current.canonical_human_id == original.canonical_human_id
        and current.effective_team_ids == original.effective_team_ids
        and current.service_principal_id == original.service_principal_id
        and current.purpose == original.purpose
        and current.operation == original.operation
        and current.membership_generation == original.membership_generation
        and current.authority_generation == original.authority_generation
        and current.service_authority_generation == original.service_authority_generation
        and current.authority_snapshot_reference == original.authority_snapshot_reference
    )


def _snapshot_is_current_content(
    snapshot: CurrentContentAuthorizationSnapshot,
    context: TrustedAuthorizationContext,
) -> bool:
    current = snapshot.current_policy
    policy = snapshot.policy
    return bool(
        snapshot.workspace_id == context.workspace_id
        and snapshot.environment_id == context.environment_id
        and snapshot.current_revision_id is not None
        and snapshot.current_revision_kind is RevisionKind.CONTENT
        and snapshot.provenance_complete
        and current is not None
        and policy is not None
        and current.access_policy_version_id == policy.access_policy_version_id
        and current.policy_generation >= 1
        and policy.workspace_id == context.workspace_id
        and policy.environment_id == context.environment_id
    )


__all__ = [
    "AuthorizationAuditStatus",
    "AuthorizationContextMode",
    "AuthorizationDecisionKind",
    "AuthorizationEvidence",
    "AuthorizationReasonCode",
    "ContentAuthorizationDecision",
    "ContentAuthorizationDecisionService",
    "ContentAuthorizationRepository",
    "CurrentContentAuthorizationSnapshot",
    "DeniedAuthorizationAttemptAudit",
    "NonInteractiveProcessingAuthority",
    "TrustedAuthorizationContext",
    "TrustedIdentityAuthority",
]
