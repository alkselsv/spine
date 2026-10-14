"""Opt-in, deliberately fail-closed Cognee production-profile gate.

This target is not part of the offline suite.  It exercises the public Cognee
SDK against operator-supplied PostgreSQL, Neo4j and PGVector services, then
keeps the overall gate red until a complete physical ``ContextProjection``
factory can run the unchanged shared contract suite.
"""

from __future__ import annotations

import os
import re

import pytest

from spine.memory.cognee.live_smoke import LiveSmokeConfig, run_local_smoke
from spine.memory.cognee.qualification import SearchLane, SmokeStatus


pytestmark = [
    pytest.mark.cognee_production,
    pytest.mark.skipif(
        os.environ.get("SPINE_COGNEE_PRODUCTION_QUALIFICATION") != "1",
        reason=(
            "set SPINE_COGNEE_PRODUCTION_QUALIFICATION=1 only in the isolated "
            "PostgreSQL + Neo4j + PGVector qualification environment"
        ),
    ),
]


_REQUIRED_SETTINGS = (
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USERNAME",
    "DB_PASSWORD",
    "GRAPH_DATABASE_HOST",
    "GRAPH_DATABASE_PORT",
    "GRAPH_DATABASE_USERNAME",
    "GRAPH_DATABASE_PASSWORD",
    "VECTOR_DB_HOST",
    "VECTOR_DB_PORT",
    "VECTOR_DB_NAME",
    "VECTOR_DB_USERNAME",
    "VECTOR_DB_PASSWORD",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "EMBEDDING_PROVIDER",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS",
    "SPINE_COGNEE_QUALIFICATION_RUN_ID",
)


def test_production_profile_preflight_requires_exact_backends() -> None:
    assert os.environ.get("DB_PROVIDER", "").lower() in {"postgres", "postgresql"}
    assert os.environ.get("GRAPH_DATABASE_PROVIDER", "").lower() == "neo4j"
    assert os.environ.get("VECTOR_DB_PROVIDER", "").lower() == "pgvector"
    missing = tuple(name for name in _REQUIRED_SETTINGS if not os.environ.get(name))
    assert missing == (), f"missing qualification settings: {', '.join(missing)}"


@pytest.mark.asyncio
async def test_public_cognee_profile_exercises_structured_sdk_lifecycle() -> None:
    run_id = os.environ["SPINE_COGNEE_QUALIFICATION_RUN_ID"]
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", run_id)
    report = await run_local_smoke(
        LiveSmokeConfig(
            dataset_name=f"spine_issue_212_{run_id}",
            node_set=("issue-212-production",),
            source_text=(
                "Synthetic Organization Alpha employs Synthetic Person Ada. "
                "Qualification marker before update."
            ),
            updated_text=(
                "Synthetic Organization Alpha employs Synthetic Person Ada. "
                "Qualification marker after update."
            ),
            query="Who works for Synthetic Organization Alpha?",
        )
    )

    assert report.api.status is SmokeStatus.SUPPORTED
    assert {item.lane for item in report.searches} == {
        SearchLane.CHUNKS,
        SearchLane.GRAPH,
        SearchLane.HYBRID,
    }
    assert all(item.status is SmokeStatus.SUPPORTED for item in report.searches)
    assert {
        "add",
        "cognify",
        "update",
        "datasets.delete_data",
        "datasets.delete_data:repeat",
        "forget:memory_only",
        "forget:dataset",
    }.issubset(report.operation_names)


def test_complete_context_projection_gate_fails_closed_until_composed() -> None:
    pytest.fail(
        "NO-GO: Spine has lower-level Cognee chunk, graph, retrieval, validation "
        "and recovery adapters, but no complete physical ContextProjection factory "
        "that can run tests/contracts/memory unchanged against the real profile. "
        "Issue #211 fake evidence is not production evidence."
    )
