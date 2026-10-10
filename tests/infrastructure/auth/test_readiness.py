from __future__ import annotations

from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
import pytest

from spine.auth import AccessTokenCredential, AuthenticationUnavailableError
from spine.infrastructure.auth import (
    OIDCAuthenticationRuntime,
    OIDCReadinessCode,
    OIDCReadinessPolicy,
    OIDCReadinessStatus,
)

from .conftest import ISSUER, JWKS_URI, MutableClock, provider_transport, signed_token


@pytest.mark.asyncio
async def test_runtime_construction_performs_no_provider_io(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    clock: MutableClock,
) -> None:
    requests: list[str] = []
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=provider_transport(
            public_jwk, observe=lambda request: requests.append(str(request.url))
        ),
        clock=clock,
    )

    assert requests == []
    await runtime.aclose()


@pytest.mark.asyncio
async def test_readiness_fetches_and_validates_discovery_and_jwks(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    clock: MutableClock,
) -> None:
    requests: list[str] = []
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=provider_transport(
            public_jwk, observe=lambda request: requests.append(str(request.url))
        ),
        clock=clock,
    )

    readiness = await runtime.check_readiness()

    assert requests == [
        f"{ISSUER}/.well-known/openid-configuration",
        JWKS_URI,
    ]
    assert readiness.status is OIDCReadinessStatus.READY
    assert readiness.code is OIDCReadinessCode.READY
    assert readiness.configuration_version == "oidc-2026-10-10"
    assert readiness.mode == "oidc"
    assert readiness.issuer_origin == "https://identity.example.test"
    assert readiness.key_cache_freshness == "fresh"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_readiness_omits_client_key_and_qualification_details(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    clock: MutableClock,
) -> None:
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=provider_transport(public_jwk),
        clock=clock,
    )

    readiness = await runtime.check_readiness()

    serialized = readiness.model_dump_json()
    assert "spine-web" not in serialized
    assert "key-1" not in serialized
    assert "provider-change" not in serialized

    await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("discovery_overrides", "code"),
    [
        (
            {"issuer": "https://identity.example.test/other"},
            OIDCReadinessCode.PROVIDER_INCOMPATIBLE,
        ),
        (
            {"jwks_uri": "http://identity.example.test/tenant/keys"},
            OIDCReadinessCode.PROVIDER_INCOMPATIBLE,
        ),
        (
            {"jwks_uri": "https://keys.example.test/tenant/keys"},
            OIDCReadinessCode.PROVIDER_INCOMPATIBLE,
        ),
    ],
)
async def test_readiness_rejects_inexact_issuer_or_ineligible_jwks_origin(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    clock: MutableClock,
    discovery_overrides: dict[str, str],
    code: OIDCReadinessCode,
) -> None:
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=provider_transport(
            public_jwk, discovery_overrides=discovery_overrides
        ),
        clock=clock,
    )

    readiness = await runtime.check_readiness()

    assert readiness.status is OIDCReadinessStatus.UNAVAILABLE
    assert readiness.code is code
    assert readiness.configuration_version == "oidc-2026-10-10"
    serialized = readiness.model_dump_json()
    assert all(value not in serialized for value in discovery_overrides.values())
    await runtime.aclose()


@pytest.mark.asyncio
async def test_readiness_does_not_follow_provider_redirects(
    oidc_settings: Any,
    clock: MutableClock,
) -> None:
    requests: list[str] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://attacker.example.test/discovery"},
        )

    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=httpx.MockTransport(redirect),
        clock=clock,
    )

    readiness = await runtime.check_readiness()

    assert readiness.status is OIDCReadinessStatus.UNAVAILABLE
    assert readiness.code is OIDCReadinessCode.PROVIDER_INCOMPATIBLE
    assert requests == [f"{ISSUER}/.well-known/openid-configuration"]
    await runtime.aclose()


@pytest.mark.asyncio
async def test_closed_runtime_readiness_performs_no_provider_io(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    clock: MutableClock,
) -> None:
    requests: list[str] = []
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=provider_transport(
            public_jwk, observe=lambda request: requests.append(str(request.url))
        ),
        clock=clock,
    )
    await runtime.aclose()

    readiness = await runtime.check_readiness()

    assert readiness.status is OIDCReadinessStatus.UNAVAILABLE
    assert readiness.code is OIDCReadinessCode.RUNTIME_CLOSED
    assert requests == []


@pytest.mark.asyncio
async def test_closed_runtime_authentication_fails_unavailable(
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
    await runtime.aclose()

    with pytest.raises(AuthenticationUnavailableError):
        await runtime.authentication.authenticate(
            AccessTokenCredential(
                access_token=SecretStr(signed_token(rsa_private_key, clock))
            ),
            object(),
        )


@pytest.mark.asyncio
async def test_deferred_startup_policy_performs_no_provider_io(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    clock: MutableClock,
) -> None:
    requests: list[str] = []
    configured = oidc_settings.model_copy(
        update={"readiness_policy": OIDCReadinessPolicy.DEFERRED}
    )
    runtime = OIDCAuthenticationRuntime(
        configured,
        transport=provider_transport(
            public_jwk, observe=lambda request: requests.append(str(request.url))
        ),
        clock=clock,
    )

    readiness = await runtime.startup()

    assert readiness.status is OIDCReadinessStatus.DEFERRED
    assert readiness.code is OIDCReadinessCode.CHECK_DEFERRED
    assert readiness.configuration_version == "oidc-2026-10-10"
    assert requests == []
    await runtime.aclose()


@pytest.mark.asyncio
async def test_required_startup_policy_runs_readiness_check(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    clock: MutableClock,
) -> None:
    requests: list[str] = []
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=provider_transport(
            public_jwk, observe=lambda request: requests.append(str(request.url))
        ),
        clock=clock,
    )

    readiness = await runtime.startup()

    assert readiness.status is OIDCReadinessStatus.READY
    assert len(requests) == 2
    await runtime.aclose()
