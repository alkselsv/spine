from __future__ import annotations

from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
import pytest

from spine.auth import (
    AccessTokenCredential,
    AuthenticationUnavailableError,
    InvalidAuthenticationError,
)
from spine.infrastructure.auth import OIDCAuthenticationRuntime

from .conftest import (
    AUDIENCE,
    CLIENT_ID,
    ISSUER,
    MutableClock,
    provider_transport,
    signed_token,
)


def credential(token: str) -> AccessTokenCredential:
    return AccessTokenCredential(access_token=SecretStr(token))


async def assert_token_is_invalid(
    *,
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
    token_options: dict[str, Any] | None = None,
    claim_overrides: dict[str, Any] | None = None,
) -> None:
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=provider_transport(public_jwk),
        clock=clock,
    )
    token = signed_token(
        rsa_private_key,
        clock,
        claim_overrides=claim_overrides,
        **(token_options or {}),
    )

    with pytest.raises(InvalidAuthenticationError):
        await runtime.authentication.authenticate(credential(token), object())
    await runtime.aclose()


@pytest.mark.asyncio
async def test_valid_access_token_returns_only_provider_alias_and_configuration_version(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
) -> None:
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=provider_transport(public_jwk),
        clock=clock,
    )
    token = signed_token(
        rsa_private_key,
        clock,
        claim_overrides={
            "roles": ["administrator"],
            "groups": ["owners"],
            "scope": "everything",
            "email": "private@example.test",
            "name": "Private Name",
        },
    )

    authenticated = await runtime.authentication.authenticate(credential(token), object())

    assert authenticated.model_dump() == {
        "alias": {
            "issuer": ISSUER,
            "subject": "provider-subject-123",
        },
        "authentication_configuration_version": "oidc-2026-10-10",
    }
    assert all(
        value not in repr(authenticated)
        for value in ("administrator", "owners", "private@example.test", "Private Name")
    )
    await runtime.aclose()


@pytest.mark.asyncio
async def test_application_access_token_media_type_is_accepted(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
) -> None:
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=provider_transport(public_jwk),
        clock=clock,
    )

    authenticated = await runtime.authentication.authenticate(
        credential(
            signed_token(
                rsa_private_key,
                clock,
                typ="application/at+jwt",
            )
        ),
        object(),
    )

    assert authenticated.alias.subject == "provider-subject-123"
    await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token_options",
    [
        {"typ": "JWT"},
        {"typ": "id+jwt"},
        {"extra_headers": {"crit": ["private"]}},
        {"kid": ""},
    ],
)
async def test_invalid_protected_header_fails_as_invalid_credential(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
    token_options: dict[str, Any],
) -> None:
    await assert_token_is_invalid(
        oidc_settings=oidc_settings,
        public_jwk=public_jwk,
        rsa_private_key=rsa_private_key,
        clock=clock,
        token_options=token_options,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"iss": "https://identity.example.test/other"},
        {"aud": "https://other-api.example.test"},
        {"aud": [AUDIENCE, "https://other-api.example.test"]},
        {"client_id": "machine-client"},
    ],
)
async def test_wrong_issuer_audience_or_client_fails_as_invalid_credential(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
    claim_overrides: dict[str, Any],
) -> None:
    await assert_token_is_invalid(
        oidc_settings=oidc_settings,
        public_jwk=public_jwk,
        rsa_private_key=rsa_private_key,
        clock=clock,
        claim_overrides=claim_overrides,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"sub": ""},
        {"exp": None},
        {"iat": None},
        {"jti": None},
        {"jti": ""},
    ],
)
async def test_missing_or_empty_required_claim_fails_as_invalid_credential(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
    claim_overrides: dict[str, Any],
) -> None:
    await assert_token_is_invalid(
        oidc_settings=oidc_settings,
        public_jwk=public_jwk,
        rsa_private_key=rsa_private_key,
        clock=clock,
        claim_overrides=claim_overrides,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"exp": 1_799_999_994},
        {"iat": 1_800_000_006},
        {"nbf": 1_800_000_006},
    ],
)
async def test_invalid_time_window_fails_as_invalid_credential(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
    claim_overrides: dict[str, Any],
) -> None:
    await assert_token_is_invalid(
        oidc_settings=oidc_settings,
        public_jwk=public_jwk,
        rsa_private_key=rsa_private_key,
        clock=clock,
        claim_overrides=claim_overrides,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim_overrides",
    [{"exp": "1800000300"}, {"iat": True}],
)
async def test_non_numeric_time_claim_fails_as_invalid_credential(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
    claim_overrides: dict[str, Any],
) -> None:
    await assert_token_is_invalid(
        oidc_settings=oidc_settings,
        public_jwk=public_jwk,
        rsa_private_key=rsa_private_key,
        clock=clock,
        claim_overrides=claim_overrides,
    )


@pytest.mark.asyncio
async def test_wrong_signature_fails_as_invalid_credential(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    clock: MutableClock,
) -> None:
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=provider_transport(public_jwk),
        clock=clock,
    )

    with pytest.raises(InvalidAuthenticationError):
        await runtime.authentication.authenticate(
            credential(signed_token(other_key, clock)), object()
        )
    await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        "one.two.three.four",
        "e30.e30.signature",
    ],
)
async def test_malformed_compact_token_fails_as_invalid_credential(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    clock: MutableClock,
    token: str,
) -> None:
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=provider_transport(public_jwk),
        clock=clock,
    )

    with pytest.raises(InvalidAuthenticationError):
        await runtime.authentication.authenticate(credential(token), object())
    await runtime.aclose()


@pytest.mark.asyncio
async def test_none_and_hmac_algorithms_fail_before_provider_io(
    oidc_settings: Any,
    clock: MutableClock,
) -> None:
    requests: list[str] = []
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=httpx.MockTransport(
            lambda request: requests.append(str(request.url)) or httpx.Response(500)
        ),
        clock=clock,
    )
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "subject",
        "client_id": CLIENT_ID,
        "exp": clock.timestamp + 100,
        "iat": clock.timestamp,
        "jti": "jti",
    }
    tokens = (
        jwt.encode(claims, key="", algorithm="none", headers={"typ": "at+jwt"}),
        jwt.encode(
            claims,
            key=b"public-key-confusion-material-32b",
            algorithm="HS256",
            headers={"typ": "at+jwt", "kid": "key-1"},
        ),
    )

    for token in tokens:
        with pytest.raises(InvalidAuthenticationError):
            await runtime.authentication.authenticate(credential(token), object())
    assert requests == []
    await runtime.aclose()


@pytest.mark.asyncio
async def test_provider_failure_without_cache_fails_as_unavailable(
    oidc_settings: Any,
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
) -> None:
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        clock=clock,
    )

    with pytest.raises(AuthenticationUnavailableError):
        await runtime.authentication.authenticate(
            credential(signed_token(rsa_private_key, clock)), object()
        )
    await runtime.aclose()
