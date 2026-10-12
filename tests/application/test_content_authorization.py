from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from spine.application.content_authorization import (
    AuthorizationAuditStatus,
    AuthorizationContextMode,
    AuthorizationDecisionKind,
    AuthorizationReasonCode,
    ContentAuthorizationDecision,
    ContentAuthorizationDecisionService,
    CurrentContentAuthorizationSnapshot,
    TrustedAuthorizationContext,
)
from spine.domain.sources import (
    AccessGrant,
    AccessPermission,
    AccessPolicyVersion,
    CurrentAccessPolicy,
    PolicyOriginKind,
    PrincipalKind,
    RevisionKind,
)


NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000002")
ENVIRONMENT_ID = UUID("20000000-0000-0000-0000-000000000001")
OTHER_ENVIRONMENT_ID = UUID("20000000-0000-0000-0000-000000000002")
HUMAN_ID = UUID("30000000-0000-0000-0000-000000000001")
TEAM_ID = UUID("30000000-0000-0000-0000-000000000002")
SERVICE_ID = UUID("40000000-0000-0000-0000-000000000001")
SOURCE_ID = UUID("50000000-0000-0000-0000-000000000001")
OTHER_SOURCE_ID = UUID("50000000-0000-0000-0000-000000000002")
REVISION_ID = UUID("60000000-0000-0000-0000-000000000001")
POLICY_ID = UUID("70000000-0000-0000-0000-000000000001")
ACTIVATION_ID = UUID("80000000-0000-0000-0000-000000000001")
AUTHORITY_SNAPSHOT_ID = UUID("90000000-0000-0000-0000-000000000001")
TRACE_ID = UUID("a0000000-0000-0000-0000-000000000001")


def grant(
    number: int,
    *,
    principal_kind: PrincipalKind,
    principal_id: UUID,
    permission: AccessPermission,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> AccessGrant:
    return AccessGrant(
        access_grant_id=UUID(int=number),
        principal_kind=principal_kind,
        principal_id=principal_id,
        permission=permission,
        purpose="answer_question" if principal_kind is PrincipalKind.SERVICE else None,
        operation="retrieve_context" if principal_kind is PrincipalKind.SERVICE else None,
        valid_from=valid_from,
        valid_until=valid_until,
    )


def policy(*grants_: AccessGrant, workspace_id: UUID = WORKSPACE_ID) -> AccessPolicyVersion:
    return AccessPolicyVersion(
        access_policy_version_id=POLICY_ID,
        workspace_id=workspace_id,
        environment_id=ENVIRONMENT_ID,
        source_object_id=SOURCE_ID,
        version=1,
        predecessor_policy_version_id=None,
        document_schema_version="access-policy.v1",
        document_hash="a" * 64,
        origin_kind=PolicyOriginKind.APPROVED_PROPOSAL,
        origin_reference=UUID("b0000000-0000-0000-0000-000000000001"),
        origin_version=1,
        origin_hash="b" * 64,
        created_by_canonical_human_id=HUMAN_ID,
        created_at=NOW,
        grants=grants_,
    )


def snapshot(
    policy_: AccessPolicyVersion | None,
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
    source_object_id: UUID = SOURCE_ID,
    current_revision_kind: RevisionKind | None = RevisionKind.CONTENT,
    provenance_complete: bool = True,
) -> CurrentContentAuthorizationSnapshot:
    current = None
    if policy_ is not None:
        current = CurrentAccessPolicy(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=source_object_id,
            access_policy_version_id=policy_.access_policy_version_id,
            policy_activation_id=ACTIVATION_ID,
            policy_generation=1,
            cas_version=1,
        )
    return CurrentContentAuthorizationSnapshot(
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=source_object_id,
        current_revision_id=REVISION_ID,
        current_revision_kind=current_revision_kind,
        provenance_complete=provenance_complete,
        current_policy=current,
        policy=policy_,
    )


def context(
    mode: AuthorizationContextMode,
    *,
    effective_team_ids: frozenset[UUID] = frozenset(),
    service_principal_id: UUID | None = None,
    authority_snapshot_reference: UUID = AUTHORITY_SNAPSHOT_ID,
) -> TrustedAuthorizationContext:
    return TrustedAuthorizationContext(
        mode=mode,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        canonical_human_id=(None if mode is AuthorizationContextMode.NONINTERACTIVE_PROCESSING else HUMAN_ID),
        effective_team_ids=effective_team_ids,
        service_principal_id=service_principal_id,
        purpose="answer_question",
        operation="retrieve_context",
        membership_generation=3,
        authority_generation=5,
        service_authority_generation=(
            7 if mode is not AuthorizationContextMode.HUMAN_DISCLOSURE else None
        ),
        authority_snapshot_reference=authority_snapshot_reference,
        trace_id=TRACE_ID,
        issued_at=NOW,
        noninteractive_authority_reference=(
            UUID("a1000000-0000-0000-0000-000000000001")
            if mode is AuthorizationContextMode.NONINTERACTIVE_PROCESSING
            else None
        ),
    )


class Identity:
    def __init__(self, current: TrustedAuthorizationContext | Exception) -> None:
        self.current = current

    async def revalidate(self, original: TrustedAuthorizationContext) -> TrustedAuthorizationContext:
        if isinstance(self.current, Exception):
            raise self.current
        return self.current


class Repository:
    def __init__(self, values: dict[UUID, CurrentContentAuthorizationSnapshot]) -> None:
        self.values = values
        self.calls: list[tuple[UUID, UUID, UUID]] = []

    async def resolve_current_content_authorization(
        self, *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID
    ) -> CurrentContentAuthorizationSnapshot | None:
        self.calls.append((workspace_id, environment_id, source_object_id))
        return self.values.get(source_object_id)


class NonInteractiveAuthority:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed

    async def authorize(self, context_: TrustedAuthorizationContext) -> bool:
        assert context_.mode is AuthorizationContextMode.NONINTERACTIVE_PROCESSING
        return self.allowed


class Audit:
    def __init__(self, failure: bool = False) -> None:
        self.failure = failure
        self.decisions: list[ContentAuthorizationDecision] = []

    async def record_denied(self, decision: ContentAuthorizationDecision) -> UUID:
        self.decisions.append(decision)
        if self.failure:
            raise RuntimeError("audit store is unavailable")
        return UUID("a2000000-0000-0000-0000-000000000001")


def service(
    repository: Repository,
    identity: Identity,
    *,
    noninteractive_authority: NonInteractiveAuthority | None = None,
    audit: Audit | None = None,
) -> ContentAuthorizationDecisionService:
    return ContentAuthorizationDecisionService(
        repository=repository,
        identity_authority=identity,
        noninteractive_authority=noninteractive_authority,
        denied_attempt_audit=audit,
        clock=lambda: NOW,
        decision_reference_factory=lambda: UUID("a3000000-0000-0000-0000-000000000001"),
    )


@pytest.mark.asyncio
async def test_human_disclosure_requires_current_direct_or_team_grant() -> None:
    current_policy = policy(
        grant(
            1,
            principal_kind=PrincipalKind.TEAM,
            principal_id=TEAM_ID,
            permission=AccessPermission.READ_CONTENT,
        )
    )
    repository = Repository({SOURCE_ID: snapshot(current_policy)})
    allowed_context = context(
        AuthorizationContextMode.HUMAN_DISCLOSURE,
        effective_team_ids=frozenset({TEAM_ID}),
    )
    decision_service = service(repository, Identity(allowed_context))

    allowed = await decision_service.decide_human_disclosure(allowed_context, [SOURCE_ID])
    denied_context = context(AuthorizationContextMode.HUMAN_DISCLOSURE)
    denied = await service(repository, Identity(denied_context)).decide_human_disclosure(
        denied_context, [SOURCE_ID]
    )

    assert allowed.allowed is True
    assert allowed.decision_kind is AuthorizationDecisionKind.HUMAN_DISCLOSURE
    assert len(allowed.policy_evidence) == 1
    assert denied.allowed is False
    assert denied.reason_code is AuthorizationReasonCode.AUTHORIZATION_DENIED
    assert denied.policy_evidence == ()


@pytest.mark.asyncio
async def test_interactive_processing_requires_human_and_unexpired_service_grants() -> None:
    current_policy = policy(
        grant(2, principal_kind=PrincipalKind.HUMAN, principal_id=HUMAN_ID, permission=AccessPermission.READ_CONTENT),
        grant(
            3,
            principal_kind=PrincipalKind.SERVICE,
            principal_id=SERVICE_ID,
            permission=AccessPermission.PROCESS_CONTENT,
            valid_from=NOW,
            valid_until=NOW + timedelta(minutes=1),
        ),
    )
    repository = Repository({SOURCE_ID: snapshot(current_policy)})
    identity = Identity(context(AuthorizationContextMode.INTERACTIVE_PROCESSING, service_principal_id=SERVICE_ID))
    decision_service = service(repository, identity)

    allowed = await decision_service.decide_interactive_processing(
        context(AuthorizationContextMode.INTERACTIVE_PROCESSING, service_principal_id=SERVICE_ID),
        [SOURCE_ID],
    )
    service_mismatch = current_policy.model_copy(
        update={
            "grants": (
                current_policy.grants[0],
                current_policy.grants[1].model_copy(update={"operation": "write_result"}),
            )
        }
    )
    repository.values[SOURCE_ID] = snapshot(service_mismatch)
    denied = await decision_service.decide_interactive_processing(
        context(AuthorizationContextMode.INTERACTIVE_PROCESSING, service_principal_id=SERVICE_ID),
        [SOURCE_ID],
    )

    assert allowed.allowed is True
    assert denied.allowed is False
    assert denied.reason_code is AuthorizationReasonCode.AUTHORIZATION_DENIED


@pytest.mark.asyncio
async def test_noninteractive_processing_needs_server_authority_and_cannot_disclose_to_human() -> None:
    current_policy = policy(
        grant(
            4,
            principal_kind=PrincipalKind.SERVICE,
            principal_id=SERVICE_ID,
            permission=AccessPermission.PROCESS_CONTENT,
            valid_from=NOW,
            valid_until=NOW + timedelta(minutes=1),
        )
    )
    repository = Repository({SOURCE_ID: snapshot(current_policy)})
    service_context = context(
        AuthorizationContextMode.NONINTERACTIVE_PROCESSING,
        service_principal_id=SERVICE_ID,
    )
    decision_service = service(
        repository,
        Identity(service_context),
        noninteractive_authority=NonInteractiveAuthority(True),
    )

    allowed = await decision_service.decide_noninteractive_processing(service_context, [SOURCE_ID])
    human_attempt = await decision_service.decide_human_disclosure(service_context, [SOURCE_ID])

    assert allowed.allowed is True
    assert allowed.decision_kind is AuthorizationDecisionKind.NONINTERACTIVE_PROCESSING
    assert human_attempt.allowed is False
    assert human_attempt.reason_code is AuthorizationReasonCode.CONTEXT_INVALID


@pytest.mark.asyncio
async def test_revoked_current_policy_is_denied_on_next_decision_without_cache() -> None:
    allowed_policy = policy(
        grant(5, principal_kind=PrincipalKind.HUMAN, principal_id=HUMAN_ID, permission=AccessPermission.READ_CONTENT)
    )
    repository = Repository({SOURCE_ID: snapshot(allowed_policy)})
    decision_service = service(
        repository,
        Identity(context(AuthorizationContextMode.HUMAN_DISCLOSURE)),
    )
    context_ = context(AuthorizationContextMode.HUMAN_DISCLOSURE)

    assert (await decision_service.decide_human_disclosure(context_, [SOURCE_ID])).allowed
    repository.values[SOURCE_ID] = snapshot(policy())
    denied = await decision_service.decide_human_disclosure(context_, [SOURCE_ID])

    assert denied.allowed is False
    assert len(repository.calls) == 2


@pytest.mark.asyncio
async def test_changed_authority_generation_requires_reauthorization() -> None:
    current_policy = policy(
        grant(8, principal_kind=PrincipalKind.HUMAN, principal_id=HUMAN_ID, permission=AccessPermission.READ_CONTENT)
    )
    repository = Repository({SOURCE_ID: snapshot(current_policy)})
    original_context = context(AuthorizationContextMode.HUMAN_DISCLOSURE)
    changed_context = original_context.model_copy(
        update={"membership_generation": original_context.membership_generation + 1}
    )

    decision = await service(repository, Identity(changed_context)).decide_human_disclosure(
        original_context, [SOURCE_ID]
    )

    assert decision.allowed is False
    assert decision.reason_code is AuthorizationReasonCode.AUTHORITY_STALE
    assert repository.calls == []


@pytest.mark.asyncio
async def test_two_tenant_scope_mismatch_and_unknown_state_fail_closed_and_audit_safely() -> None:
    audit = Audit(failure=True)
    foreign_policy = policy(
        grant(6, principal_kind=PrincipalKind.HUMAN, principal_id=HUMAN_ID, permission=AccessPermission.READ_CONTENT),
        workspace_id=OTHER_WORKSPACE_ID,
    ).model_copy(update={"environment_id": OTHER_ENVIRONMENT_ID, "source_object_id": OTHER_SOURCE_ID})
    repository = Repository({OTHER_SOURCE_ID: snapshot(foreign_policy, workspace_id=OTHER_WORKSPACE_ID, environment_id=OTHER_ENVIRONMENT_ID, source_object_id=OTHER_SOURCE_ID)})
    decision_service = service(repository, Identity(context(AuthorizationContextMode.HUMAN_DISCLOSURE)), audit=audit)

    denied = await decision_service.decide_human_disclosure(context(AuthorizationContextMode.HUMAN_DISCLOSURE), [OTHER_SOURCE_ID])
    missing = await decision_service.decide_human_disclosure(context(AuthorizationContextMode.HUMAN_DISCLOSURE), [SOURCE_ID])

    assert denied.allowed is False
    assert denied.reason_code is AuthorizationReasonCode.SOURCE_UNAVAILABLE
    assert denied.audit_status is AuthorizationAuditStatus.UNAVAILABLE
    assert missing.allowed is False
    assert missing.reason_code is AuthorizationReasonCode.SOURCE_UNAVAILABLE
    assert "foreign" not in denied.model_dump_json()


@pytest.mark.asyncio
async def test_expired_service_grant_denies_at_valid_until() -> None:
    current_policy = policy(
        grant(
            7,
            principal_kind=PrincipalKind.SERVICE,
            principal_id=SERVICE_ID,
            permission=AccessPermission.PROCESS_CONTENT,
            valid_from=NOW - timedelta(minutes=1),
            valid_until=NOW,
        )
    )
    repository = Repository({SOURCE_ID: snapshot(current_policy)})
    service_context = context(
        AuthorizationContextMode.NONINTERACTIVE_PROCESSING,
        service_principal_id=SERVICE_ID,
    )
    decision_service = service(
        repository,
        Identity(service_context),
        noninteractive_authority=NonInteractiveAuthority(True),
    )

    decision = await decision_service.decide_noninteractive_processing(service_context, [SOURCE_ID])

    assert decision.allowed is False
    assert decision.reason_code is AuthorizationReasonCode.AUTHORIZATION_DENIED


@pytest.mark.asyncio
async def test_tombstone_or_any_unauthorized_source_denies_multi_source_disclosure() -> None:
    current_policy = policy(
        grant(9, principal_kind=PrincipalKind.HUMAN, principal_id=HUMAN_ID, permission=AccessPermission.READ_CONTENT)
    )
    repository = Repository(
        {
            SOURCE_ID: snapshot(current_policy),
            OTHER_SOURCE_ID: snapshot(
                current_policy.model_copy(update={"source_object_id": OTHER_SOURCE_ID}),
                source_object_id=OTHER_SOURCE_ID,
                current_revision_kind=RevisionKind.TOMBSTONE,
            ),
        }
    )
    disclosure_context = context(AuthorizationContextMode.HUMAN_DISCLOSURE)
    decision = await service(repository, Identity(disclosure_context)).decide_human_disclosure(
        disclosure_context, [SOURCE_ID, OTHER_SOURCE_ID]
    )

    assert decision.allowed is False
    assert decision.reason_code is AuthorizationReasonCode.SOURCE_UNAVAILABLE
    assert decision.policy_evidence == ()
