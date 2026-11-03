from __future__ import annotations

import pytest

from spine.connectors.google_api import (
    EnvironmentGoogleCredentialResolver,
    GoogleCredentialResolutionError,
)
from spine.domain.connections.google import OpaqueGoogleReference


def _reference(value: int) -> OpaqueGoogleReference:
    return OpaqueGoogleReference(value=f"00000000-0000-0000-0000-{value:012d}")


@pytest.mark.asyncio
async def test_environment_credentials_resolve_only_for_exact_references() -> None:
    credential_reference = _reference(1)
    grant_reference = _reference(2)
    resolver = EnvironmentGoogleCredentialResolver(
        credential_reference=credential_reference,
        grant_reference=grant_reference,
        client_id_variable="SPINE_GOOGLE_OAUTH_CLIENT_ID",
        client_secret_variable="SPINE_GOOGLE_OAUTH_CLIENT_SECRET",
        refresh_token_variable="SPINE_GOOGLE_OAUTH_REFRESH_TOKEN",
        environment={
            "SPINE_GOOGLE_OAUTH_CLIENT_ID": "synthetic-client-id",
            "SPINE_GOOGLE_OAUTH_CLIENT_SECRET": "synthetic-client-secret",
            "SPINE_GOOGLE_OAUTH_REFRESH_TOKEN": "synthetic-refresh-token",
        },
    )

    material = await resolver.resolve(credential_reference, grant_reference)

    assert material.client_id == "synthetic-client-id"
    assert material.client_secret == "synthetic-client-secret"
    assert material.refresh_token == "synthetic-refresh-token"
    assert "synthetic-client-secret" not in repr(resolver)
    assert "synthetic-refresh-token" not in repr(resolver)


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_reference", ("credential", "grant"))
async def test_environment_credentials_reject_stale_reference_without_secret_leak(
    stale_reference: str,
) -> None:
    credential_reference = _reference(1)
    grant_reference = _reference(2)
    resolver = EnvironmentGoogleCredentialResolver(
        credential_reference=credential_reference,
        grant_reference=grant_reference,
        client_id_variable="CLIENT_ID",
        client_secret_variable="CLIENT_SECRET",
        refresh_token_variable="REFRESH_TOKEN",
        environment={
            "CLIENT_ID": "private-client-id",
            "CLIENT_SECRET": "private-client-secret",
            "REFRESH_TOKEN": "private-refresh-token",
        },
    )
    supplied_credential = (
        _reference(3) if stale_reference == "credential" else credential_reference
    )
    supplied_grant = _reference(4) if stale_reference == "grant" else grant_reference

    with pytest.raises(GoogleCredentialResolutionError) as raised:
        await resolver.resolve(supplied_credential, supplied_grant)

    message = str(raised.value)
    assert message == "Google credential resolution is unavailable."
    assert "private-" not in message


@pytest.mark.asyncio
async def test_environment_credentials_reject_missing_refresh_token() -> None:
    resolver = EnvironmentGoogleCredentialResolver(
        credential_reference=_reference(1),
        grant_reference=_reference(2),
        client_id_variable="CLIENT_ID",
        client_secret_variable="CLIENT_SECRET",
        refresh_token_variable="REFRESH_TOKEN",
        environment={"CLIENT_ID": "client", "CLIENT_SECRET": "secret"},
    )

    with pytest.raises(GoogleCredentialResolutionError):
        await resolver.resolve(_reference(1), _reference(2))
