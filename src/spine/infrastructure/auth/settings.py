"""Validated OIDC resource-server configuration without provider I/O."""

from __future__ import annotations

from enum import Enum
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_VERSION_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SAFE_IDENTIFIER = re.compile(r"[^\s\x00-\x1f\x7f]+")
_SUPPORTED_ALGORITHMS = frozenset({"RS256"})


class OIDCReadinessPolicy(str, Enum):
    """Whether provider readiness must be established during startup."""

    REQUIRED = "required"
    DEFERRED = "deferred"


class InteractiveClientQualification(BaseModel):
    """Operator evidence that one client is interactive for this audience."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_id: str = Field(min_length=1, max_length=512)
    configuration_version: str = Field(min_length=1, max_length=128)
    issuer: str = Field(min_length=1, max_length=2048)
    audience: str = Field(min_length=1, max_length=2048)
    client_credentials_for_audience_disabled: Literal[True]
    evidence_reference: str = Field(min_length=1, max_length=512)

    @field_validator("client_id", "issuer", "audience", "evidence_reference")
    @classmethod
    def _reject_ambiguous_text(cls, value: str) -> str:
        if _SAFE_IDENTIFIER.fullmatch(value) is None:
            raise ValueError("qualification values contain unsafe characters")
        return value

    @field_validator("configuration_version")
    @classmethod
    def _valid_version(cls, value: str) -> str:
        if _VERSION_IDENTIFIER.fullmatch(value) is None:
            raise ValueError("authentication configuration version is invalid")
        return value


class OIDCAuthenticationSettings(BaseSettings):
    """Immutable settings for one RFC 9068 resource-server authority."""

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_file=None,
        env_prefix="SPINE_OIDC_",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    configuration_version: str
    issuer: str = Field(min_length=1, max_length=2048)
    audience: str = Field(min_length=1, max_length=2048)
    qualified_interactive_clients: tuple[InteractiveClientQualification, ...]
    allowed_algorithms: frozenset[str] = frozenset({"RS256"})
    connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    read_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    fresh_cache_ttl_seconds: float = Field(default=300.0, gt=0, le=86_400)
    maximum_stale_seconds: float = Field(default=3_600.0, gt=0, le=604_800)
    clock_skew_seconds: float = Field(default=30.0, ge=0, le=300)
    readiness_policy: OIDCReadinessPolicy = OIDCReadinessPolicy.REQUIRED
    maximum_discovery_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)
    maximum_jwks_bytes: int = Field(default=262_144, ge=1_024, le=4_194_304)

    @field_validator("configuration_version")
    @classmethod
    def _valid_version(cls, value: str) -> str:
        if _VERSION_IDENTIFIER.fullmatch(value) is None:
            raise ValueError("authentication configuration version is invalid")
        return value

    @field_validator("issuer")
    @classmethod
    def _https_issuer(cls, value: str) -> str:
        parsed = urlsplit(value)
        try:
            parsed.port
        except ValueError:
            raise ValueError("issuer contains an invalid port") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(ord(character) <= 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError(
                "issuer must be an absolute HTTPS URL without credentials, "
                "query or fragment"
            )
        return value

    @field_validator("audience")
    @classmethod
    def _safe_audience(cls, value: str) -> str:
        if _SAFE_IDENTIFIER.fullmatch(value) is None:
            raise ValueError("audience contains unsafe characters")
        return value

    @field_validator("allowed_algorithms")
    @classmethod
    def _allowed_asymmetric_algorithms(
        cls, value: frozenset[str]
    ) -> frozenset[str]:
        if not value or not value.issubset(_SUPPORTED_ALGORITHMS):
            raise ValueError("only qualified signing algorithms are allowed")
        return value

    @model_validator(mode="after")
    def _validate_cache_and_qualifications(self) -> "OIDCAuthenticationSettings":
        if self.maximum_stale_seconds < self.fresh_cache_ttl_seconds:
            raise ValueError("maximum stale interval must include the fresh cache TTL")
        client_ids = [item.client_id for item in self.qualified_interactive_clients]
        if not client_ids:
            raise ValueError("at least one qualified interactive client is required")
        if len(set(client_ids)) != len(client_ids):
            raise ValueError("qualified interactive client IDs must be unique")
        if any(
            item.configuration_version != self.configuration_version
            or item.issuer != self.issuer
            or item.audience != self.audience
            for item in self.qualified_interactive_clients
        ):
            raise ValueError(
                "interactive client qualifications must match the exact "
                "configuration version, issuer and audience"
            )
        return self

    @property
    def qualified_interactive_client_ids(self) -> frozenset[str]:
        """Return only client IDs carrying the required operator proof."""

        return frozenset(item.client_id for item in self.qualified_interactive_clients)


__all__ = [
    "InteractiveClientQualification",
    "OIDCAuthenticationSettings",
    "OIDCReadinessPolicy",
]
