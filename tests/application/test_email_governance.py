from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from spine.application.content_authorization import (
    AuthorizationContextMode,
    AuthorizationEvidence,
    AuthorizationReasonCode,
    AuthorizationDecisionKind,
    ContentAuthorizationDecision,
    TrustedAuthorizationContext,
)
from spine.application.email_governance import (
    EmailGovernanceOperation,
    EmailGovernanceReason,
    EmailGovernedArtifact,
    EmailLineageRole,
    EmailResolvedLineage,
    EmailSourceLineage,
    GovernedEmailArtifactAccess,
)
from spine.domain.artifacts.email_governance import (
    EmailConnectionAuthorityStatus,
    EmailCurrentSourceAuthority,
    EmailProviderPresence,
    EmailSourceLifecycle,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.processing_disposition import (
    OrdinaryProcessingEligibility,
    OrdinaryProcessingEligibilityReason,
)
from spine.application.retention import (
    RetentionEligibility,
    RetentionEligibilityStatus,
    RetentionSourceDecision,
)
from spine.domain.common import ContextOrigin
from spine.domain.sources.retention import RetentionArtifactClass


WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
MESSAGE = UUID(int=10)
MESSAGE_REVISION = UUID(int=11)
ATTACHMENT = UUID(int=20)
ATTACHMENT_REVISION = UUID(int=21)
NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


def _artifact(
    artifact_class: RetentionArtifactClass,
    *,
    workspace_id: UUID = WORKSPACE,
) -> EmailGovernedArtifact:
    return EmailGovernedArtifact(
        workspace_id=workspace_id,
        environment_id=ENVIRONMENT,
        artifact_class=artifact_class,
        artifact_id=UUID(int=30),
        artifact_version=1,
        artifact_digest="a" * 64,
    )


def _lineage(artifact: EmailGovernedArtifact) -> EmailResolvedLineage:
    sources = [
        EmailSourceLineage(
            role=EmailLineageRole.MESSAGE,
            source_object_id=MESSAGE,
            source_revision_id=MESSAGE_REVISION,
        )
    ]
    if artifact.artifact_class in {
        RetentionArtifactClass.EMAIL_ATTACHMENT_ORIGINAL,
        RetentionArtifactClass.EMAIL_ATTACHMENT_ARTIFACT,
    }:
        sources.append(
            EmailSourceLineage(
                role=EmailLineageRole.ATTACHMENT,
                source_object_id=ATTACHMENT,
                source_revision_id=ATTACHMENT_REVISION,
            )
        )
    return EmailResolvedLineage(artifact=artifact, sources=tuple(sources))


def _authorization_context(
    *,
    workspace_id: UUID = WORKSPACE,
    mode: AuthorizationContextMode = AuthorizationContextMode.HUMAN_DISCLOSURE,
) -> TrustedAuthorizationContext:
    processing = mode is AuthorizationContextMode.NONINTERACTIVE_PROCESSING
    return TrustedAuthorizationContext(
        mode=mode,
        workspace_id=workspace_id,
        environment_id=ENVIRONMENT,
        canonical_human_id=None if processing else UUID(int=40),
        service_principal_id=UUID(int=45) if processing else None,
        purpose="inspect_evidence",
        operation="retrieve_email_artifact",
        membership_generation=1,
        authority_generation=1,
        service_authority_generation=1 if processing else None,
        authority_snapshot_reference=UUID(int=41),
        trace_id=UUID(int=42),
        issued_at=NOW,
        noninteractive_authority_reference=UUID(int=46) if processing else None,
    )


def _retention_context(
    *, workspace_id: UUID = WORKSPACE
) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(
            workspace_id=workspace_id,
            environment_id=ENVIRONMENT,
        ),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("retention_governance"),
        operation=PersistenceOperation("retention_eligibility"),
        trace_id=UUID(int=43),
        acting_subject_id=UUID(int=40),
        service_principal_id=None,
        provenance=TrustedContextProvenance(UUID(int=44), b"verified"),
    )


class _Lineages:
    def __init__(self, lineage: EmailResolvedLineage) -> None:
        self.lineage = lineage
        self.calls = 0

    async def resolve_email_lineage(self, artifact):
        self.calls += 1
        return self.lineage


class _Connections:
    def __init__(
        self,
        denied_source: UUID | None = None,
        *,
        lifecycle: EmailSourceLifecycle = EmailSourceLifecycle.CURRENT,
        provider_presence: EmailProviderPresence = EmailProviderPresence.PRESENT,
    ) -> None:
        self.denied_source = denied_source
        self.lifecycle = lifecycle
        self.provider_presence = provider_presence

    async def resolve_current_content(self, **values):
        return EmailCurrentSourceAuthority(
            connection_status=(
                EmailConnectionAuthorityStatus.REVOKED
                if values["source_object_id"] == self.denied_source
                else EmailConnectionAuthorityStatus.ACTIVE
            ),
            provider_presence=self.provider_presence,
            lifecycle=self.lifecycle,
            authority_generation=1,
            authority_reference=UUID(int=200 + values["source_object_id"].int),
        )


class _Processing:
    def __init__(self, denied_source: UUID | None = None) -> None:
        self.denied_source = denied_source

    async def resolve(self, **values):
        if values["source_object_id"] == self.denied_source:
            return OrdinaryProcessingEligibility(
                eligible=False,
                reason=OrdinaryProcessingEligibilityReason.CURRENT_REVISION_MISMATCH,
            )
        return OrdinaryProcessingEligibility(
            eligible=True,
            reason=OrdinaryProcessingEligibilityReason.ELIGIBLE,
            canonical_acceptance_id=UUID(int=50 + values["source_object_id"].int),
        )


class _Authorization:
    def __init__(
        self,
        *,
        allowed: bool = True,
        revision_override: UUID | None = None,
        fail: bool = False,
        revisions: dict[UUID, UUID] | None = None,
    ) -> None:
        self.allowed = allowed
        self.revision_override = revision_override
        self.fail = fail
        self.revisions = revisions or {
            MESSAGE: MESSAGE_REVISION,
            ATTACHMENT: ATTACHMENT_REVISION,
        }
        self.source_ids = ()
        self.calls = 0

    async def _decision(self, context, source_ids, decision_kind):
        self.calls += 1
        if self.fail:
            raise RuntimeError(
                "synthetic subject alice@example.test secret.txt private excerpt"
            )
        self.source_ids = tuple(source_ids)
        evidence = tuple(
            AuthorizationEvidence(
                source_object_id=source_id,
                source_revision_id=(
                    self.revision_override or self.revisions[source_id]
                ),
                policy_version_id=UUID(int=80 + source_id.int),
                retention_eligible=True,
                policy_generation=1,
            )
            for source_id in self.source_ids
        )
        return ContentAuthorizationDecision(
            allowed=self.allowed,
            decision_reference=UUID(int=60 + self.calls),
            decision_kind=decision_kind,
            workspace_id=context.workspace_id,
            environment_id=context.environment_id,
            source_object_ids=self.source_ids,
            purpose=context.purpose,
            operation=context.operation,
            policy_evidence=evidence if self.allowed else (),
            membership_generation=context.membership_generation,
            authority_generation=context.authority_generation,
            service_authority_generation=context.service_authority_generation,
            authority_snapshot_reference=context.authority_snapshot_reference,
            reason_code=(
                AuthorizationReasonCode.ALLOWED
                if self.allowed
                else AuthorizationReasonCode.AUTHORIZATION_DENIED
            ),
            trace_id=context.trace_id,
            evaluated_at=NOW + timedelta(seconds=self.calls),
        )

    async def decide_human_disclosure(self, context, source_ids):
        return await self._decision(
            context, source_ids, AuthorizationDecisionKind.HUMAN_DISCLOSURE
        )

    async def decide_interactive_processing(self, _context, source_ids):
        return await self._decision(
            _context, source_ids, AuthorizationDecisionKind.INTERACTIVE_PROCESSING
        )

    async def decide_noninteractive_processing(self, _context, source_ids):
        return await self._decision(
            _context, source_ids, AuthorizationDecisionKind.NONINTERACTIVE_PROCESSING
        )


class _Retention:
    def __init__(
        self,
        lineage: EmailResolvedLineage,
        *,
        eligible: bool = True,
        reverse: bool = False,
    ) -> None:
        self.lineage = lineage
        self.eligible = eligible
        self.reverse = reverse

    async def resolve(self, query, _context):
        status = (
            RetentionEligibilityStatus.ELIGIBLE
            if self.eligible
            else RetentionEligibilityStatus.MISSING
        )
        decisions = tuple(
            RetentionSourceDecision(
                source_object_id=item.source_object_id,
                source_revision_id=item.source_revision_id,
                status=status,
                applicability_id=UUID(int=70 + index) if self.eligible else None,
                applicability_generation=1 if self.eligible else None,
            )
            for index, item in enumerate(self.lineage.sources)
        )
        if self.reverse:
            decisions = tuple(reversed(decisions))
        return RetentionEligibility(
            workspace_id=query.artifact.workspace_id,
            environment_id=query.artifact.environment_id,
            artifact_class=query.artifact.artifact_class,
            status=status,
            source_decisions=decisions,
        )


class _DisclosureFence:
    def __init__(
        self,
        *,
        allowed: bool = True,
        values: tuple[bool, ...] | None = None,
    ) -> None:
        self.allowed = allowed
        self.values = list(values or ())
        self.calls = 0

    async def allows_disclosure(self, **_values):
        self.calls += 1
        if self.values:
            return self.values.pop(0)
        return self.allowed


def _guard(
    artifact: EmailGovernedArtifact,
    *,
    denied_connection: UUID | None = None,
    denied_current: UUID | None = None,
    policy_allowed: bool = True,
    retention_eligible: bool = True,
    disclosure_allowed: bool = True,
    policy_revision_override: UUID | None = None,
    policy_failure: bool = False,
    lineage: EmailResolvedLineage | None = None,
    source_lifecycle: EmailSourceLifecycle = EmailSourceLifecycle.CURRENT,
    provider_presence: EmailProviderPresence = EmailProviderPresence.PRESENT,
    disclosure_sequence: tuple[bool, ...] | None = None,
    reverse_retention: bool = False,
):
    lineage = lineage or _lineage(artifact)
    lineages = _Lineages(lineage)
    authorization = _Authorization(
        allowed=policy_allowed,
        revision_override=policy_revision_override,
        fail=policy_failure,
        revisions={
            item.source_object_id: item.source_revision_id
            for item in lineage.sources
        },
    )
    fence = _DisclosureFence(
        allowed=disclosure_allowed,
        values=disclosure_sequence,
    )
    guard = GovernedEmailArtifactAccess(
        lineages=lineages,
        connections=_Connections(
            denied_connection,
            lifecycle=source_lifecycle,
            provider_presence=provider_presence,
        ),
        processing=_Processing(denied_current),
        authorization=authorization,
        retention=_Retention(
            lineage,
            eligible=retention_eligible,
            reverse=reverse_retention,
        ),
        disclosure_fence=fence,
    )
    return guard, lineages, authorization, fence


async def _decide(
    guard,
    artifact,
    *,
    workspace_id=WORKSPACE,
    operation=EmailGovernanceOperation.RETRIEVE,
):
    mode = (
        AuthorizationContextMode.HUMAN_DISCLOSURE
        if operation is EmailGovernanceOperation.RETRIEVE
        else AuthorizationContextMode.NONINTERACTIVE_PROCESSING
    )
    return await guard.decide(
        operation=operation,
        artifact=artifact,
        authorization_context=_authorization_context(
            workspace_id=workspace_id,
            mode=mode,
        ),
        retention_context=_retention_context(workspace_id=workspace_id),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact_class", "operation"),
    [
        (artifact_class, operation)
        for artifact_class in (
            RetentionArtifactClass.EMAIL_RAW_MIME,
            RetentionArtifactClass.EMAIL_PARSED_BODY,
            RetentionArtifactClass.EMAIL_ATTACHMENT_ORIGINAL,
            RetentionArtifactClass.EMAIL_ATTACHMENT_ARTIFACT,
            RetentionArtifactClass.EMAIL_PROJECTION,
        )
        for operation in (
            EmailGovernanceOperation.PROCESS,
            EmailGovernanceOperation.RETRIEVE,
        )
    ],
)
async def test_current_policy_retention_and_lineage_allow_every_email_artifact_class(
    artifact_class: RetentionArtifactClass,
    operation: EmailGovernanceOperation,
) -> None:
    artifact = _artifact(artifact_class)
    guard, _lineages, authorization, _fence = _guard(artifact)

    decision = await _decide(guard, artifact, operation=operation)

    assert decision.allowed is True
    assert decision.reason is EmailGovernanceReason.ALLOWED
    assert decision.evidence is not None
    assert authorization.source_ids == tuple(
        item.source_object_id for item in _lineage(artifact).sources
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", [MESSAGE, ATTACHMENT])
async def test_parent_or_child_revocation_denies_attachment_with_identical_public_shape(
    denied: UUID,
) -> None:
    artifact = _artifact(RetentionArtifactClass.EMAIL_ATTACHMENT_ARTIFACT)
    guard, _lineages, _authorization, _fence = _guard(
        artifact, denied_connection=denied
    )

    decision = await _decide(guard, artifact)

    assert decision.model_dump(mode="json") == {
        "allowed": False,
        "reason": "unavailable",
        "evidence": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "guard_values",
    [
        {"policy_allowed": False},
        {"retention_eligible": False},
        {"disclosure_allowed": False},
    ],
)
async def test_missing_policy_retention_or_disclosure_fence_fails_closed(
    guard_values: dict[str, object],
) -> None:
    artifact = _artifact(RetentionArtifactClass.EMAIL_PARSED_BODY)
    guard, _lineages, _authorization, _fence = _guard(artifact, **guard_values)

    decision = await _decide(guard, artifact)

    assert decision.allowed is False
    assert decision.reason is EmailGovernanceReason.UNAVAILABLE
    assert decision.evidence is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lifecycle",
    [EmailSourceLifecycle.STALE, EmailSourceLifecycle.TOMBSTONED],
)
async def test_stale_or_tombstoned_source_fails_closed_before_policy_resolution(
    lifecycle: EmailSourceLifecycle,
) -> None:
    artifact = _artifact(RetentionArtifactClass.EMAIL_PROJECTION)
    guard, _lineages, authorization, _fence = _guard(
        artifact,
        source_lifecycle=lifecycle,
    )

    decision = await _decide(guard, artifact)

    assert decision.allowed is False
    assert decision.evidence is None
    assert authorization.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "presence",
    [EmailProviderPresence.ABSENT, EmailProviderPresence.DELETED],
)
async def test_provider_absence_and_deletion_are_distinct_from_access_decision(
    presence: EmailProviderPresence,
) -> None:
    artifact = _artifact(RetentionArtifactClass.EMAIL_RAW_MIME)
    guard, _lineages, authorization, _fence = _guard(
        artifact,
        provider_presence=presence,
    )

    decision = await _decide(guard, artifact)

    assert decision.allowed is False
    assert authorization.calls == 0


@pytest.mark.asyncio
async def test_wrong_workspace_denies_before_resolving_artifact_existence() -> None:
    artifact = _artifact(RetentionArtifactClass.EMAIL_PARSED_BODY)
    guard, lineages, _authorization, _fence = _guard(artifact)

    decision = await _decide(guard, artifact, workspace_id=UUID(int=999))

    assert decision.allowed is False
    assert decision.evidence is None
    assert lineages.calls == 0


@pytest.mark.asyncio
async def test_policy_evidence_for_another_revision_cannot_authorize_lineage() -> None:
    artifact = _artifact(RetentionArtifactClass.EMAIL_PARSED_BODY)
    guard, _lineages, _authorization, _fence = _guard(
        artifact,
        policy_revision_override=UUID(int=999),
    )

    decision = await _decide(guard, artifact)

    assert decision.allowed is False
    assert decision.evidence is None


@pytest.mark.asyncio
async def test_authority_failure_is_content_free_and_does_not_escape_private_details() -> None:
    artifact = _artifact(RetentionArtifactClass.EMAIL_PARSED_BODY)
    guard, _lineages, _authorization, _fence = _guard(
        artifact,
        policy_failure=True,
    )

    decision = await _decide(guard, artifact)

    serialized = str(decision.model_dump(mode="json"))
    assert serialized == "{'allowed': False, 'reason': 'unavailable', 'evidence': None}"
    assert "subject" not in serialized
    assert "alice@example.test" not in serialized
    assert "secret.txt" not in serialized
    assert "private excerpt" not in serialized


@pytest.mark.asyncio
async def test_disclosure_fence_closing_during_revalidation_denies_access() -> None:
    artifact = _artifact(RetentionArtifactClass.EMAIL_PARSED_BODY)
    guard, _lineages, _authorization, fence = _guard(
        artifact,
        disclosure_sequence=(True, False),
    )

    decision = await _decide(guard, artifact)

    assert decision.allowed is False
    assert decision.evidence is None
    assert fence.calls == 2


@pytest.mark.asyncio
async def test_retention_evidence_is_matched_by_source_not_repository_order() -> None:
    artifact = _artifact(RetentionArtifactClass.EMAIL_ATTACHMENT_ARTIFACT)
    guard, _lineages, _authorization, _fence = _guard(
        artifact,
        reverse_retention=True,
    )

    decision = await _decide(guard, artifact)

    assert decision.allowed is True
    assert decision.evidence is not None
    assert tuple(
        (
            item.source_object_id,
            item.source_revision_id,
            item.retention_applicability_id,
        )
        for item in decision.evidence.sources
    ) == (
        (MESSAGE, MESSAGE_REVISION, UUID(int=70)),
        (ATTACHMENT, ATTACHMENT_REVISION, UUID(int=71)),
    )


@pytest.mark.asyncio
async def test_reappeared_current_revision_is_allowed_only_after_all_authorities_revalidate() -> None:
    tombstoned_artifact = _artifact(RetentionArtifactClass.EMAIL_RAW_MIME)
    tombstoned, _lineages, _authorization, _fence = _guard(
        tombstoned_artifact,
        source_lifecycle=EmailSourceLifecycle.TOMBSTONED,
    )
    denied = await _decide(tombstoned, tombstoned_artifact)
    reappeared_artifact = tombstoned_artifact.model_copy(
        update={"artifact_id": UUID(int=31), "artifact_digest": "b" * 64}
    )
    reappeared_lineage = EmailResolvedLineage(
        artifact=reappeared_artifact,
        sources=(
            EmailSourceLineage(
                role=EmailLineageRole.MESSAGE,
                source_object_id=MESSAGE,
                source_revision_id=UUID(int=12),
            ),
        ),
    )
    guard, _lineages, _authorization, _fence = _guard(
        reappeared_artifact,
        lineage=reappeared_lineage,
    )

    decision = await _decide(guard, reappeared_artifact)

    assert denied.allowed is False
    assert decision.allowed is True
    assert decision.evidence is not None
    assert tuple(item.source_revision_id for item in decision.evidence.sources) == (
        UUID(int=12),
    )


def test_retention_artifact_classes_are_distinct_from_legal_hold_and_cleanup() -> None:
    values = {item.value for item in RetentionArtifactClass}

    assert {
        "email_raw_mime",
        "email_parsed_body",
        "email_attachment_original",
        "email_attachment_artifact",
        "email_projection",
    } <= values
    assert "legal_hold" not in values
    assert "physical_cleanup" not in values
