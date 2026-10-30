from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.connections.bitrix24_oauth import (
    Bitrix24ActivationRequest,
    Bitrix24BootstrapAttempt,
    Bitrix24BootstrapCoordinator,
    Bitrix24CallbackCommand,
    Bitrix24CallbackDispatcher,
    Bitrix24ConnectionActivation,
    BootstrapAttemptState,
    BootstrapRetryBlocked,
    CallbackAccepted,
    CallbackConflicting,
    CallbackDuplicate,
    CallbackLate,
    CallbackVerification,
    CallbackVerificationStatus,
    ConnectionActivated,
    Bitrix24ConfirmedUninstall,
    Bitrix24GrantLoss,
    Bitrix24UninstallEvidence,
    ReauthorizationRequired,
    RefreshClaim,
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


NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def _attempt() -> Bitrix24BootstrapAttempt:
    return Bitrix24BootstrapAttempt(
        attempt_id=_id(1),
        workspace_id=_id(2),
        environment_id=_id(3),
        approval_id=_id(4),
        connection_id=_id(5),
        connection_version_id=_id(6),
        grant_reference=OpaqueProviderReference(value=_id(7)),
        portal_member_id="member-1",
        application_id="app-1",
        installing_user_id="42",
        approved_scopes=("disk",),
        authorization_generation=8,
        configuration_generation=9,
        expires_at=datetime(2026, 10, 30, 13, tzinfo=timezone.utc),
    )


def _command() -> Bitrix24CallbackCommand:
    return Bitrix24CallbackCommand(
        workspace_id=_id(2),
        environment_id=_id(3),
        application_id="app-1",
        portal_member_id_hint="member-1",
        event_kind="ONAPPINSTALL",
        callback_reference=OpaqueProviderReference(value=_id(10)),
        payload_fingerprint="a" * 64,
        received_at=NOW,
    )


def _connection() -> ProviderConnection:
    attempt = _attempt()
    return ProviderConnection(
        connection_version_id=attempt.connection_version_id,
        connection_id=attempt.connection_id,
        approval_id=attempt.approval_id,
        version=1,
        workspace_id=attempt.workspace_id,
        environment_id=attempt.environment_id,
        service_principal_id=_id(8),
        provider_kind=ProviderKind.BITRIX24,
        credential_reference=OpaqueProviderReference(value=_id(9)),
        grant_reference=attempt.grant_reference,
        approved_resource_scope_references=(OpaqueProviderReference(value=_id(13)),),
        authorization_generation=attempt.authorization_generation,
        configuration_generation=attempt.configuration_generation,
        status=ProviderConnectionStatus.ACTIVE,
    )


class _Verifier:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, attempt, callback_reference):
        del attempt, callback_reference
        self.calls += 1
        return CallbackVerification(
            status=CallbackVerificationStatus.VERIFIED,
            portal_member_id="member-1",
            application_id="app-1",
            acting_user_id="42",
            granted_scopes=("disk",),
            pair_candidate_reference=OpaqueProviderReference(value=_id(11)),
        )


class _AllowActivation:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    async def authorize(self, request):
        del request
        return self.allowed


class _AllowUninstall:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    async def authorize(self, evidence):
        del evidence
        return self.allowed


@pytest.mark.asyncio
async def test_identical_callback_replay_is_stable_and_conflicting_replay_is_quarantined() -> (
    None
):
    repository = InMemoryBitrix24OAuthRepository(attempts=(_attempt(),))
    verifier = _Verifier()
    dispatcher = Bitrix24CallbackDispatcher(repository=repository, verifier=verifier)

    accepted = await dispatcher.dispatch(_command())
    duplicate = await dispatcher.dispatch(
        replace(
            _command(),
            callback_reference=OpaqueProviderReference(value=_id(12)),
        )
    )
    conflicting = await dispatcher.dispatch(
        replace(_command(), payload_fingerprint="b" * 64)
    )

    assert isinstance(accepted, CallbackAccepted)
    assert duplicate == CallbackDuplicate(
        receipt_id=accepted.receipt_id,
        binding_id=accepted.binding_id,
    )
    assert conflicting == CallbackConflicting(
        callback_reference=_command().callback_reference
    )
    assert verifier.calls == 1
    assert await repository.current_authority(_connection()) is None
    pair_store = InMemoryTokenPairStore(
        initial_pairs={
            OpaqueProviderReference(value=_id(11)): SecretTokenPair(
                "callback-access", "callback-refresh"
            )
        }
    )
    request = Bitrix24ActivationRequest(
        connection=_connection(),
        receipt_id=accepted.receipt_id,
        command_id=_id(14),
        occurred_at=NOW,
    )
    denied = await Bitrix24ConnectionActivation(
        repository=repository,
        authority=_AllowActivation(False),
        pair_store=pair_store,
    ).activate(request)
    assert denied.receipt_id == accepted.receipt_id
    assert await repository.current_authority(_connection()) is None
    activated = await Bitrix24ConnectionActivation(
        repository=repository,
        authority=_AllowActivation(),
        pair_store=pair_store,
    ).activate(request)
    assert isinstance(activated, ConnectionActivated)
    authority = await repository.current_authority(_connection())
    assert authority is not None
    assert authority.active_pair_reference == OpaqueProviderReference(value=_id(11))


@pytest.mark.asyncio
async def test_activation_without_exact_approved_connection_is_denied() -> None:
    repository = InMemoryBitrix24OAuthRepository(attempts=(_attempt(),))
    accepted = await Bitrix24CallbackDispatcher(
        repository=repository, verifier=_Verifier()
    ).dispatch(_command())
    pair_store = InMemoryTokenPairStore(
        initial_pairs={
            OpaqueProviderReference(value=_id(11)): SecretTokenPair(
                "candidate-access", "candidate-refresh"
            )
        }
    )

    denied = await Bitrix24ConnectionActivation(
        repository=repository,
        authority=_AllowActivation(),
        pair_store=pair_store,
    ).activate(
        Bitrix24ActivationRequest(
            connection=_connection().model_copy(update={"approval_id": _id(99)}),
            receipt_id=accepted.receipt_id,
            command_id=_id(14),
            occurred_at=NOW,
        )
    )

    assert denied.receipt_id == accepted.receipt_id
    assert await repository.current_authority(_connection()) is None


@pytest.mark.asyncio
async def test_confirmed_uninstall_requires_qualified_identity_and_separate_authority() -> None:
    authority = Bitrix24GrantAuthority(
        binding=Bitrix24GrantBinding(
            binding_id=_id(70),
            workspace_id=_id(2),
            environment_id=_id(3),
            approval_id=_id(4),
            connection_id=_id(5),
            connection_version_id=_id(6),
            grant_reference=OpaqueProviderReference(value=_id(7)),
            portal_member_id="member-1",
            application_id="app-1",
            acting_user_id="42",
            granted_scopes=(Bitrix24Scope.DISK,),
            authorization_generation=8,
            configuration_generation=9,
        ),
        active_pair_reference=OpaqueProviderReference(value=_id(71)),
        token_epoch=1,
        security_fence=0,
        state=Bitrix24GrantState.READY,
    )
    repository = InMemoryBitrix24OAuthRepository(authorities=(authority,))
    evidence = Bitrix24UninstallEvidence(
        connection=_connection(),
        binding_id=authority.binding.binding_id,
        portal_member_id="member-1",
        application_id="app-1",
        qualification_reference=OpaqueProviderReference(value=_id(72)),
        observed_at=NOW,
    )

    denied = await Bitrix24GrantLoss(
        repository=repository, authority=_AllowUninstall(False)
    ).confirm_uninstall(evidence)
    mismatched = await Bitrix24GrantLoss(
        repository=repository, authority=_AllowUninstall()
    ).confirm_uninstall(replace(evidence, portal_member_id="other-member"))
    confirmed = await Bitrix24GrantLoss(
        repository=repository, authority=_AllowUninstall()
    ).confirm_uninstall(evidence)

    assert denied == Bitrix24ConfirmedUninstall.DENIED
    assert mismatched == Bitrix24ConfirmedUninstall.IDENTITY_MISMATCH
    assert confirmed == ReauthorizationRequired(binding_id=_id(70))
    assert (await repository.current_authority(_connection())).state is (
        Bitrix24GrantState.REAUTHORIZATION_REQUIRED
    )


@pytest.mark.asyncio
async def test_orphan_pair_cleanup_proves_candidate_is_inactive_and_not_incident_retained() -> (
    None
):
    from spine.domain.connections.bitrix24_oauth import (
        Bitrix24GrantAuthority,
        Bitrix24GrantBinding,
        Bitrix24GrantState,
    )

    authority = Bitrix24GrantAuthority(
        binding=Bitrix24GrantBinding(
            binding_id=_id(30),
            workspace_id=_id(2),
            environment_id=_id(3),
            approval_id=_id(4),
            connection_id=_id(5),
            connection_version_id=_id(6),
            grant_reference=OpaqueProviderReference(value=_id(7)),
            portal_member_id="member-1",
            application_id="app-1",
            acting_user_id="42",
            granted_scopes=(Bitrix24Scope.DISK,),
            authorization_generation=8,
            configuration_generation=9,
        ),
        active_pair_reference=OpaqueProviderReference(value=_id(31)),
        token_epoch=1,
        security_fence=0,
        state=Bitrix24GrantState.READY,
    )
    store = InMemoryTokenPairStore(
        initial_pairs={
            authority.active_pair_reference: SecretTokenPair(
                "old-access", "old-refresh"
            )
        }
    )
    candidate = await store.create_candidate(
        RefreshClaim(attempt_id=_id(32), authority=authority),
        SecretTokenPair("new-access", "new-refresh"),
    )

    assert (
        await store.discard_candidate(
            candidate,
            active_references=frozenset({candidate}),
            incident_retained=False,
        )
        is False
    )
    assert (
        await store.discard_candidate(
            candidate,
            active_references=frozenset(),
            incident_retained=True,
        )
        is False
    )
    assert (
        await store.discard_candidate(
            candidate,
            active_references=frozenset(),
            incident_retained=False,
        )
        is True
    )
    assert await store.verify_candidate(candidate) is False


@pytest.mark.asyncio
async def test_uncertain_same_identity_bootstrap_blocks_a_new_attempt() -> None:
    uncertain = replace(
        _attempt(),
        state=BootstrapAttemptState.UNCERTAIN,
    )
    repository = InMemoryBitrix24OAuthRepository(attempts=(uncertain,))
    coordinator = Bitrix24BootstrapCoordinator(repository=repository)

    outcome = await coordinator.start(
        replace(_attempt(), attempt_id=_id(40), state=BootstrapAttemptState.PENDING)
    )

    assert outcome == BootstrapRetryBlocked(existing_attempt_id=uncertain.attempt_id)


@pytest.mark.asyncio
async def test_late_callback_durably_fences_same_identity_retry() -> None:
    repository = InMemoryBitrix24OAuthRepository(attempts=(_attempt(),))
    dispatcher = Bitrix24CallbackDispatcher(repository=repository, verifier=_Verifier())

    late = await dispatcher.dispatch(
        replace(
            _command(),
            received_at=datetime(2026, 10, 30, 14, tzinfo=timezone.utc),
        )
    )
    retry = await Bitrix24BootstrapCoordinator(repository=repository).start(
        replace(_attempt(), attempt_id=_id(41))
    )

    assert late == CallbackLate(attempt_id=_attempt().attempt_id)
    assert retry == BootstrapRetryBlocked(existing_attempt_id=_attempt().attempt_id)
