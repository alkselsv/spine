from __future__ import annotations

import pytest
from pydantic import ValidationError

from spine.application.production_qa_deployment import (
    ProductionQAADeploymentProfile,
    ProductionQAAProfileMismatch,
)
from spine.infrastructure.db.readiness import load_migration_inventory
from spine.infrastructure.qa_deployment_settings import ProductionQAADeploymentSettings


def _profile(**overrides: object) -> ProductionQAADeploymentProfile:
    values: dict[str, object] = {
        "profile_id": "spine-r1-local",
        "profile_version": "2026.10.22",
        "deployment_fingerprint": "a" * 64,
        "release_identity": "r1-local-2026.10",
        "commit_sha": "b" * 40,
        "database_profile_id": "spine-source-postgresql-17",
        "migration_revision": load_migration_inventory().supported_head,
        "authentication_profile_id": "local-development-v1",
        "authentication_mode": "local_development",
        "scope_policy_id": "workspace-environment-v1",
        "temporal_cluster_profile_id": "local-temporal-v1",
        "temporal_namespace": "default",
        "temporal_task_queue": "spine-r1-qa",
        "workflow_contract_version": "spine.qa.ask-admission:v1",
        "capture_service_profile_id": "api-qa-capture-v1",
        "capture_worker_workload_id": "spine-qa-worker",
        "capture_observer_workload_id": "spine-qa-observer",
        "capture_contract_version": "r1-qa-observer-capture:v1",
        "projection_profile_id": "r1-cognee-local-v1",
        "model_profile_id": "r1-provider-local-v1",
    }
    values.update(overrides)
    return ProductionQAADeploymentProfile.model_validate(values)


def test_process_profiles_accept_matching_api_and_worker_bindings() -> None:
    api_profile = _profile()
    worker_profile = _profile()

    api_profile.assert_compatible_with(worker_profile)


def test_process_profiles_reject_task_queue_mismatch_with_safe_reason() -> None:
    api_profile = _profile()
    worker_profile = _profile(temporal_task_queue="another-queue")

    with pytest.raises(ProductionQAAProfileMismatch) as error:
        api_profile.assert_compatible_with(worker_profile)

    assert error.value.reason_code == "TEMPORAL_BINDING_MISMATCH"
    assert "another-queue" not in str(error.value)


def test_production_qa_profile_rejects_unversioned_or_extra_fields() -> None:
    values = _profile().model_dump()

    with pytest.raises(ValidationError):
        ProductionQAADeploymentProfile.model_validate(
            {**values, "unreviewed_field": "ignored"}
        )


def test_deployment_settings_load_strict_versioned_profile_document() -> None:
    profile = _profile()

    loaded = ProductionQAADeploymentSettings(
        profile_json=profile.model_dump_json()
    ).load_profile()

    assert loaded == profile


def test_deployment_settings_hide_invalid_profile_values() -> None:
    secret_like_value = "unsupported-profile-value"
    settings = ProductionQAADeploymentSettings(
        profile_json=f'{{"profile_id":"{secret_like_value}"}}'
    )

    with pytest.raises(ProductionQAAProfileMismatch) as error:
        settings.load_profile()

    assert error.value.reason_code == "CONFIG_INVALID"
    assert secret_like_value not in str(error.value)
