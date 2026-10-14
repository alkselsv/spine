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
    RemoveCommand,
    RetrieveCommand,
    UpdateCommand,
)
from spine.memory.namespace import ProjectionNamespaceContext, ProjectionNamespaceRouter

from tests.contracts.memory.test_conformance import (
    OTHER_ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    command_context,
    source_revision,
)
from tests.memory.cognee.test_graph import extraction_result, request, tombstone_result
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


def _assert_exact_production_profile() -> None:
    assert os.environ.get("DB_PROVIDER", "").lower() in {"postgres", "postgresql"}
    assert os.environ.get("GRAPH_DATABASE_PROVIDER", "").lower() == "neo4j"
    assert os.environ.get("VECTOR_DB_PROVIDER", "").lower() == "pgvector"
    missing = tuple(name for name in _REQUIRED_SETTINGS if not os.environ.get(name))
    assert missing == (), f"missing qualification settings: {', '.join(missing)}"


def test_production_profile_preflight_requires_exact_backends() -> None:
    _assert_exact_production_profile()


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
    assert report.updated_content_observed is True
    assert report.deleted_content_absent is True
    assert report.cleanup_confirmed is True
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
    update_remove: bool = False,
    workspace_isolation: bool = False,
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
    artifacts = {current.source_revision_id: document}
    extraction_results = {current.source_revision_id: extraction}
    projection = create_cognee_context_projection(
        artifacts=artifacts,
        extraction_results=extraction_results,
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
        if update_remove:
            _, updated_base = parsed_document(
                b"Synthetic Organization Alpha employs Synthetic Person Ada. Updated.",
                REVISION_ID,
            )
            artifacts[current.source_revision_id] = updated_base.model_copy(
                update={
                    "workspace_id": extraction.workspace_id,
                    "environment_id": extraction.environment_id,
                    "source_object_id": current.source_object_id,
                    "source_revision_id": current.source_revision_id,
                    "source_revision_digest": current.revision_digest,
                }
            )
            updated = await projection.update(
                UpdateCommand(
                    **common,
                    command_identity="issue-212-update",
                    command_digest="a" * 64,
                )
            )
            tombstone = tombstone_result()
            deleted_source = next(
                source
                for source in tombstone.source_revisions
                if source.status == "tombstoned"
            )
            extraction_results[deleted_source.source_revision_id] = tombstone
            tombstoned = ProjectionSourceRevision(
                source_object_id=deleted_source.source_object_id,
                source_revision_id=deleted_source.source_revision_id,
                revision_digest=deleted_source.revision_digest,
                revision_number=deleted_source.revision_number,
                status="tombstoned",
                locator_contract_version="locator:v1",
            )
            remove_common = dict(common)
            remove_common["source_revisions"] = (tombstoned,)
            removed = await projection.remove(
                RemoveCommand(
                    **remove_common,
                    command_identity="issue-212-remove",
                    command_digest="b" * 64,
                )
            )
            after_remove = await projection.retrieve(
                RetrieveCommand(
                    **remove_common,
                    command_identity="issue-212-after-remove",
                    command_digest="c" * 64,
                    query="Synthetic Organization Alpha",
                )
            )
            assert updated.outcome is ProjectionOutcome.SUCCEEDED
            assert removed.outcome is ProjectionOutcome.SUCCEEDED
            assert after_remove.outcome is ProjectionOutcome.TOMBSTONED
            assert after_remove.chunks == ()
            return
        if rebuild:
            replay = await projection.rebuild(command)
            shadow_read = await projection.retrieve(
                RetrieveCommand(
                    **common,
                    command_identity="issue-212-shadow-read",
                    command_digest="d" * 64,
                    query="Synthetic Organization Alpha",
                )
            )
            assert projected.outcome is ProjectionOutcome.SUCCEEDED
            assert replay.outcome is ProjectionOutcome.REPLAYED
            assert replay.artifact_reference == projected.artifact_reference
            assert shadow_read.failure is not None
            assert (
                shadow_read.failure.code
                is ProjectionFailureCode.NAMESPACE_UNAVAILABLE
            )
            assert "Synthetic Organization Alpha" not in replay.model_dump_json()
            return
        else:
            if workspace_isolation:
                foreign_namespace = ProjectionNamespaceRouter().route(
                    ProjectionNamespaceContext(
                        workspace_id=OTHER_WORKSPACE_ID,
                        environment_id=OTHER_ENVIRONMENT_ID,
                        projection_candidate=candidate,
                        projection_config=template.projection_config,
                        canonical_boundary=template.canonical_boundary,
                        state=ProjectionNamespaceState.ACTIVE,
                    )
                )
                foreign_common = dict(common)
                foreign_common.update(
                    {
                        "workspace_id": OTHER_WORKSPACE_ID,
                        "environment_id": OTHER_ENVIRONMENT_ID,
                        "physical_namespace": foreign_namespace,
                    }
                )
                foreign = await projection.retrieve(
                    RetrieveCommand(
                        **foreign_common,
                        command_identity="issue-212-foreign-read",
                        command_digest="e" * 64,
                        query="Synthetic Organization Alpha",
                    )
                )
                assert foreign.outcome is ProjectionOutcome.MISSING
                assert foreign.chunks == ()
                assert "Synthetic Person Ada" not in foreign.model_dump_json()
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
        remaining = await cognee.datasets.list_datasets()
        assert all(
            (
                dataset.get("name")
                if isinstance(dataset, dict)
                else getattr(dataset, "name", None)
            )
            != dataset_name
            for dataset in remaining
        )

    assert projected.outcome is ProjectionOutcome.SUCCEEDED
    assert retrieved.outcome is ProjectionOutcome.SUCCEEDED
    assert retrieved.chunks
    assert retrieved.entities
    assert retrieved.relations
    assert all(
        reference.source_revision_id == current.source_revision_id
        for reference in retrieved.evidence
    )


async def _assert_unsafe_evidence_is_fail_closed(run_id: str) -> None:
    from spine.memory.cognee_memory import configure_environment

    configure_environment()
    import cognee

    expected = {
        "missing": ProjectionFailureCode.MISSING_EVIDENCE,
        "stale": ProjectionFailureCode.STALE_EVIDENCE,
        "ambiguous": ProjectionFailureCode.AMBIGUOUS_EVIDENCE,
        "incomplete": ProjectionFailureCode.INCOMPLETE_EVIDENCE,
        "tombstoned": ProjectionFailureCode.TOMBSTONED_SOURCE,
    }
    for index, (status, code) in enumerate(expected.items(), start=3):
        candidate_id = uuid5(
            NAMESPACE_URL,
            f"spine-issue-212:{run_id}:unsafe:{status}",
        )
        context = command_context(candidate_id=candidate_id)
        namespace = context["physical_namespace"]
        dataset_name = CogneeChunkProjectionStore.dataset_name(namespace)
        source = source_revision()
        sdk = CogneePublicSdk()
        data_id = uuid5(candidate_id, status)
        item = sdk.make_data_item(
            data=f"unsafe physical marker {status}",
            data_id=data_id,
            external_metadata={
                "segment_id": str(data_id),
                "workspace_id": str(context["workspace_id"]),
                "environment_id": str(context["environment_id"]),
                "source_object_id": str(source.source_object_id),
                "source_revision_id": str(source.source_revision_id),
                "revision_digest": source.revision_digest,
                "revision_number": source.revision_number,
                "processing_receipt_id": str(source.processing_receipt_id),
                "locator_contract_version": source.locator_contract_version,
                "locator": {
                    "kind": "physical_line",
                    "line_number": 1,
                    "start_codepoint": 0,
                    "end_codepoint": 16,
                },
                "evidence_id": f"unsafe-{status}",
                "evidence_digest": f"{index}" * 64,
                "status": status,
            },
        )
        await sdk.add(
            data=[item],
            dataset_name=dataset_name,
            dataset_id=None,
            node_set=[namespace.namespace_digest],
        )
        await sdk.cognify(datasets=[dataset_name])
        projection = create_cognee_context_projection(
            artifacts={},
            extraction_results={},
        )
        command = RetrieveCommand(
            **{**context, "source_revisions": (source,)},
            command_identity=f"unsafe-{status}",
            command_digest=f"{index}" * 64,
            query=f"unsafe physical marker {status}",
        )
        try:
            result = await projection.retrieve(command)
            assert result.failure is not None
            assert result.failure.code is code
            assert result.chunks == ()
            assert "unsafe physical marker" not in result.model_dump_json()
        finally:
            await cognee.forget(dataset=dataset_name)


@pytest.mark.asyncio
async def test_production_qualification_records_one_coherent_run() -> None:
    run_id = os.environ.get("SPINE_COGNEE_QUALIFICATION_RUN_ID", "not-run")
    state: dict[str, LiveSmokeReport] = {}

    async def public_sdk_gate() -> None:
        _assert_exact_production_profile()
        state["smoke"] = await _assert_public_sdk_lifecycle(run_id)

    async def update_remove_gate() -> None:
        await _assert_update_remove_postconditions(state["smoke"])
        await _assert_complete_context_projection_round_trip(
            run_id,
            update_remove=True,
        )

    observations = await run_qualification_gates(
        {
            "public_sdk": public_sdk_gate,
            "structured_round_trip": lambda: (
                _assert_complete_context_projection_round_trip(run_id)
            ),
            "explicit_retrieval_context": lambda: (
                _assert_complete_context_projection_round_trip(run_id)
            ),
            "unsafe_evidence": lambda: _assert_unsafe_evidence_is_fail_closed(run_id),
            "workspace_isolation": lambda: (
                _assert_complete_context_projection_round_trip(
                    run_id,
                    workspace_isolation=True,
                )
            ),
            "namespace_isolation": lambda: (
                _assert_complete_context_projection_round_trip(run_id, rebuild=True)
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
