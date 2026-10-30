from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.connections.bitrix24_oauth import (
    Bitrix24GrantAuthority,
    Bitrix24GrantBinding,
    Bitrix24GrantState,
    Bitrix24Scope,
)
from spine.domain.connections.provider import OpaqueProviderReference, ProviderKind


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _binding() -> Bitrix24GrantBinding:
    return Bitrix24GrantBinding(
        binding_id=_id(1),
        workspace_id=_id(2),
        environment_id=_id(3),
        approval_id=_id(4),
        connection_id=_id(5),
        connection_version_id=_id(6),
        grant_reference=OpaqueProviderReference(value=_id(7)),
        portal_member_id="portal-member-1",
        application_id="local-app-1",
        acting_user_id="42",
        granted_scopes=(Bitrix24Scope.DISK,),
        authorization_generation=8,
        configuration_generation=9,
    )


def test_grant_binding_is_immutable_and_contains_only_exact_approved_identity() -> None:
    binding = _binding()

    assert binding.provider_kind is ProviderKind.BITRIX24
    assert binding.portal_member_id == "portal-member-1"
    assert binding.acting_user_id == "42"
    with pytest.raises(ValidationError):
        binding.workspace_id = _id(99)  # type: ignore[misc]


def test_grant_authority_keeps_epoch_and_security_fence_separate_from_generations() -> (
    None
):
    authority = Bitrix24GrantAuthority(
        binding=_binding(),
        active_pair_reference=OpaqueProviderReference(value=_id(10)),
        token_epoch=3,
        security_fence=2,
        state=Bitrix24GrantState.READY,
    )

    assert authority.token_epoch == 3
    assert authority.security_fence == 2
    assert authority.binding.authorization_generation == 8
    assert (
        authority.model_dump(mode="json")
        .keys()
        .isdisjoint({"access_token", "refresh_token", "client_secret"})
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("binding_id", UUID(int=0)),
        ("portal_member_id", ""),
        ("application_id", " app-with-whitespace "),
        ("acting_user_id", ""),
    ],
)
def test_grant_binding_rejects_unbounded_or_missing_identity(
    field: str, value: object
) -> None:
    values = _binding().model_dump()
    values[field] = value

    with pytest.raises(ValidationError):
        Bitrix24GrantBinding.model_validate(values)
