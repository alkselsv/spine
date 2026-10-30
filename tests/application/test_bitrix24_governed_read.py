from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import UUID

import pytest

from spine.application.connections.bitrix24_oauth import (
    Bitrix24AccessTokenExpired,
    Bitrix24ContainmentCommand,
    Bitrix24GovernedObservationReader,
    Bitrix24GrantAdmission,
    Bitrix24GrantContainment,
    Bitrix24GrantRecovery,
    Bitrix24IdentityMismatch,
    Bitrix24IdentityUncertain,
    Bitrix24ProfileUnavailable,
    Bitrix24ReadAuthorization,
    Bitrix24ReadMethod,
    Bitrix24RefreshCommand,
    Bitrix24Scope,
    ClosedBitrix24ReadPolicy,
    Bitrix24RecoveryCommand,
    GrantRecovered,
    ObjectReadDenied,
    RecoveryDenied,
    SecretTokenPair,
    StaleReadFenced,
    PairActivated,
)
from spine.application.connections.provider import (
    ProviderCapabilityUnavailableError,
    ProviderReadAdmission,
    ProviderReadCommand,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.domain.common import ContextOrigin
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
from spine.infrastructure.connections.bitrix24_oauth import (
    InMemoryBitrix24OAuthRepository,
    InMemoryTokenPairStore,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _connection() -> ProviderConnection:
    return ProviderConnection(
        connection_version_id=_id(5),
        connection_id=_id(4),
        approval_id=_id(3),
        version=1,
        workspace_id=_id(1),
        environment_id=_id(2),
        service_principal_id=_id(6),
        provider_kind=ProviderKind.BITRIX24,
        credential_reference=OpaqueProviderReference(value=_id(7)),
        grant_reference=OpaqueProviderReference(value=_id(8)),
        approved_resource_scope_references=(OpaqueProviderReference(value=_id(9)),),
        authorization_generation=10,
        configuration_generation=11,
        status=ProviderConnectionStatus.ACTIVE,
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.BINARY_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(10)),
            ),
        ),
    )


def _authority() -> Bitrix24GrantAuthority:
    return Bitrix24GrantAuthority(
        binding=Bitrix24GrantBinding(
            binding_id=_id(12),
            workspace_id=_id(1),
            environment_id=_id(2),
            approval_id=_id(3),
            connection_id=_id(4),
            connection_version_id=_id(5),
            grant_reference=OpaqueProviderReference(value=_id(8)),
            portal_member_id="member-1",
            application_id="app-1",
            acting_user_id="42",
            granted_scopes=(Bitrix24Scope.DISK,),
            authorization_generation=10,
            configuration_generation=11,
        ),
        active_pair_reference=OpaqueProviderReference(value=_id(13)),
        token_epoch=1,
        security_fence=0,
        state=Bitrix24GrantState.READY,
    )


class _ConnectionRepository:
    def __init__(self, connection: ProviderConnection | None = None) -> None:
        self.connection = connection or _connection()

    @asynccontextmanager
    async def admit_read(self, command, context):
        del command, context
        yield self.connection


class _ProfileClient:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.tokens = []

    async def profile_user_id(self, connection, pinned):
        del connection
        self.tokens.append(pinned.tokens.access_token)
        return self.user_id


class _DataReader:
    def __init__(self) -> None:
        self.calls = 0

    async def read_pinned(self, connection, command, pinned):
        del connection, command, pinned
        self.calls += 1
        raise AssertionError("identity mismatch must deny the data method")


class _DiskReadPolicy:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    async def authorize(self, connection, command):
        del connection, command
        if not self.allowed:
            return None
        return Bitrix24ReadAuthorization(
            method=Bitrix24ReadMethod.DISK_READ,
            required_scope=Bitrix24Scope.DISK,
        )


class _CrmReadPolicy:
    async def authorize(self, connection, command):
        del connection, command
        return Bitrix24ReadAuthorization(
            method=Bitrix24ReadMethod.CRM_MATERIAL_READ,
            required_scope=Bitrix24Scope.CRM,
        )


class _NoRefresh:
    async def refresh_expired(self, connection, pinned):
        del connection, pinned
        raise AssertionError("refresh must only run after an expired-token signal")


def _command() -> ProviderReadCommand:
    return ProviderReadCommand(
        workspace_id=_id(1),
        environment_id=_id(2),
        connection_id=_id(4),
        connection_version_id=_id(5),
        resource_scope_reference=OpaqueProviderReference(value=_id(9)),
        authorization_generation=10,
        configuration_generation=11,
        required_capabilities=(ProviderCapabilityKind.BINARY_READ,),
    )


def _context() -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(workspace_id=_id(1), environment_id=_id(2)),
        origin=ContextOrigin.WORKER,
        purpose=PersistencePurpose("connector.ingestion.v1"),
        operation=PersistenceOperation("provider_connection_read"),
        trace_id=_id(20),
        acting_subject_id=None,
        service_principal_id=_id(6),
        provenance=TrustedContextProvenance(issuer_id=_id(21), signature=b"trusted"),
    )


@pytest.mark.asyncio
async def test_closed_policy_allows_only_exact_qualified_read_capability_sets() -> None:
    kinds = (
        ProviderCapabilityKind.ENUMERATION,
        ProviderCapabilityKind.METADATA_READ,
        ProviderCapabilityKind.BINARY_READ,
        ProviderCapabilityKind.STABLE_IDENTITY,
        ProviderCapabilityKind.CRM,
        ProviderCapabilityKind.STRUCTURED_READ,
    )
    connection = _connection().model_copy(
        update={
            "capabilities": tuple(
                ProviderCapability(
                    kind=kind,
                    status=ProviderCapabilityStatus.SUPPORTED,
                    qualification_reference=OpaqueProviderReference(
                        value=_id(100 + index)
                    ),
                )
                for index, kind in enumerate(kinds)
            )
        }
    )
    policy = ClosedBitrix24ReadPolicy()
    disk = replace(_command(), required_capabilities=kinds[:4])
    crm = replace(_command(), required_capabilities=kinds)
    unknown = replace(
        _command(), required_capabilities=(ProviderCapabilityKind.METADATA_READ,)
    )

    assert (await policy.authorize(connection, disk)).method is (
        Bitrix24ReadMethod.DISK_READ
    )
    assert await policy.authorize(connection, crm) is None
    assert await policy.authorize(connection, unknown) is None


@pytest.mark.asyncio
async def test_profile_identity_mismatch_denies_data_call_through_provider_admission() -> (
    None
):
    authority = _authority()
    oauth_repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    pair_store = InMemoryTokenPairStore(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair(
                access_token="access-secret", refresh_token="refresh-secret"
            )
        }
    )
    data_reader = _DataReader()
    governed_reader = Bitrix24GovernedObservationReader(
        admission=Bitrix24GrantAdmission(
            repository=oauth_repository, grant_resolver=pair_store
        ),
        repository=oauth_repository,
        profile_client=_ProfileClient("wrong-user"),
        provider=data_reader,
        policy=_DiskReadPolicy(),
        refresher=_NoRefresh(),
    )
    admission = ProviderReadAdmission(
        repository=_ConnectionRepository(), provider=governed_reader
    )

    with pytest.raises(Bitrix24IdentityMismatch):
        await admission.read(_command(), _context())

    assert data_reader.calls == 0


@pytest.mark.asyncio
async def test_missing_disk_capability_denies_before_oauth_secret_resolution() -> None:
    authority = _authority()
    oauth_repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    pair_store = InMemoryTokenPairStore(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair(
                access_token="must-not-resolve", refresh_token="must-not-resolve"
            )
        }
    )
    data_reader = _DataReader()
    governed_reader = Bitrix24GovernedObservationReader(
        admission=Bitrix24GrantAdmission(
            repository=oauth_repository, grant_resolver=pair_store
        ),
        repository=oauth_repository,
        profile_client=_ProfileClient("42"),
        provider=data_reader,
        policy=_DiskReadPolicy(),
        refresher=_NoRefresh(),
    )
    admission = ProviderReadAdmission(
        repository=_ConnectionRepository(
            _connection().model_copy(update={"capabilities": ()})
        ),
        provider=governed_reader,
    )

    with pytest.raises(ProviderCapabilityUnavailableError):
        await admission.read(_command(), _context())

    assert data_reader.calls == 0


class _ContainingDataReader:
    def __init__(self, containment: Bitrix24GrantContainment) -> None:
        self.containment = containment
        self.access_tokens = []

    async def read_pinned(self, connection, command, pinned):
        del command
        self.access_tokens.append(pinned.tokens.access_token)
        await self.containment.contain(
            Bitrix24ContainmentCommand(
                connection=connection,
                binding_id=pinned.binding_id,
                expected_security_fence=pinned.security_fence,
                incident_reference=OpaqueProviderReference(value=_id(30)),
                command_id=_id(31),
            )
        )
        return "protected-provider-result"


class _ContainmentAuthority:
    async def authorize(self, command):
        del command
        return True


@pytest.mark.asyncio
async def test_containment_during_read_discards_result_pinned_to_stale_pair() -> None:
    authority = _authority()
    oauth_repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    pair_store = InMemoryTokenPairStore(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair(
                access_token="same-access", refresh_token="same-refresh"
            )
        }
    )
    containment = Bitrix24GrantContainment(
        repository=oauth_repository, authority=_ContainmentAuthority()
    )
    data_reader = _ContainingDataReader(containment)
    profile = _ProfileClient("42")
    governed_reader = Bitrix24GovernedObservationReader(
        admission=Bitrix24GrantAdmission(
            repository=oauth_repository, grant_resolver=pair_store
        ),
        repository=oauth_repository,
        profile_client=profile,
        provider=data_reader,
        policy=_DiskReadPolicy(),
        refresher=_NoRefresh(),
    )

    with pytest.raises(StaleReadFenced):
        await governed_reader.read(_connection(), _command())

    assert profile.tokens == ["same-access"]
    assert data_reader.access_tokens == ["same-access"]
    current = await oauth_repository.current_authority(_connection())
    assert current is not None
    assert current.state is Bitrix24GrantState.CONTAINED
    assert current.security_fence == 1


def _replacement_connection() -> ProviderConnection:
    return _connection().model_copy(
        update={
            "connection_version_id": _id(81),
            "version": 2,
            "predecessor_version_id": _id(5),
            "grant_reference": OpaqueProviderReference(value=_id(80)),
            "authorization_generation": 12,
        }
    )


def _replacement_authority() -> Bitrix24GrantAuthority:
    return Bitrix24GrantAuthority(
        binding=_authority().binding.model_copy(
            update={
                "binding_id": _id(82),
                "connection_version_id": _id(81),
                "grant_reference": OpaqueProviderReference(value=_id(80)),
                "authorization_generation": 12,
            }
        ),
        active_pair_reference=OpaqueProviderReference(value=_id(83)),
        token_epoch=1,
        security_fence=0,
        state=Bitrix24GrantState.READY,
    )


class _RecoveryAuthority:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed

    async def authorize(self, command):
        del command
        return self.allowed


@pytest.mark.asyncio
async def test_recovery_requires_separate_authority_and_fresh_binding() -> None:
    old = _authority().model_copy(
        update={"state": Bitrix24GrantState.CONTAINED, "security_fence": 1}
    )
    repository = InMemoryBitrix24OAuthRepository(authorities=(old,))
    command = Bitrix24RecoveryCommand(
        contained_connection=_connection(),
        replacement_connection=_replacement_connection(),
        replacement_authority=_replacement_authority(),
        recovery_reference=OpaqueProviderReference(value=_id(84)),
        command_id=_id(85),
    )

    denied = await Bitrix24GrantRecovery(
        repository=repository,
        authority=_RecoveryAuthority(False),
        pair_store=InMemoryTokenPairStore(),
    ).recover(command)
    recovered = await Bitrix24GrantRecovery(
        repository=repository,
        authority=_RecoveryAuthority(True),
        pair_store=InMemoryTokenPairStore(
            initial_pairs={
                command.replacement_authority.active_pair_reference: SecretTokenPair(
                    "replacement-access", "replacement-refresh"
                )
            }
        ),
    ).recover(command)

    assert denied == RecoveryDenied(binding_id=_id(12))
    assert recovered == GrantRecovered(
        previous_binding_id=_id(12),
        binding_id=_id(82),
        token_epoch=1,
    )
    assert await repository.current_authority(_replacement_connection()) == (
        _replacement_authority()
    )


@pytest.mark.asyncio
async def test_recovery_rejects_reused_or_unavailable_token_pair() -> None:
    old = _authority().model_copy(
        update={"state": Bitrix24GrantState.CONTAINED, "security_fence": 1}
    )
    repository = InMemoryBitrix24OAuthRepository(authorities=(old,))
    base = Bitrix24RecoveryCommand(
        contained_connection=_connection(),
        replacement_connection=_replacement_connection(),
        replacement_authority=_replacement_authority(),
        recovery_reference=OpaqueProviderReference(value=_id(84)),
        command_id=_id(85),
    )
    reused = replace(
        base,
        replacement_authority=base.replacement_authority.model_copy(
            update={"active_pair_reference": old.active_pair_reference}
        ),
    )
    service = Bitrix24GrantRecovery(
        repository=repository,
        authority=_RecoveryAuthority(True),
        pair_store=InMemoryTokenPairStore(),
    )

    assert await service.recover(reused) == RecoveryDenied(binding_id=_id(12))
    assert await service.recover(base) == RecoveryDenied(binding_id=_id(12))


class _UnavailableProfile:
    async def profile_user_id(self, connection, pinned):
        del connection, pinned
        raise Bitrix24ProfileUnavailable()


class _DeniedDataReader:
    async def read_pinned(self, connection, command, pinned):
        del connection, command, pinned
        raise ObjectReadDenied()


class _SuccessfulDataReader:
    def __init__(self) -> None:
        self.calls = 0

    async def read_pinned(self, connection, command, pinned):
        del connection, command, pinned
        self.calls += 1
        return "must-be-fenced"


class _ConfigurationChangeFence:
    def __init__(self) -> None:
        self.calls = 0

    async def is_current(self, connection, pinned):
        del connection, pinned
        self.calls += 1
        return self.calls == 1


@pytest.mark.asyncio
async def test_profile_outage_is_identity_uncertain_and_skips_data_method() -> None:
    authority = _authority()
    repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    pair_store = InMemoryTokenPairStore(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair("access", "refresh")
        }
    )
    data_reader = _DataReader()
    reader = Bitrix24GovernedObservationReader(
        admission=Bitrix24GrantAdmission(
            repository=repository, grant_resolver=pair_store
        ),
        repository=repository,
        profile_client=_UnavailableProfile(),
        provider=data_reader,
        policy=_DiskReadPolicy(),
        refresher=_NoRefresh(),
    )

    with pytest.raises(Bitrix24IdentityUncertain):
        await reader.read(_connection(), _command())

    assert data_reader.calls == 0


@pytest.mark.asyncio
async def test_object_permission_denial_does_not_revoke_or_contain_grant() -> None:
    authority = _authority()
    repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    pair_store = InMemoryTokenPairStore(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair("access", "refresh")
        }
    )
    reader = Bitrix24GovernedObservationReader(
        admission=Bitrix24GrantAdmission(
            repository=repository, grant_resolver=pair_store
        ),
        repository=repository,
        profile_client=_ProfileClient("42"),
        provider=_DeniedDataReader(),
        policy=_DiskReadPolicy(),
        refresher=_NoRefresh(),
    )

    with pytest.raises(ObjectReadDenied):
        await reader.read(_connection(), _command())

    assert await repository.current_authority(_connection()) == authority


@pytest.mark.asyncio
async def test_configuration_change_during_read_fences_result_release() -> None:
    authority = _authority()
    authority_repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    pair_store = InMemoryTokenPairStore(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair("access", "refresh")
        }
    )
    provider = _SuccessfulDataReader()
    fence = _ConfigurationChangeFence()
    reader = Bitrix24GovernedObservationReader(
        admission=Bitrix24GrantAdmission(
            repository=authority_repository, grant_resolver=pair_store
        ),
        repository=fence,
        profile_client=_ProfileClient("42"),
        provider=provider,
        policy=_DiskReadPolicy(),
        refresher=_NoRefresh(),
    )

    with pytest.raises(StaleReadFenced):
        await reader.read(_connection(), _command())

    assert provider.calls == 1
    assert fence.calls == 2


class _RecordingResolver(InMemoryTokenPairStore):
    def __init__(self, *, initial_pairs):
        super().__init__(initial_pairs=initial_pairs)
        self.resolve_calls = 0

    async def resolve(self, authority):
        self.resolve_calls += 1
        return await super().resolve(authority)


@pytest.mark.asyncio
async def test_closed_read_policy_denies_before_oauth_secret_resolution() -> None:
    authority = _authority()
    repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    resolver = _RecordingResolver(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair("access", "refresh")
        }
    )
    reader = Bitrix24GovernedObservationReader(
        admission=Bitrix24GrantAdmission(
            repository=repository, grant_resolver=resolver
        ),
        repository=repository,
        profile_client=_ProfileClient("42"),
        provider=_DataReader(),
        policy=_DiskReadPolicy(False),
        refresher=_NoRefresh(),
    )

    with pytest.raises(ObjectReadDenied):
        await reader.read(_connection(), _command())

    assert resolver.resolve_calls == 0


@pytest.mark.asyncio
async def test_missing_crm_scope_denies_before_oauth_secret_resolution() -> None:
    authority = _authority()
    repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    resolver = _RecordingResolver(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair("access", "refresh")
        }
    )
    reader = Bitrix24GovernedObservationReader(
        admission=Bitrix24GrantAdmission(
            repository=repository, grant_resolver=resolver
        ),
        repository=repository,
        profile_client=_ProfileClient("42"),
        provider=_DataReader(),
        policy=_CrmReadPolicy(),
        refresher=_NoRefresh(),
    )

    with pytest.raises(ObjectReadDenied):
        await reader.read(_connection(), _command())

    assert resolver.resolve_calls == 0


class _ExpiryThenSuccessReader:
    def __init__(self, *, expire_twice: bool = False) -> None:
        self.calls = 0
        self.expire_twice = expire_twice
        self.epochs = []

    async def read_pinned(self, connection, command, pinned):
        del connection, command
        self.calls += 1
        self.epochs.append(pinned.token_epoch)
        if self.calls == 1 or self.expire_twice:
            raise Bitrix24AccessTokenExpired()
        return "fresh-observation"


class _ActivatingExpiredGrantRefresher:
    def __init__(self, repository, authority) -> None:
        self.repository = repository
        self.authority = authority
        self.calls = 0

    async def refresh_expired(self, connection, pinned):
        self.calls += 1
        claim = await self.repository.claim_refresh(
            Bitrix24RefreshCommand(
                connection=connection,
                expected_token_epoch=pinned.token_epoch,
                attempt_id=_id(90),
            )
        )
        return await self.repository.activate_candidate(
            claim, self.authority.active_pair_reference
        )


@pytest.mark.asyncio
async def test_expired_access_token_refreshes_once_and_repeats_full_admission() -> None:
    authority = _authority()
    repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    resolver = _RecordingResolver(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair("access", "refresh")
        }
    )
    provider = _ExpiryThenSuccessReader()
    refresher = _ActivatingExpiredGrantRefresher(repository, authority)
    profile = _ProfileClient("42")
    reader = Bitrix24GovernedObservationReader(
        admission=Bitrix24GrantAdmission(
            repository=repository, grant_resolver=resolver
        ),
        repository=repository,
        profile_client=profile,
        provider=provider,
        policy=_DiskReadPolicy(),
        refresher=refresher,
    )

    assert await reader.read(_connection(), _command()) == "fresh-observation"
    assert refresher.calls == 1
    assert resolver.resolve_calls == 2
    assert provider.epochs == [1, 2]
    assert len(profile.tokens) == 2


@pytest.mark.asyncio
async def test_second_expired_access_token_is_not_refreshed_blindly() -> None:
    authority = _authority()
    repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    resolver = _RecordingResolver(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair("access", "refresh")
        }
    )
    refresher = _ActivatingExpiredGrantRefresher(repository, authority)
    reader = Bitrix24GovernedObservationReader(
        admission=Bitrix24GrantAdmission(
            repository=repository, grant_resolver=resolver
        ),
        repository=repository,
        profile_client=_ProfileClient("42"),
        provider=_ExpiryThenSuccessReader(expire_twice=True),
        policy=_DiskReadPolicy(),
        refresher=refresher,
    )

    with pytest.raises(Bitrix24IdentityUncertain):
        await reader.read(_connection(), _command())

    assert refresher.calls == 1
