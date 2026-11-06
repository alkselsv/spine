from uuid import UUID

import pytest

from spine.application.connections.bitrix24_oauth import (
    Bitrix24GrantAdmission,
    Bitrix24PinnedGrant,
    Bitrix24ReadMethod,
    SecretTokenPair,
)
from spine.application.crm_relationship_v2_contracts import (
    CrmRelationshipBindingOperationsProfile,
    CrmRelationshipBindingOutcome,
)
from spine.application.crm_relationship_v2_admission import (
    CrmRelationshipV2AdmissionDenied,
    CrmRelationshipV2AdmissionService,
    CrmRelationshipV2DenialReason,
    CrmRelationshipV2ReadApproval,
    CrmRelationshipV2ReadRequest,
    CurrentCrmRelationshipHumanAuthority,
)
from spine.connectors.bitrix24_crm_relationship_v2 import (
    Bitrix24CrmRelationshipV2ReadResult,
)
from spine.domain.connections.bitrix24_oauth import Bitrix24Scope
from spine.domain.connections.bitrix24_oauth import (
    Bitrix24GrantAuthority,
    Bitrix24GrantBinding,
    Bitrix24GrantState,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)
from spine.domain.sources.crm_relationship_v2 import CrmRelationshipMappingProfile


def ident(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


def connection() -> ProviderConnection:
    kinds = (
        ProviderCapabilityKind.ENUMERATION,
        ProviderCapabilityKind.METADATA_READ,
        ProviderCapabilityKind.BINARY_READ,
        ProviderCapabilityKind.STABLE_IDENTITY,
        ProviderCapabilityKind.CRM,
        ProviderCapabilityKind.STRUCTURED_READ,
    )
    return ProviderConnection(
        connection_version_id=ident(4),
        connection_id=ident(3),
        approval_id=ident(5),
        version=1,
        workspace_id=ident(1),
        environment_id=ident(2),
        service_principal_id=ident(6),
        provider_kind=ProviderKind.BITRIX24,
        credential_reference=OpaqueProviderReference(value=ident(7)),
        grant_reference=OpaqueProviderReference(value=ident(8)),
        approved_resource_scope_references=(OpaqueProviderReference(value=ident(9)),),
        approved_crm_entity_types=("deal",),
        authorization_generation=10,
        configuration_generation=11,
        status=ProviderConnectionStatus.ACTIVE,
        capabilities=tuple(
            ProviderCapability(
                kind=kind,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(
                    value=ident(100 + index)
                ),
            )
            for index, kind in enumerate(kinds)
        ),
    )


def request() -> CrmRelationshipV2ReadRequest:
    return CrmRelationshipV2ReadRequest(
        workspace_id=ident(1),
        environment_id=ident(2),
        acting_human_id=ident(20),
        service_principal_id=None,
        connection_id=ident(3),
        connection_version_id=ident(4),
        authorization_generation=10,
        configuration_generation=11,
        resource_scope_reference=OpaqueProviderReference(value=ident(9)),
        provider_instance="portal.example.invalid",
        activity_namespace="bitrix24-crm-activity",
        activity_type_id="meeting",
        activity_id="activity-42",
        raw_entity_type_id="DEAL",
        entity_namespace="bitrix24-crm-entity",
        entity_id="deal-7",
        mapping_profile_id=ident(30),
        mapping_profile_version=2,
        operations_profile=CrmRelationshipBindingOperationsProfile(
            profile_id="synthetic.crm-binding:v1",
            profile_version="1",
            profile_kind="synthetic",
            page_size=20,
            max_pages=5,
            max_response_bytes=100_000,
            max_attempts=10,
        ),
    )


def profile() -> CrmRelationshipMappingProfile:
    return CrmRelationshipMappingProfile(
        schema_version="bitrix24-crm-files-mapping-profile:v1",
        record_kind="mapping_profile",
        profile_id=ident(30),
        workspace_id=ident(1),
        environment_id=ident(2),
        profile_version=2,
        provider_instance="portal.example.invalid",
        connection_id=ident(3),
        connection_approval_id=ident(5),
        qualification_reference=ident(31),
        approval_reference=ident(32),
        activity_namespace="bitrix24-crm-activity",
        activity_type_ids=("meeting",),
        raw_entity_type_ids=("DEAL",),
        entity_namespace="bitrix24-crm-entity",
        files_namespace="bitrix24-crm-files-item",
        normalizer_id="crm.activity.binding.list:entityTypeId",
        normalizer_version="v1",
        read_methods=("crm.activity.binding.list", "crm.activity.get"),
        mapping_strategy="direct_disk_object",
        disk_namespace="bitrix24-disk-object",
    )


def approval() -> CrmRelationshipV2ReadApproval:
    item = request()
    return CrmRelationshipV2ReadApproval(
        workspace_id=item.workspace_id,
        environment_id=item.environment_id,
        acting_human_id=item.acting_human_id,
        human_authorization_generation=7,
        connection_id=item.connection_id,
        connection_version_id=item.connection_version_id,
        resource_scope_reference=item.resource_scope_reference,
        provider_instance=item.provider_instance,
        provider_acting_user_id="provider-user-20",
        approved_connection_entity_type="deal",
        activity_namespace=item.activity_namespace,
        activity_type_id=item.activity_type_id,
        activity_id=item.activity_id,
        raw_entity_type_id=item.raw_entity_type_id,
        entity_namespace=item.entity_namespace,
        entity_id=item.entity_id,
        mapping_profile_id=item.mapping_profile_id,
        mapping_profile_version=item.mapping_profile_version,
        connection_approval_id=ident(5),
        qualification_reference=ident(31),
        approval_reference=ident(32),
        approved_read_methods=(
            "crm.activity.binding.list",
            "crm.activity.get",
        ),
    )


def human_authority(
    **updates: object,
) -> CurrentCrmRelationshipHumanAuthority:
    values: dict[str, object] = {
        "acting_human_id": ident(20),
        "workspace_id": ident(1),
        "environment_id": ident(2),
        "authorization_generation": 7,
        "allowed": True,
    }
    values.update(updates)
    return CurrentCrmRelationshipHumanAuthority(**values)


def grant_authority(
    granted_scopes: tuple[Bitrix24Scope, ...],
) -> Bitrix24GrantAuthority:
    crm_capable = Bitrix24Scope.CRM in granted_scopes
    return Bitrix24GrantAuthority(
        schema_version=(
            "bitrix24-grant-authority:v2"
            if crm_capable
            else "bitrix24-grant-authority:v1"
        ),
        binding=Bitrix24GrantBinding(
            schema_version=(
                "bitrix24-grant-binding:v2"
                if crm_capable
                else "bitrix24-grant-binding:v1"
            ),
            binding_id=ident(40),
            workspace_id=ident(1),
            environment_id=ident(2),
            approval_id=ident(5),
            connection_id=ident(3),
            connection_version_id=ident(4),
            grant_reference=OpaqueProviderReference(value=ident(8)),
            portal_member_id="portal-member-1",
            application_id="application-1",
            acting_user_id="provider-user-20",
            granted_scopes=granted_scopes,
            authorization_generation=10,
            configuration_generation=11,
        ),
        active_pair_reference=OpaqueProviderReference(value=ident(41)),
        token_epoch=3,
        security_fence=0,
        state=Bitrix24GrantState.READY,
    )


class HumanAuthorityFake:
    def __init__(
        self, currents: tuple[CurrentCrmRelationshipHumanAuthority | None, ...]
    ) -> None:
        self.currents = currents
        self.calls = 0

    async def resolve_current(self, *, acting_human_id, workspace_id, environment_id):
        del acting_human_id, workspace_id, environment_id
        current = self.currents[min(self.calls, len(self.currents) - 1)]
        self.calls += 1
        return current


class ConnectionAdmissionFake:
    def __init__(self, current: ProviderConnection | None = None) -> None:
        self.current = current or connection()

    async def resolve_current(self, requested):
        del requested
        return self.current


class MissingConnectionAdmissionFake:
    async def resolve_current(self, requested):
        del requested
        return None


class ScopeApprovalFake:
    def __init__(self, current: CrmRelationshipV2ReadApproval | None = None) -> None:
        self.current = current or approval()

    async def resolve_current(self, requested, admitted_connection):
        del requested, admitted_connection
        return self.current


class MissingScopeApprovalFake:
    async def resolve_current(self, requested, admitted_connection):
        del requested, admitted_connection
        return None


class ProfileRepositoryFake:
    def __init__(
        self, current: tuple[CrmRelationshipMappingProfile, ...] | None = None
    ) -> None:
        self.current = current if current is not None else (profile(),)

    async def find_current(self, requested):
        del requested
        return self.current


class GrantAdmissionFake:
    def __init__(self) -> None:
        self.authorizations = []

    async def resolve(self, admitted_connection, authorization=None):
        del admitted_connection
        self.authorizations.append(authorization)
        return Bitrix24PinnedGrant(
            binding_id=ident(40),
            acting_user_id="provider-user-20",
            token_epoch=3,
            security_fence=0,
            tokens=SecretTokenPair("access-secret", "refresh-secret"),
        )


class GrantAuthorityRepositoryFake:
    def __init__(self, authority: Bitrix24GrantAuthority | None) -> None:
        self.authority = authority

    async def current_authority(self, admitted_connection):
        del admitted_connection
        return self.authority


class TokenResolverFake:
    async def resolve(self, authority):
        del authority
        return SecretTokenPair("access-secret", "refresh-secret")


class GrantFenceFake:
    def __init__(self, currents: tuple[bool, ...] = (True, True)) -> None:
        self.currents = currents
        self.calls = 0

    async def is_current(self, admitted_connection, pinned):
        del admitted_connection, pinned
        current = self.currents[min(self.calls, len(self.currents) - 1)]
        self.calls += 1
        return current


class ProviderFake:
    def __init__(self, after_read=None) -> None:
        self.commands = []
        self.after_read = after_read

    async def read(self, command):
        self.commands.append(command)
        if self.after_read is not None:
            self.after_read()
        return Bitrix24CrmRelationshipV2ReadResult(
            binding_evidence_reference="evidence:binding:42",
            binding_evidence_digest="a" * 64,
            positive_pair_observed=True,
            outcome=CrmRelationshipBindingOutcome.POSITIVE_OBSERVED,
            binding_method="crm.activity.binding.list",
            raw_entity_type_id="DEAL",
            entity_id="deal-7",
            normalizer_id="crm.activity.binding.list:entityTypeId",
            normalizer_version="v1",
            page_count=1,
            attempt_count=1,
            response_bytes=64,
        )


def admission_service(
    *,
    human: HumanAuthorityFake | None = None,
    admitted_connection: ProviderConnection | None = None,
    scope: CrmRelationshipV2ReadApproval | None = None,
    current_profiles: tuple[CrmRelationshipMappingProfile, ...] | None = None,
    grant_fence: GrantFenceFake | None = None,
    grant_admission=None,
    provider: ProviderFake | None = None,
) -> tuple[CrmRelationshipV2AdmissionService, ProviderFake]:
    provider = provider or ProviderFake()
    return (
        CrmRelationshipV2AdmissionService(
            human_authority=human or HumanAuthorityFake((human_authority(),)),
            connection_admission=ConnectionAdmissionFake(admitted_connection),
            grant_admission=grant_admission or GrantAdmissionFake(),
            grant_fence=grant_fence or GrantFenceFake(),
            scope_approval=ScopeApprovalFake(scope),
            profiles=ProfileRepositoryFake(current_profiles),
            provider=provider,
        ),
        provider,
    )


@pytest.mark.asyncio
async def test_exact_current_scope_is_admitted_with_raw_entity_type_and_profile() -> (
    None
):
    grant = GrantAdmissionFake()
    provider = ProviderFake()
    service = CrmRelationshipV2AdmissionService(
        human_authority=HumanAuthorityFake(
            (
                CurrentCrmRelationshipHumanAuthority(
                    acting_human_id=ident(20),
                    workspace_id=ident(1),
                    environment_id=ident(2),
                    authorization_generation=7,
                    allowed=True,
                ),
            )
        ),
        connection_admission=ConnectionAdmissionFake(),
        grant_admission=grant,
        grant_fence=GrantFenceFake(),
        scope_approval=ScopeApprovalFake(),
        profiles=ProfileRepositoryFake(),
        provider=provider,
    )

    result = await service.read(request())

    assert result.positive_pair_observed is True
    assert len(provider.commands) == 1
    admitted = provider.commands[0]
    assert admitted.raw_entity_type_id == "DEAL"
    assert (
        admitted.mapping_profile is profile() or admitted.mapping_profile == profile()
    )
    assert admitted.human_authorization_generation == 7
    assert admitted.pinned_grant.acting_user_id == "provider-user-20"
    assert grant.authorizations[0].method is Bitrix24ReadMethod.CRM_MATERIAL_READ
    assert grant.authorizations[0].required_scope is Bitrix24Scope.CRM


@pytest.mark.asyncio
async def test_human_authority_change_during_read_discards_provider_result() -> None:
    before = CurrentCrmRelationshipHumanAuthority(
        acting_human_id=ident(20),
        workspace_id=ident(1),
        environment_id=ident(2),
        authorization_generation=7,
        allowed=True,
    )
    after = before.model_copy(update={"authorization_generation": 8})
    provider = ProviderFake()
    service = CrmRelationshipV2AdmissionService(
        human_authority=HumanAuthorityFake((before, after)),
        connection_admission=ConnectionAdmissionFake(),
        grant_admission=GrantAdmissionFake(),
        grant_fence=GrantFenceFake(),
        scope_approval=ScopeApprovalFake(),
        profiles=ProfileRepositoryFake(),
        provider=provider,
    )

    with pytest.raises(CrmRelationshipV2AdmissionDenied) as denied:
        await service.read(request())

    assert denied.value.reason is CrmRelationshipV2DenialReason.AUTHORITY_SUPERSEDED
    assert len(provider.commands) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "reason"),
    (
        (
            {"workspace_id": ident(101)},
            CrmRelationshipV2DenialReason.HUMAN_AUTHORITY_UNAVAILABLE,
        ),
        (
            {"environment_id": ident(102)},
            CrmRelationshipV2DenialReason.HUMAN_AUTHORITY_UNAVAILABLE,
        ),
        (
            {"connection_id": ident(103)},
            CrmRelationshipV2DenialReason.CONNECTION_UNAVAILABLE,
        ),
        (
            {"connection_version_id": ident(104)},
            CrmRelationshipV2DenialReason.CONNECTION_UNAVAILABLE,
        ),
        (
            {"authorization_generation": 12},
            CrmRelationshipV2DenialReason.CONNECTION_UNAVAILABLE,
        ),
        (
            {"configuration_generation": 13},
            CrmRelationshipV2DenialReason.CONNECTION_UNAVAILABLE,
        ),
        (
            {"resource_scope_reference": OpaqueProviderReference(value=ident(105))},
            CrmRelationshipV2DenialReason.CONNECTION_UNAVAILABLE,
        ),
        (
            {"raw_entity_type_id": "CONTACT"},
            CrmRelationshipV2DenialReason.SCOPE_UNAPPROVED,
        ),
        ({"entity_id": "deal-8"}, CrmRelationshipV2DenialReason.SCOPE_UNAPPROVED),
        (
            {"activity_id": "activity-43"},
            CrmRelationshipV2DenialReason.SCOPE_UNAPPROVED,
        ),
        ({"activity_type_id": "call"}, CrmRelationshipV2DenialReason.SCOPE_UNAPPROVED),
    ),
)
async def test_wrong_exact_scope_denies_before_provider_call(
    updates: dict[str, object], reason: CrmRelationshipV2DenialReason
) -> None:
    service, provider = admission_service()

    with pytest.raises(CrmRelationshipV2AdmissionDenied) as denied:
        await service.read(request().model_copy(update=updates))

    assert denied.value.reason is reason
    assert provider.commands == []


@pytest.mark.asyncio
async def test_missing_or_invalid_acting_human_denies_before_provider_call() -> None:
    missing_service, missing_provider = admission_service()
    invalid_service, invalid_provider = admission_service(
        human=HumanAuthorityFake((human_authority(allowed=False),))
    )

    with pytest.raises(CrmRelationshipV2AdmissionDenied) as missing:
        await missing_service.read(
            request().model_copy(update={"acting_human_id": None})
        )
    with pytest.raises(CrmRelationshipV2AdmissionDenied) as invalid:
        await invalid_service.read(request())

    assert (
        missing.value.reason
        is CrmRelationshipV2DenialReason.HUMAN_AUTHORITY_UNAVAILABLE
    )
    assert (
        invalid.value.reason
        is CrmRelationshipV2DenialReason.HUMAN_AUTHORITY_UNAVAILABLE
    )
    assert missing_provider.commands == []
    assert invalid_provider.commands == []


@pytest.mark.asyncio
async def test_service_principal_substitution_denies_before_provider_call() -> None:
    service, provider = admission_service()

    with pytest.raises(CrmRelationshipV2AdmissionDenied) as denied:
        await service.read(
            request().model_copy(update={"service_principal_id": ident(6)})
        )

    assert (
        denied.value.reason
        is CrmRelationshipV2DenialReason.SERVICE_PRINCIPAL_SUBSTITUTION
    )
    assert provider.commands == []


@pytest.mark.asyncio
async def test_unapproved_connection_entity_type_denies_before_provider_call() -> None:
    unapproved = approval().model_copy(
        update={"approved_connection_entity_type": "contact"}
    )
    service, provider = admission_service(scope=unapproved)

    with pytest.raises(CrmRelationshipV2AdmissionDenied) as denied:
        await service.read(request())

    assert denied.value.reason is CrmRelationshipV2DenialReason.SCOPE_UNAPPROVED
    assert provider.commands == []


@pytest.mark.asyncio
async def test_stale_or_ambiguous_profile_denies_before_provider_call() -> None:
    stale = profile().model_copy(update={"profile_version": 1})
    stale_service, stale_provider = admission_service(current_profiles=(stale,))
    missing_service, missing_provider = admission_service(current_profiles=())
    ambiguous_service, ambiguous_provider = admission_service(
        current_profiles=(profile(), profile())
    )

    with pytest.raises(CrmRelationshipV2AdmissionDenied) as stale_denied:
        await stale_service.read(request())
    with pytest.raises(CrmRelationshipV2AdmissionDenied) as missing_denied:
        await missing_service.read(request())
    with pytest.raises(CrmRelationshipV2AdmissionDenied) as ambiguous_denied:
        await ambiguous_service.read(request())

    assert (
        stale_denied.value.reason is CrmRelationshipV2DenialReason.PROFILE_UNAVAILABLE
    )
    assert (
        ambiguous_denied.value.reason is CrmRelationshipV2DenialReason.PROFILE_AMBIGUOUS
    )
    assert stale_provider.commands == []
    assert (
        missing_denied.value.reason is CrmRelationshipV2DenialReason.PROFILE_UNAVAILABLE
    )
    assert missing_provider.commands == []
    assert ambiguous_provider.commands == []


@pytest.mark.asyncio
async def test_stale_grant_denies_before_provider_call_and_safe_error_has_no_scope_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, provider = admission_service(grant_fence=GrantFenceFake((False,)))
    sensitive_values = (
        "portal.example.invalid",
        "activity-42",
        "deal-7",
        str(ident(9)),
    )

    with pytest.raises(CrmRelationshipV2AdmissionDenied) as denied:
        await service.read(request())

    rendered = f"{denied.value!s} {denied.value!r}"
    assert denied.value.reason is CrmRelationshipV2DenialReason.GRANT_UNAVAILABLE
    assert all(value not in rendered for value in sensitive_values)
    assert all(value not in caplog.text for value in sensitive_values)
    assert provider.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile_updates",
    (
        {"connection_approval_id": ident(201)},
        {"qualification_reference": ident(202)},
        {"approval_reference": ident(203)},
        {"raw_entity_type_ids": ("deal",)},
        {"read_methods": ("crm.activity.binding.list",)},
    ),
)
async def test_untrusted_or_unqualified_profile_denies_before_provider_call(
    profile_updates: dict[str, object],
) -> None:
    candidate = profile().model_copy(update=profile_updates)
    service, provider = admission_service(current_profiles=(candidate,))

    with pytest.raises(CrmRelationshipV2AdmissionDenied) as denied:
        await service.read(request())

    assert denied.value.reason is CrmRelationshipV2DenialReason.PROFILE_UNAVAILABLE
    assert provider.commands == []


@pytest.mark.asyncio
async def test_current_s1_grant_is_required() -> None:
    missing = Bitrix24GrantAdmission(
        repository=GrantAuthorityRepositoryFake(None),
        grant_resolver=TokenResolverFake(),
    )
    current = Bitrix24GrantAdmission(
        repository=GrantAuthorityRepositoryFake(
            grant_authority((Bitrix24Scope.DISK, Bitrix24Scope.CRM))
        ),
        grant_resolver=TokenResolverFake(),
    )
    denied_service, denied_provider = admission_service(grant_admission=missing)
    allowed_service, allowed_provider = admission_service(grant_admission=current)

    with pytest.raises(CrmRelationshipV2AdmissionDenied) as denied:
        await denied_service.read(request())
    result = await allowed_service.read(request())

    assert denied.value.reason is CrmRelationshipV2DenialReason.GRANT_UNAVAILABLE
    assert denied_provider.commands == []
    assert result.positive_pair_observed is True
    assert len(allowed_provider.commands) == 1


@pytest.mark.asyncio
async def test_grant_change_during_read_discards_provider_result() -> None:
    service, provider = admission_service(grant_fence=GrantFenceFake((True, False)))

    with pytest.raises(CrmRelationshipV2AdmissionDenied) as denied:
        await service.read(request())

    assert denied.value.reason is CrmRelationshipV2DenialReason.AUTHORITY_SUPERSEDED
    assert len(provider.commands) == 1


@pytest.mark.asyncio
async def test_missing_connection_or_scope_approval_denies_before_provider_call() -> (
    None
):
    connection_provider = ProviderFake()
    connection_service = CrmRelationshipV2AdmissionService(
        human_authority=HumanAuthorityFake((human_authority(),)),
        connection_admission=MissingConnectionAdmissionFake(),
        grant_admission=GrantAdmissionFake(),
        grant_fence=GrantFenceFake(),
        scope_approval=ScopeApprovalFake(),
        profiles=ProfileRepositoryFake(),
        provider=connection_provider,
    )
    scope_provider = ProviderFake()
    scope_service = CrmRelationshipV2AdmissionService(
        human_authority=HumanAuthorityFake((human_authority(),)),
        connection_admission=ConnectionAdmissionFake(),
        grant_admission=GrantAdmissionFake(),
        grant_fence=GrantFenceFake(),
        scope_approval=MissingScopeApprovalFake(),
        profiles=ProfileRepositoryFake(),
        provider=scope_provider,
    )

    with pytest.raises(CrmRelationshipV2AdmissionDenied) as connection_denied:
        await connection_service.read(request())
    with pytest.raises(CrmRelationshipV2AdmissionDenied) as scope_denied:
        await scope_service.read(request())

    assert (
        connection_denied.value.reason
        is CrmRelationshipV2DenialReason.CONNECTION_UNAVAILABLE
    )
    assert scope_denied.value.reason is CrmRelationshipV2DenialReason.SCOPE_UNAPPROVED
    assert connection_provider.commands == []
    assert scope_provider.commands == []


@pytest.mark.asyncio
async def test_stale_s1_grant_generation_denies_before_provider_call() -> None:
    stale = grant_authority((Bitrix24Scope.DISK, Bitrix24Scope.CRM)).model_copy(
        update={
            "binding": grant_authority(
                (Bitrix24Scope.DISK, Bitrix24Scope.CRM)
            ).binding.model_copy(update={"authorization_generation": 9})
        }
    )
    service, provider = admission_service(
        grant_admission=Bitrix24GrantAdmission(
            repository=GrantAuthorityRepositoryFake(stale),
            grant_resolver=TokenResolverFake(),
        )
    )

    with pytest.raises(CrmRelationshipV2AdmissionDenied) as denied:
        await service.read(request())

    assert denied.value.reason is CrmRelationshipV2DenialReason.GRANT_UNAVAILABLE
    assert provider.commands == []
