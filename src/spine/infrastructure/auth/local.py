"""Fail-closed authentication adapter for explicit local development only."""

from __future__ import annotations

import secrets
from typing import Any, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from spine.auth import (
    AccessTokenCredential,
    AuthenticatedAlias,
    AuthenticationAlias,
    InvalidAuthenticationError,
    is_http_bearer_token,
)
from spine.domain.common import VersionIdentifier, require_version_identifier


class LocalAuthenticationSettings(BaseSettings):
    """One immutable local credential and separately configured alias."""

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_file=None,
        env_prefix="SPINE_LOCAL_AUTH_",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    deployment_mode: Literal["development"]
    configuration_version: VersionIdentifier
    authentication_alias_issuer: str = Field(min_length=1, max_length=2048)
    authentication_alias_subject: str = Field(min_length=1, max_length=512)
    bearer_credential: SecretStr = Field(repr=False)

    @field_validator("configuration_version")
    @classmethod
    def _valid_version(cls, value: str) -> str:
        return require_version_identifier(
            value,
            field_name="configuration_version",
        )

    @field_validator("bearer_credential")
    @classmethod
    def _valid_credential(cls, value: SecretStr) -> SecretStr:
        credential = value.get_secret_value()
        if (
            len(credential) < 32
            or len(credential) > 512
            or not is_http_bearer_token(credential)
        ):
            raise ValueError("local bearer credential is malformed")
        return value

    @model_validator(mode="after")
    def _valid_alias(self) -> "LocalAuthenticationSettings":
        AuthenticationAlias(
            issuer=self.authentication_alias_issuer,
            subject=self.authentication_alias_subject,
        )
        return self

    @property
    def authentication_alias(self) -> AuthenticationAlias:
        return AuthenticationAlias(
            issuer=self.authentication_alias_issuer,
            subject=self.authentication_alias_subject,
        )


class LocalAuthenticationConfigurationError(RuntimeError):
    """Local authentication could not be activated safely at startup."""

    def __init__(self) -> None:
        super().__init__("Local authentication configuration is invalid.")


class LocalAuthenticationAdapter:
    """Authenticate one fixed bearer without deriving any authority from it."""

    __slots__ = ("_alias", "_configuration_version", "_credential")

    def __init__(self, settings: LocalAuthenticationSettings) -> None:
        try:
            if type(settings) is not LocalAuthenticationSettings:
                raise TypeError
            validated = LocalAuthenticationSettings(
                deployment_mode=settings.deployment_mode,
                configuration_version=settings.configuration_version,
                authentication_alias_issuer=settings.authentication_alias_issuer,
                authentication_alias_subject=settings.authentication_alias_subject,
                bearer_credential=settings.bearer_credential,
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise LocalAuthenticationConfigurationError() from None
        self._alias = validated.authentication_alias
        self._configuration_version = validated.configuration_version
        self._credential = validated.bearer_credential.get_secret_value()

    async def authenticate(
        self,
        credentials: AccessTokenCredential,
        diagnostic_context: Any,
    ) -> AuthenticatedAlias:
        del diagnostic_context
        try:
            if type(credentials) is not AccessTokenCredential:
                raise InvalidAuthenticationError()
            presented = credentials.access_token.get_secret_value()
            if not secrets.compare_digest(presented, self._credential):
                raise InvalidAuthenticationError()
        except InvalidAuthenticationError:
            raise
        except Exception:
            raise InvalidAuthenticationError() from None
        return AuthenticatedAlias(
            alias=AuthenticationAlias(
                issuer=self._alias.issuer,
                subject=self._alias.subject,
            ),
            authentication_configuration_version=self._configuration_version,
        )


__all__ = [
    "LocalAuthenticationAdapter",
    "LocalAuthenticationConfigurationError",
    "LocalAuthenticationSettings",
]
