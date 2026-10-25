"""Безопасная генерация локального файла `.env.qualification`."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import secrets
import tempfile
from typing import Mapping, Sequence
from uuid import UUID

from dotenv import dotenv_values
from pydantic import ValidationError

from spine.application.production_qa_deployment import ProductionQAADeploymentProfile
from spine.infrastructure.cognee_worker_settings import (
    CogneeWorkerBindingSettings,
    CogneeWorkerConfigurationError,
)
from spine.infrastructure.local_cognee_provider_guards import (
    LocalCogneeProviderAuthorization,
    LocalCogneeProviderAuthorizationSet,
)


_TTL_PATTERN = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>[mhd])$")
_RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_MINIMUM_TTL = timedelta(minutes=5)
_MAXIMUM_TTL = timedelta(days=7)


class QualificationEnvGenerationError(RuntimeError):
    """Ограниченная ошибка генерации, не содержащая значения конфигурации."""


def parse_ttl(value: str) -> timedelta:
    """Разобрать краткую длительность вида `30m`, `4h` или `2d`."""

    match = _TTL_PATTERN.fullmatch(value)
    if match is None:
        raise QualificationEnvGenerationError("ttl_invalid")
    amount = int(match.group("amount"))
    unit = match.group("unit")
    duration = {
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }[unit]
    if not _MINIMUM_TTL <= duration <= _MAXIMUM_TTL:
        raise QualificationEnvGenerationError("ttl_out_of_range")
    return duration


def _required(values: Mapping[str, str | None], name: str) -> str:
    value = values.get(name)
    if not value:
        raise QualificationEnvGenerationError(f"runtime_setting_missing:{name}")
    return value


def _runtime_values(path: Path) -> Mapping[str, str | None]:
    if not path.is_file():
        raise QualificationEnvGenerationError("runtime_env_missing")
    try:
        return dotenv_values(path, interpolate=False)
    except (OSError, UnicodeError, ValueError):
        raise QualificationEnvGenerationError("runtime_env_invalid") from None


def _scope_reference(scope_policy_id: str) -> str:
    prefix = "workspace-environment-"
    version = (
        scope_policy_id.removeprefix(prefix)
        if scope_policy_id.startswith(prefix)
        else scope_policy_id
    )
    return f"scope:workspace-environment:{version}"


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _provider_authorization(
    *,
    runtime: Mapping[str, str | None],
    workspace_a: UUID,
    workspace_b: UUID,
    environment_a: UUID,
    environment_b: UUID,
    budget: Decimal | None,
    currency: str,
    expires_at: datetime,
    run_id: str,
) -> str:
    if budget is None:
        return ""
    if budget <= 0:
        raise QualificationEnvGenerationError("provider_budget_invalid")
    binding_json = _required(runtime, "SPINE_COGNEE_WORKER_BINDING_JSON")
    try:
        binding = CogneeWorkerBindingSettings(
            binding_json=binding_json
        ).load_configuration()
    except CogneeWorkerConfigurationError as error:
        raise QualificationEnvGenerationError(
            f"worker_binding_invalid:{error.reason_code}"
        ) from None
    profile = binding.profile
    if currency != profile.limits.budget_currency:
        raise QualificationEnvGenerationError("provider_currency_mismatch")
    if budget > profile.limits.max_budget_amount:
        raise QualificationEnvGenerationError("provider_budget_exceeds_profile")
    secret_references = tuple(
        dict.fromkeys(
            (
                profile.postgres_secret_ref,
                profile.neo4j_secret_ref,
                profile.model_secret_ref,
            )
        )
    )
    authorizations = tuple(
        LocalCogneeProviderAuthorization(
            schema_version="cognee-local-provider-authorization:v1",
            deployment_mode="development",
            authorization_reference=f"authorization:local:{run_id}:{label}",
            workspace_id=workspace_id,
            environment_id=environment_id,
            profile_id=profile.profile_id,
            profile_digest=profile.profile_digest,
            amount=budget,
            currency=currency,
            expires_at=expires_at,
            secret_references=secret_references,
        )
        for label, workspace_id, environment_id in (
            ("a", workspace_a, environment_a),
            ("b", workspace_b, environment_b),
        )
    )
    document = LocalCogneeProviderAuthorizationSet(
        schema_version="cognee-local-provider-authorization-set:v1",
        deployment_mode="development",
        authorizations=authorizations,
    )
    return document.model_dump_json()


def _render(
    *,
    expires_at: datetime,
    target_reference: str,
    target_scope_reference: str,
    provider_authorization: str,
    run_id: str,
) -> str:
    return f"""# Сгенерированные одноразовые настройки локальной квалификации Spine.
# Файл содержит временные полномочия. Не коммитьте его и не используйте в production.
# Повторная генерация требует явного FORCE=1; срок действия задан в UTC.

# --- Полномочия квалификации ---
SPINE_QUALIFICATION_AUTHORITY_EXPIRES_AT={_utc_text(expires_at)}
SPINE_QUALIFICATION_TARGET_REFERENCE={target_reference}
SPINE_QUALIFICATION_TARGET_SCOPE_REFERENCE={target_scope_reference}
SPINE_QUALIFICATION_SECRET_PROVIDER_REFERENCE=provider:local-env:v1
SPINE_QUALIFICATION_SECRET_REFERENCE=secret:qualification-source:{run_id}
SPINE_QUALIFICATION_DATABASE_ROLE_REFERENCE=postgres-role:spine-runtime:v1

# --- Явная локальная авторизация провайдера ---
# Пустое значение означает запрет provider-вызовов, а не неограниченный доступ.
SPINE_COGNEE_LOCAL_PROVIDER_AUTHORIZATION_JSON={provider_authorization}

# --- Метаданные и отчёт квалификации ---
SPINE_COGNEE_QUALIFICATION_RUN_ID={run_id}
SPINE_COGNEE_QUALIFICATION_REPORT=.spine/qualification/{run_id}.json

# --- Инъекция сбоев; при обычном запуске оставляется пустой ---
SPINE_INGESTION_FAULT_PROFILE_JSON=
SPINE_PROJECTION_PUBLICATION_FAULT_PROFILE_JSON=
"""


def _write_atomic(path: Path, content: str, *, force: bool) -> None:
    if path.is_symlink():
        raise QualificationEnvGenerationError("output_symlink_rejected")
    if path.exists() and not force:
        raise QualificationEnvGenerationError("output_exists")
    parent = path.parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=parent, text=True
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        path.chmod(0o600)
    except OSError:
        raise QualificationEnvGenerationError("output_write_failed") from None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass


def generate_qualification_env(
    *,
    runtime_env: Path,
    output: Path,
    ttl: timedelta,
    provider_budget: Decimal | None,
    provider_currency: str,
    force: bool,
    now: datetime,
    run_id: str,
) -> None:
    """Создать проверенный файл квалификации без неявной авторизации провайдера."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise QualificationEnvGenerationError("clock_timezone_missing")
    if not _MINIMUM_TTL <= ttl <= _MAXIMUM_TTL:
        raise QualificationEnvGenerationError("ttl_out_of_range")
    if _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise QualificationEnvGenerationError("run_id_invalid")
    runtime = _runtime_values(runtime_env)
    try:
        deployment = ProductionQAADeploymentProfile.model_validate_json(
            _required(runtime, "SPINE_QA_DEPLOYMENT_SETTINGS_JSON")
        )
        workspace_a = UUID(_required(runtime, "SPINE_WORKSPACE_A_ID"))
        workspace_b = UUID(_required(runtime, "SPINE_WORKSPACE_B_ID"))
        environment_a = UUID(_required(runtime, "SPINE_ENVIRONMENT_A_ID"))
        environment_b = UUID(_required(runtime, "SPINE_ENVIRONMENT_B_ID"))
    except (ValidationError, ValueError):
        raise QualificationEnvGenerationError("runtime_binding_invalid") from None
    if workspace_a == workspace_b or environment_a == environment_b:
        raise QualificationEnvGenerationError("runtime_scopes_not_distinct")
    expires_at = now.astimezone(timezone.utc) + ttl
    provider_authorization = _provider_authorization(
        runtime=runtime,
        workspace_a=workspace_a,
        workspace_b=workspace_b,
        environment_a=environment_a,
        environment_b=environment_b,
        budget=provider_budget,
        currency=provider_currency,
        expires_at=expires_at,
        run_id=run_id,
    )
    content = _render(
        expires_at=expires_at,
        target_reference=(
            f"deployment:source:{deployment.deployment_fingerprint}"
        ),
        target_scope_reference=_scope_reference(str(deployment.scope_policy_id)),
        provider_authorization=provider_authorization,
        run_id=run_id,
    )
    _write_atomic(output, content, force=force)


def _budget(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        raise QualificationEnvGenerationError("provider_budget_invalid") from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Создать локальный `.env.qualification` с ограниченным сроком."
    )
    parser.add_argument("--runtime-env", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, default=Path(".env.qualification"))
    parser.add_argument("--ttl", default="4h", help="Срок: 30m, 4h или 2d")
    parser.add_argument("--provider-budget")
    parser.add_argument("--provider-currency", default="USD")
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    run_id = f"qualification-{now:%Y%m%d%H%M%S}-{secrets.token_hex(4)}"
    try:
        generate_qualification_env(
            runtime_env=arguments.runtime_env,
            output=arguments.output,
            ttl=parse_ttl(arguments.ttl),
            provider_budget=_budget(arguments.provider_budget),
            provider_currency=arguments.provider_currency,
            force=arguments.force,
            now=now,
            run_id=run_id,
        )
    except QualificationEnvGenerationError as error:
        parser.exit(2, f"Ошибка генерации qualification env: {error}\n")
    print(f"Создан файл {arguments.output} с правами 0600.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
