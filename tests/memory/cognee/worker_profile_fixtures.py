from __future__ import annotations

from decimal import Decimal

from spine.memory.cognee.worker_profile import COGNEE_WORKER_PROFILE_SCHEMA


def cognee_worker_profile_values() -> dict[str, object]:
    """Return one valid, synthetic profile payload for boundary tests."""

    return {
        "schema_version": COGNEE_WORKER_PROFILE_SCHEMA,
        "profile_id": "cognee-approved-production",
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
        "llm_revision_required": False,
        "llm_revision": None,
        "embedding_model": "openai/text-embedding-3-small",
        "embedding_revision_required": False,
        "embedding_revision": None,
        "generation_parameters_required": False,
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
            "max_budget_amount": Decimal("1.25"),
            "budget_currency": "USD",
        },
    }
