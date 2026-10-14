from __future__ import annotations

from uuid import UUID

import pytest

from spine.memory.cognee.recovery import (
    DeterministicPhysicalCleanupStore,
    PhysicalCleanupPlan,
    PhysicalCleanupStatus,
    ProjectionRepairPlan,
    ProjectionRepairStatus,
    ProjectionRecoveryCoordinator,
)
from spine.memory.cognee.rebuild import (
    ProjectionReplayItem,
    ShadowRebuildOrchestrator,
    ShadowRebuildPlan,
)
from spine.memory.cognee.validation import (
    ArtifactKind,
    PhysicalArtifact,
    PhysicalArtifactLocator,
)
from spine.memory.contracts import (
    CanonicalBoundary,
    ProjectionCandidate,
    ProjectionConfigVersion,
    ProjectionEffect,
    ProjectionEffectAction,
    ProjectionEffectStatus,
    ProjectionNamespaceState,
    ProjectionOperation,
    ProjectionSourceRevision,
)
from spine.memory.namespace import ProjectionNamespaceContext, ProjectionNamespaceRouter


WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
RUN = UUID(int=3)
CANDIDATE = UUID(int=4)
PARTITION = UUID(int=5)
CONFIG = UUID(int=6)
BOUNDARY = UUID(int=7)
NAMESPACE_DIGEST = "69491e92f0ba2f440480fe3dfdefc01098318d9b18c2e3269531007430c55aaa"


def _plan(*artifacts: PhysicalArtifact) -> PhysicalCleanupPlan:
    candidate = ProjectionCandidate(
        candidate_id=CANDIDATE,
        run_id=RUN,
        partition_id=PARTITION,
    )
    config = ProjectionConfigVersion(
        config_id=CONFIG,
        version="projection-1",
        config_digest="a" * 64,
        ontology_version="ontology-1",
        adapter_contract_version="recovery:v1",
    )
    boundary = CanonicalBoundary(
        boundary_id=BOUNDARY,
        boundary_digest="b" * 64,
        accepted_through_generation=1,
    )
    namespace = ProjectionNamespaceRouter().route(
        ProjectionNamespaceContext(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            projection_candidate=candidate,
            projection_config=config,
            canonical_boundary=boundary,
            state=ProjectionNamespaceState.FAILED,
        )
    )
    return PhysicalCleanupPlan(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_run_id=RUN,
        projection_candidate=candidate,
        projection_config=config,
        canonical_boundary=boundary,
        physical_namespace=namespace,
        cleanup_identity="cleanup-1",
        cleanup_digest="c" * 64,
        artifacts=artifacts,
    )


def _artifact(kind: ArtifactKind, artifact_id: str, data_id: int) -> PhysicalArtifact:
    return PhysicalArtifact(
        artifact_kind=kind,
        artifact_id=artifact_id,
        data_id=UUID(int=data_id),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        namespace_digest=NAMESPACE_DIGEST,
        source_object_id=UUID(int=20),
        source_revision_id=UUID(int=21),
        revision_digest="e" * 64,
        revision_number=1,
        projection_config_id=CONFIG,
        projection_config_version="projection-1",
        projection_config_digest="a" * 64,
        locators=(
            PhysicalArtifactLocator.from_payload(
                kind="line",
                payload="line:1:0-4",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_cleanup_retries_only_failed_artifacts_and_is_idempotent() -> None:
    artifacts = (
        _artifact(ArtifactKind.CHUNK, "chunk-1", 30),
        _artifact(ArtifactKind.ENTITY, "entity-1", 31),
    )
    plan = _plan(*artifacts)
    store = DeterministicPhysicalCleanupStore(fail_once_for={"entity-1"})
    coordinator = ProjectionRecoveryCoordinator(store)

    first = await coordinator.cleanup(plan)
    recovered = await coordinator.cleanup(plan)
    replay = await coordinator.cleanup(plan)

    assert first.status is PhysicalCleanupStatus.RECOVERABLE
    assert first.pending_artifact_ids == ("entity-1",)
    assert recovered.status is PhysicalCleanupStatus.SUCCEEDED
    assert recovered.deleted_artifact_ids == ("chunk-1", "entity-1")
    assert replay.status is PhysicalCleanupStatus.REPLAYED
    assert store.attempts_for("chunk-1") == 1
    assert store.attempts_for("entity-1") == 2


class _ForgedCleanupLane:
    async def delete_artifact(self, effect, *, namespace, artifact):
        return effect.model_copy(update={"artifact_id": "another-artifact"})


@pytest.mark.asyncio
async def test_cleanup_rejects_a_forged_adapter_effect_without_deleting() -> None:
    artifact = _artifact(ArtifactKind.CHUNK, "chunk-1", 30)

    result = await ProjectionRecoveryCoordinator(_ForgedCleanupLane()).cleanup(
        _plan(artifact)
    )

    assert result.status is PhysicalCleanupStatus.FAILED
    assert result.failure is not None
    assert result.failure.code.value == "integrity_conflict"
    assert result.pending_artifact_ids == ("chunk-1",)


@pytest.mark.asyncio
async def test_cleanup_rechecks_scope_after_a_forged_plan_copy() -> None:
    artifact = _artifact(ArtifactKind.CHUNK, "foreign", 32)
    forged = _plan(artifact).model_copy(
        update={
            "artifacts": (
                artifact.model_copy(update={"workspace_id": UUID(int=99)}),
            )
        }
    )
    store = DeterministicPhysicalCleanupStore()

    result = await ProjectionRecoveryCoordinator(store).cleanup(forged)

    assert result.status is PhysicalCleanupStatus.FAILED
    assert result.failure is not None
    assert result.failure.code.value == "cross_workspace"
    assert store.attempts_for("foreign") == 0


def test_cleanup_rejects_an_artifact_from_another_workspace() -> None:
    foreign = _artifact(ArtifactKind.CHUNK, "foreign", 32).model_copy(
        update={"workspace_id": UUID(int=99)}
    )

    with pytest.raises(ValueError, match="outside the workspace scope"):
        _plan(foreign)


class _RecordingChunkLane:
    def __init__(self) -> None:
        self.effects: list[ProjectionEffect] = []

    async def apply(self, effect: ProjectionEffect) -> ProjectionEffect:
        self.effects.append(effect)
        return effect.model_copy(update={"status": ProjectionEffectStatus.APPLIED})


def _repair_plan(status: str) -> tuple[ProjectionRepairPlan, ProjectionEffect]:
    candidate = ProjectionCandidate(
        candidate_id=CANDIDATE,
        run_id=RUN,
        partition_id=PARTITION,
    )
    config = ProjectionConfigVersion(
        config_id=CONFIG,
        version="projection-1",
        config_digest="a" * 64,
        ontology_version="ontology-1",
        adapter_contract_version="recovery:v1",
    )
    boundary = CanonicalBoundary(
        boundary_id=BOUNDARY,
        boundary_digest="b" * 64,
        accepted_through_generation=1,
    )
    namespace = ProjectionNamespaceRouter().route(
        ProjectionNamespaceContext(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            projection_candidate=candidate,
            projection_config=config,
            canonical_boundary=boundary,
            state=ProjectionNamespaceState.SHADOW,
        )
    )
    effect = ProjectionEffect(
        effect_id="repair-effect",
        operation=ProjectionOperation.REBUILD,
        action=ProjectionEffectAction.UPSERT_SOURCE,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        command_identity="repair-1",
        command_digest="1" * 64,
        projection_candidate=candidate,
        projection_config=config,
        canonical_boundary=boundary,
        physical_namespace=namespace,
        source_object_id=UUID(int=20),
        source_revision_id=UUID(int=21),
        revision_digest="e" * 64,
        revision_number=1,
    )
    rebuild = ShadowRebuildPlan(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_run_id=RUN,
        projection_candidate=candidate,
        projection_config=config,
        canonical_boundary=boundary,
        physical_namespace=namespace,
        rebuild_identity="repair-1",
        rebuild_digest="2" * 64,
        items=(ProjectionReplayItem.from_effect(effect),),
    )
    source = ProjectionSourceRevision(
        source_object_id=UUID(int=20),
        source_revision_id=UUID(int=21),
        revision_digest="e" * 64,
        revision_number=1,
        status=status,
        locator_contract_version="locator:v1",
    )
    return ProjectionRepairPlan(rebuild_plan=rebuild, source_revisions=(source,)), effect


@pytest.mark.asyncio
async def test_repair_fences_tombstoned_source_without_reintroducing_artifacts() -> None:
    plan, _ = _repair_plan("tombstoned")
    lane = _RecordingChunkLane()
    coordinator = ProjectionRecoveryCoordinator(
        DeterministicPhysicalCleanupStore(),
        rebuild_orchestrator=ShadowRebuildOrchestrator(chunk_lane=lane),
    )

    result = await coordinator.repair(plan)

    assert result.status is ProjectionRepairStatus.FENCED
    assert result.failure_code.value == "tombstoned_source"
    assert lane.effects == []


@pytest.mark.asyncio
async def test_repair_fences_stale_source_without_reintroducing_artifacts() -> None:
    plan, _ = _repair_plan("stale")
    lane = _RecordingChunkLane()
    coordinator = ProjectionRecoveryCoordinator(
        DeterministicPhysicalCleanupStore(),
        rebuild_orchestrator=ShadowRebuildOrchestrator(chunk_lane=lane),
    )

    result = await coordinator.repair(plan)

    assert result.status is ProjectionRepairStatus.FENCED
    assert result.failure_code.value == "revision_fenced"
    assert lane.effects == []


@pytest.mark.asyncio
async def test_repair_replays_current_immutable_input_with_source_lineage() -> None:
    plan, expected_effect = _repair_plan("current")
    lane = _RecordingChunkLane()
    coordinator = ProjectionRecoveryCoordinator(
        DeterministicPhysicalCleanupStore(),
        rebuild_orchestrator=ShadowRebuildOrchestrator(chunk_lane=lane),
    )

    result = await coordinator.repair(plan)

    assert result.status is ProjectionRepairStatus.SUCCEEDED
    assert lane.effects == [expected_effect]
