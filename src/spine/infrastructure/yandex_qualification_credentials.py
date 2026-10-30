"""Fail-closed local OAuth resolver for the isolated Yandex qualification."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import SecretStr, ValidationInfo, field_validator

from spine.auth import ExternalSecretReference
from spine.domain.common import DefinitionModel

YANDEX_QUALIFICATION_CLIENT_ID = "9b7e91e61c894ad896570742978cf5ce"
YANDEX_QUALIFICATION_OAUTH_SCOPE = "cloud_api:disk.read"
YANDEX_QUALIFICATION_ROOT = "/Spine-Qualification-678-v1"
YANDEX_QUALIFICATION_PROVIDER_REFERENCE = "provider:local-env:v1"
YANDEX_QUALIFICATION_SECRET_REFERENCE = "local-env:yandex-disk-qa:v1"

_CLIENT_ID_KEY = "SPINE_YANDEX_QUALIFICATION_CLIENT_ID"
_SCOPE_KEY = "SPINE_YANDEX_QUALIFICATION_SCOPE"
_ROOT_KEY = "SPINE_YANDEX_QUALIFICATION_ROOT"
_REFERENCE_KEY = "SPINE_YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE"
_TOKEN_KEY = "SPINE_YANDEX_QUALIFICATION_OAUTH_TOKEN"
_REQUIRED_KEYS = frozenset(
    (_CLIENT_ID_KEY, _SCOPE_KEY, _ROOT_KEY, _REFERENCE_KEY, _TOKEN_KEY)
)
_MAXIMUM_FILE_BYTES = 16 * 1024
_MAXIMUM_VALUE_CHARACTERS = 8 * 1024


def _non_nil_uuid(value: UUID, info: ValidationInfo) -> UUID:
    if value.int == 0:
        raise ValueError(f"{info.field_name} must be a non-nil UUID")
    return value


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
        return _non_nil_uuid(value, info)

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


class LocalYandexQualificationCredentialResolver:
    """Resolve one exact token from one explicit mode-0600 local file."""

    __slots__ = ("_binding", "_clock", "_env_file")

    def __init__(
        self,
        *,
        env_file: Path,
        binding: YandexQualificationCredentialBinding,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._env_file = env_file
        self._binding = binding
        self._clock = clock

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(workspace_id={self._binding.workspace_id!r}, "
            f"environment_id={self._binding.environment_id!r})"
        )

    async def resolve(
        self,
        reference: ExternalSecretReference,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> SecretStr:
        if (
            workspace_id != self._binding.workspace_id
            or environment_id != self._binding.environment_id
            or reference != self._binding.reference
            or self._clock() >= self._binding.expires_at
        ):
            raise YandexQualificationCredentialDeniedError()

        values = _read_exact_qualification_environment(self._env_file)
        if (
            values[_CLIENT_ID_KEY] != self._binding.client_id
            or values[_SCOPE_KEY] != self._binding.oauth_scope
            or values[_ROOT_KEY] != self._binding.approved_root
            or values[_REFERENCE_KEY] != self._binding.reference.secret_reference
        ):
            raise YandexQualificationCredentialDeniedError()
        return SecretStr(values[_TOKEN_KEY])


def _read_exact_qualification_environment(path: Path) -> dict[str, str]:
    if not path.is_absolute():
        raise YandexQualificationCredentialDeniedError()
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > _MAXIMUM_FILE_BYTES
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise YandexQualificationCredentialDeniedError()
        content = os.read(descriptor, _MAXIMUM_FILE_BYTES + 1)
        if len(content) > _MAXIMUM_FILE_BYTES:
            raise YandexQualificationCredentialDeniedError()
        final_metadata = os.fstat(descriptor)
        if (
            metadata.st_dev != final_metadata.st_dev
            or metadata.st_ino != final_metadata.st_ino
            or metadata.st_size != final_metadata.st_size
            or metadata.st_mtime_ns != final_metadata.st_mtime_ns
        ):
            raise YandexQualificationCredentialDeniedError()
        text = content.decode("utf-8")
    except YandexQualificationCredentialDeniedError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise YandexQualificationCredentialDeniedError() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return _parse_exact_environment(text)


def _parse_exact_environment(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise YandexQualificationCredentialDeniedError()
            name, value = line.split("=", 1)
            if (
                name not in _REQUIRED_KEYS
                or name in values
                or value != value.strip()
                or not value
                or len(value) > _MAXIMUM_VALUE_CHARACTERS
                or "\x00" in value
            ):
                raise YandexQualificationCredentialDeniedError()
            values[name] = value
    except YandexQualificationCredentialDeniedError:
        raise
    except (TypeError, ValueError):
        raise YandexQualificationCredentialDeniedError() from None
    if frozenset(values) != _REQUIRED_KEYS:
        raise YandexQualificationCredentialDeniedError()
    return values


__all__ = [
    "LocalYandexQualificationCredentialResolver",
    "YANDEX_QUALIFICATION_CLIENT_ID",
    "YANDEX_QUALIFICATION_OAUTH_SCOPE",
    "YANDEX_QUALIFICATION_PROVIDER_REFERENCE",
    "YANDEX_QUALIFICATION_ROOT",
    "YANDEX_QUALIFICATION_SECRET_REFERENCE",
    "YandexQualificationCredentialBinding",
    "YandexQualificationCredentialDeniedError",
]
