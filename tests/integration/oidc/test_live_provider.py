from __future__ import annotations

import os

from pydantic import SecretStr, ValidationError
import pytest

from spine.auth import AccessTokenCredential
from spine.infrastructure.auth import (
    InteractiveClientQualification,
    OIDCAuthenticationRuntime,
    OIDCAuthenticationSettings,
    OIDCReadinessStatus,
)


pytestmark = pytest.mark.live_oidc


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.fail(f"live OIDC qualification requires {name}")
    return value


async def test_live_provider_accepts_qualified_rfc9068_access_token() -> None:
    if os.getenv("SPINE_RUN_LIVE_OIDC") != "1":
        pytest.skip("live OIDC qualification is opt-in (SPINE_RUN_LIVE_OIDC=1)")

    configuration_version = _required_environment(
        "SPINE_LIVE_OIDC_CONFIGURATION_VERSION"
    )
    issuer = _required_environment("SPINE_LIVE_OIDC_ISSUER")
    audience = _required_environment("SPINE_LIVE_OIDC_AUDIENCE")
    client_id = _required_environment("SPINE_LIVE_OIDC_CLIENT_ID")
    access_token = _required_environment("SPINE_LIVE_OIDC_ACCESS_TOKEN")
    client_credentials_evidence = _required_environment(
        "SPINE_LIVE_OIDC_CLIENT_CREDENTIALS_DISABLED_EVIDENCE"
    )
    try:
        settings = OIDCAuthenticationSettings(
            configuration_version=configuration_version,
            issuer=issuer,
            audience=audience,
            qualified_interactive_clients=(
                InteractiveClientQualification(
                    client_id=client_id,
                    configuration_version=configuration_version,
                    issuer=issuer,
                    audience=audience,
                    client_credentials_for_audience_disabled=True,
                    evidence_reference=client_credentials_evidence,
                ),
            ),
        )
    except ValidationError as error:
        pytest.fail(f"live OIDC qualification configuration is invalid: {error}")

    async with OIDCAuthenticationRuntime(settings) as runtime:
        readiness = await runtime.startup()
        assert readiness.status is OIDCReadinessStatus.READY

        authenticated = await runtime.authentication.authenticate(
            AccessTokenCredential(access_token=SecretStr(access_token)),
            object(),
        )

    assert authenticated.authentication_configuration_version == configuration_version
    assert authenticated.alias.issuer == issuer
    assert authenticated.alias.subject
