from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from spine.auth import (
    AccessTokenCredential,
    AuthFailureCategory,
    AuthFailureRetryability,
    AuthenticationPort,
    AuthenticationUnavailableError,
    AuthorizationDeniedError,
    AuthorizationDirectory,
    AuthorizationUnavailableError,
    InvalidAuthenticationError,
)


def test_authentication_credential_redacts_access_token() -> None:
    credential = AccessTokenCredential(access_token=SecretStr("header.payload.signature"))

    assert "header.payload.signature" not in repr(credential)
    assert credential.model_dump(mode="json") == {"access_token": "**********"}


def test_authentication_credential_rejects_authority_fields() -> None:
    with pytest.raises(ValidationError):
        AccessTokenCredential(
            access_token=SecretStr("header.payload.signature"),
            roles=["administrator"],
        )


FAILURE_CONTRACTS = (
    (
        InvalidAuthenticationError,
        AuthFailureCategory.AUTHENTICATION_INVALID,
        AuthFailureRetryability.NEVER,
    ),
    (
        AuthenticationUnavailableError,
        AuthFailureCategory.AUTHENTICATION_UNAVAILABLE,
        AuthFailureRetryability.AFTER_DELAY,
    ),
    (
        AuthorizationDeniedError,
        AuthFailureCategory.AUTHORIZATION_DENIED,
        AuthFailureRetryability.NEVER,
    ),
    (
        AuthorizationUnavailableError,
        AuthFailureCategory.AUTHORIZATION_UNAVAILABLE,
        AuthFailureRetryability.AFTER_DELAY,
    ),
)


@pytest.mark.parametrize(
    ("failure_type", "category", "retryability"),
    FAILURE_CONTRACTS,
    ids=[contract[1].value for contract in FAILURE_CONTRACTS],
)
def test_auth_failures_expose_stable_category_and_retryability(
    failure_type: type[Exception],
    category: AuthFailureCategory,
    retryability: AuthFailureRetryability,
) -> None:
    failure = failure_type()

    assert failure.category is category  # type: ignore[attr-defined]
    assert failure.retryability is retryability  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "failure_type",
    [contract[0] for contract in FAILURE_CONTRACTS],
)
@pytest.mark.parametrize(
    "protected_value",
    [
        "Bearer secret-token",
        "https://tenant.example.test/private",
        "customer contract text",
        "SELECT * FROM authentication_aliases",
    ],
)
def test_auth_failures_have_disclosure_safe_messages(
    failure_type: type[Exception],
    protected_value: str,
) -> None:
    failure = failure_type()

    assert protected_value not in str(failure)
    assert protected_value not in repr(failure)


@pytest.mark.parametrize("failure_type", [contract[0] for contract in FAILURE_CONTRACTS])
def test_auth_failures_reject_external_detail(
    failure_type: type[Exception],
) -> None:
    with pytest.raises(TypeError):
        failure_type("provider failure detail")


def test_authentication_port_exposes_only_authenticate() -> None:
    assert AuthenticationPort.__dict__.keys() >= {"authenticate"}

    forbidden = {"add", "create", "delete", "get", "list", "update"}
    assert AuthenticationPort.__dict__.keys().isdisjoint(forbidden)


def test_authorization_directory_exposes_only_request_resolution() -> None:
    assert AuthorizationDirectory.__dict__.keys() >= {"resolve_request_authority"}

    forbidden = {"add", "create", "delete", "get", "list", "update"}
    assert AuthorizationDirectory.__dict__.keys().isdisjoint(forbidden)
