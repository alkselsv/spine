from __future__ import annotations

from typing import Any

from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
import pytest

from spine.auth import AccessTokenCredential, InvalidAuthenticationError
from spine.infrastructure.auth import OIDCAuthenticationRuntime

from .conftest import MutableClock, provider_transport, signed_token


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claims", "valid"),
    [
        ({"exp": 1_799_999_995.001}, True),
        ({"exp": 1_799_999_995}, False),
        ({"iat": 1_800_000_005}, True),
        ({"iat": 1_800_000_005.001}, False),
        ({"nbf": 1_800_000_005}, True),
        ({"nbf": 1_800_000_005.001}, False),
    ],
)
async def test_clock_skew_boundaries_use_injected_time(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
    claims: dict[str, float],
    valid: bool,
) -> None:
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=provider_transport(public_jwk),
        clock=clock,
    )
    token = AccessTokenCredential(
        access_token=SecretStr(
            signed_token(rsa_private_key, clock, claim_overrides=claims)
        )
    )

    if valid:
        assert (await runtime.authentication.authenticate(token, object())).alias.subject
    else:
        with pytest.raises(InvalidAuthenticationError):
            await runtime.authentication.authenticate(token, object())
    await runtime.aclose()
