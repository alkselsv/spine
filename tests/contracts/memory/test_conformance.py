from __future__ import annotations

from uuid import UUID

import pytest

from spine.memory.contracts import (
    CanonicalBoundary,
    EvidenceReference,
    ProjectionCandidate,
    ProjectionChunk,
    ProjectionConfigVersion,
    ProjectionEffectStatus,
    ProjectionEntity,
    ProjectionFailureCode,
    ProjectionLifecycle,
    ProjectionNamespace,
    ProjectionOutcome,
    ProjectionRelation,
    ProjectionSourceRevision,
    ProjectCommand,
    RebuildCommand,
    RemoveCommand,
    RetrieveCommand,
    StableLocator,
    UpdateCommand,
)

from .adapter import ContextProjectionFactory


WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
OTHER_WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000002")
ENVIRONMENT_ID = UUID("20000000-0000-4000-8000-000000000001")
OTHER_ENVIRONMENT_ID = UUID("20000000-0000-4000-8000-000000000002")
SOURCE_OBJECT_ID = UUID("30000000-0000-4000-8000-000000000001")
SOURCE_REVISION_ID = UUID("40000000-0000-4000-8000-000000000001")
RUN_ID = UUID("50000000-0000-4000-8000-000000000001")
CANDIDATE_ID = UUID("60000000-0000-4000-8000-000000000001")
PARTITION_ID = UUID("70000000-0000-4000-8000-000000000001")
ACTIVE_NAMESPACE = "active/snapshot-1"
SHADOW_NAMESPACE = "shadow/candidate-1"


def source_revision(
    *,
    status: str = "current",
    revision_id: UUID = SOURCE_REVISION_ID,
    revision_number: int = 1,
    source_object_id: UUID = SOURCE_OBJECT_ID,
) -> ProjectionSourceRevision:
    return ProjectionSourceRevision(
        source_object_id=source_object_id,
        source_revision_id=revision_id,
        revision_digest="a" * 64,
        revision_number=revision_number,
        status=status,
        processing_receipt_id=UUID("80000000-0000-4000-8000-000000000001"),
        locator_contract_version="locator:v1",
    )


def command_context(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
    source_revisions: tuple[ProjectionSourceRevision, ...] | None = None,
    namespace: str = ACTIVE_NAMESPACE,
    candidate_id: UUID = CANDIDATE_ID,
) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "environment_id": environment_id,
        "projection_run_id": RUN_ID,
        "projection_candidate": ProjectionCandidate(
            candidate_id=candidate_id,
            run_id=RUN_ID,
            partition_id=PARTITION_ID,
        ),
        "projection_config": ProjectionConfigVersion(
            config_id=UUID("90000000-0000-4000-8000-000000000001"),
            version="config-1",
            config_digest="b" * 64,
            ontology_version="ontology-1",
            adapter_contract_version="context-projection:v1",
        ),
        "canonical_boundary": CanonicalBoundary(
            boundary_id=UUID("a0000000-0000-4000-8000-000000000001"),
            boundary_digest="c" * 64,
            accepted_through_generation=1,
        ),
        "source_revisions": (
            source_revision(),
            ) if source_revisions is None else source_revisions,
        "physical_namespace": ProjectionNamespace(
            namespace=namespace,
            namespace_digest="d" * 64,
        ),
    }


def project_command(
    *,
    identity: str = "project-1",
    digest: str = "e" * 64,
    source_revisions: tuple[ProjectionSourceRevision, ...] | None = None,
) -> ProjectCommand:
    return ProjectCommand(
        **command_context(source_revisions=source_revisions),
        command_identity=identity,
        command_digest=digest,
    )


def evidence(
    *,
    status: str = "valid",
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
    evidence_id: str = "evidence-1",
) -> EvidenceReference:
    return EvidenceReference(
        evidence_id=evidence_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=SOURCE_OBJECT_ID,
        source_revision_id=SOURCE_REVISION_ID,
        locator=StableLocator(kind="page", value="2"),
        evidence_digest="f" * 64,
        status=status,
    )


def retrieve_command(
    *,
    identity: str = "retrieve-1",
    digest: str = "1" * 64,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
    namespace: str = ACTIVE_NAMESPACE,
) -> RetrieveCommand:
    return RetrieveCommand(
        **command_context(
            workspace_id=workspace_id,
            environment_id=environment_id,
            namespace=namespace,
        ),
        command_identity=identity,
        command_digest=digest,
        query="Who is the client?",
        limit=10,
    )


async def test_context_projection_contract_exposes_all_operations(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    adapter = context_projection_factory()
    context = command_context()

    results = [
        await adapter.projection.project(
            ProjectCommand(**context, command_identity="project-1", command_digest="1" * 64)
        ),
        await adapter.projection.update(
            UpdateCommand(**context, command_identity="update-1", command_digest="2" * 64)
        ),
        await adapter.projection.remove(
            RemoveCommand(**context, command_identity="remove-1", command_digest="3" * 64)
        ),
        await adapter.projection.rebuild(
            RebuildCommand(**context, command_identity="rebuild-1", command_digest="4" * 64)
        ),
    ]

    assert [result.outcome for result in results] == [
        ProjectionOutcome.SUCCEEDED,
        ProjectionOutcome.SUCCEEDED,
        ProjectionOutcome.SUCCEEDED,
        ProjectionOutcome.SUCCEEDED,
    ]


async def test_same_command_replays_without_a_second_logical_effect(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    projection = context_projection_factory().projection
    command = project_command()

    first = await projection.project(command)
    replay = await projection.project(command)

    assert first.outcome is ProjectionOutcome.SUCCEEDED
    assert replay.outcome is ProjectionOutcome.REPLAYED
    assert replay.artifact_reference == first.artifact_reference


async def test_same_identity_with_different_digest_is_integrity_conflict(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    projection = context_projection_factory().projection
    await projection.project(project_command())

    conflict = await projection.project(project_command(digest="2" * 64))

    assert conflict.outcome is ProjectionOutcome.FAILED
    assert conflict.failure is not None
    assert conflict.failure.code is ProjectionFailureCode.INTEGRITY_CONFLICT


@pytest.mark.parametrize(
    ("status", "outcome", "code"),
    (
        ("stale", ProjectionOutcome.STALE, ProjectionFailureCode.STALE_EVIDENCE),
        (
            "tombstoned",
            ProjectionOutcome.TOMBSTONED,
            ProjectionFailureCode.TOMBSTONED_SOURCE,
        ),
        (
            "ambiguous",
            ProjectionOutcome.AMBIGUOUS,
            ProjectionFailureCode.AMBIGUOUS_EVIDENCE,
        ),
        (
            "incomplete",
            ProjectionOutcome.INCOMPLETE,
            ProjectionFailureCode.INCOMPLETE_EVIDENCE,
        ),
    ),
)
async def test_non_current_revision_is_fenced_without_an_artifact(
    context_projection_factory: ContextProjectionFactory,
    status: str,
    outcome: ProjectionOutcome,
    code: ProjectionFailureCode,
) -> None:
    projection = context_projection_factory().projection
    result = await projection.project(
        project_command(source_revisions=(source_revision(status=status),))
    )

    assert result.outcome is outcome
    assert result.artifact_reference is None
    assert result.failure is not None
    assert result.failure.code is code


async def test_partial_failure_and_lost_response_are_safe_to_retry(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    adapter = context_projection_factory()
    projection = adapter.create_projection(fail_once_for=("recoverable-project:2",))
    command = project_command(
        identity="recoverable-project",
        source_revisions=(
            source_revision(),
            source_revision(
                revision_id=UUID("40000000-0000-4000-8000-000000000002"),
                revision_number=2,
                source_object_id=UUID("30000000-0000-4000-8000-000000000002"),
            ),
        ),
    )

    first = await projection.project(command)
    recovered_after_retry = await projection.project(command)

    assert first.lifecycle is ProjectionLifecycle.RECOVERABLE
    assert first.failure is not None
    assert first.failure.code is ProjectionFailureCode.PHYSICAL_FAILURE
    assert [effect.status for effect in first.effects] == [
        ProjectionEffectStatus.APPLIED,
        ProjectionEffectStatus.FAILED,
    ]
    assert recovered_after_retry.lifecycle is ProjectionLifecycle.RECOVERED
    assert recovered_after_retry.outcome is ProjectionOutcome.SUCCEEDED
    assert "synthetic protected content" not in recovered_after_retry.model_dump_json()


async def test_retrieval_round_trips_chunks_entities_relations_and_provenance(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    adapter = context_projection_factory()
    item_evidence = evidence()
    adapter.seed_retrieval(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        chunks=(
            ProjectionChunk(
                chunk_id="chunk-1",
                text="A grounded chunk",
                evidence=(item_evidence,),
            ),
        ),
        entities=(
            ProjectionEntity(
                entity_id="entity-1",
                kind="client",
                name="Acme",
                evidence=(item_evidence,),
            ),
        ),
        relations=(
            ProjectionRelation(
                relation_id="relation-1",
                kind="owns",
                subject_entity_id="entity-1",
                object_entity_id="entity-2",
                evidence=(item_evidence,),
            ),
        ),
    )

    result = await adapter.projection.retrieve(retrieve_command())

    assert result.outcome is ProjectionOutcome.SUCCEEDED
    assert result.chunks[0].text == "A grounded chunk"
    assert result.entities[0].name == "Acme"
    assert result.relations[0].subject_entity_id == "entity-1"
    assert result.evidence == (item_evidence, item_evidence, item_evidence)
    assert result.evidence[0].source_revision_id == SOURCE_REVISION_ID
    assert result.evidence[0].locator == StableLocator(kind="page", value="2")


async def test_missing_evidence_abstains_without_returning_content(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    result = await context_projection_factory().projection.retrieve(retrieve_command())

    assert result.outcome is ProjectionOutcome.MISSING
    assert result.chunks == ()
    assert result.entities == ()
    assert result.relations == ()
    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.MISSING_EVIDENCE


@pytest.mark.parametrize(
    ("status", "outcome", "code"),
    (
        ("missing", ProjectionOutcome.MISSING, ProjectionFailureCode.MISSING_EVIDENCE),
        ("stale", ProjectionOutcome.STALE, ProjectionFailureCode.STALE_EVIDENCE),
        (
            "tombstoned",
            ProjectionOutcome.TOMBSTONED,
            ProjectionFailureCode.TOMBSTONED_SOURCE,
        ),
        (
            "ambiguous",
            ProjectionOutcome.AMBIGUOUS,
            ProjectionFailureCode.AMBIGUOUS_EVIDENCE,
        ),
        (
            "incomplete",
            ProjectionOutcome.INCOMPLETE,
            ProjectionFailureCode.INCOMPLETE_EVIDENCE,
        ),
    ),
)
async def test_unsafe_evidence_never_leaks_projected_content(
    context_projection_factory: ContextProjectionFactory,
    status: str,
    outcome: ProjectionOutcome,
    code: ProjectionFailureCode,
) -> None:
    adapter = context_projection_factory()
    adapter.seed_retrieval(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        chunks=(
            ProjectionChunk(
                chunk_id="protected-chunk",
                text="protected content must not leak",
                evidence=(evidence(status=status),),
            ),
        ),
    )

    result = await adapter.projection.retrieve(
        retrieve_command(identity=f"unsafe-{status}", digest="3" * 64)
    )

    assert result.outcome is outcome
    assert result.chunks == ()
    assert result.evidence == ()
    assert result.failure is not None
    assert result.failure.code is code
    assert "protected content must not leak" not in result.model_dump_json()
    assert result.failure.safe_detail.excerpt is None


async def test_denied_workspace_or_environment_returns_safe_empty_result(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    adapter = context_projection_factory()
    secret = "workspace-a protected content"
    adapter.seed_retrieval(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        chunks=(
            ProjectionChunk(
                chunk_id="workspace-a-chunk",
                text=secret,
                evidence=(evidence(),),
            ),
        ),
    )

    result = await adapter.projection.retrieve(
        retrieve_command(
            identity="foreign-workspace",
            digest="4" * 64,
            workspace_id=OTHER_WORKSPACE_ID,
            environment_id=OTHER_ENVIRONMENT_ID,
        )
    )

    assert result.outcome is ProjectionOutcome.MISSING
    assert result.chunks == ()
    assert result.entities == ()
    assert result.relations == ()
    assert secret not in result.model_dump_json()


async def test_shadow_namespace_is_not_visible_to_ordinary_retrieval(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    adapter = context_projection_factory()
    adapter.seed_retrieval(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        physical_namespace=SHADOW_NAMESPACE,
        chunks=(
            ProjectionChunk(
                chunk_id="shadow-only",
                text="candidate content",
                evidence=(evidence(),),
            ),
        ),
    )

    ordinary_result = await adapter.projection.retrieve(
        retrieve_command(identity="ordinary-retrieval", digest="5" * 64)
    )

    assert ordinary_result.outcome is ProjectionOutcome.MISSING
    assert ordinary_result.chunks == ()
    assert "candidate content" not in ordinary_result.model_dump_json()


async def test_update_remove_and_rebuild_return_provider_neutral_results(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    projection = context_projection_factory().projection
    context = command_context()

    update = await projection.update(
        UpdateCommand(**context, command_identity="update-1", command_digest="6" * 64)
    )
    remove = await projection.remove(
        RemoveCommand(**context, command_identity="remove-1", command_digest="7" * 64)
    )
    rebuild = await projection.rebuild(
        RebuildCommand(**context, command_identity="rebuild-1", command_digest="8" * 64)
    )

    assert update.operation.value == "update"
    assert remove.operation.value == "remove"
    assert rebuild.operation.value == "rebuild"
    assert all(result.outcome is ProjectionOutcome.SUCCEEDED for result in (update, remove, rebuild))
