from __future__ import annotations

import pytest
from pydantic import ValidationError

from spine.infrastructure.auth import (
    InteractiveClientQualification,
    OIDCAuthenticationSettings,
)


def qualification(
    client_id: str = "spine-web",
    *,
    configuration_version: str = "oidc-2026-10-10",
    issuer: str = "https://identity.example.test/tenant",
    audience: str = "https://api.spine.example.test",
) -> InteractiveClientQualification:
    return InteractiveClientQualification(
        client_id=client_id,
        configuration_version=configuration_version,
        issuer=issuer,
        audience=audience,
        client_credentials_for_audience_disabled=True,
        evidence_reference="provider-change-2026-10-10",
    )


def settings(**overrides: object) -> OIDCAuthenticationSettings:
    values: dict[str, object] = {
        "configuration_version": "oidc-2026-10-10",
        "issuer": "https://identity.example.test/tenant",
        "audience": "https://api.spine.example.test",
        "qualified_interactive_clients": (qualification(),),
    }
    values.update(overrides)
    return OIDCAuthenticationSettings(**values)


def test_settings_preserve_explicit_configuration_version() -> None:
    configured = settings()

    assert configured.configuration_version == "oidc-2026-10-10"


def test_settings_are_frozen() -> None:
    configured = settings()

    with pytest.raises(ValidationError):
        configured.configuration_version = "changed"  # type: ignore[misc]


def test_settings_reject_extra_fields() -> None:
    with pytest.raises(ValidationError):
        settings(unexpected="authority")


@pytest.mark.parametrize(
    "overrides",
    [
        {"issuer": "http://identity.example.test"},
        {"issuer": "https://identity.example.test?tenant=one"},
        {"issuer": "https://identity.example.test#tenant"},
        {"issuer": "https://user@identity.example.test"},
    ],
)
def test_settings_reject_ineligible_issuer_url(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        settings(**overrides)


@pytest.mark.parametrize(
    "qualified_clients",
    [
        (),
        (qualification(), qualification()),
    ],
)
def test_settings_reject_missing_or_duplicate_interactive_clients(
    qualified_clients: tuple[InteractiveClientQualification, ...],
) -> None:
    with pytest.raises(ValidationError):
        settings(qualified_interactive_clients=qualified_clients)


@pytest.mark.parametrize("algorithm", ["HS256", "RS512", "ES256"])
def test_settings_reject_unsupported_signing_algorithm(algorithm: str) -> None:
    with pytest.raises(ValidationError):
        settings(allowed_algorithms=frozenset({algorithm}))


def test_settings_reject_stale_interval_shorter_than_fresh_ttl() -> None:
    with pytest.raises(ValidationError):
        settings(fresh_cache_ttl_seconds=120, maximum_stale_seconds=119)


def test_interactive_client_requires_negative_client_credentials_proof() -> None:
    with pytest.raises(ValidationError):
        InteractiveClientQualification(
            client_id="spine-web",
            configuration_version="oidc-2026-10-10",
            issuer="https://identity.example.test/tenant",
            audience="https://api.spine.example.test",
            client_credentials_for_audience_disabled=False,
            evidence_reference="provider-change-2026-10-10",
        )


@pytest.mark.parametrize(
    "qualified_client",
    [
        qualification(configuration_version="oidc-other"),
        qualification(issuer="https://identity.example.test/other"),
        qualification(audience="https://other-api.example.test"),
    ],
)
def test_settings_reject_qualification_for_other_configuration(
    qualified_client: InteractiveClientQualification,
) -> None:
    with pytest.raises(ValidationError):
        settings(qualified_interactive_clients=(qualified_client,))


def test_settings_expose_only_qualified_client_ids_to_token_validation() -> None:
    configured = settings(
        qualified_interactive_clients=(
            qualification("spine-web"),
            qualification("spine-admin"),
        )
    )

    assert configured.qualified_interactive_client_ids == frozenset(
        {"spine-web", "spine-admin"}
    )
