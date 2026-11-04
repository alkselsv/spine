"""Fail-closed access and retention closure for email-derived artifacts.

The guard deliberately returns one public denial shape. Provider absence,
canonical lifecycle, disclosure holds, retention applicability, legal holds and
physical cleanup remain separate authorities; none is inferred from another.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from spine.application.content_authorization import (
    AuthorizationContextMode,
    AuthorizationDecisionKind,
    ContentAuthorizationDecision,
    ContentAuthorizationDecisionService,
    TrustedAuthorizationContext,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.processing_disposition import (
    CurrentProcessingDispositionAuthority,
    OrdinaryProcessingEligibility,
)
from spine.application.retention import (
    RetentionArtifactRef,
    RetentionEligibility,
    RetentionEligibilityQuery,
)
from spine.domain.artifacts.email_governance import (
    EmailCurrentSourceAuthority,
    EmailDerivativeProcessingPlan,
    EmailGovernanceDecision,
    EmailGovernanceEvidence,
    EmailGovernanceSourceEvidence,
    EmailGovernanceOperation,
    EmailGovernanceReason,
    EmailGovernedArtifact,
    EmailLineageRole,
    EmailResolvedLineage,
    EmailSourceLineage,
)


class EmailArtifactLineageAuthority(Protocol):
    async def resolve_email_lineage(
        self, artifact: EmailGovernedArtifact
    ) -> EmailResolvedLineage | None: ...


class CurrentEmailConnectionAuthority(Protocol):
    async def resolve_current_content(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        source_revision_id: UUID,
    ) -> EmailCurrentSourceAuthority: ...


class EmailDisclosureFence(Protocol):
    async def allows_disclosure(
        self,
        *,
        context: TrustedAuthorizationContext,
        artifact: EmailGovernedArtifact,
        lineage: EmailResolvedLineage,
    ) -> bool: ...


class EmailRetentionEligibilityAuthority(Protocol):
    async def resolve(
        self,
        query: RetentionEligibilityQuery,
        context: TrustedPersistenceContext,
    ) -> RetentionEligibility: ...


class EmailGovernanceUnavailable(PermissionError):
    """Content-free processing/retrieval denial for an email derivative."""

    def __init__(self) -> None:
        super().__init__("email artifact is unavailable")


class EmailDerivativeProcessingAuthority(Protocol):
    async def authorize_plan(self, plan: EmailDerivativeProcessingPlan) -> None: ...

    async def authorize_processing(self, artifact: EmailGovernedArtifact) -> None: ...


class EmailDerivativePlanAuthority(Protocol):
    async def authorize_plan(
        self,
        *,
        plan: EmailDerivativeProcessingPlan,
        authorization_context: TrustedAuthorizationContext,
        retention_context: TrustedPersistenceContext,
    ) -> bool: ...


class GovernedEmailArtifactAccess:
    """Authorize one exact processing or retrieval use without leaking existence."""

    def __init__(
        self,
        *,
        lineages: EmailArtifactLineageAuthority,
        connections: CurrentEmailConnectionAuthority,
        processing: CurrentProcessingDispositionAuthority,
        authorization: ContentAuthorizationDecisionService,
        retention: EmailRetentionEligibilityAuthority,
        disclosure_fence: EmailDisclosureFence,
    ) -> None:
        self._lineages = lineages
        self._connections = connections
        self._processing = processing
        self._authorization = authorization
        self._retention = retention
        self._disclosure_fence = disclosure_fence

    async def decide(
        self,
        *,
        operation: EmailGovernanceOperation,
        artifact: EmailGovernedArtifact,
        authorization_context: TrustedAuthorizationContext,
        retention_context: TrustedPersistenceContext,
    ) -> EmailGovernanceDecision:
        if not self._contexts_match(
            artifact, authorization_context, retention_context
        ):
            return self._denied()
        try:
            lineage = await self._lineages.resolve_email_lineage(artifact)
            if lineage is None or lineage.artifact != artifact:
                return self._denied()
            first_processing = await self._resolve_current_authorities(lineage)
            if first_processing is None:
                return self._denied()
            policy = await self._decide_policy(
                operation, authorization_context, lineage
            )
            if not self._policy_matches(
                policy, operation, authorization_context, lineage
            ):
                return self._denied()
            retention = await self._retention.resolve(
                self._retention_query(artifact),
                retention_context,
            )
            if not self._retention_matches(retention, lineage):
                return self._denied()
            if operation is EmailGovernanceOperation.RETRIEVE:
                if not await self._disclosure_fence.allows_disclosure(
                    context=authorization_context,
                    artifact=artifact,
                    lineage=lineage,
                ):
                    return self._denied()
            # Close revocation/tombstone races while policy, retention and hold
            # authorities were being resolved.
            second_processing = await self._resolve_current_authorities(lineage)
            second_policy = await self._decide_policy(
                operation, authorization_context, lineage
            )
            second_retention = await self._retention.resolve(
                self._retention_query(artifact),
                retention_context,
            )
            if (
                second_processing is None
                or second_processing != first_processing
                or not self._same_policy_authority(policy, second_policy)
                or second_retention != retention
                or not self._policy_matches(
                    second_policy, operation, authorization_context, lineage
                )
                or not self._retention_matches(second_retention, lineage)
            ):
                return self._denied()
            if operation is EmailGovernanceOperation.RETRIEVE:
                if not await self._disclosure_fence.allows_disclosure(
                    context=authorization_context,
                    artifact=artifact,
                    lineage=lineage,
                ):
                    return self._denied()
            return self._allowed(lineage, policy, retention)
        except Exception:
            return self._denied()

    async def _resolve_current_authorities(
        self, lineage: EmailResolvedLineage
    ) -> tuple[tuple[EmailCurrentSourceAuthority, OrdinaryProcessingEligibility], ...] | None:
        resolved: list[
            tuple[EmailCurrentSourceAuthority, OrdinaryProcessingEligibility]
        ] = []
        for source in lineage.sources:
            source_authority = await self._connections.resolve_current_content(
                workspace_id=lineage.artifact.workspace_id,
                environment_id=lineage.artifact.environment_id,
                source_object_id=source.source_object_id,
                source_revision_id=source.source_revision_id,
            )
            if not source_authority.allows_current_content:
                return None
            current = await self._processing.resolve(
                workspace_id=lineage.artifact.workspace_id,
                environment_id=lineage.artifact.environment_id,
                source_object_id=source.source_object_id,
                source_revision_id=source.source_revision_id,
            )
            if not current.eligible:
                return None
            resolved.append((source_authority, current))
        return tuple(resolved)

    @staticmethod
    def _policy_matches(
        policy: ContentAuthorizationDecision | None,
        operation: EmailGovernanceOperation,
        context: TrustedAuthorizationContext,
        lineage: EmailResolvedLineage,
    ) -> bool:
        if policy is None or not policy.allowed:
            return False
        expected_kind = {
            AuthorizationContextMode.HUMAN_DISCLOSURE: (
                AuthorizationDecisionKind.HUMAN_DISCLOSURE
            ),
            AuthorizationContextMode.INTERACTIVE_PROCESSING: (
                AuthorizationDecisionKind.INTERACTIVE_PROCESSING
            ),
            AuthorizationContextMode.NONINTERACTIVE_PROCESSING: (
                AuthorizationDecisionKind.NONINTERACTIVE_PROCESSING
            ),
        }.get(context.mode)
        if (
            expected_kind is None
            or policy.decision_kind is not expected_kind
            or policy.workspace_id != lineage.artifact.workspace_id
            or policy.environment_id != lineage.artifact.environment_id
            or policy.source_object_ids
            != tuple(item.source_object_id for item in lineage.sources)
            or policy.purpose != context.purpose
            or policy.operation != context.operation
        ):
            return False
        evidence = tuple(
            (item.source_object_id, item.source_revision_id)
            for item in policy.policy_evidence
        )
        expected_evidence = tuple(
            (item.source_object_id, item.source_revision_id)
            for item in lineage.sources
        )
        return evidence == expected_evidence and (
            (operation is EmailGovernanceOperation.RETRIEVE)
            == (context.mode is AuthorizationContextMode.HUMAN_DISCLOSURE)
        )

    @staticmethod
    def _same_policy_authority(
        first: ContentAuthorizationDecision,
        second: ContentAuthorizationDecision | None,
    ) -> bool:
        if second is None:
            return False
        return (
            first.allowed,
            first.decision_kind,
            first.workspace_id,
            first.environment_id,
            first.source_object_ids,
            first.policy_evidence,
            first.membership_generation,
            first.authority_generation,
            first.service_authority_generation,
            first.authority_snapshot_reference,
            first.purpose,
            first.operation,
            first.reason_code,
        ) == (
            second.allowed,
            second.decision_kind,
            second.workspace_id,
            second.environment_id,
            second.source_object_ids,
            second.policy_evidence,
            second.membership_generation,
            second.authority_generation,
            second.service_authority_generation,
            second.authority_snapshot_reference,
            second.purpose,
            second.operation,
            second.reason_code,
        )

    async def _decide_policy(
        self,
        operation: EmailGovernanceOperation,
        context: TrustedAuthorizationContext,
        lineage: EmailResolvedLineage,
    ) -> ContentAuthorizationDecision | None:
        source_ids = tuple(item.source_object_id for item in lineage.sources)
        if operation is EmailGovernanceOperation.RETRIEVE:
            if context.mode is not AuthorizationContextMode.HUMAN_DISCLOSURE:
                return None
            return await self._authorization.decide_human_disclosure(
                context, source_ids
            )
        if context.mode is AuthorizationContextMode.INTERACTIVE_PROCESSING:
            return await self._authorization.decide_interactive_processing(
                context, source_ids
            )
        if context.mode is AuthorizationContextMode.NONINTERACTIVE_PROCESSING:
            return await self._authorization.decide_noninteractive_processing(
                context, source_ids
            )
        return None

    @staticmethod
    def _contexts_match(
        artifact: EmailGovernedArtifact,
        authorization: TrustedAuthorizationContext,
        retention: TrustedPersistenceContext,
    ) -> bool:
        try:
            retention.validate_shape()
        except Exception:
            return False
        return (
            authorization.workspace_id == artifact.workspace_id
            and authorization.environment_id == artifact.environment_id
            and isinstance(retention.scope, EnvironmentScope)
            and retention.scope.workspace_id == artifact.workspace_id
            and retention.scope.environment_id == artifact.environment_id
        )

    @staticmethod
    def _retention_matches(
        retention: RetentionEligibility, lineage: EmailResolvedLineage
    ) -> bool:
        expected = {
            (item.source_object_id, item.source_revision_id)
            for item in lineage.sources
        }
        actual = {
            (item.source_object_id, item.source_revision_id)
            for item in retention.source_decisions
        }
        return (
            retention.eligible
            and retention.artifact_class is lineage.artifact.artifact_class
            and actual == expected
            and len(actual) == len(retention.source_decisions)
        )

    @staticmethod
    def _retention_query(
        artifact: EmailGovernedArtifact,
    ) -> RetentionEligibilityQuery:
        return RetentionEligibilityQuery(
            artifact=RetentionArtifactRef(
                workspace_id=artifact.workspace_id,
                environment_id=artifact.environment_id,
                artifact_class=artifact.artifact_class,
                artifact_id=artifact.artifact_id,
                artifact_version=artifact.artifact_version,
                artifact_digest=artifact.artifact_digest,
            )
        )

    @staticmethod
    def _allowed(
        lineage: EmailResolvedLineage,
        policy: ContentAuthorizationDecision,
        retention: RetentionEligibility,
    ) -> EmailGovernanceDecision:
        retention_by_source = {
            (item.source_object_id, item.source_revision_id): item
            for item in retention.source_decisions
        }
        return EmailGovernanceDecision(
            allowed=True,
            reason=EmailGovernanceReason.ALLOWED,
            evidence=EmailGovernanceEvidence(
                sources=tuple(
                    EmailGovernanceSourceEvidence(
                        source_object_id=source.source_object_id,
                        source_revision_id=source.source_revision_id,
                        retention_applicability_id=(
                            retention_by_source[
                                (source.source_object_id, source.source_revision_id)
                            ].applicability_id
                        ),
                        retention_applicability_generation=(
                            retention_by_source[
                                (source.source_object_id, source.source_revision_id)
                            ].applicability_generation
                        ),
                    )
                    for source in lineage.sources
                ),
                policy_decision_reference=policy.decision_reference,
            ),
        )

    @staticmethod
    def _denied() -> EmailGovernanceDecision:
        return EmailGovernanceDecision(
            allowed=False,
            reason=EmailGovernanceReason.UNAVAILABLE,
        )


class GovernedEmailDerivativeProcessingAuthority:
    """Bind the generic guard to processing with server-owned contexts."""

    def __init__(
        self,
        *,
        access: GovernedEmailArtifactAccess,
        plans: EmailDerivativePlanAuthority,
        authorization_context_factory: Callable[
            [UUID, UUID], TrustedAuthorizationContext
        ],
        retention_context_factory: Callable[
            [UUID, UUID], TrustedPersistenceContext
        ],
    ) -> None:
        self._access = access
        self._plans = plans
        self._authorization_context_factory = authorization_context_factory
        self._retention_context_factory = retention_context_factory

    async def authorize_plan(self, plan: EmailDerivativeProcessingPlan) -> None:
        try:
            allowed = await self._plans.authorize_plan(
                plan=plan,
                authorization_context=self._authorization_context_factory(
                    plan.workspace_id, plan.environment_id
                ),
                retention_context=self._retention_context_factory(
                    plan.workspace_id, plan.environment_id
                ),
            )
        except Exception:
            raise EmailGovernanceUnavailable() from None
        if not allowed:
            raise EmailGovernanceUnavailable()

    async def authorize_processing(self, artifact: EmailGovernedArtifact) -> None:
        try:
            decision = await self._access.decide(
                operation=EmailGovernanceOperation.PROCESS,
                artifact=artifact,
                authorization_context=self._authorization_context_factory(
                    artifact.workspace_id, artifact.environment_id
                ),
                retention_context=self._retention_context_factory(
                    artifact.workspace_id, artifact.environment_id
                ),
            )
        except Exception:
            raise EmailGovernanceUnavailable() from None
        if not decision.allowed:
            raise EmailGovernanceUnavailable()


__all__ = [
    "CurrentEmailConnectionAuthority",
    "EmailArtifactLineageAuthority",
    "EmailDisclosureFence",
    "EmailDerivativeProcessingAuthority",
    "EmailDerivativePlanAuthority",
    "EmailGovernanceDecision",
    "EmailGovernanceEvidence",
    "EmailGovernanceOperation",
    "EmailGovernanceReason",
    "EmailGovernanceUnavailable",
    "EmailRetentionEligibilityAuthority",
    "EmailGovernedArtifact",
    "EmailLineageRole",
    "EmailResolvedLineage",
    "EmailSourceLineage",
    "GovernedEmailArtifactAccess",
    "GovernedEmailDerivativeProcessingAuthority",
]
