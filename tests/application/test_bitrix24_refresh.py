import asyncio
from uuid import UUID

import pytest

from spine.application.connections.bitrix24_oauth import (
    ActivationCommitUnknown,
    Bitrix24RefreshCommand,
    Bitrix24RefreshCoordinator,
    Bitrix24ExpiredGrantRefresh,
    Bitrix24PinnedGrant,
    GrantUncertain,
    PairActivated,
    ReauthorizationRequired,
    RefreshBusy,
    RefreshClaim,
    RefreshedTokenPair,
    RefreshInvalidGrant,
    RefreshTransportUncertain,
    RefreshUncertain,
    SecretApplicationCredential,
    SecretTokenPair,
)
from spine.domain.connections.bitrix24_oauth import (
    Bitrix24GrantAuthority,
    Bitrix24GrantBinding,
    Bitrix24GrantState,
    Bitrix24Scope,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
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


class _CredentialResolver:
    async def resolve(self, credential_reference):
        assert credential_reference == _connection().credential_reference
        return SecretApplicationCredential(
            client_id="client-id", client_secret="secret"
        )


class _BlockingRefreshTransport:
    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def refresh(self, credential, current_pair):
        assert credential.client_id == "client-id"
        assert current_pair.refresh_token == "old-refresh"
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return RefreshedTokenPair(
            tokens=SecretTokenPair(
                access_token="new-access",
                refresh_token="new-refresh",
            )
        )


@pytest.mark.asyncio
async def test_two_refresh_workers_make_one_request_and_switch_pair_atomically() -> (
    None
):
    authority = _authority()
    repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    pair_store = InMemoryTokenPairStore(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair(
                access_token="old-access",
                refresh_token="old-refresh",
            )
        }
    )
    transport = _BlockingRefreshTransport()
    coordinator = Bitrix24RefreshCoordinator(
        repository=repository,
        credential_resolver=_CredentialResolver(),
        grant_resolver=pair_store,
        pair_store=pair_store,
        transport=transport,
    )
    command = Bitrix24RefreshCommand(
        connection=_connection(),
        expected_token_epoch=1,
        attempt_id=_id(20),
    )

    owner = asyncio.create_task(coordinator.refresh(command))
    await transport.entered.wait()
    loser = await coordinator.refresh(
        Bitrix24RefreshCommand(
            connection=_connection(),
            expected_token_epoch=1,
            attempt_id=_id(21),
        )
    )
    transport.release.set()
    winner = await owner

    assert isinstance(loser, RefreshBusy)
    assert winner == PairActivated(
        binding_id=_id(12),
        pair_reference=winner.pair_reference,
        token_epoch=2,
    )
    assert transport.calls == 1
    current = await repository.current_authority(_connection())
    assert current is not None
    assert current.token_epoch == 2
    assert current.active_pair_reference == winner.pair_reference
    resolved = await pair_store.resolve(current)
    assert resolved.access_token == "new-access"
    assert resolved.refresh_token == "new-refresh"


class _RecordingRefreshExecutor:
    def __init__(self) -> None:
        self.commands = []

    async def refresh(self, command):
        self.commands.append(command)
        return PairActivated(
            binding_id=_id(12),
            pair_reference=OpaqueProviderReference(value=_id(14)),
            token_epoch=2,
        )


@pytest.mark.asyncio
async def test_expired_grant_adapter_uses_pinned_epoch_and_injected_attempt_id() -> None:
    executor = _RecordingRefreshExecutor()
    adapter = Bitrix24ExpiredGrantRefresh(
        coordinator=executor,
        attempt_id_factory=lambda: _id(22),
    )
    pinned = Bitrix24PinnedGrant(
        binding_id=_id(12),
        acting_user_id="42",
        token_epoch=1,
        security_fence=0,
        tokens=SecretTokenPair("access", "refresh"),
    )

    outcome = await adapter.refresh_expired(_connection(), pinned)

    assert isinstance(outcome, PairActivated)
    assert executor.commands == [
        Bitrix24RefreshCommand(
            connection=_connection(), expected_token_epoch=1, attempt_id=_id(22)
        )
    ]


class _UncertainRefreshTransport:
    def __init__(self) -> None:
        self.calls = 0

    async def refresh(self, credential, current_pair):
        del credential, current_pair
        self.calls += 1
        return RefreshTransportUncertain()


@pytest.mark.asyncio
async def test_lost_refresh_response_fences_grant_without_blind_retry() -> None:
    authority = _authority()
    repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    pair_store = InMemoryTokenPairStore(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair(
                access_token="old-access",
                refresh_token="old-refresh",
            )
        }
    )
    transport = _UncertainRefreshTransport()
    coordinator = Bitrix24RefreshCoordinator(
        repository=repository,
        credential_resolver=_CredentialResolver(),
        grant_resolver=pair_store,
        pair_store=pair_store,
        transport=transport,
    )
    command = Bitrix24RefreshCommand(
        connection=_connection(), expected_token_epoch=1, attempt_id=_id(30)
    )

    first = await coordinator.refresh(command)
    second = await coordinator.refresh(
        Bitrix24RefreshCommand(
            connection=_connection(), expected_token_epoch=1, attempt_id=_id(31)
        )
    )

    assert first == RefreshUncertain(binding_id=_id(12), attempt_id=_id(30))
    assert second == RefreshUncertain(binding_id=_id(12), attempt_id=_id(30))
    assert transport.calls == 1
    current = await repository.current_authority(_connection())
    assert current is not None
    assert current.state is Bitrix24GrantState.UNCERTAIN
    assert current.active_pair_reference == authority.active_pair_reference


class _InvalidGrantTransport:
    def __init__(self, *, terminal_evidence: bool) -> None:
        self.terminal_evidence = terminal_evidence

    async def refresh(self, credential, current_pair):
        del credential, current_pair
        return RefreshInvalidGrant(terminal_evidence=self.terminal_evidence)


class _FailingCandidateStore(InMemoryTokenPairStore):
    async def create_candidate(self, claim, tokens):
        del claim, tokens
        raise RuntimeError("synthetic secret-store outage")


@pytest.mark.asyncio
async def test_secret_store_write_failure_marks_refresh_uncertain() -> None:
    authority = _authority()
    repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    store = _FailingCandidateStore(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair(
                access_token="old-access", refresh_token="old-refresh"
            )
        }
    )
    coordinator = Bitrix24RefreshCoordinator(
        repository=repository,
        credential_resolver=_CredentialResolver(),
        grant_resolver=store,
        pair_store=store,
        transport=_ImmediateRefreshTransport(),
    )

    outcome = await coordinator.refresh(
        Bitrix24RefreshCommand(
            connection=_connection(), expected_token_epoch=1, attempt_id=_id(45)
        )
    )

    assert outcome == RefreshUncertain(binding_id=_id(12), attempt_id=_id(45))
    current = await repository.current_authority(_connection())
    assert current is not None
    assert current.state is Bitrix24GrantState.UNCERTAIN
    assert current.active_pair_reference == authority.active_pair_reference


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_evidence", "expected_type", "expected_state"),
    [
        (False, GrantUncertain, Bitrix24GrantState.UNCERTAIN),
        (
            True,
            ReauthorizationRequired,
            Bitrix24GrantState.REAUTHORIZATION_REQUIRED,
        ),
    ],
)
async def test_invalid_grant_requires_qualified_terminal_evidence(
    terminal_evidence: bool,
    expected_type: type,
    expected_state: Bitrix24GrantState,
) -> None:
    authority = _authority()
    repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    pair_store = InMemoryTokenPairStore(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair(
                access_token="old-access",
                refresh_token="old-refresh",
            )
        }
    )
    coordinator = Bitrix24RefreshCoordinator(
        repository=repository,
        credential_resolver=_CredentialResolver(),
        grant_resolver=pair_store,
        pair_store=pair_store,
        transport=_InvalidGrantTransport(terminal_evidence=terminal_evidence),
    )

    outcome = await coordinator.refresh(
        Bitrix24RefreshCommand(
            connection=_connection(), expected_token_epoch=1, attempt_id=_id(40)
        )
    )

    assert isinstance(outcome, expected_type)
    current = await repository.current_authority(_connection())
    assert current is not None
    assert current.state is expected_state


class _ImmediateRefreshTransport:
    async def refresh(self, credential, current_pair):
        del credential, current_pair
        return RefreshedTokenPair(
            tokens=SecretTokenPair(
                access_token="new-access", refresh_token="new-refresh"
            )
        )


class _UnknownCommitRepository:
    def __init__(self, repository: InMemoryBitrix24OAuthRepository) -> None:
        self.repository = repository

    async def claim_refresh(self, command):
        return await self.repository.claim_refresh(command)

    async def activate_candidate(self, claim, candidate):
        await self.repository.activate_candidate(claim, candidate)
        raise ActivationCommitUnknown()

    async def resolve_activation(self, claim, candidate):
        return await self.repository.resolve_activation(claim, candidate)

    async def mark_refresh_uncertain(self, claim):
        return await self.repository.mark_refresh_uncertain(claim)

    async def mark_grant_uncertain(self, claim):
        return await self.repository.mark_grant_uncertain(claim)

    async def require_reauthorization(self, claim):
        return await self.repository.require_reauthorization(claim)

    async def current_authority(self, connection):
        return await self.repository.current_authority(connection)


@pytest.mark.asyncio
async def test_unknown_database_commit_is_resolved_from_canonical_pointer() -> None:
    authority = _authority()
    canonical = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    repository = _UnknownCommitRepository(canonical)
    pair_store = InMemoryTokenPairStore(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair(
                access_token="old-access", refresh_token="old-refresh"
            )
        }
    )
    coordinator = Bitrix24RefreshCoordinator(
        repository=repository,
        credential_resolver=_CredentialResolver(),
        grant_resolver=pair_store,
        pair_store=pair_store,
        transport=_ImmediateRefreshTransport(),
    )

    outcome = await coordinator.refresh(
        Bitrix24RefreshCommand(
            connection=_connection(), expected_token_epoch=1, attempt_id=_id(50)
        )
    )

    assert isinstance(outcome, PairActivated)
    assert outcome.token_epoch == 2
    current = await canonical.current_authority(_connection())
    assert current is not None
    assert current.active_pair_reference == outcome.pair_reference


@pytest.mark.asyncio
async def test_restarted_claim_owner_fails_closed_instead_of_repeating_transport() -> (
    None
):
    authority = _authority()
    repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    command = Bitrix24RefreshCommand(
        connection=_connection(), expected_token_epoch=1, attempt_id=_id(60)
    )

    claimed = await repository.claim_refresh(command)
    recovered = await repository.claim_refresh(command)
    later = await repository.claim_refresh(
        Bitrix24RefreshCommand(
            connection=_connection(), expected_token_epoch=1, attempt_id=_id(61)
        )
    )

    assert isinstance(claimed, RefreshClaim)
    assert recovered == RefreshUncertain(binding_id=_id(12), attempt_id=_id(60))
    assert later == recovered
