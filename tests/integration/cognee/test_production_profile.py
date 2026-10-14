"""Opt-in, deliberately fail-closed Cognee production-profile gate.

This target is not part of the offline suite.  It exercises the public Cognee
SDK against operator-supplied PostgreSQL, Neo4j and PGVector services and
verifies that the complete physical ``ContextProjection`` composition loads
against that exact profile.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

import pytest

from spine.memory.cognee.live_smoke import LiveSmokeConfig, run_local_smoke
from spine.memory.cognee.context_projection import (
    create_cognee_context_projection,
)
from spine.memory.cognee.indexing.store import CogneeChunkProjectionStore
from spine.memory.cognee.qualification import SearchLane, SmokeStatus
from spine.memory.cognee.production_qualification import (
    GateObservation,
    GateStatus,
    required_gate_ids,
    write_issue212_report,
)
from spine.memory.contracts import (
    ProjectionCandidate,
    ProjectionNamespaceState,
    ProjectionOutcome,
    ProjectionSourceRevision,
    ProjectCommand,
    RetrieveCommand,
)
from spine.memory.namespace import ProjectionNamespaceContext, ProjectionNamespaceRouter

from tests.memory.cognee.test_graph import extraction_result, request
from tests.memory.cognee.test_indexing import REVISION_ID, parsed_document


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


@pytest.mark.asyncio
async def test_complete_context_projection_round_trips_public_cognee_profile() -> None:
    from spine.memory.cognee_memory import configure_environment

    configure_environment()
    import cognee

    run_id = os.environ["SPINE_COGNEE_QUALIFICATION_RUN_ID"]
    extraction = extraction_result()
    current = next(
        source for source in extraction.source_revisions if source.status == "current"
    )
    template = request(extraction)
    projection_run_id = uuid5(NAMESPACE_URL, f"spine-issue-212:{run_id}:run")
    candidate = ProjectionCandidate(
        candidate_id=uuid5(NAMESPACE_URL, f"spine-issue-212:{run_id}:candidate"),
        run_id=projection_run_id,
        partition_id=uuid5(NAMESPACE_URL, f"spine-issue-212:{run_id}:partition"),
    )
    namespace = ProjectionNamespaceRouter().route(
        ProjectionNamespaceContext(
            workspace_id=extraction.workspace_id,
            environment_id=extraction.environment_id,
            projection_candidate=candidate,
            projection_config=template.projection_config,
            canonical_boundary=template.canonical_boundary,
            state=ProjectionNamespaceState.ACTIVE,
        )
    )
    _, base_document = parsed_document(
        b"Synthetic Organization Alpha employs Synthetic Person Ada.",
        REVISION_ID,
    )
    document = base_document.model_copy(
        update={
            "workspace_id": extraction.workspace_id,
            "environment_id": extraction.environment_id,
            "source_object_id": current.source_object_id,
            "source_revision_id": current.source_revision_id,
            "source_revision_digest": current.revision_digest,
        }
    )
    source = ProjectionSourceRevision(
        source_object_id=current.source_object_id,
        source_revision_id=current.source_revision_id,
        revision_digest=current.revision_digest,
        revision_number=current.revision_number,
        status="current",
        locator_contract_version="locator:v1",
    )
    common = {
        "workspace_id": extraction.workspace_id,
        "environment_id": extraction.environment_id,
        "projection_run_id": projection_run_id,
        "projection_candidate": candidate,
        "projection_config": template.projection_config,
        "canonical_boundary": template.canonical_boundary,
        "source_revisions": (source,),
        "physical_namespace": namespace,
    }
    projection = create_cognee_context_projection(
        artifacts={current.source_revision_id: document},
        extraction_results={current.source_revision_id: extraction},
    )
    dataset_name = CogneeChunkProjectionStore.dataset_name(namespace)

    try:
        projected = await projection.project(
            ProjectCommand(
                **common,
                command_identity="issue-212-project",
                command_digest="1" * 64,
            )
        )
        retrieved = await projection.retrieve(
            RetrieveCommand(
                **common,
                command_identity="issue-212-retrieve",
                command_digest="2" * 64,
                query="Who works for Synthetic Organization Alpha?",
            )
        )
    finally:
        await cognee.forget(dataset=dataset_name)

    assert projected.outcome is ProjectionOutcome.SUCCEEDED
    assert retrieved.outcome is ProjectionOutcome.SUCCEEDED
    assert retrieved.chunks
    assert retrieved.entities
    assert retrieved.relations
    assert all(
        reference.source_revision_id == current.source_revision_id
        for reference in retrieved.evidence
    )

    report_path = os.environ.get("SPINE_COGNEE_QUALIFICATION_REPORT")
    if report_path:
        passed = {
            "public_sdk",
            "structured_round_trip",
            "explicit_retrieval_context",
        }
        write_issue212_report(
            Path(report_path),
            qualification_run_id=run_id,
            observations=tuple(
                GateObservation(
                    gate_id=gate_id,
                    status=(
                        GateStatus.PASSED
                        if gate_id in passed
                        else GateStatus.NOT_RUN
                    ),
                    safe_code=(
                        None
                        if gate_id in passed
                        else "live_prerequisite_unavailable"
                    ),
                )
                for gate_id in required_gate_ids()
            ),
        )
