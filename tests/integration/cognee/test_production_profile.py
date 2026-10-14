"""Opt-in, deliberately fail-closed Cognee production-profile gate.

This target is not part of the offline suite.  It exercises the public Cognee
SDK against operator-supplied PostgreSQL, Neo4j and PGVector services and
verifies that the complete physical ``ContextProjection`` composition loads
against that exact profile.
"""

from __future__ import annotations

import asyncio
from importlib.util import find_spec
import logging
import os
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

import pytest

from spine.memory.cognee.context_projection import (
    CogneeContextProjection,
    CogneeGraphEffectStore,
    create_cognee_context_projection,
)
from spine.memory.cognee.graph import CogneeGraphProjectionWriter
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
from spine.memory.cognee.retrieval.adapter import CogneeStructuredRetrieval
from spine.memory.validation import (
    ArtifactKind,
    PhysicalArtifact,
    PhysicalArtifactLocator,
    PhysicalProjectionExpectation,
    PhysicalProjectionSource,
    PhysicalValidationCode,
    validate_physical_projection,
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
from tests.memory.cognee.test_graph import extraction_result, request
from tests.memory.cognee.test_indexing import NEXT_REVISION_ID, REVISION_ID, parsed_document


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

logger = logging.getLogger(__name__)


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

_REQUIRED_BACKEND_MODULES = ("asyncpg", "neo4j", "pgvector")


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
    missing_modules = tuple(
        name for name in _REQUIRED_BACKEND_MODULES if find_spec(name) is None
    )
    assert missing_modules == (), (
        "missing production backend modules: " + ", ".join(missing_modules)
    )
    assert os.environ.get("ENABLE_BACKEND_ACCESS_CONTROL", "true").lower() != "false"
    if os.environ.get("GRAPH_DATASET_DATABASE_HANDLER") == "neo4j_community":
        assert os.environ.get("NEO4J_COMMUNITY_IMAGE")
        assert os.environ.get("NEO4J_ENCRYPTION_KEY") not in {None, "", "test_key"}


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
    for observation in report.searches:
        if observation.status is not SmokeStatus.SUPPORTED:
            logger.error(
                "public search shape rejected: lane=%s missing_shapes=%s "
                "missing_evidence=%s",
                observation.lane.value,
                ",".join(observation.missing_shapes) or "none",
                ",".join(observation.missing_evidence_kinds) or "none",
            )
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


def _expected_physical_artifacts(
    *,
    document,
    extraction,
    source: ProjectionSourceRevision,
    namespace,
    projection_config,
) -> tuple[PhysicalArtifact, ...]:
    """Derive the canonical expectation without consulting adapter manifests."""

    common = {
        "workspace_id": document.workspace_id,
        "environment_id": document.environment_id,
        "namespace_digest": namespace.namespace_digest,
        "source_object_id": source.source_object_id,
        "source_revision_id": source.source_revision_id,
        "revision_digest": source.revision_digest,
        "revision_number": source.revision_number,
        "projection_config_id": projection_config.config_id,
        "projection_config_version": projection_config.version,
        "projection_config_digest": projection_config.config_digest,
    }
    expected = [
        PhysicalArtifact(
            artifact_kind=ArtifactKind.CHUNK,
            artifact_id=str(segment.segment_id),
            data_id=segment.segment_id,
            locators=(
                PhysicalArtifactLocator.from_payload(
                    kind=segment.locator.kind,
                    payload=segment.locator,
                ),
            ),
            **common,
        )
        for segment in document.segments
    ]
    graph_namespace = uuid5(
        NAMESPACE_URL,
        f"spine:{namespace.namespace_digest}",
    )
    for kind, artifacts in (
        (ArtifactKind.ENTITY, extraction.entities),
        (ArtifactKind.RELATION, extraction.relations),
    ):
        for artifact in artifacts:
            artifact_id = (
                artifact.entity_id
                if kind is ArtifactKind.ENTITY
                else artifact.relation_id
            )
            expected.append(
                PhysicalArtifact(
                    artifact_kind=kind,
                    artifact_id=artifact_id,
                    data_id=uuid5(
                        graph_namespace,
                        f"{source.source_object_id}:{kind.value}:{artifact_id}",
                    ),
                    locators=tuple(
                        PhysicalArtifactLocator.from_payload(
                            kind=reference.locator.kind,
                            payload=reference.locator,
                        )
                        for reference in artifact.evidence
                    ),
                    **common,
                )
            )
    return tuple(expected)


async def _assert_complete_context_projection_round_trip(
    run_id: str,
    *,
    inject_cognify_failure: bool = False,
    rebuild: bool = False,
    update_remove: bool = False,
    workspace_isolation: bool = False,
    validate_physical: bool = False,
    explicit_context: bool = False,
    active_shadow_isolation: bool = False,
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
        snapshot_id=uuid5(NAMESPACE_URL, f"spine-issue-212:{run_id}:snapshot"),
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
        (
            b"Synthetic Organization Alpha employs Synthetic Person Ada. "
            b"Active namespace marker."
            if active_shadow_isolation
            else b"Synthetic Organization Alpha employs Synthetic Person Ada."
        ),
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
        processing_receipt_id=uuid5(
            NAMESPACE_URL,
            f"spine-issue-212:{run_id}:processing-receipt",
        ),
        locator_contract_version=document.locator_contract_version,
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
    sdk = _FailOnceCogneeSdk() if inject_cognify_failure else CogneePublicSdk()
    chunks = CogneeChunkProjectionStore(sdk=sdk, artifacts=artifacts)
    graph_writer = CogneeGraphProjectionWriter(sdk=sdk)
    graph = CogneeGraphEffectStore(
        writer=graph_writer,
        extraction_results=extraction_results,
    )
    projection = CogneeContextProjection(
        effect_store=chunks,
        graph_effect_store=graph,
        retrieval=CogneeStructuredRetrieval(),
    )
    dataset_name = CogneeChunkProjectionStore.dataset_name(namespace)
    cleanup_dataset_names = [dataset_name]
    command_type = RebuildCommand if rebuild else ProjectCommand
    command = command_type(
        **common,
        command_identity=("issue-212-rebuild" if rebuild else "issue-212-project"),
        command_digest="1" * 64,
    )
    execute = projection.rebuild if rebuild else projection.project

    try:
        active_common: dict[str, object] | None = None
        if active_shadow_isolation:
            active_candidate = candidate.model_copy(
                update={
                    "candidate_id": uuid5(
                        NAMESPACE_URL,
                        f"spine-issue-212:{run_id}:active-candidate",
                    ),
                    "snapshot_id": uuid5(
                        NAMESPACE_URL,
                        f"spine-issue-212:{run_id}:active-snapshot",
                    ),
                }
            )
            active_namespace = ProjectionNamespaceRouter().route(
                ProjectionNamespaceContext(
                    workspace_id=extraction.workspace_id,
                    environment_id=extraction.environment_id,
                    projection_candidate=active_candidate,
                    projection_config=template.projection_config,
                    canonical_boundary=template.canonical_boundary,
                    state=ProjectionNamespaceState.ACTIVE,
                )
            )
            active_common = {
                **common,
                "projection_candidate": active_candidate,
                "physical_namespace": active_namespace,
            }
            cleanup_dataset_names.append(
                CogneeChunkProjectionStore.dataset_name(active_namespace)
            )
            active_projected = await projection.project(
                ProjectCommand(
                    **active_common,
                    command_identity="issue-212-active-project",
                    command_digest="7" * 64,
                )
            )
            assert active_projected.outcome is ProjectionOutcome.SUCCEEDED
            _, shadow_base = parsed_document(
                b"Synthetic Organization Alpha employs Synthetic Person Ada. "
                b"Shadow namespace marker.",
                REVISION_ID,
            )
            artifacts[current.source_revision_id] = shadow_base.model_copy(
                update={
                    "workspace_id": extraction.workspace_id,
                    "environment_id": extraction.environment_id,
                    "source_object_id": current.source_object_id,
                    "source_revision_id": current.source_revision_id,
                    "source_revision_digest": current.revision_digest,
                }
            )
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
                NEXT_REVISION_ID,
            )
            updated_digest = updated_base.source_revision_digest
            artifacts[NEXT_REVISION_ID] = updated_base.model_copy(
                update={
                    "workspace_id": extraction.workspace_id,
                    "environment_id": extraction.environment_id,
                    "source_object_id": current.source_object_id,
                    "source_revision_id": NEXT_REVISION_ID,
                    "source_revision_digest": updated_digest,
                }
            )
            updated_extraction = extraction.model_copy(
                update={
                    "source_revisions": (
                        current.model_copy(
                            update={
                                "source_revision_id": NEXT_REVISION_ID,
                                "revision_digest": updated_digest,
                                "revision_number": current.revision_number + 1,
                            }
                        ),
                    ),
                    "entities": tuple(
                        entity.model_copy(
                            update={
                                "source_revision_id": NEXT_REVISION_ID,
                                "evidence": tuple(
                                    evidence.model_copy(
                                        update={
                                            "source_revision_id": NEXT_REVISION_ID,
                                            "revision_digest": updated_digest,
                                            "revision_number": current.revision_number + 1,
                                        }
                                    )
                                    for evidence in entity.evidence
                                ),
                            }
                        )
                        for entity in extraction.entities
                    ),
                    "relations": tuple(
                        relation.model_copy(
                            update={
                                "source_revision_id": NEXT_REVISION_ID,
                                "evidence": tuple(
                                    evidence.model_copy(
                                        update={
                                            "source_revision_id": NEXT_REVISION_ID,
                                            "revision_digest": updated_digest,
                                            "revision_number": current.revision_number + 1,
                                        }
                                    )
                                    for evidence in relation.evidence
                                ),
                            }
                        )
                        for relation in extraction.relations
                    ),
                    "result_digest": "",
                }
            )
            extraction_results[NEXT_REVISION_ID] = updated_extraction
            updated_source = source.model_copy(
                update={
                    "source_revision_id": NEXT_REVISION_ID,
                    "revision_digest": updated_digest,
                    "revision_number": source.revision_number + 1,
                }
            )
            updated_common = dict(common)
            updated_common["source_revisions"] = (updated_source,)
            updated = await projection.update(
                UpdateCommand(
                    **updated_common,
                    command_identity="issue-212-update",
                    command_digest="a" * 64,
                )
            )
            after_update = await projection.retrieve(
                RetrieveCommand(
                    **updated_common,
                    command_identity="issue-212-after-update",
                    command_digest="9" * 64,
                    query="Qualification marker Updated",
                )
            )
            tombstone_id = uuid5(
                NAMESPACE_URL,
                f"spine-issue-212:{run_id}:tombstone",
            )
            tombstone_digest = "f" * 64
            tombstone_revision_number = updated_source.revision_number + 1
            extraction_results[tombstone_id] = updated_extraction.model_copy(
                update={
                    "source_revisions": (
                        current.model_copy(
                            update={
                                "source_revision_id": tombstone_id,
                                "revision_digest": tombstone_digest,
                                "revision_number": tombstone_revision_number,
                                "status": "tombstoned",
                            }
                        ),
                    ),
                    "entities": (),
                    "relations": (),
                    "result_digest": "",
                }
            )
            tombstoned = updated_source.model_copy(
                update={
                    "source_revision_id": tombstone_id,
                    "revision_digest": tombstone_digest,
                    "revision_number": tombstone_revision_number,
                    "status": "tombstoned",
                }
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
                    **updated_common,
                    command_identity="issue-212-after-remove",
                    command_digest="c" * 64,
                    query="Qualification marker Updated",
                )
            )
            assert updated.outcome is ProjectionOutcome.SUCCEEDED
            assert after_update.outcome is ProjectionOutcome.SUCCEEDED
            assert any("Updated" in chunk.text for chunk in after_update.chunks)
            assert removed.outcome is ProjectionOutcome.SUCCEEDED
            assert after_remove.chunks == ()
            assert after_remove.entities == ()
            assert after_remove.relations == ()
            assert "Updated" not in after_remove.model_dump_json()
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
            if active_shadow_isolation:
                assert active_common is not None
                active_read = await projection.retrieve(
                    RetrieveCommand(
                        **active_common,
                        command_identity="issue-212-active-read",
                        command_digest="6" * 64,
                        query="Active namespace marker",
                    )
                )
                assert active_read.outcome is ProjectionOutcome.SUCCEEDED
                assert any(
                    "Active namespace marker" in chunk.text
                    for chunk in active_read.chunks
                )
                assert "Shadow namespace marker" not in active_read.model_dump_json()
            assert projected.outcome is ProjectionOutcome.SUCCEEDED
            assert replay.outcome is ProjectionOutcome.REPLAYED
            assert replay.artifact_reference == projected.artifact_reference
            assert shadow_read.failure is not None
            assert (
                shadow_read.failure.code
                is ProjectionFailureCode.NAMESPACE_UNAVAILABLE
            )
            assert "Synthetic Organization Alpha" not in replay.model_dump_json()
            if validate_physical:
                physical_artifacts = (
                    await chunks.physical_artifacts_for(
                        workspace_id=extraction.workspace_id,
                        environment_id=extraction.environment_id,
                        namespace_digest=namespace.namespace_digest,
                    )
                ) + (
                    await graph_writer.physical_artifacts_for(
                        workspace_id=extraction.workspace_id,
                        environment_id=extraction.environment_id,
                        namespace_digest=namespace.namespace_digest,
                    )
                )
                expected_artifacts = _expected_physical_artifacts(
                    document=document,
                    extraction=extraction,
                    source=source,
                    namespace=namespace,
                    projection_config=template.projection_config,
                )
                expectation = PhysicalProjectionExpectation(
                    workspace_id=extraction.workspace_id,
                    environment_id=extraction.environment_id,
                    namespace_digest=namespace.namespace_digest,
                    projection_config_id=template.projection_config.config_id,
                    projection_config_version=template.projection_config.version,
                    projection_config_digest=template.projection_config.config_digest,
                    sources=(
                        PhysicalProjectionSource(
                            source_object_id=source.source_object_id,
                            source_revision_id=source.source_revision_id,
                            revision_digest=source.revision_digest,
                            revision_number=source.revision_number,
                        ),
                    ),
                    expected_artifacts=expected_artifacts,
                )
                validation = validate_physical_projection(
                    expectation,
                    physical_artifacts,
                )
                assert validation.passed is True
                assert {item.artifact_kind.value for item in validation.counts} == {
                    "chunk",
                    "entity",
                    "relation",
                }
                corrupted = physical_artifacts[0].model_copy(
                    update={"namespace_digest": "0" * 64}
                )
                diagnostic = validate_physical_projection(
                    expectation,
                    (corrupted, *physical_artifacts[1:]),
                )
                assert diagnostic.passed is False
                assert PhysicalValidationCode.NAMESPACE_MISMATCH in {
                    item.code for item in diagnostic.diagnostics
                }
                assert "Synthetic Organization Alpha" not in diagnostic.model_dump_json()
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
                if foreign.outcome is not ProjectionOutcome.MISSING:
                    logger.error(
                        "foreign workspace retrieval mismatch: outcome=%s "
                        "failure_code=%s",
                        foreign.outcome.value,
                        (
                            foreign.failure.code.value
                            if foreign.failure is not None
                            else "none"
                        ),
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
            if explicit_context:
                wrong_snapshot = candidate.model_copy(
                    update={
                        "snapshot_id": uuid5(
                            NAMESPACE_URL,
                            f"spine-issue-212:{run_id}:wrong-snapshot",
                        )
                    }
                )
                wrong_boundary = template.canonical_boundary.model_copy(
                    update={
                        "boundary_id": uuid5(
                            NAMESPACE_URL,
                            f"spine-issue-212:{run_id}:wrong-boundary",
                        ),
                        "boundary_digest": "8" * 64,
                    }
                )
                wrong_namespace = ProjectionNamespaceRouter().route(
                    ProjectionNamespaceContext(
                        workspace_id=extraction.workspace_id,
                        environment_id=extraction.environment_id,
                        projection_candidate=wrong_snapshot,
                        projection_config=template.projection_config,
                        canonical_boundary=template.canonical_boundary,
                        state=ProjectionNamespaceState.ACTIVE,
                    )
                )
                mismatched_commands = (
                    RetrieveCommand(
                        **{
                            **common,
                            "projection_candidate": wrong_snapshot,
                        },
                        command_identity="issue-212-wrong-snapshot",
                        command_digest="3" * 64,
                        query="Synthetic Organization Alpha",
                    ),
                    RetrieveCommand(
                        **{
                            **common,
                            "canonical_boundary": wrong_boundary,
                        },
                        command_identity="issue-212-wrong-boundary",
                        command_digest="4" * 64,
                        query="Synthetic Organization Alpha",
                    ),
                    RetrieveCommand(
                        **{
                            **common,
                            "physical_namespace": wrong_namespace,
                        },
                        command_identity="issue-212-wrong-namespace",
                        command_digest="5" * 64,
                        query="Synthetic Organization Alpha",
                    ),
                )
                mismatched_results = [
                    await projection.retrieve(item) for item in mismatched_commands
                ]
                assert all(
                    result.failure is not None
                    and result.failure.code
                    is ProjectionFailureCode.NAMESPACE_UNAVAILABLE
                    for result in mismatched_results
                )
                assert all(result.chunks == () for result in mismatched_results)
                assert all(
                    "Synthetic Organization Alpha" not in result.model_dump_json()
                    for result in mismatched_results
                )
    finally:
        remaining = await _forget_existing_datasets(cognee, cleanup_dataset_names)
        assert all(
            (
                dataset.get("name")
                if isinstance(dataset, dict)
                else getattr(dataset, "name", None)
            )
            not in cleanup_dataset_names
            for dataset in remaining
        )

    assert projected.outcome is ProjectionOutcome.SUCCEEDED
    if retrieved.outcome is not ProjectionOutcome.SUCCEEDED:
        logger.error(
            "structured retrieval mismatch: outcome=%s failure_code=%s "
            "chunks=%s entities=%s relations=%s",
            retrieved.outcome.value,
            retrieved.failure.code.value if retrieved.failure is not None else "none",
            len(retrieved.chunks),
            len(retrieved.entities),
            len(retrieved.relations),
        )
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
            if result.failure.code is not code:
                logger.error(
                    "unsafe evidence classification mismatch: status=%s expected=%s "
                    "actual=%s",
                    status,
                    code.value,
                    result.failure.code.value,
                )
            assert result.failure.code is code
            assert result.chunks == ()
            assert "unsafe physical marker" not in result.model_dump_json()
        finally:
            await _forget_existing_datasets(cognee, (dataset_name,))


async def _forget_existing_datasets(
    cognee: object,
    dataset_names: tuple[str, ...] | list[str],
) -> object:
    """Forget only datasets confirmed by the public inventory.

    Cognee 1.5.4 raises an ``AttributeError`` when ``forget(dataset=name)`` is
    called for a missing dataset.  That cleanup error must not mask the gate's
    original fail-closed observation.
    """

    datasets_api = getattr(cognee, "datasets")
    datasets = await datasets_api.list_datasets()
    existing = {
        (
            dataset.get("name")
            if isinstance(dataset, dict)
            else getattr(dataset, "name", None)
        )
        for dataset in datasets
    }
    forget = getattr(cognee, "forget")
    for dataset_name in dataset_names:
        if dataset_name in existing:
            await forget(dataset=dataset_name)
    return await datasets_api.list_datasets()


@pytest.mark.asyncio
async def test_production_qualification_records_one_coherent_run() -> None:
    run_id = os.environ.get("SPINE_COGNEE_QUALIFICATION_RUN_ID", "not-run")

    async def public_sdk_gate() -> None:
        _assert_exact_production_profile()
        await _assert_public_sdk_lifecycle(run_id)

    async def update_remove_gate() -> None:
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
                _assert_complete_context_projection_round_trip(
                    run_id,
                    explicit_context=True,
                )
            ),
            "unsafe_evidence": lambda: _assert_unsafe_evidence_is_fail_closed(run_id),
            "workspace_isolation": lambda: (
                _assert_complete_context_projection_round_trip(
                    run_id,
                    workspace_isolation=True,
                )
            ),
            "namespace_isolation": lambda: (
                _assert_complete_context_projection_round_trip(
                    run_id,
                    rebuild=True,
                    active_shadow_isolation=True,
                )
            ),
            "update_remove_tombstone": update_remove_gate,
            "rebuild_recovery_validation_diagnostics": lambda: (
                _assert_complete_context_projection_round_trip(
                    run_id,
                    inject_cognify_failure=True,
                    rebuild=True,
                    validate_physical=True,
                )
            ),
            "concurrency_failure_cleanup": lambda: (
                _assert_complete_context_projection_round_trip(
                    run_id,
                    inject_cognify_failure=True,
                )
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
