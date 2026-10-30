from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.auth import ExternalSecretReference
from spine.infrastructure.yandex_qualification_credentials import (
    LocalYandexQualificationCredentialResolver,
    YandexQualificationCredentialBinding,
    YandexQualificationCredentialDeniedError,
)

NOW = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000678")
ENVIRONMENT_ID = UUID("00000000-0000-0000-0000-000000000701")
REFERENCE = ExternalSecretReference(
    provider_reference="provider:local-env:v1",
    secret_reference="local-env:yandex-disk-qa:v1",
)
TOKEN = "synthetic-test-token-value"


def _binding(**overrides: object) -> YandexQualificationCredentialBinding:
    values: dict[str, object] = {
        "schema_version": "yandex-qualification-credential-binding:v1",
        "deployment_mode": "qualification",
        "workspace_id": WORKSPACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "reference": REFERENCE,
        "client_id": "9b7e91e61c894ad896570742978cf5ce",
        "oauth_scope": "cloud_api:disk.read",
        "approved_root": "/Spine-Qualification-678-v1",
        "expires_at": NOW + timedelta(hours=4),
    }
    values.update(overrides)
    return YandexQualificationCredentialBinding.model_validate(values)


async def _resolve(
    resolver: LocalYandexQualificationCredentialResolver,
    *,
    reference: ExternalSecretReference = REFERENCE,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
):
    return await resolver.resolve(
        reference,
        workspace_id=workspace_id,
        environment_id=environment_id,
    )


def _write_env(path: Path, *, token: str = TOKEN, extra: str = "") -> None:
    path.write_text(
        "\n".join(
            (
                "SPINE_YANDEX_QUALIFICATION_CLIENT_ID=9b7e91e61c894ad896570742978cf5ce",
                "SPINE_YANDEX_QUALIFICATION_SCOPE=cloud_api:disk.read",
                "SPINE_YANDEX_QUALIFICATION_ROOT=/Spine-Qualification-678-v1",
                "SPINE_YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE=local-env:yandex-disk-qa:v1",
                f"SPINE_YANDEX_QUALIFICATION_OAUTH_TOKEN={token}",
                extra,
            )
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


@pytest.mark.asyncio
async def test_exact_active_binding_resolves_secret_without_repr_disclosure(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env.qualification"
    _write_env(env_file)
    resolver = LocalYandexQualificationCredentialResolver(
        env_file=env_file,
        binding=_binding(),
        clock=lambda: NOW,
    )

    secret = await _resolve(resolver)

    assert secret.get_secret_value() == TOKEN
    assert TOKEN not in repr(secret)
    assert TOKEN not in repr(resolver)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workspace_id", "environment_id", "reference"),
    (
        (
            UUID("00000000-0000-0000-0000-000000000679"),
            ENVIRONMENT_ID,
            REFERENCE,
        ),
        (
            WORKSPACE_ID,
            UUID("00000000-0000-0000-0000-000000000702"),
            REFERENCE,
        ),
        (
            WORKSPACE_ID,
            ENVIRONMENT_ID,
            ExternalSecretReference(
                provider_reference="provider:local-env:v1",
                secret_reference="local-env:other:v1",
            ),
        ),
        (
            WORKSPACE_ID,
            ENVIRONMENT_ID,
            ExternalSecretReference(
                provider_reference="provider:other:v1",
                secret_reference="local-env:yandex-disk-qa:v1",
            ),
        ),
    ),
)
async def test_cross_scope_or_reference_request_is_denied_before_file_read(
    tmp_path: Path,
    workspace_id: UUID,
    environment_id: UUID,
    reference: ExternalSecretReference,
) -> None:
    resolver = LocalYandexQualificationCredentialResolver(
        env_file=tmp_path / "missing",
        binding=_binding(),
        clock=lambda: NOW,
    )

    with pytest.raises(YandexQualificationCredentialDeniedError) as raised:
        await _resolve(
            resolver,
            workspace_id=workspace_id,
            environment_id=environment_id,
            reference=reference,
        )

    assert str(raised.value) == "qualification credential denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "extra"),
    (
        (0o640, ""),
        (0o604, ""),
        (
            0o600,
            "SPINE_YANDEX_QUALIFICATION_OAUTH_TOKEN=second-synthetic-token",
        ),
        (0o600, "SPINE_UNAPPROVED_SETTING=value"),
    ),
)
async def test_unsafe_permissions_duplicate_or_extra_configuration_is_denied(
    tmp_path: Path,
    mode: int,
    extra: str,
) -> None:
    env_file = tmp_path / ".env.qualification"
    _write_env(env_file, extra=extra)
    env_file.chmod(mode)
    resolver = LocalYandexQualificationCredentialResolver(
        env_file=env_file,
        binding=_binding(),
        clock=lambda: NOW,
    )

    with pytest.raises(YandexQualificationCredentialDeniedError) as raised:
        await _resolve(resolver)

    assert str(raised.value) == "qualification credential denied"
    assert TOKEN not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("SPINE_YANDEX_QUALIFICATION_CLIENT_ID", "another-client"),
        ("SPINE_YANDEX_QUALIFICATION_SCOPE", "cloud_api:disk.write"),
        ("SPINE_YANDEX_QUALIFICATION_ROOT", "/"),
        ("SPINE_YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE", "local-env:other:v1"),
        ("SPINE_YANDEX_QUALIFICATION_OAUTH_TOKEN", ""),
    ),
)
async def test_mismatched_or_empty_approved_configuration_is_denied(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    env_file = tmp_path / ".env.qualification"
    _write_env(env_file)
    content = env_file.read_text(encoding="utf-8")
    lines = [
        f"{name}={value}" if line.startswith(f"{name}=") else line
        for line in content.splitlines()
    ]
    env_file.write_text("\n".join(lines), encoding="utf-8")
    env_file.chmod(0o600)
    resolver = LocalYandexQualificationCredentialResolver(
        env_file=env_file,
        binding=_binding(),
        clock=lambda: NOW,
    )

    with pytest.raises(YandexQualificationCredentialDeniedError):
        await _resolve(resolver)


@pytest.mark.asyncio
async def test_expired_binding_is_denied_before_file_read(tmp_path: Path) -> None:
    resolver = LocalYandexQualificationCredentialResolver(
        env_file=tmp_path / "missing",
        binding=_binding(expires_at=NOW),
        clock=lambda: NOW,
    )

    with pytest.raises(YandexQualificationCredentialDeniedError):
        await _resolve(resolver)


@pytest.mark.asyncio
async def test_missing_configuration_is_denied_with_sanitized_error(
    tmp_path: Path,
) -> None:
    resolver = LocalYandexQualificationCredentialResolver(
        env_file=tmp_path / ".env.qualification",
        binding=_binding(),
        clock=lambda: NOW,
    )

    with pytest.raises(YandexQualificationCredentialDeniedError) as raised:
        await _resolve(resolver)

    assert str(raised.value) == "qualification credential denied"


@pytest.mark.asyncio
async def test_malformed_configuration_is_denied_without_content_disclosure(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env.qualification"
    env_file.write_text(f"malformed-line-{TOKEN}", encoding="utf-8")
    env_file.chmod(0o600)
    resolver = LocalYandexQualificationCredentialResolver(
        env_file=env_file,
        binding=_binding(),
        clock=lambda: NOW,
    )

    with pytest.raises(YandexQualificationCredentialDeniedError) as raised:
        await _resolve(resolver)

    assert TOKEN not in str(raised.value)


def test_production_binding_cannot_be_constructed() -> None:
    with pytest.raises(ValidationError):
        _binding(deployment_mode="production")


@pytest.mark.asyncio
async def test_symlinked_configuration_is_denied(tmp_path: Path) -> None:
    real_file = tmp_path / "credential"
    _write_env(real_file)
    alias = tmp_path / ".env.qualification"
    alias.symlink_to(real_file)
    resolver = LocalYandexQualificationCredentialResolver(
        env_file=alias,
        binding=_binding(),
        clock=lambda: NOW,
    )

    with pytest.raises(YandexQualificationCredentialDeniedError):
        await _resolve(resolver)
