from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from spine.application.diagnostics import DiagnosticContext
from spine.auth import AccessTokenCredential, InvalidAuthenticationError
from spine.infrastructure.auth import (
    LocalAuthenticationAdapter,
    LocalAuthenticationConfigurationError,
    LocalAuthenticationSettings,
)


LOCAL_CREDENTIAL = "local-development-credential-0000001"


def settings(**overrides: object) -> LocalAuthenticationSettings:
    values: dict[str, object] = {
        "deployment_mode": "development",
        "configuration_version": "local-2026-10-11",
        "authentication_alias_issuer": "spine-local://development",
        "authentication_alias_subject": "configured-local-administrator",
        "bearer_credential": LOCAL_CREDENTIAL,
    }
    values.update(overrides)
    return LocalAuthenticationSettings(**values)


@pytest.mark.parametrize("deployment_mode", ["staging", "production", "", "dev"])
def test_local_settings_reject_non_development_deployment_modes(
    deployment_mode: str,
) -> None:
    with pytest.raises(ValidationError):
        settings(deployment_mode=deployment_mode)


@pytest.mark.parametrize(
    "overrides",
    [
        {"configuration_version": ""},
        {"configuration_version": "version with spaces"},
        {"authentication_alias_issuer": ""},
        {"authentication_alias_issuer": "local\nissuer"},
        {"authentication_alias_subject": ""},
        {"authentication_alias_subject": "local\x00subject"},
        {"bearer_credential": "too-short"},
        {"bearer_credential": "local credential containing whitespace 0001"},
        {"bearer_credential": "local-development-credential-00000!"},
    ],
)
def test_local_settings_reject_malformed_version_alias_or_credential(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        settings(**overrides)


def test_local_settings_require_every_guard_value(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SPINE_LOCAL_AUTH_DEPLOYMENT_MODE",
        "SPINE_LOCAL_AUTH_CONFIGURATION_VERSION",
        "SPINE_LOCAL_AUTH_AUTHENTICATION_ALIAS_ISSUER",
        "SPINE_LOCAL_AUTH_AUTHENTICATION_ALIAS_SUBJECT",
        "SPINE_LOCAL_AUTH_BEARER_CREDENTIAL",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError):
        LocalAuthenticationSettings()


def test_local_settings_accept_rfc_6750_b64token_padding() -> None:
    configured = settings(
        bearer_credential="local-development-credential-0000001===",
    )

    assert configured.bearer_credential.get_secret_value().endswith("===")


def test_local_adapter_revalidates_settings_at_startup() -> None:
    bypassed = LocalAuthenticationSettings.model_construct(
        deployment_mode="production",
        configuration_version="local-2026-10-11",
        authentication_alias_issuer="spine-local://production",
        authentication_alias_subject="configured-local-administrator",
        bearer_credential=SecretStr(LOCAL_CREDENTIAL),
    )

    with pytest.raises(LocalAuthenticationConfigurationError):
        LocalAuthenticationAdapter(bypassed)


async def test_local_adapter_authenticates_only_the_configured_alias() -> None:
    adapter = LocalAuthenticationAdapter(settings())
    context = DiagnosticContext(
        trace_id=UUID("50000000-0000-0000-0000-000000000074")
    )

    authenticated = await adapter.authenticate(
        AccessTokenCredential(access_token=SecretStr(LOCAL_CREDENTIAL)),
        context,
    )

    assert authenticated.alias.issuer == "spine-local://development"
    assert authenticated.alias.subject == "configured-local-administrator"
    assert authenticated.authentication_configuration_version == "local-2026-10-11"


@pytest.mark.parametrize(
    "credential",
    [
        "configured-local-administrator",
        "administrator",
        "20000000-0000-0000-0000-000000000073",
        "different-local-development-secret",
    ],
)
async def test_local_bearer_cannot_select_identity_role_or_scope(
    credential: str,
) -> None:
    adapter = LocalAuthenticationAdapter(settings())
    context = DiagnosticContext(
        trace_id=UUID("50000000-0000-0000-0000-000000000074")
    )

    with pytest.raises(InvalidAuthenticationError):
        await adapter.authenticate(
            AccessTokenCredential(access_token=SecretStr(credential)),
            context,
        )
