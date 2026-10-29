"""Production authorization adapter for parsed-content inspection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.auth.contracts import RequestedAuthorizationScope
from spine.application.content_authorization import (
    AuthorizationContextMode,
    ContentAuthorizationDecision,
    ContentAuthorizationDecisionService,
    CurrentContentAuthorizationSnapshot,
    TrustedAuthorizationContext,
)
from spine.application.diagnostics import AuditEventRegistry
from spine.application.parsed_content_inspection import (
    ParsedContentDecisionAudit,
    ParsedContentInspectionRequester,
)
from spine.application.transcript_publication import TranscriptAuthorizationSnapshot
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.sources import RevisionKind
from spine.domain.audit import (
    AuditObjectReference,
    AuditOutcome,
    ObjectAccessDecisionAuditPayload,
)


class _HumanAuthorityResolver(Protocol):
    async def resolve_human_authority(
        self,
        *,
        principal_id: UUID,
        requested_scope: RequestedAuthorizationScope,
    ) -> int | None: ...


@dataclass(frozen=True, slots=True)
class ProductionParsedContentAuthorizer:
    """Revalidate one interactive human and the current source policy."""

    uow_factory: UnitOfWorkFactory
    authority_resolver: _HumanAuthorityResolver

    async def decide_read_content(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        source_object_id: UUID,
        source_revision_id: UUID,
    ) -> ContentAuthorizationDecision:
        del source_revision_id
        scope = context.scope
        if not isinstance(scope, EnvironmentScope):
            raise ValueError("parsed content authorization requires environment scope")
        trusted = TrustedAuthorizationContext(
            mode=AuthorizationContextMode.HUMAN_DISCLOSURE,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            canonical_human_id=requester.subject_id,
            effective_team_ids=requester.effective_team_ids,
            service_principal_id=None,
            purpose=context.purpose.value,
            operation=context.operation.value,
            membership_generation=requester.authorization_generation,
            authority_generation=requester.authorization_generation,
            service_authority_generation=None,
            authority_snapshot_reference=uuid5(
                NAMESPACE_URL,
                f"spine:parsed-content-authority:v1:{context.trace_id}",
            ),
            trace_id=context.trace_id,
            issued_at=datetime.now(timezone.utc),
        )
        repository = _RequestContentAuthorizationRepository(
            uow_factory=self.uow_factory,
            context=context,
        )
        identity_authority = _RequestIdentityAuthority(
            authority_resolver=self.authority_resolver,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
        )
        return await ContentAuthorizationDecisionService(
            repository=repository,
            identity_authority=identity_authority,
        ).decide_human_disclosure(trusted, (source_object_id,))


@dataclass(frozen=True, slots=True)
class UnitOfWorkParsedContentDecisionAudit(ParsedContentDecisionAudit):
    """Commit object-level authorization evidence before content disclosure."""

    uow_factory: UnitOfWorkFactory
    audit_events: AuditEventRegistry

    async def record_decision(
        self,
        *,
        context: TrustedPersistenceContext,
        decision: ContentAuthorizationDecision,
        requested_source_revision_id: UUID,
    ) -> UUID:
        scope = context.scope
        if (
            not isinstance(scope, EnvironmentScope)
            or decision.workspace_id != scope.workspace_id
            or decision.environment_id != scope.environment_id
            or decision.trace_id != context.trace_id
            or decision.purpose != context.purpose.value
            or decision.operation != context.operation.value
            or len(decision.source_object_ids) != 1
        ):
            raise ValueError("parsed content decision audit scope is invalid")
        source_object_id = decision.source_object_ids[0]
        evidence = decision.policy_evidence[0] if decision.allowed else None
        if evidence is not None and evidence.source_object_id != source_object_id:
            raise ValueError("parsed content decision audit evidence is invalid")
        event = self.audit_events.build_event(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            event_type="access.decision",
            schema_version=3,
            payload=ObjectAccessDecisionAuditPayload(
                purpose=decision.purpose,
                operation=decision.operation,
                decision_kind=decision.decision_kind.value,
                decision_reference=decision.decision_reference,
                authority_snapshot_reference=decision.authority_snapshot_reference,
                membership_generation=decision.membership_generation,
                authority_generation=decision.authority_generation,
                service_authority_generation=decision.service_authority_generation,
                source_revision_id=requested_source_revision_id,
                authorized_source_revision_id=(
                    None if evidence is None else evidence.source_revision_id
                ),
                policy_version_id=(
                    None if evidence is None else evidence.policy_version_id
                ),
                policy_generation=(
                    None if evidence is None else evidence.policy_generation
                ),
            ),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=decision.evaluated_at,
            target=AuditObjectReference(
                object_type="source_object",
                object_id=source_object_id,
                schema_version=1,
            ),
            outcome=(AuditOutcome.ALLOWED if decision.allowed else AuditOutcome.DENIED),
            reason=decision.reason_code.value,
            producer_deduplication_id=(
                f"parsed_content.authorization.{decision.decision_reference.hex}"
            ),
        )
        async with self.uow_factory(context) as uow:
            audit_reference = await uow.audit.append(event)
            await uow.commit()
        return audit_reference


@dataclass(frozen=True, slots=True)
class ProductionTranscriptEvidenceAuthorization:
    """Current human source authorization and revision state for transcript reads."""

    uow_factory: UnitOfWorkFactory
    authorizer: ProductionParsedContentAuthorizer
    decision_audit: UnitOfWorkParsedContentDecisionAudit

    async def recheck(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        source_revision_id: UUID,
        source_revision_digest: str,
        transcript_artifact_id: UUID,
    ) -> TranscriptAuthorizationSnapshot:
        del transcript_artifact_id
        scope = context.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id,
            scope.environment_id,
        ) != (workspace_id, environment_id):
            return TranscriptAuthorizationSnapshot(
                allowed=False,
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_object_id=source_object_id,
                authorization_generation=requester.authorization_generation,
                reason_code="scope_denied",
            )
        decision = await self.authorizer.decide_read_content(
            context=context,
            requester=requester,
            source_object_id=source_object_id,
            source_revision_id=source_revision_id,
        )
        await self.decision_audit.record_decision(
            context=context,
            decision=decision,
            requested_source_revision_id=source_revision_id,
        )
        current_revision_id = None
        current_revision_digest = None
        if decision.allowed:
            async with self.uow_factory(context) as uow:
                head = await uow.canonical.resolve_current(source_object_id)
                revision = (
                    None
                    if head is None
                    else await uow.sources.resolve_revision(head.current_revision_id)
                )
                if revision is not None:
                    current_revision_id = revision.revision_id
                    current_revision_digest = revision.revision_digest
        allowed = bool(
            decision.allowed
            and decision.workspace_id == workspace_id
            and decision.environment_id == environment_id
            and decision.source_object_ids == (source_object_id,)
            and current_revision_id is not None
            and current_revision_digest is not None
        )
        return TranscriptAuthorizationSnapshot(
            allowed=allowed,
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=source_object_id,
            current_revision_id=current_revision_id,
            current_revision_digest=current_revision_digest,
            authorization_generation=requester.authorization_generation,
            reason_code="allowed" if allowed else "authorization_denied",
        )


@dataclass(frozen=True, slots=True)
class _RequestContentAuthorizationRepository:
    uow_factory: UnitOfWorkFactory
    context: TrustedPersistenceContext

    async def resolve_current_content_authorization(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
    ) -> CurrentContentAuthorizationSnapshot | None:
        scope = self.context.scope
        if not isinstance(scope, EnvironmentScope) or (
            workspace_id,
            environment_id,
        ) != (scope.workspace_id, scope.environment_id):
            return None
        async with self.uow_factory(self.context) as uow:
            source = await uow.sources.resolve_source(source_object_id)
            head = await uow.canonical.resolve_current(source_object_id)
            current_policy = await uow.access_policies.resolve_current(
                source_object_id
            )
            revision = (
                None
                if head is None
                else await uow.sources.resolve_revision(head.current_revision_id)
            )
            policy = (
                None
                if current_policy is None
                else await uow.access_policies.resolve_policy(
                    current_policy.access_policy_version_id
                )
            )
            provenance = (
                ()
                if revision is None
                else await uow.sources.list_provenance(revision.revision_id)
            )
            await uow.commit()
        if source is None or head is None or revision is None:
            return None
        return CurrentContentAuthorizationSnapshot(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=source_object_id,
            current_revision_id=head.current_revision_id,
            current_revision_kind=head.current_revision_kind,
            current_revision_observed_at=revision.observed_at,
            retention_eligible=head.current_revision_kind is RevisionKind.CONTENT,
            provenance_complete=bool(provenance),
            current_policy=current_policy,
            policy=policy,
        )


class _RequestIdentityAuthority:
    def __init__(
        self,
        *,
        authority_resolver: _HumanAuthorityResolver,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> None:
        self._resolver = authority_resolver
        self._workspace_id = workspace_id
        self._environment_id = environment_id

    async def revalidate(
        self, context: TrustedAuthorizationContext
    ) -> TrustedAuthorizationContext:
        if (
            context.workspace_id != self._workspace_id
            or context.environment_id != self._environment_id
            or context.canonical_human_id is None
            or context.service_principal_id is not None
        ):
            raise ValueError("parsed content authority scope changed")
        generation = await self._resolver.resolve_human_authority(
            principal_id=context.canonical_human_id,
            requested_scope=RequestedAuthorizationScope(
                workspace_id=self._workspace_id,
                environment_id=self._environment_id,
            ),
        )
        if generation is None:
            raise ValueError("parsed content human authority is unavailable")
        return context.model_copy(
            update={
                "membership_generation": generation,
                "authority_generation": generation,
            }
        )


__all__ = [
    "ProductionParsedContentAuthorizer",
    "UnitOfWorkParsedContentDecisionAudit",
]
