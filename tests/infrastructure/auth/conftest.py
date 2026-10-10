from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest

from spine.infrastructure.auth import (
    InteractiveClientQualification,
    OIDCAuthenticationSettings,
)


ISSUER = "https://identity.example.test/tenant"
JWKS_URI = "https://identity.example.test/tenant/keys"
AUDIENCE = "https://api.spine.example.test"
CLIENT_ID = "spine-web"


class MutableClock:
    def __init__(self, timestamp: float = 1_800_000_000.0) -> None:
        self.timestamp = timestamp

    def __call__(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc)

    def advance(self, seconds: float) -> None:
        self.timestamp += seconds


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture(scope="session")
def rsa_private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def public_jwk(rsa_private_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    value = jwt.algorithms.RSAAlgorithm.to_jwk(
        rsa_private_key.public_key(), as_dict=True
    )
    value.update({"kid": "key-1", "use": "sig", "alg": "RS256"})
    return value


@pytest.fixture
def oidc_settings() -> OIDCAuthenticationSettings:
    return OIDCAuthenticationSettings(
        configuration_version="oidc-2026-10-10",
        issuer=ISSUER,
        audience=AUDIENCE,
        qualified_interactive_clients=(
            InteractiveClientQualification(
                client_id=CLIENT_ID,
                configuration_version="oidc-2026-10-10",
                issuer=ISSUER,
                audience=AUDIENCE,
                client_credentials_for_audience_disabled=True,
                evidence_reference="provider-change-2026-10-10",
            ),
        ),
        fresh_cache_ttl_seconds=60,
        maximum_stale_seconds=300,
        clock_skew_seconds=5,
    )


def provider_transport(
    public_jwk: dict[str, Any],
    *,
    discovery_overrides: dict[str, Any] | None = None,
    jwks_overrides: dict[str, Any] | None = None,
    observe: Callable[[httpx.Request], None] | None = None,
) -> httpx.MockTransport:
    discovery: dict[str, Any] = {"issuer": ISSUER, "jwks_uri": JWKS_URI}
    discovery.update(discovery_overrides or {})
    jwks: dict[str, Any] = {"keys": [public_jwk]}
    jwks.update(jwks_overrides or {})

    def handler(request: httpx.Request) -> httpx.Response:
        if observe is not None:
            observe(request)
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=discovery)
        if str(request.url) == JWKS_URI:
            return httpx.Response(200, json=jwks)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def signed_token(
    private_key: rsa.RSAPrivateKey,
    clock: MutableClock,
    *,
    kid: str = "key-1",
    typ: str = "at+jwt",
    algorithm: str = "RS256",
    claim_overrides: dict[str, Any] | None = None,
    extra_headers: dict[str, Any] | None = None,
) -> str:
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "provider-subject-123",
        "client_id": CLIENT_ID,
        "exp": clock.timestamp + 300,
        "iat": clock.timestamp - 10,
        "jti": "access-token-123",
    }
    claims.update(claim_overrides or {})
    headers: dict[str, Any] = {"kid": kid, "typ": typ}
    headers.update(extra_headers or {})
    return jwt.encode(claims, private_key, algorithm=algorithm, headers=headers)
