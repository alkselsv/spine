from __future__ import annotations

import asyncio
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
from spine.infrastructure.auth import (
    OIDCAuthenticationRuntime,
    OIDCReadinessCode,
    OIDCReadinessStatus,
)

from .conftest import ISSUER, JWKS_URI, MutableClock, signed_token


def jwk_for(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    value = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    value.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return value


def credential(token: str) -> AccessTokenCredential:
    return AccessTokenCredential(access_token=SecretStr(token))


class RotatingProvider:
    def __init__(self, keys: list[dict[str, Any]]) -> None:
        self.keys = keys
        self.available = True
        self.discovery_requests = 0
        self.jwks_requests = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        if request.url.path.endswith("/.well-known/openid-configuration"):
            self.discovery_requests += 1
            if not self.available:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={"issuer": ISSUER, "jwks_uri": JWKS_URI},
            )
        if str(request.url) == JWKS_URI:
            self.jwks_requests += 1
            if not self.available:
                return httpx.Response(503)
            return httpx.Response(200, json={"keys": self.keys})
        return httpx.Response(404)


@pytest.mark.asyncio
async def test_unknown_kid_triggers_one_refresh_and_accepts_rotated_key(
    oidc_settings: Any,
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
) -> None:
    rotated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = RotatingProvider([jwk_for(rsa_private_key, "key-1")])
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=httpx.MockTransport(provider),
        clock=clock,
    )
    assert (await runtime.check_readiness()).status is OIDCReadinessStatus.READY
    provider.keys = [jwk_for(rotated_key, "key-2")]

    authenticated = await runtime.authentication.authenticate(
        credential(signed_token(rotated_key, clock, kid="key-2")), object()
    )

    assert authenticated.alias.subject == "provider-subject-123"
    assert provider.discovery_requests == 2
    assert provider.jwks_requests == 2
    await runtime.aclose()


@pytest.mark.asyncio
async def test_fresh_matching_key_performs_no_refresh(
    oidc_settings: Any,
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
) -> None:
    provider = RotatingProvider([jwk_for(rsa_private_key, "key-1")])
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=httpx.MockTransport(provider),
        clock=clock,
    )
    await runtime.check_readiness()
    token = credential(signed_token(rsa_private_key, clock))

    results = await asyncio.gather(
        *(runtime.authentication.authenticate(token, object()) for _ in range(20))
    )

    assert len(results) == 20
    assert provider.discovery_requests == 1
    assert provider.jwks_requests == 1
    await runtime.aclose()


@pytest.mark.asyncio
async def test_still_unknown_kid_refreshes_at_most_once_per_attempt(
    oidc_settings: Any,
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
) -> None:
    unknown_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = RotatingProvider([jwk_for(rsa_private_key, "key-1")])
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=httpx.MockTransport(provider),
        clock=clock,
    )
    await runtime.check_readiness()

    with pytest.raises(InvalidAuthenticationError):
        await runtime.authentication.authenticate(
            credential(signed_token(unknown_key, clock, kid="missing-key")), object()
        )

    assert provider.discovery_requests == 2
    assert provider.jwks_requests == 2
    await runtime.aclose()


@pytest.mark.asyncio
async def test_unknown_kid_refresh_failure_is_provider_unavailable(
    oidc_settings: Any,
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
) -> None:
    unknown_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = RotatingProvider([jwk_for(rsa_private_key, "key-1")])
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=httpx.MockTransport(provider),
        clock=clock,
    )
    await runtime.check_readiness()
    provider.available = False

    with pytest.raises(AuthenticationUnavailableError):
        await runtime.authentication.authenticate(
            credential(signed_token(unknown_key, clock, kid="unknown-key")), object()
        )

    assert provider.discovery_requests == 2
    assert provider.jwks_requests == 1
    await runtime.aclose()


@pytest.mark.asyncio
async def test_concurrent_unknown_kid_rotation_uses_one_shared_refresh(
    oidc_settings: Any,
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
) -> None:
    rotated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = RotatingProvider([jwk_for(rsa_private_key, "key-1")])
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=httpx.MockTransport(provider),
        clock=clock,
    )
    await runtime.check_readiness()
    provider.keys = [jwk_for(rotated_key, "key-2")]
    rotated_token = credential(signed_token(rotated_key, clock, kid="key-2"))

    results = await asyncio.gather(
        *(runtime.authentication.authenticate(rotated_token, object()) for _ in range(20))
    )

    assert len(results) == 20
    assert provider.discovery_requests == 2
    assert provider.jwks_requests == 2
    await runtime.aclose()


@pytest.mark.asyncio
async def test_matching_stale_key_survives_bounded_outage_then_expires(
    oidc_settings: Any,
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
) -> None:
    provider = RotatingProvider([jwk_for(rsa_private_key, "key-1")])
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=httpx.MockTransport(provider),
        clock=clock,
    )
    token = credential(signed_token(rsa_private_key, clock))
    await runtime.check_readiness()
    clock.advance(61)
    provider.available = False

    results = await asyncio.gather(
        *(runtime.authentication.authenticate(token, object()) for _ in range(20))
    )

    assert len(results) == 20
    assert provider.discovery_requests == 2
    readiness = await runtime.check_readiness()
    assert readiness.status is OIDCReadinessStatus.DEGRADED
    assert readiness.code is OIDCReadinessCode.STALE_KEYS

    clock.advance(240)
    with pytest.raises(AuthenticationUnavailableError):
        await runtime.authentication.authenticate(token, object())
    assert (await runtime.check_readiness()).status is OIDCReadinessStatus.UNAVAILABLE
    await runtime.aclose()


@pytest.mark.asyncio
async def test_incompatible_refresh_never_falls_back_to_stale_key(
    oidc_settings: Any,
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
) -> None:
    existing = jwk_for(rsa_private_key, "key-1")
    provider = RotatingProvider([existing])
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=httpx.MockTransport(provider),
        clock=clock,
    )
    token = credential(signed_token(rsa_private_key, clock))
    await runtime.check_readiness()
    clock.advance(61)
    provider.keys = [existing, existing]

    with pytest.raises(AuthenticationUnavailableError):
        await runtime.authentication.authenticate(token, object())

    readiness = await runtime.check_readiness()
    assert readiness.status is OIDCReadinessStatus.UNAVAILABLE
    assert readiness.code is OIDCReadinessCode.PROVIDER_INCOMPATIBLE
    await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "keys",
    [
        pytest.param(
            lambda key: [jwk_for(key, "key-1"), jwk_for(key, "key-1")],
            id="duplicate-kid",
        ),
        pytest.param(
            lambda key: [{**jwk_for(key, "key-1"), "use": "enc"}],
            id="wrong-use",
        ),
        pytest.param(
            lambda key: [{**jwk_for(key, "key-1"), "alg": "RS512"}],
            id="incompatible-algorithm",
        ),
        pytest.param(
            lambda key: [
                jwk_for(
                    rsa.generate_private_key(public_exponent=65537, key_size=1024),
                    "weak-key",
                )
            ],
            id="weak-rsa-key",
        ),
        pytest.param(lambda key: [], id="missing-key"),
    ],
)
async def test_ambiguous_or_incompatible_jwks_fails_closed(
    oidc_settings: Any,
    rsa_private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
    keys: Any,
) -> None:
    provider = RotatingProvider(keys(rsa_private_key))
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=httpx.MockTransport(provider),
        clock=clock,
    )

    readiness = await runtime.check_readiness()

    assert readiness.status is OIDCReadinessStatus.UNAVAILABLE
    assert readiness.code is OIDCReadinessCode.PROVIDER_INCOMPATIBLE
    await runtime.aclose()
