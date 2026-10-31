"""Application credential contract for the isolated Yandex qualification."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import ValidationInfo, field_validator

from spine.auth import ExternalSecretReference
from spine.domain.common import DefinitionModel


YANDEX_QUALIFICATION_CLIENT_ID = "9b7e91e61c894ad896570742978cf5ce"
YANDEX_QUALIFICATION_OAUTH_SCOPE = "cloud_api:disk.read"
YANDEX_QUALIFICATION_ROOT = "/Spine-Qualification-678-v1"
YANDEX_QUALIFICATION_PROVIDER_REFERENCE = "provider:local-env:v1"
YANDEX_QUALIFICATION_SECRET_REFERENCE = "local-env:yandex-disk-qa:v1"


class YandexQualificationCredentialBinding(DefinitionModel):
    """Trusted, expiring operator binding for one qualification scope."""

    schema_version: Literal["yandex-qualification-credential-binding:v1"]
    deployment_mode: Literal["qualification"]
    workspace_id: UUID
    environment_id: UUID
    reference: ExternalSecretReference
    client_id: Literal[YANDEX_QUALIFICATION_CLIENT_ID]
    oauth_scope: Literal[YANDEX_QUALIFICATION_OAUTH_SCOPE]
    approved_root: Literal[YANDEX_QUALIFICATION_ROOT]
    expires_at: datetime

    @field_validator("workspace_id", "environment_id")
    @classmethod
    def validate_ids(cls, value: UUID, info: ValidationInfo) -> UUID:
        if value.int == 0:
            raise ValueError(f"{info.field_name} must be a non-nil UUID")
        return value

    @field_validator("reference")
    @classmethod
    def validate_reference(
        cls, value: ExternalSecretReference
    ) -> ExternalSecretReference:
        if (
            value.provider_reference != YANDEX_QUALIFICATION_PROVIDER_REFERENCE
            or value.secret_reference != YANDEX_QUALIFICATION_SECRET_REFERENCE
        ):
            raise ValueError("reference must select the approved qualification secret")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("credential binding expiry must be timezone-aware")
        return value


class YandexQualificationCredentialDeniedError(RuntimeError):
    """Sanitized denial that cannot disclose file contents or secret values."""

    def __init__(self) -> None:
        super().__init__("qualification credential denied")


__all__ = [
    "YANDEX_QUALIFICATION_CLIENT_ID",
    "YANDEX_QUALIFICATION_OAUTH_SCOPE",
    "YANDEX_QUALIFICATION_PROVIDER_REFERENCE",
    "YANDEX_QUALIFICATION_ROOT",
    "YANDEX_QUALIFICATION_SECRET_REFERENCE",
    "YandexQualificationCredentialBinding",
    "YandexQualificationCredentialDeniedError",
]
