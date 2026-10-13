from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.memory.contracts import (
    CanonicalBoundary,
    EvidenceReference,
    ProjectionCandidate,
    ProjectionChunk,
    ProjectionConfigVersion,
    ProjectionEntity,
    ProjectionFailureCode,
    ProjectionEffectAction,
    ProjectionEffectStatus,
    ProjectionLifecycle,
    ProjectionNamespace,
    ProjectionOutcome,
    ProjectionResult,
    ProjectionRelation,
    ProjectionSourceRevision,
    ProjectCommand,
    RebuildCommand,
    RemoveCommand,
    RetrieveCommand,
    StableLocator,
    UpdateCommand,
)
from spine.memory.fake import DeterministicContextProjection
from spine.memory.fake import DeterministicPhysicalStore


WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
SOURCE = UUID(int=3)
REVISION = UUID(int=4)
RUN = UUID(int=5)
CANDIDATE = UUID(int=6)
PARTITION = UUID(int=7)


def projection_source(
    *,
    status: str = "current",
    revision_id: UUID = REVISION,
    revision_number: int = 1,
    source_object_id: UUID = SOURCE,
) -> ProjectionSourceRevision:
    return ProjectionSourceRevision(
        source_object_id=source_object_id,
        source_revision_id=revision_id,
        revision_digest="a" * 64,
        revision_number=revision_number,
        status=status,
        processing_receipt_id=UUID(int=8),
        locator_contract_version="locator:v1",
    )


def command_context(
    *,
    source_status: str = "current",
    workspace_id: UUID = WORKSPACE,
    environment_id: UUID = ENVIRONMENT,
) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "environment_id": environment_id,
        "projection_run_id": RUN,
        "projection_candidate": ProjectionCandidate(
            candidate_id=CANDIDATE,
            run_id=RUN,
            partition_id=PARTITION,
        ),
        "projection_config": ProjectionConfigVersion(
            config_id=UUID(int=9),
            version="config-1",
            config_digest="b" * 64,
            ontology_version="ontology-1",
            adapter_contract_version="context-projection:v1",
        ),
        "canonical_boundary": CanonicalBoundary(
            boundary_id=UUID(int=10),
            boundary_digest="c" * 64,
            accepted_through_generation=1,
        ),
        "source_revisions": (projection_source(status=source_status),),
        "physical_namespace": ProjectionNamespace(
            namespace="shadow/run-5/candidate-6/partition-7",
            namespace_digest="d" * 64,
        ),
    }


def project_command(*, identity: str = "project-1", digest: str = "e" * 64, source_status: str = "current") -> ProjectCommand:
    return ProjectCommand(
        **command_context(source_status=source_status),
        command_identity=identity,
        command_digest=digest,
    )


def test_projection_commands_are_immutable_versioned_and_tenant_scoped() -> None:
    command = project_command()

    assert command.schema_version == "context-projection-command:v1"
    assert command.workspace_id == WORKSPACE
    assert command.environment_id == ENVIRONMENT
    assert command.projection_candidate.candidate_id == CANDIDATE

    with pytest.raises((TypeError, ValidationError)):
        command.command_identity = "changed"  # type: ignore[misc]


def test_projection_commands_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ProjectCommand(**project_command().model_dump(), unexpected="rejected")


def test_projection_result_rejects_inconsistent_recovery_lifecycle() -> None:
    with pytest.raises(ValidationError):
        ProjectionResult(
            operation="project",
            outcome=ProjectionOutcome.SUCCEEDED,
            command_identity="project-1",
            command_digest="e" * 64,
            lifecycle=ProjectionLifecycle.RECOVERABLE,
        )


@pytest.mark.asyncio
async def test_fake_replays_same_command_and_rejects_digest_conflict() -> None:
    projection = DeterministicContextProjection()
    command = project_command()

    first = await projection.project(command)
    replay = await projection.project(command)
    conflict = await projection.project(
        project_command(identity=command.command_identity, digest="f" * 64)
    )

    assert first.outcome is ProjectionOutcome.SUCCEEDED
    assert replay.outcome is ProjectionOutcome.REPLAYED
    assert replay.artifact_reference == first.artifact_reference
    assert conflict.outcome is ProjectionOutcome.FAILED
    assert conflict.failure is not None
    assert conflict.failure.code is ProjectionFailureCode.INTEGRITY_CONFLICT
    assert len(projection.recorded_commands) == 1


@pytest.mark.asyncio
async def test_fake_returns_structured_retrieval_with_provenance() -> None:
    projection = DeterministicContextProjection()
    await projection.project(project_command())
    evidence = EvidenceReference(
        evidence_id="evidence-1",
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        locator=StableLocator(kind="page", value="2"),
        evidence_digest="1" * 64,
    )
    projection.seed_retrieval(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        chunks=(
            ProjectionChunk(
                chunk_id="chunk-1",
                text="A grounded chunk",
                evidence=(evidence,),
            ),
        ),
        entities=(
            ProjectionEntity(
                entity_id="entity-1",
                kind="client",
                name="Acme",
                evidence=(evidence,),
            ),
        ),
        relations=(
            ProjectionRelation(
                relation_id="relation-1",
                kind="owns",
                subject_entity_id="entity-1",
                object_entity_id="entity-2",
                evidence=(evidence,),
            ),
        ),
    )

    result = await projection.retrieve(
        RetrieveCommand(
            **command_context(),
            command_identity="retrieve-1",
            command_digest="2" * 64,
            query="Who is the client?",
            limit=10,
        )
    )

    assert result.outcome is ProjectionOutcome.SUCCEEDED
    assert result.chunks[0].evidence[0].locator.value == "2"
    assert result.entities[0].name == "Acme"
    assert result.relations[0].subject_entity_id == "entity-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "outcome", "code"),
    (
        ("stale", ProjectionOutcome.STALE, ProjectionFailureCode.STALE_EVIDENCE),
        ("tombstoned", ProjectionOutcome.TOMBSTONED, ProjectionFailureCode.TOMBSTONED_SOURCE),
        ("ambiguous", ProjectionOutcome.AMBIGUOUS, ProjectionFailureCode.AMBIGUOUS_EVIDENCE),
        ("incomplete", ProjectionOutcome.INCOMPLETE, ProjectionFailureCode.INCOMPLETE_EVIDENCE),
    ),
)
async def test_fake_returns_safe_typed_outcome_for_unsafe_source_status(
    status: str,
    outcome: ProjectionOutcome,
    code: ProjectionFailureCode,
) -> None:
    result = await DeterministicContextProjection().project(
        project_command(source_status=status)
    )

    assert result.outcome is outcome
    assert result.failure is not None
    assert result.failure.code is code
    assert result.failure.safe_detail.excerpt is None


@pytest.mark.asyncio
async def test_fake_records_all_supported_command_kinds() -> None:
    projection = DeterministicContextProjection()
    context = command_context()
    commands = (
        project_command(),
        UpdateCommand(**context, command_identity="update-1", command_digest="3" * 64),
        RemoveCommand(**context, command_identity="remove-1", command_digest="4" * 64),
        RebuildCommand(**context, command_identity="rebuild-1", command_digest="5" * 64),
    )

    results = [await projection.project(commands[0])]
    results.extend(
        [
            await projection.update(commands[1]),
            await projection.remove(commands[2]),
            await projection.rebuild(commands[3]),
        ]
    )

    assert [result.outcome for result in results] == [
        ProjectionOutcome.SUCCEEDED,
        ProjectionOutcome.SUCCEEDED,
        ProjectionOutcome.SUCCEEDED,
        ProjectionOutcome.SUCCEEDED,
    ]
    assert tuple(command.command_identity for command in projection.recorded_commands) == (
        "project-1",
        "update-1",
        "remove-1",
        "rebuild-1",
    )


@pytest.mark.asyncio
async def test_fake_does_not_turn_unseeded_or_cross_workspace_data_into_content() -> None:
    projection = DeterministicContextProjection()
    evidence = EvidenceReference(
        evidence_id="workspace-a-evidence",
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        locator=StableLocator(kind="page", value="1"),
        evidence_digest="7" * 64,
    )
    projection.seed_retrieval(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        chunks=(ProjectionChunk(chunk_id="chunk-a", text="private", evidence=(evidence,)),),
    )
    result = await projection.retrieve(
        RetrieveCommand(
            **command_context(workspace_id=UUID(int=11), environment_id=UUID(int=12)),
            command_identity="retrieve-foreign",
            command_digest="6" * 64,
            query="anything",
        )
    )

    assert result.outcome is ProjectionOutcome.MISSING
    assert result.chunks == ()
    assert result.entities == ()
    assert result.relations == ()
    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.MISSING_EVIDENCE
    assert result.failure.safe_detail.model_dump(exclude_none=True) == {
        "schema_version": "projection-safe-diagnostic:v1",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "outcome", "code"),
    (
        ("missing", ProjectionOutcome.MISSING, ProjectionFailureCode.MISSING_EVIDENCE),
        ("stale", ProjectionOutcome.STALE, ProjectionFailureCode.STALE_EVIDENCE),
        ("tombstoned", ProjectionOutcome.TOMBSTONED, ProjectionFailureCode.TOMBSTONED_SOURCE),
        ("ambiguous", ProjectionOutcome.AMBIGUOUS, ProjectionFailureCode.AMBIGUOUS_EVIDENCE),
        ("incomplete", ProjectionOutcome.INCOMPLETE, ProjectionFailureCode.INCOMPLETE_EVIDENCE),
    ),
)
async def test_retrieval_rejects_unsafe_evidence_without_returning_content(
    status: str,
    outcome: ProjectionOutcome,
    code: ProjectionFailureCode,
) -> None:
    projection = DeterministicContextProjection()
    evidence = EvidenceReference(
        evidence_id="unsafe-evidence",
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        locator=StableLocator(kind="page", value="1"),
        evidence_digest="8" * 64,
        status=status,
    )
    projection.seed_retrieval(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        chunks=(ProjectionChunk(chunk_id="unsafe", text="must not leak", evidence=(evidence,)),),
    )

    result = await projection.retrieve(
        RetrieveCommand(
            **command_context(),
            command_identity=f"retrieve-{status}",
            command_digest="9" * 64,
            query="anything",
        )
    )

    assert result.outcome is outcome
    assert result.chunks == ()
    assert result.failure is not None
    assert result.failure.code is code


@pytest.mark.asyncio
async def test_fake_fences_older_revision_after_newer_revision() -> None:
    projection = DeterministicContextProjection()
    newer = project_command()
    newer = newer.model_copy(
        update={
            "source_revisions": (
                projection_source(revision_id=UUID(int=12), revision_number=2),
            )
        }
    )
    older = project_command(identity="project-older").model_copy(
        update={"source_revisions": (projection_source(revision_number=1),)}
    )

    applied = await projection.project(newer)
    fenced = await projection.project(older)

    assert applied.lifecycle is ProjectionLifecycle.SUCCEEDED
    assert fenced.lifecycle is ProjectionLifecycle.FENCED
    assert fenced.outcome is ProjectionOutcome.STALE
    assert fenced.failure is not None
    assert fenced.failure.code is ProjectionFailureCode.REVISION_FENCED
    assert fenced.effects == ()


@pytest.mark.asyncio
async def test_projection_fencing_and_effects_are_workspace_isolated() -> None:
    projection = DeterministicContextProjection()
    workspace_a = await projection.project(project_command())
    workspace_b = await projection.project(
        ProjectCommand(
            **command_context(
                workspace_id=UUID(int=11),
                environment_id=UUID(int=12),
            ),
            command_identity="project-1",
            command_digest="e" * 64,
        )
    )

    assert workspace_a.outcome is ProjectionOutcome.SUCCEEDED
    assert workspace_b.outcome is ProjectionOutcome.SUCCEEDED
    assert workspace_a.effects[0].effect_id != workspace_b.effects[0].effect_id


@pytest.mark.asyncio
async def test_tombstone_fences_prior_content_but_allows_newer_reappearance() -> None:
    projection = DeterministicContextProjection()
    await projection.project(project_command())

    tombstone = RemoveCommand(
        **command_context(source_status="tombstoned"),
        command_identity="remove-1",
        command_digest="3" * 64,
    ).model_copy(
        update={
            "source_revisions": (
                projection_source(
                    status="tombstoned",
                    revision_id=UUID(int=12),
                    revision_number=2,
                ),
            )
        }
    )
    stale_rebuild = RebuildCommand(
        **command_context(),
        command_identity="rebuild-stale",
        command_digest="4" * 64,
    )
    reappearance = ProjectCommand(
        **command_context(),
        command_identity="project-reappearance",
        command_digest="5" * 64,
    ).model_copy(
        update={
            "source_revisions": (
                projection_source(revision_id=UUID(int=13), revision_number=3),
            )
        }
    )

    removed = await projection.remove(tombstone)
    stale = await projection.rebuild(stale_rebuild)
    restored = await projection.project(reappearance)

    assert removed.lifecycle is ProjectionLifecycle.SUCCEEDED
    assert removed.effects[0].action is ProjectionEffectAction.REMOVE_SOURCE
    assert removed.effects[0].status is ProjectionEffectStatus.APPLIED
    assert stale.outcome is ProjectionOutcome.STALE
    assert stale.failure is not None
    assert stale.failure.code is ProjectionFailureCode.REVISION_FENCED
    assert restored.outcome is ProjectionOutcome.SUCCEEDED


@pytest.mark.asyncio
async def test_partial_physical_failure_is_recoverable_and_retry_is_idempotent() -> None:
    store = DeterministicPhysicalStore(fail_once_for={"project-1:2"})
    projection = DeterministicContextProjection(physical_store=store)
    command = project_command().model_copy(
        update={
            "source_revisions": (
                projection_source(revision_id=UUID(int=11), revision_number=1),
                projection_source(
                    revision_id=UUID(int=12),
                    revision_number=2,
                    source_object_id=UUID(int=13),
                ),
            )
        }
    )

    first = await projection.project(command)
    recovered = await projection.project(command)

    assert first.lifecycle is ProjectionLifecycle.RECOVERABLE
    assert first.failure is not None
    assert first.failure.code is ProjectionFailureCode.PHYSICAL_FAILURE
    assert first.failure.retryable is True
    assert [effect.status for effect in first.effects] == [
        ProjectionEffectStatus.APPLIED,
        ProjectionEffectStatus.FAILED,
    ]
    assert recovered.lifecycle is ProjectionLifecycle.RECOVERED
    assert recovered.outcome is ProjectionOutcome.SUCCEEDED
    assert store.attempts_for("project-1:1") == 1
    assert store.attempts_for("project-1:2") == 2


@pytest.mark.asyncio
async def test_concurrent_same_command_has_one_physical_effect() -> None:
    store = DeterministicPhysicalStore()
    projection = DeterministicContextProjection(physical_store=store)
    command = project_command()

    first, second = await asyncio.gather(
        projection.project(command), projection.project(command)
    )

    assert {first.outcome, second.outcome} == {
        ProjectionOutcome.SUCCEEDED,
        ProjectionOutcome.REPLAYED,
    }
    assert store.attempts_for("project-1:1") == 1
