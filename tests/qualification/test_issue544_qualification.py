from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import SecretStr

from spine.application.content_authorization import (
    AuthorizationContextMode,
    AuthorizationReasonCode,
    ContentAuthorizationDecisionService,
    CurrentContentAuthorizationSnapshot,
    TrustedAuthorizationContext,
)
from spine.auth.qualification_targets import (
    ExternalSecretReference,
    InMemoryQualificationSecretResolver,
    InMemoryQualificationTargetBindingResolver,
    QualificationBindingDeniedError,
    QualificationDatabaseAccess,
    QualificationSecretRecord,
    QualificationSecretStatus,
    QualificationTarget,
    QualificationTargetBinding,
    QualificationTargetBindingRequest,
    QualificationTargetRole,
)
from spine.auth.records import (
    AuthorizationRecordProvenance,
    AuthorizationRecordStatus,
    QualificationAuthorityBinding,
    QualificationPrincipalKind,
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


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
WORKSPACE_ID = UUID("81000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE_ID = UUID("81000000-0000-0000-0000-000000000002")
SOURCE_ENVIRONMENT_ID = UUID("82000000-0000-0000-0000-000000000001")
RESTORE_ENVIRONMENT_ID = UUID("82000000-0000-0000-0000-000000000002")
SOURCE_ID = UUID("83000000-0000-0000-0000-000000000001")
POLICY_ID = UUID("84000000-0000-0000-0000-000000000001")
SERVICE_ID = UUID("85000000-0000-0000-0000-000000000001")
HUMAN_ID = UUID("86000000-0000-0000-0000-000000000001")
AUTHORITY_ID = UUID("87000000-0000-0000-0000-000000000001")
TARGET_BINDING_ID = UUID("88000000-0000-0000-0000-000000000001")
TRACE_ID = UUID("89000000-0000-0000-0000-000000000001")


@dataclass
class _Repository:
    snapshot: CurrentContentAuthorizationSnapshot

    async def resolve_current_content_authorization(
        self, *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID
    ) -> CurrentContentAuthorizationSnapshot | None:
        snapshot = self.snapshot
        if (
            snapshot.workspace_id,
            snapshot.environment_id,
            snapshot.source_object_id,
        ) != (workspace_id, environment_id, source_object_id):
            return None
        return snapshot


@dataclass
class _IdentityAuthority:
    current: TrustedAuthorizationContext

    async def revalidate(
        self, original: TrustedAuthorizationContext
    ) -> TrustedAuthorizationContext:
        del original
        return self.current


@dataclass
class _NoninteractiveAuthority:
    allowed: bool = True

    async def authorize(self, context: TrustedAuthorizationContext) -> bool:
        assert context.mode is AuthorizationContextMode.NONINTERACTIVE_PROCESSING
        return self.allowed


def _issue544_authority(
    *,
    status: AuthorizationRecordStatus = AuthorizationRecordStatus.ACTIVE,
    expires_at: datetime = NOW + timedelta(hours=1),
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = SOURCE_ENVIRONMENT_ID,
) -> QualificationAuthorityBinding:
    return QualificationAuthorityBinding(
        binding_id=AUTHORITY_ID,
        qualification_id=UUID("8a000000-0000-0000-0000-000000000001"),
        workspace_id=workspace_id,
        environment_id=environment_id,
        principal_kind=QualificationPrincipalKind.SERVICE,
        principal_id=SERVICE_ID,
        purpose="qualification_recovery",
        operation="source_operator",
        expires_at=expires_at,
        version=1,
        status=status,
        provenance=AuthorizationRecordProvenance(
            change_id=UUID("8b000000-0000-0000-0000-000000000001"),
            recorded_by="synthetic-issue-544",
        ),
    )


def _issue544_target_binding(*, workspace_id: UUID = WORKSPACE_ID) -> QualificationTargetBinding:
    return QualificationTargetBinding(
        binding_id=TARGET_BINDING_ID,
        binding_series_id=TARGET_BINDING_ID,
        binding_version=1,
        predecessor_binding_id=None,
        authority_binding_id=AUTHORITY_ID,
        authority_binding_version=1,
        qualification_id=UUID("8a000000-0000-0000-0000-000000000001"),
        workspace_id=workspace_id,
        environment_id=SOURCE_ENVIRONMENT_ID,
        target=QualificationTarget.SOURCE,
        target_reference="target-inventory:source:v1",
        target_scope_reference="target-scope:source:v1",
        principal_id=SERVICE_ID,
        principal_kind=QualificationPrincipalKind.SERVICE,
        purpose="qualification_recovery",
        operation="source_operator",
        alias_reference="principal-alias:source:v1",
        secret=ExternalSecretReference(
            provider_reference="approved-provider-config:v1",
            secret_reference="qualification/source/postgres:v1",
        ),
        database_role_reference="postgres-role:source-operator:v1",
        target_role=QualificationTargetRole.SOURCE_OPERATOR,
        database_access=QualificationDatabaseAccess.WRITE,
        access_policy_references=(POLICY_ID,),
    )


def _issue544_request(binding: QualificationTargetBinding) -> QualificationTargetBindingRequest:
    return QualificationTargetBindingRequest(
        binding_id=binding.binding_id,
        binding_series_id=binding.binding_series_id,
        binding_version=binding.binding_version,
        predecessor_binding_id=binding.predecessor_binding_id,
        authority_binding_id=binding.authority_binding_id,
        authority_binding_version=binding.authority_binding_version,
        qualification_id=binding.qualification_id,
        workspace_id=binding.workspace_id,
        environment_id=binding.environment_id,
        target=binding.target,
        target_reference=binding.target_reference,
        target_scope_reference=binding.target_scope_reference,
        principal_id=binding.principal_id,
        principal_kind=binding.principal_kind,
        purpose=binding.purpose,
        operation=binding.operation,
        alias_reference=binding.alias_reference,
        secret=binding.secret,
        database_role_reference=binding.database_role_reference,
        target_role=binding.target_role,
        required_database_access=binding.database_access,
        access_policy_references=binding.access_policy_references,
    )


def _issue544_policy(*grants: AccessGrant) -> AccessPolicyVersion:
    return AccessPolicyVersion(
        access_policy_version_id=POLICY_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=SOURCE_ENVIRONMENT_ID,
        source_object_id=SOURCE_ID,
        version=1,
        predecessor_policy_version_id=None,
        document_schema_version="access-policy.v1",
        document_hash="a" * 64,
        origin_kind=PolicyOriginKind.APPROVED_SOURCE_CONFIGURATION,
        origin_reference=UUID("8c000000-0000-0000-0000-000000000001"),
        origin_version=1,
        origin_hash="b" * 64,
        created_by_canonical_human_id=HUMAN_ID,
        created_at=NOW,
        grants=grants,
    )


def _issue544_snapshot(policy: AccessPolicyVersion) -> CurrentContentAuthorizationSnapshot:
    return CurrentContentAuthorizationSnapshot(
        workspace_id=WORKSPACE_ID,
        environment_id=SOURCE_ENVIRONMENT_ID,
        source_object_id=SOURCE_ID,
        current_revision_id=UUID("8d000000-0000-0000-0000-000000000001"),
        current_revision_kind=RevisionKind.CONTENT,
        provenance_complete=True,
        current_policy=CurrentAccessPolicy(
            workspace_id=WORKSPACE_ID,
            environment_id=SOURCE_ENVIRONMENT_ID,
            source_object_id=SOURCE_ID,
            access_policy_version_id=POLICY_ID,
            policy_activation_id=UUID("8e000000-0000-0000-0000-000000000001"),
            policy_generation=1,
            cas_version=1,
        ),
        policy=policy,
    )


def _issue544_context(mode: AuthorizationContextMode) -> TrustedAuthorizationContext:
    return TrustedAuthorizationContext(
        mode=mode,
        workspace_id=WORKSPACE_ID,
        environment_id=SOURCE_ENVIRONMENT_ID,
        canonical_human_id=(HUMAN_ID if mode is AuthorizationContextMode.INTERACTIVE_PROCESSING else None),
        service_principal_id=SERVICE_ID,
        purpose="qualification_recovery",
        operation="source_operator",
        membership_generation=1,
        authority_generation=1,
        service_authority_generation=1,
        authority_snapshot_reference=UUID("8f000000-0000-0000-0000-000000000001"),
        trace_id=TRACE_ID,
        issued_at=NOW,
        noninteractive_authority_reference=(
            UUID("90000000-0000-0000-0000-000000000001")
            if mode is AuthorizationContextMode.NONINTERACTIVE_PROCESSING
            else None
        ),
    )


def _issue544_target_resolver(
    binding: QualificationTargetBinding,
    authority: QualificationAuthorityBinding,
) -> InMemoryQualificationTargetBindingResolver:
    return InMemoryQualificationTargetBindingResolver(
        (binding,),
        authority_bindings=(authority,),
        approved_access_policy_references=frozenset({POLICY_ID}),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_issue544_allowed_binding_is_still_limited_by_access_policy() -> None:
    binding = _issue544_target_binding()
    resolved = await _issue544_target_resolver(
        binding, _issue544_authority()
    ).resolve(_issue544_request(binding))

    secret_resolver = InMemoryQualificationSecretResolver(
        (
            QualificationSecretRecord(
                reference=resolved.secret,
                target=resolved.target,
                target_reference=resolved.target_reference,
                database_role_reference=resolved.database_role_reference,
                status=QualificationSecretStatus.ACTIVE,
                expires_at=NOW + timedelta(hours=1),
                value=SecretStr("synthetic-secret"),
            ),
        ),
        clock=lambda: NOW,
    )

    authorization = ContentAuthorizationDecisionService(
        repository=_Repository(
            _issue544_snapshot(
                _issue544_policy(
                    AccessGrant(
                        access_grant_id=UUID("91000000-0000-0000-0000-000000000001"),
                        principal_kind=PrincipalKind.SERVICE,
                        principal_id=SERVICE_ID,
                        permission=AccessPermission.PROCESS_CONTENT,
                        purpose="qualification_recovery",
                        operation="source_operator",
                    )
                )
            )
        ),
        identity_authority=_IdentityAuthority(
            _issue544_context(AuthorizationContextMode.NONINTERACTIVE_PROCESSING)
        ),
        noninteractive_authority=_NoninteractiveAuthority(),
        clock=lambda: NOW,
    )

    decision = await authorization.decide_noninteractive_processing(
        _issue544_context(AuthorizationContextMode.NONINTERACTIVE_PROCESSING), [SOURCE_ID]
    )

    assert decision.allowed is True
    assert decision.policy_evidence[0].policy_version_id == POLICY_ID
    secret = await secret_resolver.resolve(
        resolved.secret,
        target=resolved.target,
        target_reference=resolved.target_reference,
        database_role_reference=resolved.database_role_reference,
    )
    assert secret.get_secret_value() == "synthetic-secret"


@pytest.mark.asyncio
async def test_issue544_revocation_after_admission_denies_the_next_protected_action() -> None:
    binding = _issue544_target_binding()
    context = _issue544_context(AuthorizationContextMode.NONINTERACTIVE_PROCESSING)
    identity = _IdentityAuthority(context)
    admitted_binding = await _issue544_target_resolver(
        binding, _issue544_authority()
    ).resolve(_issue544_request(binding))
    authorization = ContentAuthorizationDecisionService(
        repository=_Repository(
            _issue544_snapshot(
                _issue544_policy(
                    AccessGrant(
                        access_grant_id=UUID("93000000-0000-0000-0000-000000000001"),
                        principal_kind=PrincipalKind.SERVICE,
                        principal_id=SERVICE_ID,
                        permission=AccessPermission.PROCESS_CONTENT,
                        purpose="qualification_recovery",
                        operation="source_operator",
                    )
                )
            )
        ),
        identity_authority=identity,
        noninteractive_authority=_NoninteractiveAuthority(),
        clock=lambda: NOW,
    )

    admitted = await authorization.decide_noninteractive_processing(
        context, [SOURCE_ID]
    )
    identity.current = context.model_copy(update={"service_authority_generation": 2})
    denied = await authorization.decide_noninteractive_processing(context, [SOURCE_ID])

    assert admitted_binding.binding_id == binding.binding_id
    assert admitted.allowed is True
    assert denied.allowed is False
    assert denied.reason_code is AuthorizationReasonCode.AUTHORITY_STALE
    with pytest.raises(QualificationBindingDeniedError):
        await _issue544_target_resolver(
            binding, _issue544_authority(status=AuthorizationRecordStatus.REVOKED)
        ).resolve(_issue544_request(binding))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ("wrong_workspace", "wrong_environment", "expired", "revoked"),
)
async def test_issue544_target_resolution_fails_closed_for_scope_and_authority(
    failure: str,
) -> None:
    binding = _issue544_target_binding()
    authority = _issue544_authority(
        status=(
            AuthorizationRecordStatus.REVOKED
            if failure == "revoked"
            else AuthorizationRecordStatus.ACTIVE
        ),
        expires_at=(NOW if failure == "expired" else NOW + timedelta(hours=1)),
    )
    request = _issue544_request(binding)
    if failure == "wrong_workspace":
        request = request.model_copy(update={"workspace_id": OTHER_WORKSPACE_ID})
    if failure == "wrong_environment":
        request = request.model_copy(update={"environment_id": RESTORE_ENVIRONMENT_ID})

    with pytest.raises(QualificationBindingDeniedError):
        await _issue544_target_resolver(binding, authority).resolve(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", (AccessPermission.READ_CONTENT, AccessPermission.PROCESS_CONTENT))
async def test_issue544_read_and_process_grants_are_independent(missing: AccessPermission) -> None:
    grants = (
        AccessGrant(
            access_grant_id=UUID("92000000-0000-0000-0000-000000000001"),
            principal_kind=PrincipalKind.HUMAN,
            principal_id=HUMAN_ID,
            permission=AccessPermission.READ_CONTENT,
        ),
        AccessGrant(
            access_grant_id=UUID("92000000-0000-0000-0000-000000000002"),
            principal_kind=PrincipalKind.SERVICE,
            principal_id=SERVICE_ID,
            permission=AccessPermission.PROCESS_CONTENT,
            purpose="qualification_recovery",
            operation="source_operator",
        ),
    )
    policy = _issue544_policy(*(grant for grant in grants if grant.permission is not missing))
    authorization = ContentAuthorizationDecisionService(
        repository=_Repository(_issue544_snapshot(policy)),
        identity_authority=_IdentityAuthority(
            _issue544_context(AuthorizationContextMode.INTERACTIVE_PROCESSING)
        ),
        clock=lambda: NOW,
    )

    decision = await authorization.decide_interactive_processing(
        _issue544_context(AuthorizationContextMode.INTERACTIVE_PROCESSING), [SOURCE_ID]
    )

    assert decision.allowed is False
    assert decision.reason_code is AuthorizationReasonCode.AUTHORIZATION_DENIED


@pytest.mark.asyncio
async def test_issue544_both_content_grants_allow_interactive_processing() -> None:
    policy = _issue544_policy(
        AccessGrant(
            access_grant_id=UUID("94000000-0000-0000-0000-000000000001"),
            principal_kind=PrincipalKind.HUMAN,
            principal_id=HUMAN_ID,
            permission=AccessPermission.READ_CONTENT,
        ),
        AccessGrant(
            access_grant_id=UUID("94000000-0000-0000-0000-000000000002"),
            principal_kind=PrincipalKind.SERVICE,
            principal_id=SERVICE_ID,
            permission=AccessPermission.PROCESS_CONTENT,
            purpose="qualification_recovery",
            operation="source_operator",
        ),
    )
    authorization = ContentAuthorizationDecisionService(
        repository=_Repository(_issue544_snapshot(policy)),
        identity_authority=_IdentityAuthority(
            _issue544_context(AuthorizationContextMode.INTERACTIVE_PROCESSING)
        ),
        clock=lambda: NOW,
    )

    decision = await authorization.decide_interactive_processing(
        _issue544_context(AuthorizationContextMode.INTERACTIVE_PROCESSING), [SOURCE_ID]
    )

    assert decision.allowed is True
    assert decision.reason_code is AuthorizationReasonCode.ALLOWED
