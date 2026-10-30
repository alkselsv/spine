from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.connections.bitrix24_oauth import (
    Bitrix24BootstrapAttempt,
    Bitrix24CallbackCommand,
    Bitrix24CallbackDispatcher,
    Bitrix24GrantAdmission,
    Bitrix24GrantDenied,
    BootstrapMarkedUncertain,
    CallbackAccepted,
    CallbackAmbiguous,
    CallbackIdentityMismatch,
    CallbackIdentityUncertain,
    CallbackLate,
    CallbackUnsupportedDropped,
    CallbackVerification,
    CallbackVerificationStatus,
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


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _connection(*, workspace_id: UUID = _id(1)) -> ProviderConnection:
    return ProviderConnection(
        connection_version_id=_id(5),
        connection_id=_id(4),
        approval_id=_id(3),
        version=1,
        workspace_id=workspace_id,
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


def _authority(*, workspace_id: UUID = _id(1)) -> Bitrix24GrantAuthority:
    return Bitrix24GrantAuthority(
        binding=Bitrix24GrantBinding(
            binding_id=_id(12),
            workspace_id=workspace_id,
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


class _AuthorityRepository:
    def __init__(self, authority: Bitrix24GrantAuthority) -> None:
        self.authority = authority

    async def current_authority(
        self, connection: ProviderConnection
    ) -> Bitrix24GrantAuthority | None:
        del connection
        return self.authority


class _GrantResolver:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, authority: Bitrix24GrantAuthority) -> SecretTokenPair:
        del authority
        self.calls += 1
        return SecretTokenPair(
            access_token="access-secret", refresh_token="refresh-secret"
        )


class _CallbackRepository:
    def __init__(self, attempts=()) -> None:
        self.find_calls = 0
        self.attempts = attempts
        self.record_calls = 0
        self.uncertain_attempts = []

    async def existing_receipt(self, command):
        del command
        return None

    async def find_pending(self, command):
        del command
        self.find_calls += 1
        return self.attempts

    async def mark_attempt_uncertain(self, attempt):
        self.uncertain_attempts.append(attempt.attempt_id)
        return BootstrapMarkedUncertain(attempt_id=attempt.attempt_id)

    async def record_verified(self, command, attempt, verification):
        del command, attempt, verification
        self.record_calls += 1
        return CallbackAccepted(receipt_id=_id(60), binding_id=_id(61))


class _CallbackVerifier:
    def __init__(self, result=None) -> None:
        self.calls = 0
        self.result = result

    async def verify(self, attempt, callback_reference):
        del attempt, callback_reference
        self.calls += 1
        if self.result is None:
            raise AssertionError("unsupported callback must not be verified")
        return self.result


def _bootstrap_attempt() -> Bitrix24BootstrapAttempt:
    return Bitrix24BootstrapAttempt(
        attempt_id=_id(51),
        workspace_id=_id(1),
        environment_id=_id(2),
        approval_id=_id(3),
        connection_id=_id(4),
        connection_version_id=_id(5),
        grant_reference=OpaqueProviderReference(value=_id(8)),
        portal_member_id="member-1",
        application_id="app-1",
        installing_user_id="42",
        approved_scopes=("disk",),
        authorization_generation=10,
        configuration_generation=11,
        expires_at=datetime(2026, 10, 30, 13, tzinfo=timezone.utc),
    )


def _callback_command() -> Bitrix24CallbackCommand:
    return Bitrix24CallbackCommand(
        workspace_id=_id(1),
        environment_id=_id(2),
        application_id="app-1",
        portal_member_id_hint="member-1",
        event_kind="ONAPPINSTALL",
        callback_reference=OpaqueProviderReference(value=_id(50)),
        payload_fingerprint="a" * 64,
        received_at=datetime(2026, 10, 30, 12, tzinfo=timezone.utc),
    )


def test_crm_scope_remains_disabled_for_offline_bootstrap() -> None:
    with pytest.raises(ValueError, match="only the disk scope"):
        replace(_bootstrap_attempt(), approved_scopes=("disk", "crm"))


@pytest.mark.asyncio
async def test_exact_current_binding_is_resolved_as_one_pinned_pair() -> None:
    resolver = _GrantResolver()
    admission = Bitrix24GrantAdmission(
        repository=_AuthorityRepository(_authority()),
        grant_resolver=resolver,
    )

    pinned = await admission.resolve(_connection())

    assert pinned.token_epoch == 1
    assert pinned.binding_id == _id(12)
    assert pinned.tokens.access_token == "access-secret"
    assert pinned.tokens.refresh_token == "refresh-secret"
    assert resolver.calls == 1
    assert "access-secret" not in repr(pinned)
    assert "refresh-secret" not in repr(pinned)


@pytest.mark.asyncio
async def test_cross_workspace_substitution_is_denied_before_secret_resolution() -> (
    None
):
    resolver = _GrantResolver()
    admission = Bitrix24GrantAdmission(
        repository=_AuthorityRepository(_authority(workspace_id=_id(99))),
        grant_resolver=resolver,
    )

    with pytest.raises(Bitrix24GrantDenied):
        await admission.resolve(_connection())

    assert resolver.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authority",
    (
        _authority().model_copy(
            update={
                "binding": _authority().binding.model_copy(
                    update={"authorization_generation": 9}
                )
            }
        ),
        _authority().model_copy(
            update={
                "binding": _authority().binding.model_copy(
                    update={"grant_reference": OpaqueProviderReference(value=_id(99))}
                )
            }
        ),
        _authority().model_copy(
            update={
                "binding": _authority().binding.model_copy(
                    update={"configuration_generation": 10}
                )
            }
        ),
    ),
    ids=(
        "stale-authorization-generation",
        "wrong-grant-reference",
        "stale-configuration-generation",
    ),
)
async def test_stale_or_wrong_grant_is_denied_before_secret_resolution(
    authority: Bitrix24GrantAuthority,
) -> None:
    resolver = _GrantResolver()
    admission = Bitrix24GrantAdmission(
        repository=_AuthorityRepository(authority),
        grant_resolver=resolver,
    )

    with pytest.raises(Bitrix24GrantDenied):
        await admission.resolve(_connection())

    assert resolver.calls == 0


@pytest.mark.asyncio
async def test_unsupported_callback_is_dropped_without_resolving_technical_user_grant() -> (
    None
):
    repository = _CallbackRepository()
    verifier = _CallbackVerifier()
    dispatcher = Bitrix24CallbackDispatcher(
        repository=repository,
        verifier=verifier,
    )

    outcome = await dispatcher.dispatch(
        Bitrix24CallbackCommand(
            workspace_id=_id(1),
            environment_id=_id(2),
            application_id="app-1",
            portal_member_id_hint="member-1",
            event_kind="ONAPPUSERREADY",
            callback_reference=OpaqueProviderReference(value=_id(50)),
            payload_fingerprint="a" * 64,
            received_at=datetime(2026, 10, 30, 12, tzinfo=timezone.utc),
        )
    )

    assert outcome == CallbackUnsupportedDropped(event_kind="ONAPPUSERREADY")
    assert repository.find_calls == 0
    assert verifier.calls == 0


@pytest.mark.asyncio
async def test_verified_install_callback_returns_stable_secret_free_receipt() -> None:
    repository = _CallbackRepository((_bootstrap_attempt(),))
    verifier = _CallbackVerifier(
        CallbackVerification(
            status=CallbackVerificationStatus.VERIFIED,
            portal_member_id="member-1",
            application_id="app-1",
            acting_user_id="42",
            granted_scopes=("disk",),
            pair_candidate_reference=OpaqueProviderReference(value=_id(52)),
        )
    )
    dispatcher = Bitrix24CallbackDispatcher(repository=repository, verifier=verifier)

    outcome = await dispatcher.dispatch(_callback_command())

    assert outcome == CallbackAccepted(receipt_id=_id(60), binding_id=_id(61))
    assert repository.record_calls == 1
    assert verifier.calls == 1
    assert "token" not in repr(outcome).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [0, 2])
async def test_no_or_multiple_pending_callbacks_fail_closed(
    candidate_count: int,
) -> None:
    attempts = tuple(
        replace(
            _bootstrap_attempt(),
            attempt_id=_id(70 + index),
            installing_user_id=str(42 + index),
        )
        for index in range(candidate_count)
    )
    verifier = _CallbackVerifier()
    dispatcher = Bitrix24CallbackDispatcher(
        repository=_CallbackRepository(attempts), verifier=verifier
    )

    outcome = await dispatcher.dispatch(_callback_command())

    assert outcome == CallbackAmbiguous(candidate_count=candidate_count)
    assert verifier.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("portal_member_id", "application_id", "acting_user_id"),
    [
        ("wrong-member", "app-1", "42"),
        ("member-1", "wrong-app", "42"),
        ("member-1", "app-1", "wrong-user"),
    ],
)
async def test_wrong_callback_identity_never_records_binding(
    portal_member_id: str,
    application_id: str,
    acting_user_id: str,
) -> None:
    repository = _CallbackRepository((_bootstrap_attempt(),))
    dispatcher = Bitrix24CallbackDispatcher(
        repository=repository,
        verifier=_CallbackVerifier(
            CallbackVerification(
                status=CallbackVerificationStatus.VERIFIED,
                portal_member_id=portal_member_id,
                application_id=application_id,
                acting_user_id=acting_user_id,
                granted_scopes=("disk",),
                pair_candidate_reference=OpaqueProviderReference(value=_id(52)),
            )
        ),
    )

    outcome = await dispatcher.dispatch(_callback_command())

    assert outcome == CallbackIdentityMismatch(attempt_id=_id(51))
    assert repository.record_calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("portal_member_id", ""),
        ("application_id", ""),
        ("installing_user_id", ""),
    ],
)
def test_bootstrap_rejects_missing_provider_identity(field: str, value: str) -> None:
    with pytest.raises(ValueError, match="provider identity"):
        replace(_bootstrap_attempt(), **{field: value})


@pytest.mark.asyncio
async def test_late_callback_is_rejected_before_verification() -> None:
    verifier = _CallbackVerifier()
    repository = _CallbackRepository((_bootstrap_attempt(),))
    dispatcher = Bitrix24CallbackDispatcher(repository=repository, verifier=verifier)

    outcome = await dispatcher.dispatch(
        replace(
            _callback_command(),
            received_at=datetime(2026, 10, 30, 14, tzinfo=timezone.utc),
        )
    )

    assert outcome == CallbackLate(attempt_id=_id(51))
    assert verifier.calls == 0
    assert repository.uncertain_attempts == [_id(51)]


@pytest.mark.asyncio
async def test_untrusted_callback_token_returns_identity_uncertain() -> None:
    dispatcher = Bitrix24CallbackDispatcher(
        repository=_CallbackRepository((_bootstrap_attempt(),)),
        verifier=_CallbackVerifier(
            CallbackVerification(status=CallbackVerificationStatus.UNCERTAIN)
        ),
    )

    outcome = await dispatcher.dispatch(_callback_command())

    assert outcome == CallbackIdentityUncertain(attempt_id=_id(51))
