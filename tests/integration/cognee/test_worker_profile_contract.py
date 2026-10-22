from __future__ import annotations

from decimal import Decimal

from spine.memory.cognee.worker_profile import (
    COGNEE_WORKER_PROFILE_SCHEMA,
    CogneeWorkerProfile,
    ProfileApproval,
    ProfileConfigurationStatus,
    profile_configuration_status,
)


def test_worker_profile_json_contract_round_trips_without_secret_values() -> None:
    values = {
        "schema_version": COGNEE_WORKER_PROFILE_SCHEMA,
        "profile_id": "cognee-approved-production",
        "profile_digest": "0" * 64,
        "deployment_id": "spine-prod-eu-1",
        "release_id": "release-2026.10.22",
        "approval_reference": "change-548-approved-profile",
        "cognee_version": "1.5.4",
        "postgres_server_major": "17",
        "neo4j_apoc_server_version": "5.26.32",
        "pgvector_extension_version": "0.8.7",
        "neo4j_python_version": "5.28.6",
        "pgvector_python_version": "0.3.6",
        "llm_model": "openai/gpt-5.4-nano",
        "llm_revision": None,
        "embedding_model": "openai/text-embedding-3-small",
        "embedding_revision": None,
        "generation_parameters": [],
        "postgres_secret_ref": "secretref://production/spine/postgres",
        "neo4j_secret_ref": "secretref://production/spine/neo4j",
        "model_secret_ref": "secretref://production/spine/openai",
        "limits": {
            "max_requests_per_run": 20,
            "max_concurrency": 2,
            "max_input_tokens": 32_000,
            "max_output_tokens": 2_000,
            "request_timeout_seconds": 60,
            "run_deadline_seconds": 600,
            "max_retries": 2,
            "max_budget_amount": "1.25",
            "budget_currency": "USD",
        },
    }
    values["profile_digest"] = CogneeWorkerProfile.digest_for(values)
    profile = CogneeWorkerProfile.model_validate(values)
    persisted_shape = profile.model_dump(mode="json")
    reloaded = CogneeWorkerProfile.model_validate(persisted_shape)

    approval = ProfileApproval(
        profile_id=reloaded.profile_id,
        profile_digest=reloaded.profile_digest,
        approval_reference=reloaded.approval_reference,
        deployment_id=reloaded.deployment_id,
        release_id=reloaded.release_id,
        approved=True,
    )
    status = profile_configuration_status(
        profile=reloaded,
        approval=approval,
        deployment_id=reloaded.deployment_id,
        release_id=reloaded.release_id,
    )

    assert reloaded == profile
    assert status is ProfileConfigurationStatus.VALID
    assert persisted_shape["postgres_secret_ref"] == "secretref://production/spine/postgres"
    assert "password" not in str(persisted_shape).lower()
    assert isinstance(reloaded.limits.max_budget_amount, Decimal)
