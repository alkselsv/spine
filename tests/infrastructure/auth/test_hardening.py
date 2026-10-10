from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError
import pytest

from spine.auth import (
    AccessTokenCredential,
    AuthenticationUnavailableError,
    InvalidAuthenticationError,
)
from spine.infrastructure.auth import OIDCAuthenticationRuntime

from .conftest import MutableClock, provider_transport, signed_token


@pytest.mark.asyncio
async def test_oversized_header_and_claim_parts_fail_before_provider_io(
    oidc_settings: Any,
    rsa_private_key: Any,
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
    tokens = (
        signed_token(
            rsa_private_key,
            clock,
            extra_headers={"oversized": "h" * 5_000},
        ),
        signed_token(
            rsa_private_key,
            clock,
            claim_overrides={"oversized": "c" * 9_000},
        ),
    )

    for token in tokens:
        with pytest.raises(InvalidAuthenticationError):
            await runtime.authentication.authenticate(
                AccessTokenCredential(access_token=SecretStr(token)), object()
            )
    assert requests == []
    await runtime.aclose()


def test_total_oversized_credential_is_rejected_by_parent_contract() -> None:
    with pytest.raises(ValidationError):
        AccessTokenCredential(access_token=SecretStr("x" * 16_385))


@pytest.mark.asyncio
async def test_provider_and_token_secrets_never_escape_errors_or_logging(
    oidc_settings: Any,
    rsa_private_key: Any,
    clock: MutableClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    protected = (
        "raw-token-corpus",
        "provider-secret-corpus",
        "private-claim-corpus",
    )

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(protected[1], request=request)

    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=httpx.MockTransport(unavailable),
        clock=clock,
    )
    token = signed_token(
        rsa_private_key,
        clock,
        claim_overrides={"private": protected[2]},
        extra_headers={"private": protected[0]},
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(AuthenticationUnavailableError) as captured:
        await runtime.authentication.authenticate(
            AccessTokenCredential(access_token=SecretStr(token)), object()
        )

    exposed = f"{captured.value!r}\n{captured.value}\n{caplog.text}"
    assert token not in exposed
    assert all(value not in exposed for value in protected)
    readiness = await runtime.check_readiness()
    assert all(value not in readiness.model_dump_json() for value in protected)
    await runtime.aclose()


@pytest.mark.asyncio
async def test_invalid_claim_set_never_escapes_error_or_logging(
    oidc_settings: Any,
    public_jwk: dict[str, Any],
    rsa_private_key: Any,
    clock: MutableClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    protected = "private-claim-corpus"
    runtime = OIDCAuthenticationRuntime(
        oidc_settings,
        transport=provider_transport(public_jwk),
        clock=clock,
    )
    token = signed_token(
        rsa_private_key,
        clock,
        claim_overrides={"aud": protected, "roles": [protected]},
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(InvalidAuthenticationError) as captured:
        await runtime.authentication.authenticate(
            AccessTokenCredential(access_token=SecretStr(token)), object()
        )

    exposed = f"{captured.value!r}\n{captured.value}\n{caplog.text}"
    assert token not in exposed
    assert protected not in exposed
    await runtime.aclose()


def test_importing_oidc_modules_performs_no_network_or_client_construction() -> None:
    program = """
import httpx
import socket

def reject(*args, **kwargs):
    raise AssertionError("import-time side effect")

httpx.AsyncClient = reject
socket.socket.connect = reject

import spine.auth
import spine.infrastructure.auth.settings
import spine.infrastructure.auth.oidc
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = "src"

    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).parents[3],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
