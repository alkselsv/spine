"""Opt-in, deliberately fail-closed Cognee production-profile gate.

This target is not part of the offline suite.  It exercises the public Cognee
SDK against operator-supplied PostgreSQL, Neo4j and PGVector services and
verifies that the complete physical ``ContextProjection`` composition loads
against that exact profile.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

import pytest

from spine.memory.cognee.context_projection import (
    create_cognee_context_projection,
)
from spine.memory.cognee.indexing.store import (
    CogneeChunkProjectionStore,
    CogneePublicSdk,
)
from spine.memory.cognee.live_smoke import (
    LiveSmokeConfig,
    LiveSmokeReport,
    run_local_smoke,
)
from spine.memory.cognee.qualification import SearchLane, SmokeStatus
from spine.memory.cognee.production_qualification import (
    GateStatus,
    run_qualification_gates,
    write_issue212_report,
)
from spine.memory.contracts import (
    ProjectionCandidate,
    ProjectionFailureCode,
    ProjectionLifecycle,
    ProjectionNamespaceState,
    ProjectionOutcome,
    ProjectionSourceRevision,
    ProjectCommand,
    RebuildCommand,
    RetrieveCommand,
)
from spine.memory.namespace import ProjectionNamespaceContext, ProjectionNamespaceRouter

from tests.contracts.memory.test_conformance import (
    OTHER_ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    SHADOW_CANDIDATE_ID,
    command_context,
    retrieve_command,
    source_revision,
)
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


class _FailOnceCogneeSdk:
    """Public-SDK proxy for one bounded physical failure injection."""

    version = "1.5.4"

    def __init__(self) -> None:
        self._delegate = CogneePublicSdk()
        self._failed = False

    def make_data_item(self, **kwargs: object) -> object:
        return self._delegate.make_data_item(**kwargs)

    async def add(self, **kwargs: object) -> object:
        return await self._delegate.add(**kwargs)

    async def cognify(self, **kwargs: object) -> object:
        if not self._failed:
            self._failed = True
            raise RuntimeError("synthetic provider failure containing protected text")
        return await self._delegate.cognify(**kwargs)

    async def update(self, **kwargs: object) -> object:
        return await self._delegate.update(**kwargs)

    async def delete_data(self, **kwargs: object) -> object:
        return await self._delegate.delete_data(**kwargs)

    async def list_datasets(self) -> object:
        return await self._delegate.list_datasets()


def test_production_profile_preflight_requires_exact_backends() -> None:
    assert os.environ.get("DB_PROVIDER", "").lower() in {"postgres", "postgresql"}
    assert os.environ.get("GRAPH_DATABASE_PROVIDER", "").lower() == "neo4j"
    assert os.environ.get("VECTOR_DB_PROVIDER", "").lower() == "pgvector"
    missing = tuple(name for name in _REQUIRED_SETTINGS if not os.environ.get(name))
    assert missing == (), f"missing qualification settings: {', '.join(missing)}"


async def _assert_public_sdk_lifecycle(run_id: str) -> LiveSmokeReport:
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
    return report


async def _assert_update_remove_postconditions(report: LiveSmokeReport) -> None:
    assert report.updated_content_observed is True
    assert report.deleted_content_absent is True
    assert "datasets.delete_data:repeat" in report.operation_names


async def _assert_complete_context_projection_round_trip(
    run_id: str,
    *,
    inject_cognify_failure: bool = False,
    rebuild: bool = False,
) -> None:
    from spine.memory.cognee_memory import configure_environment

    configure_environment()
    import cognee

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
            state=(
                ProjectionNamespaceState.SHADOW
                if rebuild
                else ProjectionNamespaceState.ACTIVE
            ),
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
        sdk=_FailOnceCogneeSdk() if inject_cognify_failure else None,
    )
    dataset_name = CogneeChunkProjectionStore.dataset_name(namespace)
    command_type = RebuildCommand if rebuild else ProjectCommand
    command = command_type(
        **common,
        command_identity=("issue-212-rebuild" if rebuild else "issue-212-project"),
        command_digest="1" * 64,
    )
    execute = projection.rebuild if rebuild else projection.project

    try:
        if inject_cognify_failure:
            first, projected = await asyncio.gather(
                execute(command),
                execute(command),
            )
            assert first.outcome is ProjectionOutcome.FAILED
            assert projected.outcome is ProjectionOutcome.SUCCEEDED
            assert projected.lifecycle is ProjectionLifecycle.RECOVERED
            assert "protected text" not in first.model_dump_json()
        else:
            projected = await execute(command)
        if rebuild:
            replay = await projection.rebuild(command)
            assert projected.outcome is ProjectionOutcome.SUCCEEDED
            assert replay.outcome is ProjectionOutcome.REPLAYED
            assert replay.artifact_reference == projected.artifact_reference
            assert "Synthetic Organization Alpha" not in replay.model_dump_json()
            return
        else:
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


async def _assert_unsafe_evidence_is_fail_closed() -> None:
    projection = create_cognee_context_projection(
        artifacts={},
        extraction_results={},
    )
    expected = {
        "missing": ProjectionFailureCode.MISSING_EVIDENCE,
        "stale": ProjectionFailureCode.STALE_EVIDENCE,
        "ambiguous": ProjectionFailureCode.AMBIGUOUS_EVIDENCE,
        "incomplete": ProjectionFailureCode.INCOMPLETE_EVIDENCE,
        "tombstoned": ProjectionFailureCode.TOMBSTONED_SOURCE,
    }
    for index, (status, code) in enumerate(expected.items(), start=3):
        command = RetrieveCommand(
            **command_context(source_revisions=(source_revision(status=status),)),
            command_identity=f"unsafe-{status}",
            command_digest=f"{index}" * 64,
            query="protected synthetic content",
        )
        result = await projection.retrieve(command)
        assert result.failure is not None
        assert result.failure.code is code
        assert result.chunks == ()
        assert "protected synthetic content" not in result.model_dump_json()


async def _assert_workspace_isolation_is_fail_closed(run_id: str) -> None:
    await _assert_complete_context_projection_round_trip(run_id)
    projection = create_cognee_context_projection(
        artifacts={},
        extraction_results={},
    )
    namespace = command_context()["physical_namespace"]
    result = await projection.retrieve(
        retrieve_command(
            identity="foreign-workspace",
            digest="8" * 64,
            workspace_id=OTHER_WORKSPACE_ID,
            environment_id=OTHER_ENVIRONMENT_ID,
            namespace=namespace,
        )
    )
    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.NAMESPACE_UNAVAILABLE
    assert result.chunks == ()


async def _assert_shadow_namespace_is_not_ordinary_retrieval(run_id: str) -> None:
    await _assert_complete_context_projection_round_trip(run_id, rebuild=True)
    projection = create_cognee_context_projection(
        artifacts={},
        extraction_results={},
    )
    shadow_namespace = command_context(
        candidate_id=SHADOW_CANDIDATE_ID,
        state=ProjectionNamespaceState.SHADOW,
    )["physical_namespace"]
    result = await projection.retrieve(
        retrieve_command(
            identity="shadow-retrieval",
            digest="9" * 64,
            namespace=shadow_namespace,
            candidate_id=SHADOW_CANDIDATE_ID,
            state=ProjectionNamespaceState.SHADOW,
        )
    )
    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.NAMESPACE_UNAVAILABLE
    assert result.chunks == ()

@pytest.mark.asyncio
async def test_production_qualification_records_one_coherent_run() -> None:
    run_id = os.environ["SPINE_COGNEE_QUALIFICATION_RUN_ID"]
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", run_id)
    state: dict[str, LiveSmokeReport] = {}

    async def public_sdk_gate() -> None:
        state["smoke"] = await _assert_public_sdk_lifecycle(run_id)

    async def update_remove_gate() -> None:
        await _assert_update_remove_postconditions(state["smoke"])

    observations = await run_qualification_gates(
        {
            "public_sdk": public_sdk_gate,
            "structured_round_trip": lambda: (
                _assert_complete_context_projection_round_trip(run_id)
            ),
            "explicit_retrieval_context": lambda: (
                _assert_complete_context_projection_round_trip(run_id)
            ),
            "unsafe_evidence": _assert_unsafe_evidence_is_fail_closed,
            "workspace_isolation": lambda: (
                _assert_workspace_isolation_is_fail_closed(run_id)
            ),
            "namespace_isolation": lambda: (
                _assert_shadow_namespace_is_not_ordinary_retrieval(run_id)
            ),
            "update_remove_tombstone": update_remove_gate,
            "rebuild_recovery_validation_diagnostics": lambda: (
                _assert_complete_context_projection_round_trip(
                    run_id,
                    inject_cognify_failure=True,
                    rebuild=True,
                )
            ),
            "concurrency_failure_cleanup": lambda: (
                _assert_complete_context_projection_round_trip(
                    run_id,
                    inject_cognify_failure=True,
                )
            ),
            "canonical_lifecycle_separation": lambda: (
                _assert_complete_context_projection_round_trip(run_id)
            ),
        }
    )
    report_path = os.environ.get("SPINE_COGNEE_QUALIFICATION_REPORT")
    if report_path:
        write_issue212_report(
            Path(report_path),
            qualification_run_id=run_id,
            observations=observations,
        )
    failed = tuple(
        item.gate_id for item in observations if item.status is GateStatus.FAILED
    )
    not_run = tuple(
        item.gate_id for item in observations if item.status is GateStatus.NOT_RUN
    )
    assert failed == (), f"qualification gates failed: {', '.join(failed)}"
    assert not_run == (), f"qualification gates not run: {', '.join(not_run)}"
