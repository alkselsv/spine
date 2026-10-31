from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID

import pytest

from spine.application.authorized_retrieval_scope import (
    AuthorizedRetrievalScopeOutcome,
    AuthorizedRetrievalScopeReason,
    AuthorizedRetrievalScopeRequest,
    AuthorizedRetrievalScopeResolver,
)
from spine.application.content_authorization import (
    AuthorizationContextMode,
    ContentAuthorizationDecisionService,
    CurrentContentAuthorizationSnapshot,
    TrustedAuthorizationContext,
)
from spine.application.context_profiles import EffectiveContextRetrievalPolicy
from spine.domain.context_profiles import (
    CompletenessRequirement,
    ContextProfileRef,
    PartialAnswerFallback,
    RetrievalStrategy,
)
from spine.domain.projections import (
    CanonicalAsOf,
    ProjectionActivation,
    ProjectionActivationKind,
    ProjectionConfigVersion,
    ProjectionSnapshot,
    ProjectionSnapshotMember,
    ProjectionSnapshotMemberDisposition,
    ProjectionSnapshotState,
    PublishedActivePublication,
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


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
WORKSPACE = UUID(int=1)
OTHER_WORKSPACE = UUID(int=2)
ENVIRONMENT = UUID(int=3)
OTHER_ENVIRONMENT = UUID(int=4)
HUMAN = UUID(int=5)
SERVICE = UUID(int=6)
SOURCE_A = UUID(int=10)
SOURCE_B = UUID(int=11)
REVISION_A = UUID(int=20)
REVISION_B = UUID(int=21)


def _grant(number: int, principal: PrincipalKind, principal_id: UUID) -> AccessGrant:
    return AccessGrant(
        access_grant_id=UUID(int=number),
        principal_kind=principal,
        principal_id=principal_id,
        permission=(
            AccessPermission.READ_CONTENT
            if principal is PrincipalKind.HUMAN
            else AccessPermission.PROCESS_CONTENT
        ),
        purpose="answer_question" if principal is PrincipalKind.SERVICE else None,
        operation="retrieve_context" if principal is PrincipalKind.SERVICE else None,
        valid_from=NOW - timedelta(minutes=1) if principal is PrincipalKind.SERVICE else None,
        valid_until=NOW + timedelta(minutes=1) if principal is PrincipalKind.SERVICE else None,
    )


def _policy(source_id: UUID, grants: tuple[AccessGrant, ...]) -> AccessPolicyVersion:
    return AccessPolicyVersion(
        access_policy_version_id=UUID(int=100 + source_id.int),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=source_id,
        version=1,
        predecessor_policy_version_id=None,
        document_schema_version="access-policy.v1",
        document_hash="a" * 64,
        origin_kind=PolicyOriginKind.APPROVED_PROPOSAL,
        origin_reference=UUID(int=200 + source_id.int),
        origin_version=1,
        origin_hash="b" * 64,
        created_by_canonical_human_id=HUMAN,
        created_at=NOW,
        grants=grants,
    )


def _authorization_snapshot(
    source_id: UUID,
    revision_id: UUID,
    grants: tuple[AccessGrant, ...],
    *,
    revision_kind: RevisionKind = RevisionKind.CONTENT,
    observed_at: datetime | None = NOW,
    retention_eligible: bool = True,
) -> CurrentContentAuthorizationSnapshot:
    policy = _policy(source_id, grants)
    return CurrentContentAuthorizationSnapshot(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=source_id,
        current_revision_id=revision_id,
        current_revision_kind=revision_kind,
        current_revision_observed_at=observed_at,
        retention_eligible=retention_eligible,
        provenance_complete=True,
        current_policy=CurrentAccessPolicy(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_object_id=source_id,
            access_policy_version_id=policy.access_policy_version_id,
            policy_activation_id=UUID(int=300 + source_id.int),
            policy_generation=2,
            cas_version=1,
        ),
        policy=policy,
    )


class _Identity:
    def __init__(self, current: TrustedAuthorizationContext) -> None:
        self.current = current

    async def revalidate(self, context: TrustedAuthorizationContext) -> TrustedAuthorizationContext:
        return self.current


class _AuthorizationRepository:
    def __init__(self, snapshots: dict[UUID, CurrentContentAuthorizationSnapshot]) -> None:
        self.snapshots = snapshots
        self.calls: list[UUID] = []

    async def resolve_current_content_authorization(
        self, *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID
    ) -> CurrentContentAuthorizationSnapshot | None:
        self.calls.append(source_object_id)
        return self.snapshots.get(source_object_id)


class _SnapshotMembers:
    def __init__(self, members: tuple[ProjectionSnapshotMember, ...]) -> None:
        self.members = members
        self.calls: list[UUID] = []

    async def list_snapshot_members(self, projection_snapshot_id: UUID) -> tuple[ProjectionSnapshotMember, ...]:
        self.calls.append(projection_snapshot_id)
        return self.members


class _ProviderScopeSafety:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.scopes = []

    async def can_express(self, scope: object) -> bool:
        self.scopes.append(scope)
        return self.allowed


class _DisclosureConstraint:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.calls = []

    async def allows(self, context, member, evaluated_at) -> bool:
        self.calls.append((context, member, evaluated_at))
        return self.allowed


def _context(
    mode: AuthorizationContextMode = AuthorizationContextMode.HUMAN_DISCLOSURE,
    *,
    purpose: str = "answer_question",
    operation: str = "retrieve_context",
    service: bool = False,
) -> TrustedAuthorizationContext:
    return TrustedAuthorizationContext(
        mode=mode,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        canonical_human_id=None if mode is AuthorizationContextMode.NONINTERACTIVE_PROCESSING else HUMAN,
        service_principal_id=SERVICE if service else None,
        purpose=purpose,
        operation=operation,
        membership_generation=7,
        authority_generation=8,
        service_authority_generation=9 if service else None,
        authority_snapshot_reference=UUID(int=400),
        trace_id=UUID(int=401),
        issued_at=NOW,
        noninteractive_authority_reference=(UUID(int=402) if mode is AuthorizationContextMode.NONINTERACTIVE_PROCESSING else None),
    )


def _publication() -> PublishedActivePublication:
    config_payload: dict[str, object] = {}
    config_digest = sha256(
        b'{"payload":{},"schema":"projection-config:r1"}'
    ).hexdigest()
    config = ProjectionConfigVersion(
        projection_config_version_id=UUID(int=503),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        version=1,
        ontology_version_id=UUID(int=504),
        parser_contract_version="parser:v1",
        locator_contract_version="locator:v1",
        extraction_contract_version="extractor:v1",
        embedding_profile_version="embedding:v1",
        adapter_contract_version="adapter:v1",
        config_payload=config_payload,
        config_digest=config_digest,
        created_at=NOW,
    )
    snapshot = ProjectionSnapshot(
        projection_snapshot_id=UUID(int=505),
        projection_run_id=UUID(int=506),
        projection_candidate_id=UUID(int=507),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        snapshot_state=ProjectionSnapshotState.FINALIZED,
        projection_kind="qa",
        projection_config_version_id=config.projection_config_version_id,
        canonical_boundary_id=UUID(int=508),
        candidate_evidence_digest="c" * 64,
        manifest_digest="d" * 64,
        member_count=2,
        created_at=NOW,
    )
    activation = ProjectionActivation(
        projection_activation_id=UUID(int=509),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot_id=snapshot.projection_snapshot_id,
        activation_generation=4,
        activation_kind=ProjectionActivationKind.PUBLISH,
        command_identity="activation-262",
        command_digest="e" * 64,
        idempotency_receipt_id=UUID(int=510),
        audit_event_id=UUID(int=511),
        activated_at=NOW,
    )
    return PublishedActivePublication(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot=snapshot,
        projection_activation=activation,
        activation_generation=4,
        active_pointer_cas_version=5,
        canonical_boundary_id=snapshot.canonical_boundary_id,
        canonical_boundary_manifest_digest="f" * 64,
        accepted_through_generation=10,
        canonical_boundary_membership_ref="members",
        as_of=CanonicalAsOf(
            canonical_boundary_id=snapshot.canonical_boundary_id,
            accepted_through_generation=10,
            boundary_time=NOW,
        ),
        projection_config_version=config,
        freshness="current",
        health="healthy",
    )


def _member(source_id: UUID, revision_id: UUID, number: int) -> ProjectionSnapshotMember:
    return ProjectionSnapshotMember(
        projection_snapshot_member_id=UUID(int=600 + number),
        projection_snapshot_id=UUID(int=505),
        canonical_boundary_id=UUID(int=508),
        canonical_boundary_member_id=UUID(int=700 + number),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=source_id,
        source_revision_id=revision_id,
        disposition=ProjectionSnapshotMemberDisposition.INCLUDED,
        projection_partition_id=UUID(int=800 + number),
        projection_receipt_id=UUID(int=900 + number),
    )


def _profile() -> EffectiveContextRetrievalPolicy:
    return EffectiveContextRetrievalPolicy(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        profile_ref=ContextProfileRef(
            profile_id=UUID(int=1000), profile_version=1, profile_digest="1" * 64
        ),
        retrieval_strategy=RetrievalStrategy.HYBRID,
        max_results=20,
        max_tokens=1000,
        max_relation_hops=2,
        freshness_max_age_seconds=60,
        completeness_requirement=CompletenessRequirement.BEST_EFFORT,
        partial_answer_fallback=PartialAnswerFallback.ALLOWED,
    )


def _resolver(
    snapshots: dict[UUID, CurrentContentAuthorizationSnapshot],
    members: tuple[ProjectionSnapshotMember, ...],
    *,
    context: TrustedAuthorizationContext | None = None,
    provider_allowed: bool | None = None,
) -> tuple[AuthorizedRetrievalScopeResolver, _AuthorizationRepository, _ProviderScopeSafety | None]:
    repository = _AuthorizationRepository(snapshots)
    authorization = ContentAuthorizationDecisionService(
        repository=repository,
        identity_authority=_Identity(context or _context()),
        clock=lambda: NOW,
        decision_reference_factory=lambda: UUID(int=1100),
    )
    safety = _ProviderScopeSafety(provider_allowed) if provider_allowed is not None else None
    return (
        AuthorizedRetrievalScopeResolver(
            authorization=authorization,
            snapshot_members=_SnapshotMembers(members),
            disclosure_constraint=_DisclosureConstraint(True),
            provider_scope_safety=safety,
            clock=lambda: NOW,
        ),
        repository,
        safety,
    )


def _request(
    *,
    context: TrustedAuthorizationContext | None = None,
    profile: EffectiveContextRetrievalPolicy | None = None,
    publication: PublishedActivePublication | None = None,
) -> AuthorizedRetrievalScopeRequest:
    return AuthorizedRetrievalScopeRequest(
        trusted_context=context or _context(),
        profile=profile or _profile(),
        publication=publication or _publication(),
    )


@pytest.mark.asyncio
async def test_authorized_human_scope_contains_only_current_source_and_pinned_publication() -> None:
    resolver, repository, safety = _resolver(
        {SOURCE_A: _authorization_snapshot(SOURCE_A, REVISION_A, (_grant(1, PrincipalKind.HUMAN, HUMAN),))},
        (_member(SOURCE_A, REVISION_A, 1),),
        provider_allowed=True,
    )

    result = await resolver.resolve(_request())

    assert result.outcome is AuthorizedRetrievalScopeOutcome.RESOLVED
    assert result.reason is AuthorizedRetrievalScopeReason.RESOLVED
    assert result.scope is not None
    assert result.scope.projection_snapshot_id == UUID(int=505)
    assert result.scope.activation_generation == 4
    assert result.scope.canonical_as_of.accepted_through_generation == 10
    assert [item.source_object_id for item in result.scope.source_constraints] == [SOURCE_A]
    assert repository.calls == [SOURCE_A]
    assert safety is not None and safety.scopes == [result.scope]


@pytest.mark.asyncio
async def test_mixed_access_omits_unauthorized_source_without_denied_source_leakage() -> None:
    resolver, _, _ = _resolver(
        {
            SOURCE_A: _authorization_snapshot(SOURCE_A, REVISION_A, (_grant(2, PrincipalKind.HUMAN, HUMAN),)),
            SOURCE_B: _authorization_snapshot(SOURCE_B, REVISION_B, ()),
        },
        (_member(SOURCE_A, REVISION_A, 1), _member(SOURCE_B, REVISION_B, 2)),
        provider_allowed=True,
    )

    result = await resolver.resolve(_request())

    assert result.outcome is AuthorizedRetrievalScopeOutcome.RESOLVED
    assert result.scope is not None
    assert [item.source_object_id for item in result.scope.source_constraints] == [SOURCE_A]
    assert str(SOURCE_B) not in result.model_dump_json()


@pytest.mark.asyncio
async def test_no_authorized_source_returns_safe_denial_without_source_identifiers() -> None:
    resolver, _, _ = _resolver(
        {SOURCE_A: _authorization_snapshot(SOURCE_A, REVISION_A, ())},
        (_member(SOURCE_A, REVISION_A, 1),),
    )

    result = await resolver.resolve(_request())

    assert result.outcome is AuthorizedRetrievalScopeOutcome.DENIED
    assert result.reason is AuthorizedRetrievalScopeReason.NO_AUTHORIZED_SOURCES
    assert result.scope is None
    assert str(SOURCE_A) not in result.model_dump_json()


@pytest.mark.asyncio
async def test_interactive_processing_requires_human_and_service_grants() -> None:
    context = _context(AuthorizationContextMode.INTERACTIVE_PROCESSING, service=True)
    resolver, _, _ = _resolver(
        {
            SOURCE_A: _authorization_snapshot(
                SOURCE_A,
                REVISION_A,
                (_grant(3, PrincipalKind.HUMAN, HUMAN),),
            )
        },
        (_member(SOURCE_A, REVISION_A, 1),),
        context=context,
    )

    result = await resolver.resolve(_request(context=context))

    assert result.outcome is AuthorizedRetrievalScopeOutcome.DENIED
    assert result.reason is AuthorizedRetrievalScopeReason.NO_AUTHORIZED_SOURCES


@pytest.mark.asyncio
async def test_scope_rejects_wrong_purpose_and_cross_environment_without_repository_call() -> None:
    context = _context(purpose="other_purpose")
    resolver, repository, _ = _resolver(
        {SOURCE_A: _authorization_snapshot(SOURCE_A, REVISION_A, (_grant(4, PrincipalKind.HUMAN, HUMAN),))},
        (_member(SOURCE_A, REVISION_A, 1),),
        context=context,
    )

    result = await resolver.resolve(_request(context=context))

    assert result.outcome is AuthorizedRetrievalScopeOutcome.DENIED
    assert result.reason is AuthorizedRetrievalScopeReason.INVALID_TRUSTED_CONTEXT
    assert repository.calls == []

    foreign_profile = _profile().model_copy(update={"workspace_id": OTHER_WORKSPACE})
    result = await resolver.resolve(_request(profile=foreign_profile))
    assert result.outcome is AuthorizedRetrievalScopeOutcome.DENIED
    assert result.reason is AuthorizedRetrievalScopeReason.SCOPE_MISMATCH
    assert repository.calls == []

    foreign_config = _publication().projection_config_version.model_copy(
        update={"workspace_id": OTHER_WORKSPACE}
    )
    foreign_publication = _publication().model_copy(
        update={"projection_config_version": foreign_config}
    )
    result = await resolver.resolve(_request(publication=foreign_publication))
    assert result.outcome is AuthorizedRetrievalScopeOutcome.DENIED
    assert result.reason is AuthorizedRetrievalScopeReason.SCOPE_MISMATCH
    assert repository.calls == []

    foreign_profile = _profile().model_copy(update={"environment_id": OTHER_ENVIRONMENT})
    result = await resolver.resolve(_request(profile=foreign_profile))
    assert result.outcome is AuthorizedRetrievalScopeOutcome.DENIED
    assert result.reason is AuthorizedRetrievalScopeReason.SCOPE_MISMATCH
    assert repository.calls == []


@pytest.mark.asyncio
async def test_superseded_revision_fails_closed_instead_of_using_old_publication_member() -> None:
    resolver, _, _ = _resolver(
        {
            SOURCE_A: _authorization_snapshot(
                SOURCE_A,
                UUID(int=999),
                (_grant(5, PrincipalKind.HUMAN, HUMAN),),
            )
        },
        (_member(SOURCE_A, REVISION_A, 1),),
    )

    result = await resolver.resolve(_request())

    assert result.outcome is AuthorizedRetrievalScopeOutcome.UNAVAILABLE
    assert result.reason is AuthorizedRetrievalScopeReason.SOURCE_UNAVAILABLE
    assert result.scope is None


@pytest.mark.asyncio
async def test_stale_revision_fails_closed_and_provider_narrowing_is_required_when_declared() -> None:
    resolver, _, _ = _resolver(
        {
            SOURCE_A: _authorization_snapshot(
                SOURCE_A,
                REVISION_A,
                (_grant(6, PrincipalKind.HUMAN, HUMAN),),
                observed_at=NOW - timedelta(minutes=2),
            )
        },
        (_member(SOURCE_A, REVISION_A, 1),),
        provider_allowed=True,
    )
    stale = await resolver.resolve(_request())
    assert stale.outcome is AuthorizedRetrievalScopeOutcome.UNAVAILABLE
    assert stale.reason is AuthorizedRetrievalScopeReason.SOURCE_STALE

    resolver, _, _ = _resolver(
        {SOURCE_A: _authorization_snapshot(SOURCE_A, REVISION_A, (_grant(7, PrincipalKind.HUMAN, HUMAN),))},
        (_member(SOURCE_A, REVISION_A, 1),),
        provider_allowed=False,
    )
    unsafe = await resolver.resolve(_request())
    assert unsafe.outcome is AuthorizedRetrievalScopeOutcome.UNSAFE
    assert unsafe.reason is AuthorizedRetrievalScopeReason.UNSAFE_PROVIDER_NARROWING
    assert unsafe.scope is None


@pytest.mark.asyncio
async def test_missing_provider_safety_proof_fails_closed() -> None:
    resolver, _, _ = _resolver(
        {SOURCE_A: _authorization_snapshot(SOURCE_A, REVISION_A, (_grant(8, PrincipalKind.HUMAN, HUMAN),))},
        (_member(SOURCE_A, REVISION_A, 1),),
    )

    result = await resolver.resolve(_request())

    assert result.outcome is AuthorizedRetrievalScopeOutcome.UNSAFE
    assert result.reason is AuthorizedRetrievalScopeReason.UNSAFE_PROVIDER_NARROWING
    assert result.scope is None


@pytest.mark.asyncio
async def test_active_disclosure_constraint_denies_retrieval_before_provider_read() -> None:
    member = _member(SOURCE_A, REVISION_A, 1)
    repository = _AuthorizationRepository(
        {
            SOURCE_A: _authorization_snapshot(
                SOURCE_A,
                REVISION_A,
                (_grant(80, PrincipalKind.HUMAN, HUMAN),),
            )
        }
    )
    constraint = _DisclosureConstraint(False)
    resolver = AuthorizedRetrievalScopeResolver(
        authorization=ContentAuthorizationDecisionService(
            repository=repository,
            identity_authority=_Identity(_context()),
            clock=lambda: NOW,
            decision_reference_factory=lambda: UUID(int=1100),
        ),
        snapshot_members=_SnapshotMembers((member,)),
        provider_scope_safety=_ProviderScopeSafety(True),
        disclosure_constraint=constraint,
        clock=lambda: NOW,
    )

    result = await resolver.resolve(_request())

    assert result.outcome is AuthorizedRetrievalScopeOutcome.UNAVAILABLE
    assert result.reason is AuthorizedRetrievalScopeReason.SOURCE_UNAVAILABLE
    assert repository.calls == []
    assert constraint.calls == [(_context(), member, NOW)]


@pytest.mark.asyncio
async def test_missing_retention_or_tombstoned_current_revision_fails_closed() -> None:
    resolver, _, _ = _resolver(
        {
            SOURCE_A: _authorization_snapshot(
                SOURCE_A,
                REVISION_A,
                (_grant(9, PrincipalKind.HUMAN, HUMAN),),
                retention_eligible=None,
            )
        },
        (_member(SOURCE_A, REVISION_A, 1),),
        provider_allowed=True,
    )
    missing_retention = await resolver.resolve(_request())
    assert missing_retention.outcome is AuthorizedRetrievalScopeOutcome.UNAVAILABLE
    assert missing_retention.reason is AuthorizedRetrievalScopeReason.SOURCE_UNAVAILABLE

    resolver, _, _ = _resolver(
        {
            SOURCE_A: _authorization_snapshot(
                SOURCE_A,
                REVISION_A,
                (_grant(10, PrincipalKind.HUMAN, HUMAN),),
                revision_kind=RevisionKind.TOMBSTONE,
            )
        },
        (_member(SOURCE_A, REVISION_A, 1),),
        provider_allowed=True,
    )
    tombstoned = await resolver.resolve(_request())
    assert tombstoned.outcome is AuthorizedRetrievalScopeOutcome.UNAVAILABLE
    assert tombstoned.reason is AuthorizedRetrievalScopeReason.SOURCE_UNAVAILABLE


@pytest.mark.asyncio
async def test_expired_service_grant_is_denied_for_interactive_scope() -> None:
    context = _context(AuthorizationContextMode.INTERACTIVE_PROCESSING, service=True)
    expired = _grant(11, PrincipalKind.SERVICE, SERVICE).model_copy(
        update={"valid_until": NOW}
    )
    resolver, _, _ = _resolver(
        {
            SOURCE_A: _authorization_snapshot(
                SOURCE_A,
                REVISION_A,
                (
                    _grant(12, PrincipalKind.HUMAN, HUMAN),
                    expired,
                ),
            )
        },
        (_member(SOURCE_A, REVISION_A, 1),),
        context=context,
        provider_allowed=True,
    )

    result = await resolver.resolve(_request(context=context))

    assert result.outcome is AuthorizedRetrievalScopeOutcome.DENIED
    assert result.reason is AuthorizedRetrievalScopeReason.NO_AUTHORIZED_SOURCES
