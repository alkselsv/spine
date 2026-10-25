from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from dotenv import dotenv_values

from spine.infrastructure.local_cognee_provider_guards import (
    LocalCogneeProviderAuthorizationSet,
)
from spine.memory.cognee.worker_profile import CogneeWorkerProfile
from spine.scripts.qualification_env import (
    QualificationEnvGenerationError,
    generate_qualification_env,
)
from tests.infrastructure.test_cognee_worker_settings import binding_payload


NOW = datetime(2026, 10, 25, 10, tzinfo=timezone.utc)
RUN_ID = "qualification-20261025-abcdef12"


def _runtime_profile() -> dict[str, object]:
    return {
        "schema_version": "spine-production-runtime-settings:v1",
        "profile_id": "spine-local-qa",
        "profile_version": "v1",
        "deployment_fingerprint": "a" * 64,
        "release_identity": "spine-local-r1",
        "commit_sha": "b" * 40,
        "database_profile_id": "spine-local-postgresql-v1",
        "migration_revision": "20261122-59",
        "authentication_profile_id": "local-identity-v1",
        "authentication_mode": "local_development",
        "scope_policy_id": "workspace-environment-v1",
        "temporal_cluster_profile_id": "spine-local-temporal-v1",
        "temporal_namespace": "default",
        "temporal_task_queue": "spine-r1-qa",
        "workflow_contract_version": "spine.qa.ask-admission:v1",
        "capture_service_profile_id": "api-qa-capture-v1",
        "capture_worker_workload_id": "spine-qa-worker",
        "capture_observer_workload_id": "spine-qa-observer",
        "capture_contract_version": "r1-qa-observer-capture:v1",
        "projection_profile_id": "spine-cognee-local-v1",
        "model_profile_id": "spine-openai-r1-v1",
    }


def _write_runtime_env(path: Path, *, include_binding: bool = False) -> None:
    values = {
        "SPINE_QA_DEPLOYMENT_SETTINGS_JSON": json.dumps(
            _runtime_profile(), separators=(",", ":")
        ),
        "SPINE_WORKSPACE_A_ID": "50800000-0000-4000-8000-00000000000a",
        "SPINE_WORKSPACE_B_ID": "50800000-0000-4000-8000-00000000000b",
        "SPINE_ENVIRONMENT_A_ID": "50800000-0000-4000-8000-00000000000e",
        "SPINE_ENVIRONMENT_B_ID": "50800000-0000-4000-8000-00000000000f",
    }
    if include_binding:
        values["SPINE_COGNEE_WORKER_BINDING_JSON"] = json.dumps(
            binding_payload(), separators=(",", ":")
        )
    path.write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()),
        encoding="utf-8",
    )


def test_generator_creates_bounded_file_without_implicit_provider_approval(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / ".env"
    output = tmp_path / ".env.qualification"
    _write_runtime_env(runtime)

    generate_qualification_env(
        runtime_env=runtime,
        output=output,
        ttl=timedelta(hours=4),
        provider_budget=None,
        provider_currency="USD",
        force=False,
        now=NOW,
        run_id=RUN_ID,
    )

    values = dotenv_values(output, interpolate=False)
    assert values["SPINE_QUALIFICATION_AUTHORITY_EXPIRES_AT"] == (
        "2026-10-25T14:00:00Z"
    )
    assert values["SPINE_QUALIFICATION_TARGET_REFERENCE"] == (
        f"deployment:source:{'a' * 64}"
    )
    assert values["SPINE_QUALIFICATION_TARGET_SCOPE_REFERENCE"] == (
        "scope:workspace-environment:v1"
    )
    assert values["SPINE_COGNEE_LOCAL_PROVIDER_AUTHORIZATION_JSON"] == ""
    assert values["SPINE_COGNEE_QUALIFICATION_RUN_ID"] == RUN_ID
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_generator_requires_force_to_replace_existing_file(tmp_path: Path) -> None:
    runtime = tmp_path / ".env"
    output = tmp_path / ".env.qualification"
    _write_runtime_env(runtime)
    output.write_text("existing\n", encoding="utf-8")

    with pytest.raises(QualificationEnvGenerationError, match="output_exists"):
        generate_qualification_env(
            runtime_env=runtime,
            output=output,
            ttl=timedelta(hours=4),
            provider_budget=None,
            provider_currency="USD",
            force=False,
            now=NOW,
            run_id=RUN_ID,
        )

    assert output.read_text(encoding="utf-8") == "existing\n"


def test_generator_builds_explicit_scope_bound_provider_authorizations(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / ".env"
    output = tmp_path / ".env.qualification"
    _write_runtime_env(runtime, include_binding=True)

    generate_qualification_env(
        runtime_env=runtime,
        output=output,
        ttl=timedelta(hours=4),
        provider_budget=Decimal("1.00"),
        provider_currency="USD",
        force=False,
        now=NOW,
        run_id=RUN_ID,
    )

    values = dotenv_values(output, interpolate=False)
    authorization = LocalCogneeProviderAuthorizationSet.model_validate_json(
        values["SPINE_COGNEE_LOCAL_PROVIDER_AUTHORIZATION_JSON"]
    )
    selected_profile = CogneeWorkerProfile.model_validate(
        binding_payload()["profile"]
    )
    assert len(authorization.authorizations) == 2
    assert {item.amount for item in authorization.authorizations} == {
        Decimal("1.00")
    }
    assert {item.profile_digest for item in authorization.authorizations} == {
        selected_profile.profile_digest
    }
    assert {str(item.workspace_id) for item in authorization.authorizations} == {
        "50800000-0000-4000-8000-00000000000a",
        "50800000-0000-4000-8000-00000000000b",
    }


def test_generator_rejects_budget_above_approved_profile_limit(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / ".env"
    _write_runtime_env(runtime, include_binding=True)

    with pytest.raises(
        QualificationEnvGenerationError, match="provider_budget_exceeds_profile"
    ):
        generate_qualification_env(
            runtime_env=runtime,
            output=tmp_path / ".env.qualification",
            ttl=timedelta(hours=4),
            provider_budget=Decimal("1.26"),
            provider_currency="USD",
            force=False,
            now=NOW,
            run_id=RUN_ID,
        )
